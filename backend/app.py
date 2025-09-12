# -*- coding: utf-8 -*-
import os
import threading
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

from .shopify_client import ShopifyClient
from .indexer import CatalogIndexer
from .deepseek_client import DeepseekClient
from .prompts import SYSTEM_PROMPT, USER_TEMPLATE
from .utils import money

# ---- Paths estáticos ----
BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "..", "widget")

# Cargar .env
load_dotenv()

# ---- App & CORS ----
app = Flask(__name__)
_allowed = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
CORS(
    app,
    resources={r"/*": {
        "origins": _allowed,
        "allow_headers": ["Content-Type", "X-Admin-Secret"],
        "methods": ["GET", "POST", "OPTIONS"],
    }},
)

# ---- Servicios ----
shop = ShopifyClient()
indexer = CatalogIndexer(shop, os.getenv("STORE_BASE_URL", "https://master.com.mx"))
deeps = DeepseekClient()

# Construye índice al iniciar (no tumbar la app si falla)
try:
    indexer.build()
except Exception as e:
    print(f"[WARN] Index build failed at startup: {e}", flush=True)

def _admin_ok(req) -> bool:
    return req.headers.get("X-Admin-Secret") == os.getenv("ADMIN_REINDEX_SECRET", "")

# ---- Rutas públicas ----
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

# ---- Chat robusto (si hay productos, nunca responde "lo siento") ----
@app.post("/api/chat")
def chat():
    data = request.get_json(force=True)
    query = (data.get("message") or "").strip()
    if not query:
        return jsonify({
            "answer": "¿Qué estás buscando? Puedo ayudarte a encontrar productos del catálogo.",
            "products": []
        })

    items = indexer.search(query, k=5)

    # Tarjetas para el widget
    cards = []
    for it in items:
        v = it["variant"]
        cards.append({
            "title": it["title"],
            "image": it["image"],
            "price": money(v["price"]),
            "compare_at_price": money(v["compare_at_price"]) if v["compare_at_price"] else None,
            "buy_url": it["buy_url"],
            "product_url": it["product_url"],
            "inventory": it["variant"]["inventory"],
        })

    # Si no hay resultados, respuesta útil y corta
    if not items:
        return jsonify({
            "answer": "No encontré resultados exactos para tu consulta. Prueba con palabras clave como marca, modelo o categoría. 😉",
            "products": []
        })

    # Intento con LLM usando mini catálago
    context = indexer.mini_catalog_json(items)
    user_msg = USER_TEMPLATE.format(query=query, catalog_json=context)
    answer = ""
    try:
        answer = deeps.chat(SYSTEM_PROMPT, user_msg) or ""
    except Exception as e:
        print(f"[WARN] Deepseek chat error: {e}", flush=True)
        answer = ""

    # Si el LLM niega o queda vacío, generamos respuesta basada en catálogo
    neg_tokens = ["no dispongo", "no tengo información", "no cuento", "lo siento"]
    if (not answer) or any(tok in answer.lower() for tok in neg_tokens):
        tops = [f"- {c['title']} — {c['price']}  \n  {c['product_url']}" for c in cards[:5]]
        answer = (
            f"Encontré estas opciones relacionadas con “{query}”:  \n"
            + "\n".join(tops)
            + "\n\n¿Quieres que filtre por precio, marca o disponibilidad?"
        )

    return jsonify({"answer": answer, "products": cards})

# ---- Admin: reindex en background ----
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
    items = indexer.search(q, k=5) if q else []
    out = []
    for it in items:
        out.append({
            "title": it["title"],
            "handle": it["handle"],
            "sku": it["variant"]["sku"],
            "price": it["variant"]["price"],
            "stock": sum(x["available"] for x in it["variant"]["inventory"]),
        })
    return {"ok": True, "q": q, "count": len(out), "items": out}

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
    sqlite_path = os.getenv("SQLITE_PATH") or "/data/catalog.db"
    env = {
        "api_version": os.getenv("SHOPIFY_API_VERSION", "2024-10"),
        "shop": os.getenv("SHOPIFY_STORE_DOMAIN") or os.getenv("SHOPIFY_SHOP"),
        "token_present": bool(os.getenv("SHOPIFY_ACCESS_TOKEN") or os.getenv("SHOPIFY_TOKEN")),
        "force_rest": os.getenv("FORCE_REST", "1") == "1",
        "require_active": os.getenv("REQUIRE_ACTIVE", "1"),
        "sqlite_path": sqlite_path,
        "db_dir_exists": os.path.isdir(os.path.dirname(sqlite_path)),
        "db_dir_writable": os.access(os.path.dirname(sqlite_path), os.W_OK),
        "db_file_exists": os.path.exists(sqlite_path),
    }
    probe = {"ok": True}
    try:
        probe["locations"] = len(shop.list_locations())
    except Exception:
        probe["locations"] = 0
        probe["ok"] = False
    try:
        probe["sample_count"] = len(indexer.sample_products(limit=3))
        probe["db_error"] = None
    except Exception as e:
        probe["sample_count"] = 0
        probe["db_error"] = str(e)
        probe["ok"] = False
    return {"ok": True, "env": env, "probe": probe}

@app.get("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
