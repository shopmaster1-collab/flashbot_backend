# chatbot_backend/integrations/shopify_api.py

import requests
import os
import re
from flask import request

# Tokens para cada tienda
SHOPIFY_TOKEN_MX = os.getenv("SHOPIFY_TOKEN")               # master.mx
SHOPIFY_TOKEN_COM_MX = os.getenv("SHOPIFY_TOKEN_MASTER")    # master.com.mx

# Dominios internos de Shopify
SHOPIFY_STORE_MX = "airb2bsafe-8329.myshopify.com"
SHOPIFY_STORE_COM_MX = "master-electronicos.myshopify.com"

API_VERSION = "2024-04"

def get_shopify_context(origin=None):
    """
    Detecta el dominio y retorna (store, headers)
    """
    if not origin:
        origin = request.headers.get("Origin", "")

    print(f"[DEBUG] Origin detectado: {origin}")

    if "master.com.mx" in origin:
        token = SHOPIFY_TOKEN_COM_MX
        store = SHOPIFY_STORE_COM_MX
        print("[DEBUG] Usando SHOPIFY_TOKEN_MASTER y tienda master-electronicos")
    else:
        token = SHOPIFY_TOKEN_MX
        store = SHOPIFY_STORE_MX
        print("[DEBUG] Usando SHOPIFY_TOKEN (default) y tienda airb2bsafe")

    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token
    }

    return store, headers

def get_products(limit=10, origin=None):
    store, headers = get_shopify_context(origin)
    url = f"https://{store}/admin/api/{API_VERSION}/products.json?limit={limit}"
    try:
        print(f"[DEBUG] GET productos desde {url}")
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        productos_raw = response.json().get("products", [])

        productos = []
        for p in productos_raw:
            productos.append({
                "id": p.get("id"),
                "title": p.get("title", ""),
                "type": p.get("product_type", ""),
                "price": p.get("variants", [{}])[0].get("price", "N/A"),
                "image": p.get("image", {}).get("src", ""),
                "link": f"https://{store}/products/{p.get('handle', '')}",
                "body_html": p.get("body_html", "").lower(),
                "sku": p.get("variants", [{}])[0].get("sku", "").lower(),
                "variant_id": p.get("variants", [{}])[0].get("id")
            })
        return productos

    except Exception as e:
        print(f"[❌ Error en get_products()] {e}")
        return []

def get_product_by_title(keyword, origin=None):
    keyword = keyword.lower()
    productos = get_products(limit=100, origin=origin)
    encontrados = []

    for p in productos:
        if (
            keyword in p["title"].lower()
            or keyword in p["body_html"]
            or keyword in p["sku"]
        ):
            encontrados.append({
                "id": p["id"],
                "title": p["title"],
                "type": p["type"],
                "price": p["price"],
                "image": p["image"],
                "link": p["link"],
                "variant_id": p["variant_id"]
            })

    return encontrados

def get_shopify_products(keyword="", origin=None):
    return get_product_by_title(keyword, origin) if keyword else get_products(origin=origin)

def get_product_details(product_id, origin=None):
    store, headers = get_shopify_context(origin)
    try:
        url = f"https://{store}/admin/api/{API_VERSION}/products/{product_id}.json"
        print(f"[DEBUG] GET detalles del producto {product_id} desde {url}")
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json().get("product", {})
    except Exception as e:
        print(f"[❌ Error en get_product_details({product_id})] {e}")
        raise e

def get_inventory_by_variant_id(variant_id, origin=None):
    store, headers = get_shopify_context(origin)
    try:
        print(f"[DEBUG] Obteniendo inventario para variant_id: {variant_id} en tienda {store}")

        variant_url = f"https://{store}/admin/api/{API_VERSION}/variants/{variant_id}.json"
        variant_res = requests.get(variant_url, headers=headers)
        variant_res.raise_for_status()
        inventory_item_id = variant_res.json()["variant"]["inventory_item_id"]

        levels_url = f"https://{store}/admin/api/{API_VERSION}/inventory_levels.json?inventory_item_ids={inventory_item_id}"
        levels_res = requests.get(levels_url, headers=headers)
        levels_res.raise_for_status()
        inventory_levels = levels_res.json().get("inventory_levels", [])

        locs_res = requests.get(f"https://{store}/admin/api/{API_VERSION}/locations.json", headers=headers)
        locs_res.raise_for_status()
        locations = {loc["id"]: loc["name"] for loc in locs_res.json().get("locations", [])}

        resultado = []
        for level in inventory_levels:
            loc_id = level.get("location_id")
            resultado.append({
                "sucursal": locations.get(loc_id, "Desconocida"),
                "cantidad": level.get("available", 0)
            })

        return resultado

    except Exception as e:
        print(f"[❌ Error en get_inventory_by_variant_id({variant_id})] {e}")
        raise e

def extract_manual_url(description):
    """
    Extrae el primer link .pdf desde la descripción del producto (body_html).
    """
    if not description:
        return None
    match = re.search(r'(https?://[^\s"\']+\.pdf)', description)
    return match.group(1) if match else None
