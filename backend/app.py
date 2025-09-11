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
        '<code>POST /api/admin/reindex</code>.</p>'
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

@app.post("/api/admin/reindex")
def reindex():
    # Seguridad simple por header
    if request.headers.get("X-Admin-Secret") != os.getenv("ADMIN_REINDEX_SECRET", ""):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    # Ejecuta el rebuild en background para responder inmediato
    threading.Thread(target=indexer.build, daemon=True).start()
    return {"ok": True, "message": "reindex started"}

@app.get("/static/<path:fname>")
def static_files(fname):
    return send_from_directory(STATIC_DIR, fname)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
