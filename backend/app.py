# -*- coding: utf-8 -*-
"""
Flask App – Backend del chatbot con capacidad de consulta de pedidos desde Google Sheets.

NUEVO:
- OrderService (cache en SQLite, refresco desde Google Sheet o CSV publicado)
- POST /api/orders/lookup        -> consulta por order_id, tracking o texto libre
- POST /admin/orders/reload      -> recarga el cache desde la fuente (CSV/Sheets)
- handle_orders_intent(...)      -> helper para integrarlo en tu /api/chat sin romper nada

No interfiere con rutas existentes: solo agrega nuevas rutas y utilitarios.
"""

import os
import io
import csv
import json
import time
import math
import sqlite3
import datetime as dt
import re
from typing import Dict, Any, List, Optional, Tuple

import requests
from flask import Flask, request, jsonify

# --------------------------------------------------------------------------------------
# Utilidades generales
# --------------------------------------------------------------------------------------

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return default

def parse_service_account_json() -> Optional[dict]:
    """
    Devuelve el JSON del Service Account si:
    - GOOGLE_SERVICE_ACCOUNT_JSON contiene JSON inline, o
    - es una ruta a archivo con el JSON del service account.
    """
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    # ¿Es JSON inline?
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except Exception:
            return None
    # ¿Es ruta?
    if os.path.exists(raw):
        try:
            with open(raw, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None
    return None

def money_mx(n: Optional[float]) -> str:
    try:
        if n is None or (isinstance(n, float) and (math.isnan(n) or math.isinf(n))):
            return "$0.00"
        return f"${n:,.2f}"
    except Exception:
        return "$0.00"

def safe_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        s = str(x).strip().replace(",", "")
        if s == "":
            return None
        return float(s)
    except Exception:
        return None

def safe_int(x) -> Optional[int]:
    try:
        if x is None:
            return None
        s = str(x).strip()
        if s == "":
            return None
        return int(float(s))
    except Exception:
        return None

def to_date(s: str) -> Optional[str]:
    """
    Convierte cualquier string de fecha a ISO YYYY-MM-DD para guardar en DB.
    A la hora de responder al cliente, se formatea a DD/MM/AAAA.
    """
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    fmts = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%m/%d/%Y",
        "%d/%m/%y",
        "%Y/%m/%d",
        "%d-%m-%y",
    ]
    for f in fmts:
        try:
            d = dt.datetime.strptime(s, f).date()
            return d.strftime("%Y-%m-%d")
        except Exception:
            pass
    # Último intento: dejar tal cual si parece una fecha
    return None

def fmt_date_ddmmyyyy(iso_date: Optional[str]) -> Optional[str]:
    if not iso_date:
        return None
    try:
        d = dt.datetime.strptime(iso_date, "%Y-%m-%d").date()
        return d.strftime("%d/%m/%Y")
    except Exception:
        return iso_date

# --------------------------------------------------------------------------------------
# OrderService – carga y cache de pedidos
# --------------------------------------------------------------------------------------

class OrderService:
    """
    Carga pedidos desde:
      A) CSV publicado (ORDERS_CSV_URL)
      B) Google Sheets API con Service Account (ORDERS_SPREADSHEET_ID + ORDERS_RANGE)

    Normaliza columnas y guarda en SQLite para respuestas rápidas.
    """

    DEFAULT_DB = os.getenv("ORDERS_SQLITE_PATH", "/data/orders.sqlite3")

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or self.DEFAULT_DB
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._ensure_schema()

    # ------------------ DB schema ------------------

    def _ensure_schema(self):
        con = sqlite3.connect(self.db_path)
        con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT,
            tracking TEXT,
            carrier TEXT,
            sku TEXT,
            qty INTEGER,
            unit_price REAL,
            total_price REAL,
            start_date TEXT,   -- ISO YYYY-MM-DD
            ship_date TEXT,    -- ISO YYYY-MM-DD
            customer_email TEXT,
            customer_phone TEXT,
            raw_json TEXT
        )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_ordnum ON orders(order_number)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_tracking ON orders(tracking)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_email ON orders(customer_email)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_phone ON orders(customer_phone)")
        con.commit()
        con.close()

    def _clear(self):
        con = sqlite3.connect(self.db_path)
        con.execute("DELETE FROM orders")
        con.commit()
        con.close()

    # ------------------ Fuente de datos ------------------

    def _load_from_csv_url(self, url: str) -> List[Dict[str, Any]]:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        content = r.content.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(content))
        rows = [dict(row) for row in reader]
        return rows

    def _load_from_google_sheets(self) -> List[Dict[str, Any]]:
        """
        Llamada directa a la API de Google Sheets v4 vía token del Service Account.

        Requiere:
          - GOOGLE_SERVICE_ACCOUNT_JSON (inline o ruta)
          - ORDERS_SPREADSHEET_ID
          - ORDERS_RANGE  (p.ej. 'Pedidos!A:Z')
        """
        sa = parse_service_account_json()
        spreadsheet_id = os.getenv("ORDERS_SPREADSHEET_ID", "").strip()
        range_name = os.getenv("ORDERS_RANGE", "Pedidos!A:Z")
        assert sa and spreadsheet_id, "Faltan credenciales o SPREADSHEET_ID para Google Sheets"

        # 1) OAuth2 JWT -> access_token
        token_url = "https://oauth2.googleapis.com/token"
        import jwt  # PyJWT
        now = int(time.time())
        scope = "https://www.googleapis.com/auth/spreadsheets.readonly"
        payload = {
            "iss": sa["client_email"],
            "scope": scope,
            "aud": token_url,
            "iat": now,
            "exp": now + 3600,
        }
        signed = jwt.encode(payload, sa["private_key"], algorithm="RS256")
        token_resp = requests.post(token_url, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": signed,
        }, timeout=30)
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        # 2) Sheets API
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{range_name}?majorDimension=ROWS"
        headers = {"Authorization": f"Bearer {access_token}"}
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        values = data.get("values") or []
        if not values:
            return []

        headers_row = values[0]
        rows = []
        for r in values[1:]:
            row = {}
            for i, h in enumerate(headers_row):
                key = str(h).strip()
                row[key] = r[i] if i < len(r) else ""
            rows.append(row)
        return rows

    # ------------------ Normalización ------------------

    def _norm_key(self, k: str) -> str:
        return re.sub(r"[^a-z0-9_]+", "_", k.strip().lower())

    def _normalize_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """
        Mapear nombres flexibles de columnas a nuestro esquema.
        Acepta sinónimos comunes en español.
        """
        # Construir dict con claves normalizadas
        n = {self._norm_key(k): v for k, v in row.items()}

        def pick(*names, default=None):
            for name in names:
                kn = self._norm_key(name)
                if kn in n and str(n[kn]).strip() != "":
                    return n[kn]
            return default

        order_number = pick("Número de orden", "No de orden", "orden", "order", "order_number", "numero de orden", "pedido", "folio")
        tracking     = pick("Guía", "tracking", "no de guia", "numero de guia", "num guia", "guia")
        carrier      = pick("Paquetería", "carrier", "paqueteria")
        sku          = pick("SKU", "sku", "clave", "codigo", "código")
        qty          = safe_int(pick("Pzas", "pzas", "cantidad", "qty", "cantidad pzs", "piezas"))
        unit_price   = safe_float(pick("Precio unitario", "precio unitario", "unit_price", "precio_u"))
        total_price  = safe_float(pick("Precio total", "total", "precio total", "total_price"))
        start_date   = to_date(pick("Fecha de inicio", "inicio", "fecha compra", "start_date"))
        ship_date    = to_date(pick("Fecha de envío", "envio", "fecha envio", "ship_date"))
        customer_email = pick("Correo", "email", "mail", "customer_email")
        customer_phone = pick("Telefono", "teléfono", "telefono", "phone", "customer_phone")

        # Si no hay total y sí hay unit + qty, calcúlalo
        if total_price is None and unit_price is not None and qty is not None:
            total_price = round(unit_price * qty, 2)

        return {
            "order_number": str(order_number or "").strip(),
            "tracking": str(tracking or "").strip(),
            "carrier": str(carrier or "").strip(),
            "sku": str(sku or "").strip(),
            "qty": qty,
            "unit_price": unit_price,
            "total_price": total_price,
            "start_date": start_date,
            "ship_date": ship_date,
            "customer_email": str(customer_email or "").strip(),
            "customer_phone": str(customer_phone or "").strip(),
            "raw_json": json.dumps(row, ensure_ascii=False),
        }

    # ------------------ Carga a cache ------------------

    def reload(self) -> Dict[str, Any]:
        """
        Recarga el cache desde la fuente configurada.
        """
        csv_url = os.getenv("ORDERS_CSV_URL", "").strip()
        if csv_url:
            rows = self._load_from_csv_url(csv_url)
        else:
            rows = self._load_from_google_sheets()

        norm = [self._normalize_row(r) for r in rows]
        # Volcar en DB
        con = sqlite3.connect(self.db_path)
        self._ensure_schema()
        con.execute("DELETE FROM orders")
        insert_sql = """
        INSERT INTO orders (order_number, tracking, carrier, sku, qty, unit_price, total_price,
                            start_date, ship_date, customer_email, customer_phone, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        cur = con.cursor()
        cur.executemany(insert_sql, [
            (
                r["order_number"], r["tracking"], r["carrier"], r["sku"], r["qty"], r["unit_price"],
                r["total_price"], r["start_date"], r["ship_date"], r["customer_email"], r["customer_phone"], r["raw_json"]
            ) for r in norm
        ])
        con.commit()
        con.close()
        return {"ok": True, "rows": len(norm)}

    # ------------------ Búsquedas ------------------

    ORDER_RE = re.compile(r"(?:pedido|orden|#|folio)\s*[:#]?\s*([A-Z0-9\-\_]+)", re.I)
    TRACK_RE = re.compile(r"(?:gu[ií]a|tracking)\s*[:#]?\s*([A-Z0-9\-\_]+)", re.I)

    def extract_identifiers(self, text: str) -> Dict[str, Optional[str]]:
        if not text:
            return {"order_id": None, "tracking": None}
        t = text.strip()
        m1 = self.ORDER_RE.search(t)
        m2 = self.TRACK_RE.search(t)
        return {
            "order_id": m1.group(1).strip() if m1 else None,
            "tracking": m2.group(1).strip() if m2 else None,
        }

    def lookup(self, order_id: Optional[str] = None, tracking: Optional[str] = None,
               email: Optional[str] = None, phone: Optional[str] = None) -> Dict[str, Any]:
        """
        Devuelve resumen y detalle (ítems) del pedido. Si encuentra múltiples filas del mismo pedido,
        agrega cantidades y totales.
        """
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        rows: List[sqlite3.Row] = []
        if tracking:
            cur.execute("SELECT * FROM orders WHERE tracking = ?", (tracking,))
            rows = cur.fetchall()
            # Si hay tracking, tratamos de encontrar el order_number asociado para agregar el resto
            if rows and rows[0]["order_number"]:
                cur.execute("SELECT * FROM orders WHERE order_number = ?", (rows[0]["order_number"],))
                rows = cur.fetchall()
        elif order_id:
            cur.execute("SELECT * FROM orders WHERE order_number = ?", (order_id,))
            rows = cur.fetchall()
        elif email:
            cur.execute("SELECT * FROM orders WHERE customer_email = ?", (email,))
            rows = cur.fetchall()
        elif phone:
            cur.execute("SELECT * FROM orders WHERE customer_phone LIKE ?", (f"%{phone[-4:]}%",))
            rows = cur.fetchall()

        con.close()

        if not rows:
            return {"ok": False, "reason": "not_found"}

        # Agregación por pedido
        order_number = rows[0]["order_number"] or order_id
        tracking_code = None
        carrier = None
        start_date = None
        ship_date = None

        items = []
        total_qty = 0
        total_amount = 0.0

        for r in rows:
            # Preferir valores no vacíos
            if (r["tracking"] or "").strip():
                tracking_code = r["tracking"]
            if (r["carrier"] or "").strip():
                carrier = r["carrier"]
            if (r["start_date"] or "").strip() and not start_date:
                start_date = r["start_date"]
            if (r["ship_date"] or "").strip() and not ship_date:
                ship_date = r["ship_date"]

            qty = r["qty"] if r["qty"] is not None else 0
            up = r["unit_price"] if r["unit_price"] is not None else 0.0
            line_total = r["total_price"]
            if line_total is None:
                line_total = round((up or 0.0) * (qty or 0), 2)

            items.append({
                "sku": r["sku"],
                "qty": qty,
                "unit_price": up,
                "line_total": line_total
            })
            total_qty += qty or 0
            total_amount += line_total or 0.0

        summary = {
            "lines": len(items),
            "total_qty": total_qty,
            "total_amount": round(total_amount, 2),
            "currency": "MXN"
        }

        message_parts = []
        head = f"Pedido **{order_number}**"
        if ship_date:
            head += f" — **Enviado el {fmt_date_ddmmyyyy(ship_date)}**"
        elif start_date:
            head += f" — **En preparación** (iniciado el {fmt_date_ddmmyyyy(start_date)})"
        else:
            head += " — **En preparación**"
        if carrier and tracking_code:
            head += f" por **{carrier}** (guía **{tracking_code}**)"
        message_parts.append(head + ".")

        message_parts.append(f"Resumen: {summary['lines']} renglones / **{summary['total_qty']} pzas**. Total: **{money_mx(summary['total_amount'])}**.")

        # Detalle de líneas
        for it in items:
            message_parts.append(f"- SKU {it['sku']} — {it['qty']} pzas — {money_mx(it['unit_price'])} c/u — Subtotal {money_mx(it['line_total'])}")

        return {
            "ok": True,
            "order_id": order_number,
            "status": "Enviado" if ship_date else "En preparación",
            "start_date": start_date,
            "ship_date": ship_date,
            "carrier": carrier,
            "tracking": tracking_code,
            "summary": summary,
            "items": items,
            "message": "\n".join(message_parts),
        }

# --------------------------------------------------------------------------------------
# Flask App + Rutas nuevas
# --------------------------------------------------------------------------------------

app = Flask(__name__)
orders = OrderService()

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "ts": int(time.time())})

@app.route("/admin/orders/reload", methods=["POST"])
def admin_orders_reload():
    """
    Recarga el cache desde el Sheet/CSV.
    Protege esta ruta con un reverse proxy o header personalizado si lo deseas.
    """
    try:
        result = orders.reload()
        return jsonify({"ok": True, "reloaded": result["rows"]})
    except AssertionError as ae:
        return jsonify({"ok": False, "error": str(ae)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": repr(e)}), 500

@app.route("/api/orders/lookup", methods=["POST"])
def api_orders_lookup():
    """
    Body (cualquiera de los campos):
    {
      "query": "quiero saber el estatus de mi pedido #ME-12345",
      "order_id": "ME-12345",
      "tracking": "ABC123456MX",
      "email": "cliente@correo.com",
      "phone": "5512345678"
    }
    """
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    order_id = (data.get("order_id") or "").strip() or None
    tracking = (data.get("tracking") or "").strip() or None
    email = (data.get("email") or "").strip() or None
    phone = (data.get("phone") or "").strip() or None

    # Extraer id desde texto libre si no llegó explícito
    if query and not (order_id or tracking):
        ids = orders.extract_identifiers(query)
        order_id = order_id or ids.get("order_id")
        tracking = tracking or ids.get("tracking")

    # Si no hay identificadores, pedirlos
    if not (order_id or tracking or email or phone):
        return jsonify({
            "ok": False,
            "need": "identifier",
            "message": "¿Tienes tu número de orden o el número de guía para buscar tu pedido?"
        }), 200

    try:
        res = orders.lookup(order_id=order_id, tracking=tracking, email=email, phone=phone)
        if not res.get("ok"):
            return jsonify({
                "ok": False,
                "message": "No encontré coincidencias. ¿Puedes confirmar tu número de orden o guía?"
            }), 200
        # Adjuntar tracking_url si se reconoce la paquetería
        trk = res.get("tracking")
        carrier = (res.get("carrier") or "").lower()
        if trk:
            if "estafeta" in carrier:
                res["tracking_url"] = f"https://rastreo.estafeta.com/{trk}"
            elif "fedex" in carrier:
                res["tracking_url"] = f"https://www.fedex.com/fedextrack/?trknbr={trk}"
            elif "dhl" in carrier:
                res["tracking_url"] = f"https://www.dhl.com/mx-es/home/rastreo.html?tracking-id={trk}"
            elif "99minutos" in carrier or "99 minutos" in carrier:
                res["tracking_url"] = f"https://99minutos.com/track?tracking={trk}"
        return jsonify(res), 200
    except Exception as e:
        return jsonify({"ok": False, "error": repr(e)}), 500

# --------------------------------------------------------------------------------------
# Helper para integrarlo a tu /api/chat actual sin cambiarlo
# --------------------------------------------------------------------------------------

ORDER_INTENT_PATTERNS = [
    r"\bestatus\b.*\b(pedido|orden|gu[ií]a)\b",
    r"\bseguimiento\b.*\b(gu[ií]a|pedido|orden)\b",
    r"\b(dónde|donde)\b.*\b(est[aá]|va)\b.*\b(mi\b.*\b(pedido|orden|gu[ií]a))",
    r"\bmi\b.*\b(pedido|orden|gu[ií]a)\b",
]

ORDER_RE = re.compile(OrderService.ORDER_RE.pattern, re.I)
TRACK_RE = re.compile(OrderService.TRACK_RE.pattern, re.I)
INTENT_RES = [re.compile(p, re.I) for p in ORDER_INTENT_PATTERNS]

def handle_orders_intent(message: str, meta: Optional[dict] = None) -> Optional[dict]:
    """
    Llama esta función desde tu flujo actual de /api/chat ANTES de tu lógica normal:
        maybe = handle_orders_intent(user_message)
        if maybe: return jsonify(maybe)

    Si detecta intención de pedido/guía, responde inmediatamente con el JSON del pedido.
    Si no, devuelve None y tu flujo sigue normal.
    """
    text = (message or "").strip()
    if not text:
        return None

    # ¿Coincide con intención?
    if not any(p.search(text) for p in INTENT_RES) and not (ORDER_RE.search(text) or TRACK_RE.search(text)):
        return None

    ids = orders.extract_identifiers(text)
    res = orders.lookup(order_id=ids.get("order_id"), tracking=ids.get("tracking"))
    if not res.get("ok"):
        return {
            "ok": False,
            "need": "identifier",
            "message": "¿Me compartes tu número de orden o guía para localizar tu pedido?"
        }
    # Adjunta tracking_url si aplica
    trk = res.get("tracking")
    carrier = (res.get("carrier") or "").lower()
    if trk:
        if "estafeta" in carrier:
            res["tracking_url"] = f"https://rastreo.estafeta.com/{trk}"
        elif "fedex" in carrier:
            res["tracking_url"] = f"https://www.fedex.com/fedextrack/?trknbr={trk}"
        elif "dhl" in carrier:
            res["tracking_url"] = f"https://www.dhl.com/mx-es/home/rastreo.html?tracking-id={trk}"
        elif "99minutos" in carrier or "99 minutos" in carrier:
            res["tracking_url"] = f"https://99minutos.com/track?tracking={trk}"
    return res

# --------------------------------------------------------------------------------------
# Punto de entrada
# --------------------------------------------------------------------------------------

if __name__ == "__main__":
    # Carga inicial del cache si ORDERS_AUTORELOAD=1
    if env_bool("ORDERS_AUTORELOAD", True):
        try:
            orders.reload()
        except Exception as e:
            print("WARN: No se pudo cargar pedidos al inicio:", repr(e))
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
