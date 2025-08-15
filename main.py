from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

# ---- Integraciones existentes ----
from integrations.shopify_api import (
    get_shopify_products,
    get_products,
    get_inventory_by_variant_id,
    get_product_details,
    extract_manual_url,
    get_shopify_context,
)

# API version (usa la de tu integración si existe)
try:
    from integrations.shopify_api import API_VERSION as SHOPIFY_API_VERSION
except Exception:
    SHOPIFY_API_VERSION = "2024-04"

# NLP
try:
    from utils.nlp_tools import extract_keywords_from_text
except Exception:
    from nlp_tools import extract_keywords_from_text

# CORS centralizado (si config.py existe)
try:
    from config import ALLOWED_ORIGINS
    _CORS_ORIGINS = ALLOWED_ORIGINS
except Exception:
    _CORS_ORIGINS = [
        "https://master.mx", "https://www.master.mx",
        "https://master.com.mx", "https://www.master.com.mx"
    ]

app = Flask(__name__)
CORS(app, origins=_CORS_ORIGINS, supports_credentials=True)

# ---------- Utilidades seguras de mapeo ----------

def _safe_first_variant(product: dict) -> dict:
    variants = product.get("variants") or []
    return variants[0] if isinstance(variants, list) and variants else {}

def _safe_image_src(product: dict) -> str:
    image_obj = product.get("image")
    if isinstance(image_obj, dict):
        return image_obj.get("src") or ""
    return ""

def _map_product_for_cards(p: dict, store_domain: str) -> dict:
    v = _safe_first_variant(p)
    return {
        "id": p.get("id"),
        "title": p.get("title") or "",
        "type": p.get("product_type") or "",
        "price": v.get("price", "N/A"),
        "image": _safe_image_src(p),
        "handle": (p.get("handle") or ""),
        "admin_link": f"https://{store_domain}/products/{p.get('handle') or ''}",
        "body_html": (p.get("body_html") or ""),
        "sku": (v.get("sku") or ""),
        "vendor": p.get("vendor") or "",
        "tags": p.get("tags") or "",
        "variant_id": v.get("id") or 0,
    }

# ---------- Fallback multi-estrategia a Shopify ----------

def _fetch_products_multi(origin: str, limit: int = 50):
    """
    Llama a Shopify probando varias combinaciones de filtros y devuelve:
    - products: lista mapeada
    - attempts: detalle de cada intento (url, status_code, count)
    """
    store, headers = get_shopify_context(origin=origin)

    candidate_urls = [
        f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json?limit={limit}&status=any",
        f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json?limit={limit}",
        f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json?limit={limit}&published_status=any",
    ]

    attempts = []
    for url in candidate_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            code = resp.status_code
            if code != 200:
                attempts.append({"url": url, "status": code, "count": 0, "note": "non-200"})
                continue
            payload = resp.json() or {}
            raw = payload.get("products") or []
            mapped = [_map_product_for_cards(p, store_domain=store) for p in raw]
            attempts.append({"url": url, "status": code, "count": len(mapped)})
            if mapped:
                return mapped, attempts
        except Exception as e:
            attempts.append({"url": url, "status": "exception", "count": 0, "error": str(e)})

    # Si ninguno dio resultados, devolvemos vacío con trazas
    return [], attempts

def _shopify_fallback_search(user_text: str, origin: str, limit: int = 100) -> tuple[list, list]:
    """
    Fallback literal: busca 'contains' con el texto del usuario.
    Ahora considera: title, body_html, sku, vendor, tags, product_type.
    Retorna (resultados, attempts) para depuración.
    """
    products, attempts = _fetch_products_multi(origin=origin, limit=limit)
    text = (user_text or "").strip().lower()
    if not products:
        return [], attempts

    results = []
    for p in products:
        if any([
            text in (p["title"].lower()),
            text in (p["body_html"].lower()),
            text in (p["sku"].lower()),
            text in (p["vendor"].lower()),
            text in (p["tags"].lower()),
            text in ((p["type"] or "").lower()),
        ]):
            results.append(p)
    return results, attempts

# ---------- Rutas ----------

@app.route("/")
def home():
    return "✅ Chatbot backend activo."

@app.route("/chat", methods=["POST", "OPTIONS"])
def chat():
    if request.method == "OPTIONS":
        return "", 200

    try:
        data = request.get_json(force=True)
        user_message = (data.get("message") or "").strip()
        origin = data.get("origin") or request.headers.get("Origin", "")

        if not user_message:
            return jsonify({"success": False, "error": "Mensaje vacío"}), 400

        # 1) Búsqueda original por keywords vía integración
        keywords = extract_keywords_from_text(user_message)
        print(f"[DEBUG] Keywords: {
