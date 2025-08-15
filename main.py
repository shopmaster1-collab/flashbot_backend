from flask import Flask, request, jsonify
from flask_cors import CORS
from integrations.shopify_api import (
    get_shopify_products,
    get_products,
    get_inventory_by_variant_id,
    get_product_details,
    extract_manual_url,
    get_shopify_context
)
from utils.nlp_tools import extract_keywords_from_text
from config import ALLOWED_ORIGINS  # ✅ usar lista centralizada de orígenes
import requests

app = Flask(__name__)
CORS(app, origins=ALLOWED_ORIGINS, supports_credentials=True)  # ✅ CORS unificado

@app.route('/')
def home():
    return "✅ Chatbot backend activo."

@app.route('/chat', methods=['POST', 'OPTIONS'])
def chat():
    if request.method == 'OPTIONS':
        return '', 200

    try:
        data = request.get_json(force=True)
        user_message = (data.get("message") or "").strip()
        origin = data.get("origin") or request.headers.get("Origin", "")

        if not user_message:
            return jsonify({"success": False, "error": "Mensaje vacío"}), 400

        keywords = extract_keywords_from_text(user_message)
        print(f"[KEYWORDS EXTRAÍDAS] {keywords}")  # 👈 Debug añadido aquí

        productos_encontrados = []
        for kw in keywords:
            encontrados = get_shopify_products(kw, origin=origin)
            productos_encontrados.extend(encontrados)

        productos_unicos = {p["title"]: p for p in productos_encontrados}.values()

        if not productos_unicos:
            bot_response = "No encontré resultados para esa búsqueda. ¿Quieres intentar con otra palabra clave?"
        else:
            domain_for_links = "master.mx"
            if "master.com.mx" in origin:
                domain_for_links = "master.com.mx"

            cards = []

            for p in list(productos_unicos)[:5]:
                try:
                    title = p.get("title")
                    price = p.get("price", "N/A")
                    image = p.get("image", "")
                    handle = p.get("link", "#").split("/products/")[-1]
                    url = f"https://{domain_for_links}/products/{handle}"
                    variant_id = p.get("variant_id", 0)
                    product_id = p.get("id")
                    checkout_url = f"https://{domain_for_links}/cart/{variant_id}:1"

                    manual_url = None
                    try:
                        if product_id:
                            details = get_product_details(product_id, origin=origin)
                            manual_url = extract_manual_url(details.get("body_html", ""))
                    except:
                        pass

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

                    try:
                        inventario = get_inventory_by_variant_id(variant_id, origin=origin)
                        if inventario:
                            lista = "".join([f"<li>{i['sucursal']}: {i['cantidad']} disponibles</li>" for i in inventario])
                            inventario_html = f"<div style='font-size:12px;color:#007bff;margin-top:6px;'><strong>📦 Inventario:</strong><ul style='margin:4px 0 0 18px;padding:0;'>{lista}</ul></div>"
                            card_html += inventario_html
                    except:
                        pass

                    card_html += "</div></div>"
                    cards.append(card_html)

                except:
                    continue

            if cards:
                bot_response = "<div>🔍 Estos productos podrían interesarte:</div>" + "".join(cards)
            else:
                bot_response = "Hubo un error interno al procesar los productos. Intenta con otra búsqueda."

        return jsonify({
            "success": True,
            "response": bot_response,
            "meta": {
                "domain": origin or "desconocido",
                "ip": request.remote_addr
            }
        })

    except Exception as e:
        print(f"[ERROR EN /chat] {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/debug', methods=['GET'])
def debug_products():
    try:
        origin = request.headers.get("Origin", "")
        productos = get_products(limit=5, origin=origin)
        return jsonify({
            "success": True,
            "productos": productos
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/inventario/<int:variant_id>', methods=['GET'])
def ver_inventario(variant_id):
    try:
        origin = request.headers.get("Origin", "")
        inventario = get_inventory_by_variant_id(variant_id, origin=origin)
        return jsonify({
            "success": True,
            "variant_id": variant_id,
            "inventario": inventario
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/debug/shopify-context', methods=['GET'])
def debug_shopify_context():
    try:
        origin = request.headers.get("Origin", "")
        store, headers = get_shopify_context(origin=origin)
        url = f"https://{store}/admin/api/2024-04/products.json?limit=1"
        response = requests.get(url, headers=headers)
        result = response.json() if response.status_code == 200 else {
            "status": response.status_code,
            "error": response.text
        }

        return jsonify({
            "success": True,
            "origin": origin or "no origin",
            "shopify_store": store,
            "token_used": "SHOPIFY_TOKEN_MASTER" if "master.com.mx" in origin else "SHOPIFY_TOKEN",
            "url": url,
            "response": result
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

if __name__ == '__main__':
    app.run(debug=True)
