# -*- coding: utf-8 -*-
# orders_endpoints.py
import os, re, time, html, logging
from flask import Blueprint, request, jsonify

# Reutilizamos tu lector de hoja publicada
try:
    from .orders_report import OrdersSheetReader, render_vertical_md
except Exception:
    # En despliegue real esto debería existir; si no, devolvemos un error claro
    OrdersSheetReader = None
    def render_vertical_md(rows): return "Sin renderer disponible."

bp_orders = Blueprint("bp_orders", __name__)

# Instancia compartida con caché de la hoja
_ORDERS_URL = os.getenv("ORDERS_PUBHTML_URL", "").strip()
_TTL = int(os.getenv("ORDERS_AUTORELOAD", "45") or "45")
_reader = None
if OrdersSheetReader and _ORDERS_URL:
    try:
        _reader = OrdersSheetReader(_ORDERS_URL, ttl=_TTL)
    except Exception as e:
        logging.exception("OrdersSheetReader init failed: %s", e)
else:
    logging.warning("orders_endpoints: missing deps or ORDERS_PUBHTML_URL")

_ORDER_RE = re.compile(r"\d{4,15}")

def _extract_order_no(raw: str) -> str:
    if not raw: return ""
    m = _ORDER_RE.search(str(raw))
    return m.group(0) if m else ""

@bp_orders.post("/api/orders/status")
def order_status():
    """
    Endpoint dedicado para consultar el estatus de un pedido.
    Body esperado: { "order_no": "1234567" }  (acepta con o sin '#')
    Respuesta: { ok, answer(markdown), rows_count }
    """
    if _reader is None:
        return jsonify({"ok": False, "error": "Orders module not ready (missing ORDERS_PUBHTML_URL or bs4)."}), 500

    data = request.get_json(silent=True) or {}
    raw_no = data.get("order_no", "")
    order_no = _extract_order_no(raw_no)

    if not order_no:
        return jsonify({"ok": False, "error": "Número de orden inválido. Ingresa 4 a 15 dígitos."}), 400

    try:
        rows = _reader.find_by_order(order_no)
    except Exception as e:
        logging.exception("orders lookup failed: %s", e)
        return jsonify({"ok": False, "error": "Error consultando el reporte de pedidos."}), 500

    if not rows:
        return jsonify({"ok": True, "answer": f"No encontramos información para el pedido #{order_no}.", "rows_count": 0})

    md = render_vertical_md(rows)
    return jsonify({"ok": True, "answer": md, "rows_count": len(rows)})

@bp_orders.get("/api/admin/orders-ping")
def orders_ping():
    """
    Diagnóstico: trae headers y una muestra para validar que la hoja pubblicada carga en Render.
    Recomendado proteger con X-Admin-Secret en Nginx/Firewall si lo deseas.
    """
    if _reader is None:
        return jsonify({"ok": False, "error": "Orders module not ready"}), 500
    try:
        meta = _reader.meta()
        sample = _reader.sample(3)
        return jsonify({"ok": True, "meta": meta, "sample": sample})
    except Exception as e:
        logging.exception("orders ping failed: %s", e)
        return jsonify({"ok": False, "error": repr(e)}), 500
