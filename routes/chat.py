# routes/chat.py

from flask import Blueprint, request, jsonify
from utils.logger import log_event
from utils.parser import clean_text
from deepseek_client import ask_deepseek
from shopify_api import buscar_productos_shopify

chat_bp = Blueprint('chat', __name__)

@chat_bp.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.get_json(force=True)
        message = data.get("message", "").strip()

        if not message:
            return jsonify({"success": False, "error": "Mensaje vacío"}), 400

        # 🔍 Consulta primero a Shopify si la pregunta incluye palabras clave
        palabras_clave = ["agua", "sensor", "medidor", "consumo", "nivel"]
        if any(p in message.lower() for p in palabras_clave):
            productos = buscar_productos_shopify("agua")
            if productos:
                respuesta = "Encontré estos productos relacionados con agua:\n"
                for p in productos[:3]:
                    respuesta += f"- {p['title']} (${p['price']})\n{p['url']}\n\n"
                return jsonify({
                    "success": True,
                    "response": respuesta.strip(),
                    "meta": {
                        "domain": request.referrer or "desconocido",
                        "ip": request.remote_addr
                    }
                })

        # 🤖 Si no hay coincidencias, pregunta a DeepSeek
        respuesta_ai = ask_deepseek(message)

        return jsonify({
            "success": True,
            "response": respuesta_ai,
            "meta": {
                "domain": request.referrer or "desconocido",
                "ip": request.remote_addr
            }
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
