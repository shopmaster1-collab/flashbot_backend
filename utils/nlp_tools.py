# chatbot_backend/integrations/nlp_tools.py

import re
from flask import request

def extract_keywords_from_text(text):
    """
    Extrae palabras clave relevantes desde una frase conversacional.
    Clasifica la intención en subcategorías útiles para búsquedas en Shopify,
    adaptadas según el dominio (master.mx vs master.com.mx).
    """
    text_lower = text.lower()
    text_clean = re.sub(r"[^\w\s]", "", text_lower)

    # Categorías base
    subcategorias = {
        "agua": ["agua", "tinaco", "cisterna", "sumergible", "nivel"],
        "gas": ["gas", "cilindro", "tanque", "fuga", "lp", "butano", "propano"],
        "energia": ["energía", "eléctrico", "voltaje", "amperaje", "batería", "cargador", "pila", "led"],
        "humo": ["humo", "co2", "monóxido", "incendio", "sensor humo", "detector"],
        "iot": ["iot", "app", "wifi", "bluetooth", "smart", "inteligente"],
    }

    # Ajustes para master.com.mx según catálogo detectado :contentReference[oaicite:1]{index=1}
    origin = request.headers.get("Origin", "")
    if "master.com.mx" in origin:
        subcategorias.update({
            "hdmi": ["hdmi", "alta definición", "4k", "2k"],
            "cable": ["cable", "hdmi", "coaxial", "usb"],
            "video": ["video", "tv", "proyector", "decoder", "decodificador", "cctv", "cámara", "dvr"],
            "audio": ["audio", "bocina", "micrófono", "bafle", "audífono"],
            "control remoto": ["control remoto"],
            "soporte": ["soporte", "trípode", "soportes"],
            "herramienta": ["herramienta", "cautín", "soldar", "cargador", "batería"],
            "seguridad": ["seguridad", "cctv", "alarma"],
        })

    detected = set()
    for clave, sinonimos in subcategorias.items():
        for palabra in sinonimos:
            if palabra in text_clean:
                detected.add(clave)

    return list(detected) if detected else ["sensor"]
