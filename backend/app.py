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

BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "..", "widget")

load_dotenv()

app = Flask(__name__)

# ---------- CORS ----------
_allowed_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
CORS(app, resources={
    r"/api/*": {
        "origins": _allowed_origins,
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "X-Admin-Secret"],
        "max_age": 86400,
    }
})

@app.after_request
def _force_cors_headers(resp):
    # Garantiza headers CORS (útil para preflight y respuestas personalizadas)
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Access-Control-Allow-Origin", request.headers.get("Origin", "*"))
        resp.headers.setdefault("Vary", "Origin")
        resp.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        resp.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, X-Admin-Secret")
        resp.headers.setdefault("Access-Control-Max-Age", "86400")
    return resp

# Preflight genérico para /api/admin/*
@app.route("/api/admin/<path:subpath>", methods=["OPTIONS"])
def _admin_preflight(subpath):
    return ("", 200)

# ---------- Servicios ----------
shop = ShopifyClient()
indexer = CatalogIndexer(shop, os.getenv("STORE_BASE_URL", "https://master.com.mx"))
deeps = DeepseekClient()

# Construye índice al iniciar (no tumbar la app si falla)
try:
    indexer.build()
except Exception as e:
    print(f"[WARN] Index build failed at startup: {e}", flush=True)

# ---------- Rutas públicas ----------
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
        '<code>GET /api/admin/diag</code>'
        ".</p>"
    )

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/api/chat")
def chat():
    data = request.get_json(force=True)
    query = (data.get("message") or "").strip()
    if not query:
        return jsonify({"answer": "lo siento, no dispongo de esa información", "products": []})

    items = indexer.search(query, k=5)
    if not items:
        return jsonify({"answer": "lo siento, no dispongo de esa información", "products": []})

    context = indexer.mini_catalog_json(items)
    user_msg = USER_TEMPLATE.format(query=query, catalog_json=context)
    try:
        answer = deeps.chat(SYSTEM_PROMPT, user_msg)
    except Exception as e:
        print(f"[WARN] Deepseek chat error: {e}", flush=True)
        answer = "lo siento, no dispongo de esa información"

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
    return jsonify({"answer": answer, "products": cards})

# ---------- Admin (requiere X-Admin-Secret) ----------
def _admin_ok(req) -> bool:
    return req.headers.get("X-Admin-Secret") == os.getenv("ADMIN_REINDEX_SECRET", "")

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

@app.get("/api/admin/diag")
def admin_diag():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    env = {
        "store_domain": os.getenv("SHOPIFY_STORE_DOMAIN"),
        "api_version": os.getenv("SHOPIFY_API_VERSION", "2024-10"),
    }
    probe = shop.probe()
    return {"ok": True, "env": env, "probe": probe}

# ---------- Estáticos (widget) ----------
@app.get("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
