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
    DeepseekClient = None  # type: ignore

BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "..", "widget")

load_dotenv()

app = Flask(__name__)

# --- CORS (ampliado a todas las rutas) ---
_allowed = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
CORS(
    app,
    resources={r"/*": {
        "origins": _allowed,
        "allow_headers": ["Content-Type", "X-Admin-Secret"],
        "methods": ["GET", "POST", "OPTIONS"],
    }},
)

# --- Servicios ---
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
        ".</p>"
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
    inch = re.findall(r"\b(1[9-9]|[2-9]\d|100)\b", ql)
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


def _format_answer(query: str, items: list) -> str:
    """
    Arma una respuesta clara con bullets + breve explicación de por qué se sugieren.
    """
    pat = _detect_patterns(query)
    intro_bits = []

    if pat.get("matrix"):
        intro_bits.append(f"patrón {pat['matrix']}")
    if pat.get("inches"):
        intro_bits.append(f"tamaño {', '.join(sorted(pat['inches']))}”")
    if pat.get("cats"):
        intro_bits.append("categoría " + ", ".join(pat["cats"]))
    if pat.get("water"):
        intro_bits.append("términos de agua/nivel")

    if intro_bits:
        intro = f"Encontré opciones relevantes para “{query}” ({'; '.join(intro_bits)}):"
    else:
        intro = f"Encontré estas opciones relacionadas con “{query}”:"

    lines = [intro]
    for it in items:
        title = it.get("title") or "Producto"
        sku = it.get("sku") or ""
        price = it.get("price") or ""
        url = it.get("product_url") or ""
        sku_txt = f" — {sku}" if sku else ""
        price_txt = f" — {price}" if price else ""
        lines.append(f"- {title}{sku_txt}{price_txt}\n  {url}")

    lines.append("\n¿Quieres que filtre por precio, marca, disponibilidad o tipo (p. ej. 1×2, 1×4, ‘para 55”’, ‘UHF’, etc.)?")
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


@app.post("/api/chat")
def chat():
    data = request.get_json(force=True) or {}
    query = (data.get("message") or "").strip()
    k = int(data.get("k") or 5)

    if not query:
        return jsonify({"answer": "¿Qué producto buscas? Puedo ayudarte con divisores HDMI, soportes, antenas, controles, cables, sensores y más.", "products": []})

    # Buscar en el índice
    items = indexer.search(query, k=k)

    # Si no hay resultados, responde claro
    if not items:
        return jsonify({
            "answer": f"No encontré resultados directos para “{query}”. Prueba con palabras clave específicas (p. ej. ‘divisor hdmi 1×4’, ‘soporte pared 55”, ‘antena exterior UHF’, ‘control Samsung’, ‘cable RCA audio video’).",
            "products": []
        })

    # Tarjetas (para el widget)
    cards = _cards_from_items(items)

    # Redacción base determinística (si luego usamos Deepseek, preservamos la lista)
    simple = _plain_items(items)
    base_answer = _format_answer(query, simple)

    # Reescritura opcional con Deepseek
    if deeps is not None:
        try:
            # Prompt minimalista y restrictivo: que NO borre la lista ni la estructura.
            system_prompt = (
                "Eres un asistente de una tienda. Mejora la redacción del mensaje del usuario manteniendo SIEMPRE "
                "la lista de productos tal como aparece (mismos bullets, títulos, SKU, precios y enlaces). "
                "Corrige estilo y añade una frase final de ayuda, sin inventar información."
            )
            user_prompt = base_answer
            pretty = deeps.chat(system_prompt, user_prompt)
            # Precaución: si por alguna razón quita bullets, usamos la base.
            if pretty and pretty.count("- ") >= simple and len(pretty) > 40:
                answer = pretty
            else:
                answer = base_answer
        except Exception as e:
            print(f"[WARN] Deepseek chat error: {e}", flush=True)
            answer = base_answer
    else:
        answer = base_answer

    return jsonify({"answer": answer, "products": cards})


# ---- Reindex en background ----
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
    return {"ok": True, "message": "reindex started"}


@app.get("/api/admin/stats")
def admin_stats():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return {"ok": True, **indexer.stats()}


@app.get("/api/admin/search")
def admin_search():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    q = (request.args.get("q") or "").strip()
    k = int(request.args.get("k") or 5)
    debug = (request.args.get("debug") == "1")

    items = indexer.search(q, k=k) if q else []
    out = []
    for it in items:
        out.append({
            "title": it["title"],
            "handle": it["handle"],
            "sku": it["variant"]["sku"],
            "price": it["variant"]["price"],
            "stock": sum(x["available"] for x in it["variant"]["inventory"]),
        })

    if not debug:
        return {"ok": True, "q": q, "count": len(out), "items": out}

    # info de depuración: patrones detectados
    dbg = _detect_patterns(q)
    return {"ok": True, "q": q, "count": len(out), "items": out, "debug": dbg}


@app.get("/api/admin/discards")
def admin_discards():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return {"ok": True, **indexer.discard_stats()}


@app.get("/api/admin/products")
def admin_products():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    limit = int(request.args.get("limit", 10))
    return {"ok": True, "items": indexer.sample_products(limit=limit)}


@app.get("/api/admin/diag")
def admin_diag():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    # Estado de DB y entorno
    sqlite_path = os.getenv("SQLITE_PATH", "")
    db_dir = os.path.dirname(sqlite_path) if sqlite_path else ""
    env = {
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

    try:
        probe["locations"] = len(shop.list_locations())
    except Exception:
        probe["locations"] = 0

    return {"ok": True, "env": env, "probe": probe}


@app.get("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
