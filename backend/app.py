# -*- coding: utf-8 -*-
import os
import re
import threading
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

from .shopify_client import ShopifyClient
from .indexer import CatalogIndexer
from .utils import money

# Deepseek opcional
try:
    from .deepseek_client import DeepseekClient
except Exception:
    DeepseekClient = None

load_dotenv()

app = Flask(__name__)

# ---- CORS
_allowed = [o.strip() for o in (os.getenv("ALLOWED_ORIGINS") or "*").split(",") if o.strip()]
CORS(
    app,
    resources={r"/*": {
        "origins": _allowed,
        "allow_headers": ["Content-Type", "X-Admin-Secret"],
        "methods": ["GET", "POST", "OPTIONS"],
    }},
)

# ---- Servicios (ShopifyClient lee envs internamente)
shop = ShopifyClient()
indexer = CatalogIndexer(shop, os.getenv("STORE_BASE_URL", "https://master.com.mx"))

CHAT_WRITER = (os.getenv("CHAT_WRITER") or "none").strip().lower()  # none | deepseek
deeps = None
if CHAT_WRITER == "deepseek" and DeepseekClient is not None:
    try:
        deeps = DeepseekClient()
    except Exception:
        deeps = None

# Construcción de índice al iniciar (no tumbar si falla)
try:
    indexer.build()
except Exception as e:
    print(f"[WARN] Index build failed at startup: {e}", flush=True)


def _admin_ok(req) -> bool:
    return req.headers.get("X-Admin-Secret") == os.getenv("ADMIN_REINDEX_SECRET", "")


@app.get("/")
def home():
    return (
        "<h1>Maxter backend</h1>"
        "<p>OK ✅. Endpoints: "
        '<a href="/health">/health</a>, '
        '<code>POST /api/chat</code>, '
        '<code>POST /api/admin/reindex</code>, '
        '<code>GET /api/admin/stats</code>, '
        '<code>GET /api/admin/search?q=...</code>, '
        '<code>GET /api/admin/discards</code>, '
        '<code>GET /api/admin/products</code>, '
        '<code>GET /api/admin/diag</code>, '
        '<code>GET /api/admin/preview?q=...</code>'
        "</p>"
    )


@app.get("/health")
def health():
    return {"ok": True}


# --------- util de patrones para la redacción ----------
_PAT_ONE_BY_N = re.compile(r"\b(\d+)\s*[x×]\s*(\d+)\b", re.IGNORECASE)

def _detect_patterns(q: str) -> dict:
    ql = (q or "").lower()
    pat = {}
    m = _PAT_ONE_BY_N.search(ql)
    if m:
        pat["matrix"] = f"{m.group(1)}x{m.group(2)}"

    inch = re.findall(r"\b(1[9]|[2-9]\d|100)\b", ql)
    if inch:
        pat["inches"] = sorted(set(inch))

    cats = []
    for key in ["hdmi", "rca", "coaxial", "antena", "soporte", "control", "cctv", "vga", "usb"]:
        if key in ql:
            cats.append(key)
    if cats:
        pat["cats"] = cats

    # Intenciones
    if any(w in ql for w in ["agua", "fuga", "inundacion", "inundación", "nivel", "boya", "cisterna", "tinaco"]):
        pat["water"] = True
    if any(w in ql for w in ["gas", "estacionario", "propano", "butano", "lp"]):
        pat["gas"] = True

    # Calificadores
    if any(w in ql for w in ["valvula", "válvula"]): pat["valve"] = True
    if any(w in ql for w in ["ultra", "ultrason", "ultrasónico", "ultrasonico"]): pat["ultra"] = True
    if any(w in ql for w in ["presion", "presión"]): pat["pressure"] = True
    if "bluetooth" in ql: pat["bt"] = True
    if ("wifi" in ql) or ("app" in ql): pat["wifi"] = True
    if any(w in ql for w in ["pantalla", "display"]): pat["display"] = True
    if "alarma" in ql: pat["alarm"] = True
    return pat


def _format_answer(query: str, items: list) -> str:
    pat = _detect_patterns(query)
    bits = []
    if pat.get("water"): bits.append("monitoreo de nivel de agua en tinacos/cisternas")
    if pat.get("gas"): bits.append("medición/monitoreo de gas en tanque estacionario")
    if pat.get("valve"): bits.append("con válvula electrónica")
    if pat.get("ultra"): bits.append("sensor ultrasónico")
    if pat.get("pressure"): bits.append("sensor de presión")
    if pat.get("bt"): bits.append("con Bluetooth")
    if pat.get("wifi"): bits.append("con WiFi/App")
    if pat.get("display"): bits.append("con pantalla/display")
    if pat.get("alarm"): bits.append("con alarma de nivel bajo")
    if pat.get("matrix"): bits.append(f"matriz {pat['matrix']}")
    if pat.get("inches"): bits.append(f"tamaños {', '.join(pat['inches'])}”")
    if pat.get("cats"): bits.append("categorías: " + ", ".join(pat["cats"]))

    lines = []
    if bits: lines.append("Consideré: " + "; ".join(bits) + ".")
    lines.append("Estas son las opciones más relevantes que encontré.")
    lines.append("¿Quieres acotar por marca, precio, disponibilidad o tipo?")
    return "\n".join(lines)


def _cards_from_items(items):
    cards = []
    for it in items:
        v = it["variant"]
        cards.append({
            "title": it["title"],
            "image": it["image"],
            "price": money(v.get("price")) if v.get("price") is not None else None,
            "compare_at_price": money(v.get("compare_at_price")) if v.get("compare_at_price") else None,
            "buy_url": it["buy_url"],
            "product_url": it["product_url"],
            "inventory": v.get("inventory"),
        })
    return cards


def _plain_items(items):
    out = []
    for it in items:
        v = it["variant"]
        out.append({
            "title": it.get("title"),
            "sku": v.get("sku"),
            "price": money(v.get("price")) if v.get("price") is not None else None,
            "product_url": it.get("product_url"),
            "buy_url": it.get("buy_url"),
        })
    return out


# ---------- Señales por intención ----------

# Agua / nivel
_WATER_ALLOW_FAMILIES = [
    "iot-waterv", "iot-waterultra", "iot-waterp", "iot-water",
    "easy-waterultra", "easy-water",
    # tolerancias
    "iot waterv", "iot waterultra", "iot waterp", "iot water",
    "easy waterultra", "easy water",
]
_WATER_ALLOW_KEYWORDS = ["tinaco", "cisterna", "nivel", "agua"]
_WATER_BLOCK = [
    "bm-carsensor", "carsensor", "car", "auto", "vehiculo", "vehículo",
    "ar-rain", "rain", "lluvia",
    "ar-gasc", "gasc", "gas ", " gas-", " gas|",  # castigo gas SOLO en intención agua
    "ar-knock", "knock", "golpe",
    "co2", "humo", "smoke",
]

# Gas / tanque estacionario
_GAS_ALLOW_FAMILIES = [
    "iot-gassensorv", "iot-gassensor", "easy-gas", "connect-gas",
    # tolerancias
    "iot gassensorv", "iot gassensor", "easy gas", "connect gas",
]
_GAS_ALLOW_KEYWORDS = ["gas", "tanque", "estacionario", "estacionaria", "lp", "propano", "butano"]
_GAS_BLOCK = [
    # evitar colados de agua u otros
    "water", "waterv", "waterultra", "waterp", "easy-water", "easy water",
    "ar-rain", "rain", "lluvia",
    "ar-knock", "knock", "golpe",
    "co2", "humo", "smoke",
    "carsensor", "bm-carsensor", "auto", "vehiculo", "vehículo",
]

def _concat_fields(it) -> str:
    v = it.get("variant", {})
    parts = [
        it.get("title") or "",
        it.get("handle") or "",
        it.get("tags") or "",
        it.get("vendor") or "",
        it.get("product_type") or "",
        v.get("sku") or "",
    ]
    if isinstance(it.get("skus"), (list, tuple)):
        parts.extend([x for x in it["skus"] if x])
    return " ".join(parts).lower()


def _score_family(st: str, ql: str, allow_keywords, allow_fams, extras) -> int:
    s = 0
    if any(w in st for w in allow_keywords): s += 20
    for fam in allow_fams:
        if fam in st: s += 80
    # extras (diccionario de reglas por intención)
    # water/gas comparten calificados pero con diferentes pesos
    if extras.get("want_valve"):
        for key in extras.get("valve_fams", []):
            if key in st: s += extras.get("valve_bonus", 60)
    if extras.get("want_ultra"):
        for key in extras.get("ultra_fams", []):
            if key in st: s += extras.get("ultra_bonus", 60)
    if extras.get("want_pressure"):
        for key in extras.get("pressure_fams", []):
            if key in st: s += extras.get("pressure_bonus", 60)
    if extras.get("want_bt"):
        for key in extras.get("bt_fams", []):
            if key in st: s += extras.get("bt_bonus", 40)
    if extras.get("want_wifi"):
        for key in extras.get("wifi_fams", []):
            if key in st: s += extras.get("wifi_bonus", 40)
    if extras.get("want_display"):
        for key in extras.get("display_fams", []):
            if key in st: s += extras.get("display_bonus", 35)
    if extras.get("want_alarm"):
        for key in extras.get("alarm_words", []):
            if key in st: s += extras.get("alarm_bonus", 25)
    return s


def _intent_from_query(query: str) -> str | None:
    ql = (query or "").lower()
    if any(w in ql for w in _WATER_ALLOW_KEYWORDS + ["cisterna", "tinaco"]):
        # si menciona gas explícito, gana gas
        if "gas" not in ql:
            return "water"
    if "gas" in ql or any(w in ql for w in ["tanque", "estacionario", "lp", "propano", "butano"]):
        return "gas"
    return None


def _rerank_for_water(query: str, items: list):
    ql = (query or "").lower()
    water_intent = _intent_from_query(query) == "water"
    if not water_intent or not items:
        return items

    want_valve = ("valvula" in ql) or ("válvula" in ql)
    extras = {
        "want_valve": want_valve,
        "want_ultra": any(w in ql for w in ["ultra", "ultrason", "ultrasónico", "ultrasonico"]),
        "want_pressure": any(w in ql for w in ["presion", "presión"]),
        "want_bt": "bluetooth" in ql,
        "want_wifi": ("wifi" in ql) or ("app" in ql),
        "valve_fams": ["iot-waterv", "iot waterv"],
        "ultra_fams": ["waterultra", "easy-waterultra", "easy waterultra"],
        "pressure_fams": ["iot-waterp", "iot waterp"],
        "bt_fams": ["easy-water", "easy water", "easy-waterultra", "easy waterultra"],
        "wifi_fams": ["iot-water", "iot water", "iot-waterv", "iot waterv", "iot-waterultra", "iot waterultra"],
        "valve_bonus": 90, "ultra_bonus": 60, "pressure_bonus": 60, "bt_bonus": 40, "wifi_bonus": 40,
    }

    rescored, positives = [], []
    for idx, it in enumerate(items):
        st = _concat_fields(it)
        blocked = any(b in st for b in _WATER_BLOCK)
        base = max(0, 30 - idx)
        score = _score_family(st, ql, _WATER_ALLOW_KEYWORDS, _WATER_ALLOW_FAMILIES, extras)
        total = score + base - (120 if blocked else 0)
        is_wv = ("iot-waterv" in st) or ("iot waterv" in st)
        rec = (total, score, blocked, is_wv, it)
        rescored.append(rec)
        if score >= 60 and not blocked:
            positives.append(rec)

    if positives:
        positives.sort(key=lambda x: x[0], reverse=True)
        if want_valve:
            wv = [r for r in positives if r[3]]  # is_wv
            others = [r for r in positives if not r[3]]
            ordered = wv + others
        else:
            ordered = positives
        return [it for (_t, _s, _b, _flag, it) in ordered]

    rescored.sort(key=lambda x: x[0], reverse=True)
    return [it for (_t, _s, _b, _flag, it) in rescored]


def _rerank_for_gas(query: str, items: list):
    ql = (query or "").lower()
    gas_intent = _intent_from_query(query) == "gas"
    if not gas_intent or not items:
        return items

    want_valve = ("valvula" in ql) or ("válvula" in ql)
    extras = {
        "want_valve": want_valve,
        "want_ultra": False,  # no aplica espíritu ultra en gas
        "want_pressure": False,
        "want_bt": "bluetooth" in ql,
        "want_wifi": ("wifi" in ql) or ("app" in ql),
        "want_display": any(w in ql for w in ["pantalla", "display"]),
        "want_alarm": "alarma" in ql,
        "valve_fams": ["iot-gassensorv", "iot gassensorv"],
        "bt_fams": ["easy-gas", "easy gas"],
        "wifi_fams": ["iot-gassensor", "iot gassensor", "connect-gas", "connect gas"],
        "display_fams": ["easy-gas", "easy gas"],
        "alarm_words": ["alarma", "alerta"],
        "valve_bonus": 95, "bt_bonus": 45, "wifi_bonus": 45, "display_bonus": 40, "alarm_bonus": 25,
    }

    rescored, positives = [], []
    for idx, it in enumerate(items):
        st = _concat_fields(it)
        blocked = any(b in st for b in _GAS_BLOCK)
        base = max(0, 30 - idx)
        score = _score_family(st, ql, _GAS_ALLOW_KEYWORDS, _GAS_ALLOW_FAMILIES, extras)
        total = score + base - (120 if blocked else 0)
        is_valve = ("iot-gassensorv" in st) or ("iot gassensorv" in st)
        rec = (total, score, blocked, is_valve, it)
        rescored.append(rec)
        if score >= 60 and not blocked:
            positives.append(rec)

    if positives:
        positives.sort(key=lambda x: x[0], reverse=True)
        if want_valve:
            vs = [r for r in positives if r[3]]
            others = [r for r in positives if not r[3]]
            ordered = vs + others
        else:
            ordered = positives
        return [it for (_t, _s, _b, _flag, it) in ordered]

    rescored.sort(key=lambda x: x[0], reverse=True)
    return [it for (_t, _s, _b, _flag, it) in rescored]


def _apply_intent_rerank(query: str, items: list):
    """Selecciona y aplica el rerank según intención detectada."""
    intent = _intent_from_query(query)
    if intent == "water":
        return _rerank_for_water(query, items)
    if intent == "gas":
        return _rerank_for_gas(query, items)
    return items


@app.post("/api/chat")
def chat():
    data = request.get_json(force=True) or {}
    query = (data.get("message") or "").strip()
    k = int(data.get("k") or 5)

    if not query:
        return jsonify({"answer": "¿Qué producto buscas? Puedo ayudarte con soportes, antenas, controles, cables, sensores y más.", "products": []})

    # Buscar en el índice (pedimos un poco más para rerank robusto)
    items = indexer.search(query, k=max(k, 20))

    # Re-rank según intención (agua/gas)
    items = _apply_intent_rerank(query, items)

    # Recortar a k
    items = items[:k] if k and isinstance(items, list) else items

    if not items:
        return jsonify({
            "answer": "No encontré resultados directos. Prueba con palabras clave específicas (p. ej. ‘divisor hdmi 1×4’, ‘soporte pared 55”’, ‘antena exterior UHF’, ‘control Samsung’, ‘cable RCA audio video’).",
            "products": []
        })

    cards = _cards_from_items(items)
    base_answer = _format_answer(query, items)

    if deeps:
        try:
            system_prompt = (
                "Actúa como asesor de compras para una tienda retail de electrónica. "
                "Responde claro, breve (5-20 frases), sin inventar datos. "
                "No modifiques ni repitas los precios; ya van en tarjetas."
            )
            user_prompt = base_answer
            pretty = deeps.chat(system_prompt, user_prompt)
            answer = pretty if (pretty and len(pretty) > 40) else base_answer
        except Exception as e:
            print(f"[WARN] Deepseek chat error: {e}", flush=True)
            answer = base_answer
    else:
        answer = base_answer

    return jsonify({"answer": answer, "products": cards})


# --- Reindex en background (sin cambios)
def _do_reindex():
    try:
        print("[INDEX] Reindex started", flush=True)
        indexer.build()
        print("[INDEX] Reindex finished", flush=True)
        print(f"[INDEX] Stats: {indexer.stats()}", flush=True)
    except Exception as e:
        import traceback
        print(f"[INDEX] Reindex failed: {e}\n{traceback.format_exc()}", flush=True)


@app.post("/api/admin/reindex")
def reindex():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    threading.Thread(target=_do_reindex, daemon=True).start()
    return {"ok": True}


@app.get("/api/admin/stats")
def admin_stats():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return indexer.stats()


@app.get("/api/admin/search")
def admin_search():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    q = (request.args.get("q") or "").strip()
    k = int(request.args.get("k") or 12)
    items = indexer.search(q, k=k)
    return jsonify({"q": q, "k": k, "items": _plain_items(items)})


@app.get("/api/admin/discards")
def admin_discards():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return jsonify(indexer.discards())


@app.get("/api/admin/products")
def admin_products():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    page = int(request.args.get("page") or 1)
    size = max(1, min(100, int(request.args.get("size") or 20)))
    filt = (request.args.get("f") or "").strip().lower()
    data = indexer.sample_products(page=page, size=size, q=filt)
    return jsonify(data)


@app.get("/api/admin/diag")
def admin_diag():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    sqlite_path = os.getenv("SQLITE_PATH")
    db_dir = os.path.dirname(sqlite_path) if sqlite_path else None
    info = {
        "api_version": os.getenv("SHOPIFY_API_VERSION", "2024-10"),
        "shop": os.getenv("SHOPIFY_STORE_DOMAIN", os.getenv("SHOPIFY_SHOP", "")),
        "sqlite_path": sqlite_path or "(default)",
        "db_dir_exists": bool(db_dir and os.path.isdir(db_dir)),
        "db_dir_writable": bool(db_dir and os.path.isdir(db_dir) and os.access(db_dir, os.W_OK)),
        "db_file_exists": bool(sqlite_path and os.path.isfile(sqlite_path)),
        "force_rest": os.getenv("FORCE_REST", "0") == "1",
        "require_active": os.getenv("REQUIRE_ACTIVE", "1"),
        "token_present": bool(os.getenv("SHOPIFY_ACCESS_TOKEN") or os.getenv("SHOPIFY_TOKEN")),
    }
    probe = {"ok": True, "db_error": None}
    try:
        stats = indexer.stats()
        probe["sample_count"] = stats.get("products", 0)
    except Exception as e:
        probe["ok"] = False
        probe["db_error"] = str(e)
    info["probe"] = probe
    return jsonify(info)


# --- Vista de re-rank (diagnóstico)
@app.get("/api/admin/preview")
def admin_preview():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    q = (request.args.get("q") or "").strip()
    k = int(request.args.get("k") or 12)
    raw = indexer.search(q, k=max(k, 30))
    reranked = _apply_intent_rerank(q, raw)
    return jsonify({
        "q": q,
        "raw_titles": [i.get("title") for i in raw[:k]],
        "reranked_titles": [i.get("title") for i in reranked[:k]],
    })


# --- Estáticos del widget
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "..", "widget")

@app.get("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)

@app.get("/widget/<path:fname>")
def widget_files(fname):
    return send_from_directory(STATIC_DIR, fname)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
