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

# Deepseek es opcional
try:
    from .deepseek_client import DeepseekClient
except Exception:
    DeepseekClient = None

load_dotenv()

app = Flask(__name__)

# CORS
_allowed = [o.strip() for o in (os.getenv("ALLOWED_ORIGINS") or "*").split(",") if o.strip()]
CORS(
    app,
    resources={r"/*": {
        "origins": _allowed,
        "allow_headers": ["Content-Type", "X-Admin-Secret"],
        "methods": ["GET", "POST", "OPTIONS"],
    }},
)

# --- Servicios (sin kwargs: ShopifyClient lee envs internamente) ---
shop = ShopifyClient()
indexer = CatalogIndexer(shop, os.getenv("STORE_BASE_URL", "https://master.com.mx"))

CHAT_WRITER = (os.getenv("CHAT_WRITER") or "none").strip().lower()  # none | deepseek
deeps = None
if CHAT_WRITER == "deepseek" and DeepseekClient is not None:
    try:
        deeps = DeepseekClient()
    except Exception:
        deeps = None

# Construye índice al iniciar (no tumbar la app si falla)
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
        '<code>GET /api/admin/diag</code>'
        "</p>"
    )


@app.get("/health")
def health():
    return {"ok": True}


# --------- utils de patrón/explicación ----------
_PAT_ONE_BY_N = re.compile(r"\b(\d+)\s*[x×]\s*(\d+)\b", re.IGNORECASE)

def _detect_patterns(q: str) -> dict:
    ql = (q or "").lower()
    pat = {}
    m = _PAT_ONE_BY_N.search(ql)
    if m:
        pat["matrix"] = f"{m.group(1)}x{m.group(2)}"

    inch = re.findall(r"\b(1[9]|[2-9]\d|100)\b", ql)
    if inch:
        pat["inches"] = list(set(inch))

    cats = []
    for key in ["hdmi", "rca", "coaxial", "antena", "soporte", "control", "cctv", "vga", "usb"]:
        if key in ql:
            cats.append(key)
    if cats:
        pat["cats"] = cats

    for w in ["agua", "fuga", "inundacion", "inundación", "nivel", "boya", "cisterna", "tinaco"]:
        if w in ql:
            pat["water"] = True
            break
    return pat


def _format_answer(query: str, items: list) -> str:
    pat = _detect_patterns(query)
    intro_bits = []
    if pat.get("water"):
        intro_bits.append("monitoreo de nivel de agua en tinacos/cisternas")
    if pat.get("matrix"):
        intro_bits.append(f"patrón {pat['matrix']}")
    if pat.get("inches"):
        intro_bits.append(f"tamaño {', '.join(sorted(pat['inches']))}”")
    if pat.get("cats"):
        intro_bits.append("categorías: " + ", ".join(pat["cats"]))

    lines = []
    if intro_bits:
        lines.append("Consideré: " + "; ".join(intro_bits) + ".")
    lines.append("Estas son las opciones más relevantes que encontré.")
    lines.append("¿Quieres acotar por precio, marca, disponibilidad o tipo?")
    return "\n".join(lines)


def _cards_from_items(items):
    cards = []
    for it in items:
        v = it["variant"]
        cards.append({
            "title": it["title"],
            "image": it["image"],
            "price": money(v["price"]) if v.get("price") is not None else None,
            "compare_at_price": money(v["compare_at_price"]) if v.get("compare_at_price") else None,
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
            "title": it["title"],
            "sku": v.get("sku"),
            "price": money(v["price"]) if v.get("price") is not None else None,
            "product_url": it["product_url"],
            "buy_url": it["buy_url"],
        })
    return out


# ---------- Re-ranker específico para intención de agua/tinaco ----------
def _rerank_for_water(query: str, items: list):
    ql = (query or "").lower()
    water_intent = any(w in ql for w in ["agua","nivel","tinaco","cisterna","bomba","válvula","valvula"])
    if not water_intent or not items:
        return items

    pref_map = {
        "iot-waterv": 0,
        "iot-waterultra": 0,
        "iot-waterp": 0,
        "iot-water": 0,
        "easy-waterultra": 0,
        "easy-water": 0,
    }
    if ("valvula" in ql) or ("válvula" in ql):
        pref_map["iot-waterv"] += 40
    if ("ultra" in ql) or ("ultrason" in ql) or ("ultrasónico" in ql) or ("ultrasonico" in ql):
        pref_map["iot-waterultra"] += 40
        pref_map["easy-waterultra"] += 20
    if ("presion" in ql) or ("presión" in ql):
        pref_map["iot-waterp"] += 40
    if ("bluetooth" in ql):
        pref_map["easy-water"] += 30
        pref_map["easy-waterultra"] += 30
    if ("wifi" in ql) or ("app" in ql):
        pref_map["iot-water"] += 20
        pref_map["iot-waterultra"] += 20
        pref_map["iot-waterv"] += 10

    def strong_text(it):
        v = it.get("variant", {})
        skus = " ".join([s for s in (v.get("sku"),) if s]) + " " + " ".join(it.get("skus") or [])
        return " ".join([
            (it.get("title") or ""),
            (it.get("handle") or ""),
            (it.get("tags") or ""),
            (it.get("vendor") or ""),
            (it.get("product_type") or ""),
            skus
        ]).lower()

    def fam_score(st: str) -> int:
        s = 0
        if any(w in st for w in ["tinaco","cisterna","nivel","agua"]):
            s += 20
        for key, bonus in pref_map.items():
            if key in st:
                s += 60 + bonus
        if "iot water" in st: s += 25
        if "easy water" in st: s += 25
        return s

    rescored = []
    for idx, it in enumerate(items):
        st = strong_text(it)
        base = max(0, 30 - idx)  # mantener orden original si no hay señales
        rescored.append((fam_score(st) + base, it))

    rescored.sort(key=lambda x: x[0], reverse=True)
    return [it for _, it in rescored]


@app.post("/api/chat")
def chat():
    data = request.get_json(force=True) or {}
    query = (data.get("message") or "").strip()
    k = int(data.get("k") or 5)

    if not query:
        return jsonify({"answer": "¿Qué producto buscas? Puedo ayudarte con soportes, antenas, controles, cables, sensores y más.", "products": []})

    # Buscar en el índice
    items = indexer.search(query, k=k)

    # Rerank si hay intención de agua/tinaco
    items = _rerank_for_water(query, items)

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


# --- Reindex en background ---
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
    return jsonify({
        "q": q,
        "k": k,
        "items": _plain_items(items),
    })


@app.get("/api/admin/discards")
def admin_discards():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return jsonify(indexer.discards())


@app.get("/api/admin/products")
def admin_products():
    """Pequeño viewer para muestrear productos crudos y depurar coincidencias."""
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


# --- Rutas de estáticos del widget ---
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
