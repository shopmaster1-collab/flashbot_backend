# deepseek_client.py
import requests
from config import DEEPSEEK_API_KEY

def ask_deepseek(user_question):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "Responde solo con información basada en los productos de la tienda. Sé conciso."},
            {"role": "user", "content": user_question}
        ],
        "temperature": 0.3
    }

    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 200:
        return response.json()['choices'][0]['message']['content']
    else:
        return "Lo siento, hubo un error al conectar con el asistente. Intenta más tarde."
