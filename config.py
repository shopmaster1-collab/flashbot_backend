# config.py

import os

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# Incluye apex, WWW y dominios de vista previa en myshopify para ambas tiendas
_default_origins = (
    "https://master.com.mx,"
    "https://www.master.com.mx,"
    "https://master.mx,"
    "https://www.master.mx,"
    "https://master-electronicos.myshopify.com,"
    "https://airb2bsafe-8329.myshopify.com"
)

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", _default_origins).split(",")
    if o.strip()
]
