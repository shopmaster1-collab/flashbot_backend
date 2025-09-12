# -*- coding: utf-8 -*-
import os
import threading
import traceback
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

# --- Servicios / inicialización segura ---
shop = None
try:
    shop = ShopifyClient()  # ahora acepta SHOPIFY_TOKEN o SHOPIFY_ACCESS_TOKEN; SHOPIFY_SHOP o SHOPIFY_STORE_DOMAIN
except Exception as e:
    # No tumbar la app si falta config; Indexer usará fallback REST si está disponible
    print(f"[WARN] ShopifyClient init failed: {e}", flush=True)

indexer = CatalogIndexer(shop, os.getenv("STORE_BASE_URL", "https://master.com.mx"))
deeps = DeepseekClient()

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

@app.post("/api/chat")
def chat():
    data = request.get_json(force=True)
    query = (data.get("message") or "").strip()
    if not query:
        return jsonify({"answer": "lo siento, no dispongo de esa información", "products": []})

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

    # Tarjetas
    cards = []
    for it in items:
        v = it["variant"]
        cards.append({
            "title": it["title"],
            "image": it["image"],
            "price": money(v["price"]),
            "compare_at_price": money(v["compare_at_price"]) if v.get("compare_at_price") else None,
            "buy_url": it["buy_url"],
            "product_url": it["product_url"],
            "inventory": v.get("inventory") or [],
        })

    return jsonify({"answer": answer, "products": cards})

# ---- Reindex en background ----
def _do_reindex():
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
            "sku": it["variant"].get("sku"),
            "price": it["variant"]["price"],
            "stock": sum(x["available"] for x in it["variant"].get("inventory") or []),
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
    try:
        # No crear clientes nuevos aquí; solo inspeccionar entorno y hacer sondas ligeras
        shop_domain = (os.getenv("SHOPIFY_STORE_DOMAIN") or os.getenv("SHOPIFY_SHOP") or "").strip()
        token = (
            os.getenv("SHOPIFY_ACCESS_TOKEN")
            or os.getenv("SHOPIFY_TOKEN")
            or os.getenv("SHOPIFY_ACCES_TOKEN")
            or ""
        ).strip()
        api_version = os.getenv("SHOPIFY_API_VERSION", "2024-10")
        sqlite_path = (os.getenv("SQLITE_PATH") or os.path.join(BASE_DIR, "data", "catalog.sqlite3")).strip()
        force_rest = os.getenv("FORCE_REST", "0") == "1"

        env = {
            "shop": shop_domain,
            "api_version": api_version,
            "sqlite_path": sqlite_path,
            "token_present": bool(token),
            "require_active": os.getenv("REQUIRE_ACTIVE", "0"),
            "force_rest": force_rest,
        }

        probe = {"ok": True}
        # prueba muy ligera; si no hay cliente, devuelve 0 sin romper
        try:
            probe["locations"] = len(shop.list_locations()) if shop else 0
        except Exception:
            probe["locations"] = 0
            probe["ok"] = False

        probe["sample_products"] = indexer.sample_products(limit=3)

        return {"ok": True, "env": env, "probe": probe}
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "trace": traceback.format_exc(),
        }, 200  # nunca devolver 500 en diag

@app.get("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
