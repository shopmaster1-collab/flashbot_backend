# backend/app.py
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

# --- Servicios (instanciación) ---
shop = ShopifyClient()
indexer = CatalogIndexer(shop, os.getenv("STORE_BASE_URL", "https://master.com.mx"))
deeps = DeepseekClient()

def _admin_ok(req) -> bool:
    """Valida el header X-Admin-Secret contra ADMIN_REINDEX_SECRET."""
    return req.headers.get("X-Admin-Secret") == os.getenv("ADMIN_REINDEX_SECRET", "")

# Construye índice al iniciar (no tumbar la app si falla)
try:
    indexer.build()
except Exception as e:
    print(f"[WARN] Index build failed at startup: {e}", flush=True)

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
        '<code>GET /api/admin/products?limit=N</code>, '
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

    # Buscar en catálogo
    items = indexer.search(query, k=5)
    if not items:
        return jsonify({"answer": "lo siento, no dispongo de esa información", "products": []})

    # Redacción con Deepseek (contexto limitado)
    context = indexer.mini_catalog_json(items)
    user_msg = USER_TEMPLATE.format(query=query, catalog_json=context)
    try:
        answer = deeps.chat(SYSTEM_PROMPT, user_msg)
    except Exception as e:
        print(f"[WARN] Deepseek chat error: {e}", flush=True)
        answer = "lo siento, no dispongo de esa información"

    # Formateo de tarjetas
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
    """Diagnóstico rápido: muestra flags de entorno y prueba Shopify ligera."""
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    env_info = {
        "api_version": os.getenv("SHOPIFY_API_VERSION", "2025-01"),
        "store_domain": os.getenv("SHOPIFY_STORE_DOMAIN", ""),
        "require_active": os.getenv("REQUIRE_ACTIVE", "1"),
        "require_image": os.getenv("REQUIRE_IMAGE", "1"),
        "require_stock": os.getenv("REQUIRE_STOCK", "0"),
        "require_sku": os.getenv("REQUIRE_SKU", "0"),
        "min_body_chars": int(os.getenv("MIN_BODY_CHARS", "0")),
    }

    probe = {"ok": True}
    try:
        locs = shop.list_locations()
        probe["locations"] = len(locs)
        # muestra 3 productos de forma liviana (id, título, status, #imagenes, #variantes)
        sample = shop._get(
            "/products.json",
            params={"limit": 3, "fields": "id,title,handle,status,images,variants"},
        ).get("products", [])
        probe["sample_products"] = [
            {
                "id": p.get("id"),
                "title": p.get("title"),
                "status": p.get("status"),
                "images": len(p.get("images", [])),
                "variants": len(p.get("variants", [])),
            }
            for p in sample
        ]
    except Exception as e:
        probe["ok"] = False
        probe["error"] = str(e)

    return {"ok": True, "env": env_info, "probe": probe}

@app.get("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
