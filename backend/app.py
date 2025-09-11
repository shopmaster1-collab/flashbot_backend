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
CORS(app, resources={r"/api/*": {"origins": os.getenv("ALLOWED_ORIGINS", "*").split(",")}})

# --- servicios ---
shop = ShopifyClient()
indexer = CatalogIndexer(shop, os.getenv("STORE_BASE_URL", "https://master.com.mx"))
deeps = DeepseekClient()

# build inicial (no tumbar app si falla)
try:
    indexer.build()
except Exception as e:
    print(f"[WARN] Index build failed at startup: {e}", flush=True)

# ---- helpers ----
def _admin_ok(req) -> bool:
    return req.headers.get("X-Admin-Secret") == os.getenv("ADMIN_REINDEX_SECRET", "")

# ---- rutas públicas ----
@app.get("/")
def home():
    return (
        "<h1>Maxter backend</h1>"
        "<p>OK ✅. Endpoints: "
        '<a href="/health">/health</a>, '
        '<code>POST /api/chat</code>, '
        '<code>POST /api/admin/reindex</code>, '
        '<code>GET /api/admin/stats</code>, '
        '<code>GET /api/admin/discards</code>, '
        '<code>GET /api/admin/products</code>, '
        '<code>GET /api/admin/search?q=...</code>'
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

    # Redacción con Deepseek
    context = indexer.mini_catalog_json(items)
    user_msg = USER_TEMPLATE.format(query=query, catalog_json=context)
    try:
        answer = deeps.chat(SYSTEM_PROMPT, user_msg)
    except Exception as e:
        print(f"[WARN] Deepseek chat error: {e}", flush=True)
        answer = "lo siento, no dispongo de esa información"

    # Tarjetas UI
    cards = []
    for it in items:
        v = it["variant"]
        cards.append({
            "title": it["title"],
            "image": it["image"],
            "price": money(v["price"]) if v["price"] is not None else None,
            "compare_at_price": money(v["compare_at_price"]) if v.get("compare_at_price") else None,
            "buy_url": it["buy_url"],
            "product_url": it["product_url"],
            "inventory": v["inventory"],  # [{name, available}, ...] (puede ir vacío con REQUIRE_STOCK=0)
        })

    return jsonify({"answer": answer, "products": cards})

# ---- admin ----
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

@app.get("/api/admin/discards")
def admin_discards():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return indexer.discard_stats()

@app.get("/api/admin/products")
def admin_products():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    limit = int(request.args.get("limit", 10))
    return {"ok": True, "items": indexer.sample_products(limit=limit)}

@app.get("/api/admin/search")
def admin_search():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    q = (request.args.get("q") or "").strip()
    items = indexer.search(q, k=5) if q else []
    out = []
    for it in items:
        v = it["variant"]
        out.append({
            "title": it["title"],
            "handle": it["handle"],
            "sku": v.get("sku"),
            "price": v.get("price"),
            "stock_total": sum(x["available"] for x in v["inventory"]) if v["inventory"] else 0,
        })
    return {"ok": True, "q": q, "count": len(out), "items": out}

# archivos estáticos del widget
@app.get("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
