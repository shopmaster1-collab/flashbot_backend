# -*- coding: utf-8 -*-
import os
import requests

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

class DeepseekClient:
    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY no configurada")

    def chat(self, system: str, user: str, temperature: float = 0.2) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "stream": False,
        }
        r = requests.post(DEEPSEEK_API_URL, json=payload, headers=headers, timeout=40)
        r.raise_for_status()
        data = r.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            return "lo siento, no dispongo de esa información"
