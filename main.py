from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

# Imports de tu integración actual
from integrations.shopify_api import (
    get_shopify_products,
    get_products,
    get_inventory_by_variant_id,
    get_product_details,
    extract_manual_url,
    get_shopify_context,
)
# Intentamos tomar la versión de API desde tu módulo; si no, usamos un default seguro.
try:
    from integrations.shopify_api import API_VERSION as SHOPIFY_API_VERSION
except Exception:
    SHOPIFY_API_VERSION = "2024-04"

# NLP
from utils.nlp_tools import extract_keywords_from_text

# (Opcional) CORS centralizado desde config.py
try:
    from config import ALLOWED_ORIGINS
    _CORS_ORIGINS = ALLOWED_ORIGINS
except Exception:
    # Fallback si no existe config.py con ALLOWED_ORIGINS
    _CORS_ORIGINS = ["https://master.mx", "https://www.master.mx", "https://master.com.mx", "https://www.master.com.mx"]

app = Flask(__name__)
CORS(app, origins=_CORS_ORIGINS, supports_credentials=True)


def _safe_first_variant(product: dict) -> dict:
    variants = product.get("variants") or []
    return variants[0] if isinstance(variants, list) and variants else {}


def _safe_image_src(product: dict) -> str:
    image_obj = product.get("image")
    if isinstance(image_obj, dict):
        return image_obj.get("src") or ""
    return ""


def _map_product_for_cards(p: dict, store_domain: str) -> dict:
    """Normaliza un producto crudo de Shopify para las tarjetas."""
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
        "variant_id": v.get("id") or 0,
    }


def _shopify_fallback_search(user_text: str, origin: str, limit: int = 50) -> list:
    """
    Fallback directo a Shopify si get_shopify_products() devuelve vacío.
    Busca 'contains' en title/body_html/sku con el texto literal del usuario.
    """
    store, headers = get_shopify_context(origin=origin)
    url = f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json?limit={limit}&status=any"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        products_raw = (resp.json() or {}).get("products", []) or []
    except Exception as e:
        print(f"[❌ Fallback Shopify fetch error] {e}")
        return []

    text = (user_text or "").strip().lower()
    results = []
    for p in products_raw:
        mp = _map_product_for_cards(p, store_domain=store)
        hay = (
            text in (mp["title"].lower())
            or text in (mp["body_html"].lower())
            or text in (mp["sku"].lower())
        )
        if hay:
            results.append(mp)
    return results


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

        # 1) Búsqueda original (por keywords)
        keywords = extract_keywords_from_text(user_message)
        print(f"[DEBUG] Keywords: {keywords} | Origin: {origin}")

        encontrados = []
        for kw in keywords:
            try:
                encontrados.extend(get_shopify_products(kw, origin=origin))
            except Exception as e:
                print(f"[WARN] get_shopify_products('{kw}') falló: {e}")

        # 2) Normaliza estructura si viene del integrador
        productos = []
        for p in encontrados:
            # Soporta formato existente de integrations.shopify_api (title, image, link, variant_id, id, body_html)
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
                "sku": "",
                "variant_id": p.get("variant_id", 0),
            })

        # 3) Fallback literal si no hubo resultados
        if not productos:
            print("[DEBUG] Sin resultados por keywords; usando fallback literal directo a Shopify...")
            productos = _shopify_fallback_search(user_message, origin=origin, limit=100)

        # Si sigue vacío, responde amable
        if not productos:
            return jsonify({
                "success": True,
                "response": "No encontré resultados para esa búsqueda. ¿Quieres intentar con otra palabra clave?",
                "meta": {"domain": origin or "desconocido", "ip": request.remote_addr}
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
            admin_link = p.get("admin_link") or ""
            variant_id = p.get("variant_id", 0)
            product_id = p.get("id")
            url = f"https://{domain_for_links}/products/{handle}" if handle else f"https://{domain_for_links}"
            checkout_url = f"https://{domain_for_links}/cart/{variant_id}:1" if variant_id else f"https://{domain_for_links}/cart"

            # Manual de producto
            manual_url = None
            try:
                # Si ya tenemos body_html en el resultado del fallback, úsalo; si no, consulta detalles
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

            # Inventario por sucursal (si hay variant_id)
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
            "meta": {"domain": origin or "desconocido", "ip": request.remote_addr}
        })

    except Exception as e:
        print(f"[ERROR /chat] {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/debug", methods=["GET"])
def debug_products():
    """
    Mantengo tu endpoint original: usa la integración interna.
    Si tu lista interna falla, verás [] aquí (lo cual explica por qué /chat estaba vacío).
    """
    try:
        origin = request.headers.get("Origin", "")
        productos = get_products(limit=5, origin=origin)
        return jsonify({"success": True, "productos": productos})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/debug/raw", methods=["GET"])
def debug_products_raw():
    """
    Nuevo endpoint de diagnóstico: llama DIRECTO a Shopify para listar 5 productos,
    ignorando la función interna que hoy te devuelve [].
    """
    try:
        origin = request.headers.get("Origin", "")
        store, headers = get_shopify_context(origin=origin)
        url = f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json?limit=5&status=any"
        r = requests.get(url, headers=headers, timeout=15)
        data = r.json() if r.status_code == 200 else {"status": r.status_code, "error": r.text}
        # Mapeo seguro mínimo para inspección rápida
        mapped = [_map_product_for_cards(p, store) for p in (data.get("products") or [])]
        return jsonify({"success": True, "origin": origin or "no origin", "shopify_store": store, "url": url, "count": len(mapped), "productos": mapped})
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
        url = f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json?limit=1&status=any"
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


