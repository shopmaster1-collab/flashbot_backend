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
            text in ((p["handle"] or "").lower()),  # ← NUEVO
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
        print(f"[DEBUG] Keywords: {keywords} | Origin: {origin}")

        encontrados = []
        for kw in keywords:
            try:
                encontrados.extend(get_shopify_products(kw, origin=origin))
            except Exception as e:
                print(f"[WARN] get_shopify_products('{kw}') falló: {e}")

        # Normaliza estructura si viene del integrador
        productos = []
        for p in encontrados:
            handle = ""
            if p.get("link"):
                handle = p["link"].split("/products/")[-1]
            productos.append({
                "id": p.get("id"),
                "title": p.get("title") or "",
                "type": p.get("type") or "",
                "price": p.get("price", "N/A"),
                "image": p.get("image", ""),
                "handle": handle,
                "admin_link": p.get("link", ""),
                "body_html": (p.get("body_html") or ""),
                "sku": p.get("sku", ""),
                "variant_id": p.get("variant_id", 0),
            })

        # 2) Fallback literal directo a Shopify si no hubo resultados
        attempts = []
        if not productos:
            print("[DEBUG] Sin resultados por keywords; usando fallback literal multi-estrategia (campos extendidos)...")
            productos, attempts = _shopify_fallback_search(user_message, origin=origin, limit=100)

        if not productos:
            # Devolvemos también hint de diagnóstico (cuántos intentos y con qué URLs)
            return jsonify({
                "success": True,
                "response": "No encontré resultados para esa búsqueda. ¿Quieres intentar con otra palabra clave?",
                "meta": {
                    "domain": origin or "desconocido",
                    "ip": request.remote_addr,
                    "diagnostic": attempts
                }
            })

        # Evita duplicados por título
        vistos = set()
        unicos = []
        for p in productos:
            t = p.get("title") or ""
            if t not in vistos:
                vistos.add(t)
                unicos.append(p)

        # Dominio público para links visibles
        domain_for_links = "master.mx"
        if "master.com.mx" in (origin or ""):
            domain_for_links = "master.com.mx"

        # Render tarjetas
        cards = []
        for p in unicos[:5]:
            title = p["title"]
            price = p.get("price", "N/A")
            image = p.get("image", "")
            handle = p.get("handle") or ""
            variant_id = p.get("variant_id", 0)
            product_id = p.get("id")

            url = f"https://{domain_for_links}/products/{handle}" if handle else f"https://{domain_for_links}"
            checkout_url = f"https://{domain_for_links}/cart/{variant_id}:1" if variant_id else f"https://{domain_for_links}/cart"

            # Manual de producto
            manual_url = None
            try:
                body_html = p.get("body_html") or ""
                if not body_html and product_id:
                    details = get_product_details(product_id, origin=origin)
                    body_html = (details.get("body_html") or "")
                manual_url = extract_manual_url(body_html)
            except Exception as e:
                print(f"[WARN] extract_manual_url falló: {e}")

            card_html = f"""
            <div style="display:flex;align-items:center;margin-bottom:12px;border-bottom:1px solid #ddd;padding-bottom:8px">
              <img src="{image}" alt="{title}" style="width:60px;height:60px;object-fit:cover;margin-right:12px;border-radius:8px">
              <div style="flex:1;">
                <div style="font-weight:bold">{title}</div>
                <div style="display:flex;align-items:center;gap:10px;margin:4px 0;">
                  <div style="color:#007bff;font-weight:bold">${price}</div>
                  <a href="{checkout_url}" target="_blank" style="background:#198754;color:#fff;padding:4px 10px;border-radius:6px;text-decoration:none;font-size:12px;">🛒 Comprar ahora</a>
                </div>
                <div style="display:flex;align-items:center;gap:10px;margin-top:4px;">
                  <a href="{url}" target="_blank" style="color:#007bff;text-decoration:underline;font-size:13px;">Ver producto</a>
                  {f'<a href="{manual_url}" target="_blank" style="color:#6c757d;text-decoration:underline;font-size:12px;">Manual de producto</a>' if manual_url else ''}
                </div>
            """
            # Inventario por sucursal
            try:
                if variant_id:
                    inventario = get_inventory_by_variant_id(variant_id, origin=origin)
                    if inventario:
                        lista = "".join([f"<li>{i['sucursal']}: {i['cantidad']} disponibles</li>" for i in inventario])
                        card_html += f"<div style='font-size:12px;color:#007bff;margin-top:6px;'><strong>📦 Inventario:</strong><ul style='margin:4px 0 0 18px;padding:0;'>{lista}</ul></div>"
            except Exception as e:
                print(f"[WARN] inventario falló: {e}")

            card_html += "</div></div>"
            cards.append(card_html)

        html = "<div>🔍 Estos productos podrían interesarte:</div>" + "".join(cards)

        return jsonify({
            "success": True,
            "response": html,
            "meta": {"domain": origin or "desconocido", "ip": request.remote_addr, "diagnostic": attempts}
        })

    except Exception as e:
        print(f"[ERROR /chat] {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/debug", methods=["GET"])
def debug_products():
    try:
        origin = request.headers.get("Origin", "")
        productos = get_products(limit=5, origin=origin)
        return jsonify({"success": True, "productos": productos})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/debug/raw", methods=["GET"])
def debug_products_raw():
    """
    Diagnóstico crudo: prueba varias URLs y te dice cuántos productos trajo cada una.
    """
    try:
        origin = request.headers.get("Origin", "")
        products, attempts = _fetch_products_multi(origin=origin, limit=5)
        store, _ = get_shopify_context(origin=origin)
        return jsonify({
            "success": True,
            "origin": origin or "no origin",
            "shopify_store": store,
            "count": len(products),
            "attempts": attempts,
            "productos": products
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/inventario/<int:variant_id>", methods=["GET"])
def ver_inventario(variant_id):
    try:
        origin = request.headers.get("Origin", "")
        inventario = get_inventory_by_variant_id(variant_id, origin=origin)
        return jsonify({"success": True, "variant_id": variant_id, "inventario": inventario})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/debug/shopify-context", methods=["GET"])
def debug_shopify_context():
    try:
        origin = request.headers.get("Origin", "")
        store, headers = get_shopify_context(origin=origin)
        url = f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json?limit=1"
        response = requests.get(url, headers=headers, timeout=15)
        result = response.json() if response.status_code == 200 else {"status": response.status_code, "error": response.text}
        return jsonify({
            "success": True,
            "origin": origin or "no origin",
            "shopify_store": store,
            "token_used": "SHOPIFY_TOKEN_MASTER" if "master.com.mx" in (origin or "") else "SHOPIFY_TOKEN",
            "url": url,
            "response": result
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)


