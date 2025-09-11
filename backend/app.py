# -*- coding: utf-8 -*-
import os
import threading
import traceback
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

# Carga .env antes de importar clientes (por si leen variables en import)
load_dotenv()

from .shopify_client import ShopifyClient
from .indexer import CatalogIndexer
from .deepseek_client import DeepseekClient
from .prompts import SYSTEM_PROMPT, USER_TEMPLATE
from .utils import money

BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "..", "widget")

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

# ---------- Servicios (con manejo de errores para no tumbar la app) ----------
shop = None
indexer = None
deeps = None
startup_errors = []

try:
    shop = ShopifyClient()
except Exception as e:
    startup_errors.append(f"ShopifyClient init error: {e}")
    print(f"[FATAL] ShopifyClient init error: {e}\n{traceback.format_exc()}", flush=True)

try:
    if shop:
        indexer = CatalogIndexer(shop, os.getenv("STORE_BASE_URL", "https://master.com.mx"))
except Exception as e:
    startup_errors.append(f"CatalogIndexer init error: {e}")
    print(f"[FATAL] CatalogIndexer init error: {e}\n{traceback.format_exc()}", flush=True)

try:
    deeps = DeepseekClient()
except Exception as e:
    startup_errors.append(f"DeepseekClient init error: {e}")
    print(f"[WARN] DeepseekClient init error: {e}\n{traceback.format_exc()}", flush=True)

# Construye índice al iniciar (no tumbar la app si falla)
if indexer:
    try:
        print("[BOOT] Building initial index...", flush=True)
        indexer.build()
        print("[BOOT] Initial index built", flush=True)
    except Exception as e:
        startup_errors.append(f"Index build failed at startup: {e}")
        print(f"[WARN] Index build failed at startup: {e}\n{traceback.format_exc()}", flush=True)
else:
    print("[BOOT] Skipping initial index build (indexer is None)", flush=True)

# -------------------- RUTAS PÚBLICAS --------------------

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
        '<code>GET /api/admin/products?limit=10</code>, '
        '<code>GET /api/admin/diag</code>'
        ".</p>"
    )

@app.get("/health")
def health():
    ready = bool(indexer)  # listo si al menos creó el indexer
    return {"ok": True, "ready": ready, "errors": startup_errors}

@app.post("/api/chat")
def chat():
    if not indexer:
        return jsonify({"answer": "lo siento, no dispongo de esa información", "products": []})

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
    answer = "lo siento, no dispongo de esa información"
    if deeps:
        try:
            answer = deeps.chat(SYSTEM_PROMPT, user_msg)
        except Exception as e:
            print(f"[WARN] Deepseek chat error: {e}\n{traceback.format_exc()}", flush=True)

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

# -------------------- ADMIN (requiere X-Admin-Secret) --------------------

def _admin_ok(req) -> bool:
    return req.headers.get("X-Admin-Secret") == os.getenv("ADMIN_REINDEX_SECRET", "")

def _do_reindex():
    if not indexer:
        print("[INDEX] Reindex requested but indexer is None", flush=True)
        return
    try:
        print("[INDEX] Reindex started", flush=True)
        indexer.build()
        print("[INDEX] Reindex finished", flush=True)
        print(f"[INDEX] Stats: {indexer.stats()}", flush=True)
    except Exception as e:
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
    if not indexer:
        return {"ok": True, "products": 0, "variants": 0, "inventory_levels": 0, "note": "indexer is None"}
    return {"ok": True, **indexer.stats()}

@app.get("/api/admin/search")
def admin_search():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if not indexer:
        return {"ok": True, "q": request.args.get("q", ""), "count": 0, "items": []}
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
    if not indexer:
        return {"ok": True, "by_reason": [], "sample": [], "note": "indexer is None"}
    return {"ok": True, **indexer.discard_stats()}

@app.get("/api/admin/products")
def admin_products():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if not indexer:
        return {"ok": True, "items": [], "note": "indexer is None"}
    limit = int(request.args.get("limit", 10))
    return {"ok": True, "items": indexer.sample_products(limit=limit)}

@app.get("/api/admin/diag")
def admin_diag():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    env = {
        "store_domain": os.getenv("SHOPIFY_STORE_DOMAIN"),
        "api_version": os.getenv("SHOPIFY_API_VERSION", "2024-10"),
        "store_base_url": os.getenv("STORE_BASE_URL", "https://master.com.mx"),
        "has_token": bool(os.getenv("SHOPIFY_TOKEN")),
        "has_deepseek": bool(os.getenv("DEEPSEEK_API_KEY")),
    }
    probe = {"ok": False, "error": "shopify client not ready"}
    if shop:
        try:
            probe = shop.probe()
        except Exception as e:
            probe = {"ok": False, "error": str(e)}
    return {"ok": True, "env": env, "probe": probe, "startup_errors": startup_errors}

# -------------------- ESTÁTICOS (widget) --------------------

@app.get("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)

# -------------------- MAIN --------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"[BOOT] Starting Flask on 0.0.0.0:{port}", flush=True)
    app.run(host="0.0.0.0", port=port)
