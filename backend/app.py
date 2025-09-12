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
CORS(
    app,
    resources={r"/*": {"origins": os.getenv("ALLOWED_ORIGINS", "*").split(",")}},
    supports_credentials=False,
)

# --- Paths y clientes ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "..", "widget")

store_base = os.getenv("STORE_BASE_URL", "https://master.com.mx").rstrip("/")
shop_client = ShopifyClient(
    shop=os.getenv("SHOPIFY_STORE_DOMAIN") or os.getenv("SHOPIFY_SHOP", ""),
    token=os.getenv("SHOPIFY_ACCESS_TOKEN") or os.getenv("SHOPIFY_TOKEN", ""),
    api_version=os.getenv("SHOPIFY_API_VERSION", "2024-10"),
)
indexer = CatalogIndexer(shop_client, store_base)

deeps = None
if os.getenv("CHAT_WRITER", "none").lower() == "deepseek" and DeepseekClient:
    try:
        deeps = DeepseekClient(api_key=os.getenv("DEEPSEEK_API_KEY", ""))
    except Exception:
        deeps = None


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


# --------- util de redacción/explicación ----------
_PAT_ONE_BY_N = re.compile(r"\b(\d+)\s*[x×]\s*(\d+)\b", re.IGNORECASE)

def _detect_patterns(q: str) -> dict:
    """Detecta patrones útiles para explicar relevancia (1x4, pulgadas, 'uhf', etc.)."""
    ql = q.lower()
    pat = {}
    m = _PAT_ONE_BY_N.search(ql)
    if m:
        pat["matrix"] = f"{m.group(1)}x{m.group(2)}"
    # pulgadas (número entre 19 y 100 aprox), muy común en soportes/pantallas
    inch = re.findall(r"\b(1[9]|[2-9]\d|100)\b", ql)
    if inch:
        pat["inches"] = list(set(inch))
    # categorías rápidas
    cat = []
    for key in ["hdmi", "rca", "coaxial", "antena", "soporte", "control", "cctv", "vga", "usb"]:
        if key in ql:
            cat.append(key)
    if cat:
        pat["cats"] = cat
    # agua / nivel
    for w in ["agua", "fuga", "inundacion", "inundación", "nivel", "boya", "cisterna", "tinaco"]:
        if w in ql:
            pat["water"] = True
            break

    return pat


def _format_answer(query: str, items):
    """Texto corto basado solo en lo encontrado (sin inventar info)."""
    pat = _detect_patterns(query)
    lines = []
    if "water" in pat:
        lines.append("Te muestro opciones para medir/monitorear nivel de agua en tinacos o cisternas.")
    if "matrix" in pat:
        lines.append(f"También consideré la matriz solicitada ({pat['matrix']}).")
    if "inches" in pat:
        pulgadas = ", ".join(sorted(pat["inches"]))
        lines.append(f"Detecté tamaño(s) en pulgadas: {pulgadas}.")
    if "cats" in pat:
        cats = ", ".join(pat["cats"])
        lines.append(f"Categorías relevantes: {cats}.")
    if not lines:
        lines.append("Estas son las opciones más relevantes que encontré.")
    lines.append("Si quieres, puedo acotar por precio, marca, disponibilidad o tipo (p. ej. 1×2, 1×4, ‘para 55”’, ‘UHF’, etc.)?")
    return "\n".join(lines)


def _cards_from_items(items):
    """Transforma resultados del indexer a tarjetas del widget/chat."""
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
            "inventory": it["variant"]["inventory"],
        })
    return cards


def _plain_items(items):
    """Versión simple (para enviar al LLM o a la redacción) con precio ya formateado."""
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

    # Señales específicas por modelo
    pref_map = {
        "iot-waterv": 0,
        "iot-waterultra": 0,
        "iot-waterp": 0,
        "iot-water": 0,
        "easy-waterultra": 0,
        "easy-water": 0,
    }
    # Ajustes por calificadores en la consulta
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
        # Palabras genéricas que deberían existir
        if any(w in st for w in ["tinaco","cisterna","nivel","agua"]):
            s += 20
        # Ponderaciones por familia
        for key, bonus in pref_map.items():
            if key in st:
                s += 60 + bonus
        # Tolerancia a guiones/variantes
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

    # Si no hay resultados, responde claro
    if not items:
        return jsonify({
            "answer": "No encontré resultados directos. Prueba con palabras clave específicas (p. ej. ‘divisor hdmi 1×4’, ‘soporte pared 55”’, ‘antena exterior UHF’, ‘control Samsung’, ‘cable RCA audio video’).",
            "products": []
        })

    # Armar tarjetas para el widget
    cards = _cards_from_items(items)

    # Redacción breve (sin inventar info)
    simple = _plain_items(items)
    base_answer = _format_answer(query, items)

    # Si hay deepseek y lo pediste, intenta embellecer (con fallback seguro)
    if deeps:
        try:
            system_prompt = (
                "Actúa como asesor de compras para una tienda retail de electrónica. "
                "Responde claro, breve (5-20 frases), sin inventar datos. "
                "No modifiques ni repitas los precios; ya van en tarjetas."
            )
            user_prompt = base_answer
            pretty = deeps.chat(system_prompt, user_prompt)
            # Precaución: si por alguna razón quita bullets, usamos la base.
            if pretty and pretty.count("- ") >= len(simple) and len(pretty) > 40:
                answer = pretty
            else:
                answer = base_answer
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
        # leer conteo rápido
        stats = indexer.stats()
        probe["sample_count"] = stats.get("products", 0)
    except Exception as e:
        probe["ok"] = False
        probe["db_error"] = str(e)

    info["probe"] = probe
    return jsonify(info)


# --- Rutas de estáticos del widget ---
@app.get("/static/<path:fname>")
def static_files(fname):
    # Ruta histórica /static/...  -> sirve archivos desde /widget
    return send_from_directory(STATIC_DIR, fname)

@app.get("/widget/<path:fname>")
def widget_files(fname):
    # Alias conveniente /widget/...
    return send_from_directory(STATIC_DIR, fname)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
