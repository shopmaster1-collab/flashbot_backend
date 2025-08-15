import os
import re
import requests
from flask import request

# Tokens para cada tienda
SHOPIFY_TOKEN_MX = os.getenv("SHOPIFY_TOKEN")               # master.mx
SHOPIFY_TOKEN_COM_MX = os.getenv("SHOPIFY_TOKEN_MASTER")    # master.com.mx

# Dominios internos de Shopify (admin)
SHOPIFY_STORE_MX = "airb2bsafe-8329.myshopify.com"
SHOPIFY_STORE_COM_MX = "master-electronicos.myshopify.com"

API_VERSION = "2024-04"


def get_shopify_context(origin=None):
    """
    Detecta el dominio y retorna (store, headers) correctos según Origin.
    """
    if not origin:
        origin = request.headers.get("Origin", "")

    if "master.com.mx" in origin:
        token = SHOPIFY_TOKEN_COM_MX
        store = SHOPIFY_STORE_COM_MX
        print("[DEBUG] Context: master.com.mx → SHOPIFY_TOKEN_MASTER, master-electronicos")
    else:
        token = SHOPIFY_TOKEN_MX
        store = SHOPIFY_STORE_MX
        print("[DEBUG] Context: master.mx → SHOPIFY_TOKEN, airb2bsafe-8329")

    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return store, headers


def _safe_first_variant(product: dict) -> dict:
    """
    Devuelve el primer variant como dict o {} si no existe.
    """
    variants = product.get("variants") or []
    return variants[0] if isinstance(variants, list) and variants else {}


def _safe_image_src(product: dict) -> str:
    """
    Devuelve el .src de la imagen o "" si image es None o no es dict.
    """
    image_obj = product.get("image")
    if isinstance(image_obj, dict):
        return image_obj.get("src", "") or ""
    return ""


def _map_product(product: dict, store: str) -> dict:
    """
    Normaliza un product crudo de Shopify para uso en tarjetas/respuestas.
    """
    v = _safe_first_variant(product)
    return {
        "id": product.get("id"),
        "title": product.get("title") or "",
        "type": product.get("product_type") or "",
        "price": v.get("price", "N/A"),
        "image": _safe_image_src(product),
        "link": f"https://{store}/products/{(product.get('handle') or '')}",
        "body_html": (product.get("body_html") or ""),
        "sku": (v.get("sku") or ""),
        "vendor": product.get("vendor") or "",
        "tags": product.get("tags") or "",
        "variant_id": v.get("id") or 0,
    }


def _fetch_products_multi(store: str, headers: dict, limit: int = 50):
    """
    Intenta varias URLs y regresa la primera con resultados.
    (En tus pruebas, la URL SIN 'status=any' sí devolvió productos.)
    """
    candidate_urls = [
        f"https://{store}/admin/api/{API_VERSION}/products.json?limit={limit}",                     # ← esta te devuelve datos hoy
        f"https://{store}/admin/api/{API_VERSION}/products.json?limit={limit}&status=any",
        f"https://{store}/admin/api/{API_VERSION}/products.json?limit={limit}&published_status=any",
    ]
    for url in candidate_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code != 200:
                print(f"[WARN] {url} → {resp.status_code}")
                continue
            raw = (resp.json() or {}).get("products") or []
            if raw:
                print(f"[DEBUG] {url} → {len(raw)} productos")
                return raw
            else:
                print(f"[DEBUG] {url} → 0 productos")
        except Exception as e:
            print(f"[ERROR] fetch {url}: {e}")
    return []


def get_products(limit=10, origin=None):
    """
    Lista productos desde la tienda detectada.
    - Multi-estrategia de URL
    - Mapeo tolerante a nulls
    """
    store, headers = get_shopify_context(origin)
    try:
        raw = _fetch_products_multi(store, headers, limit=limit)
        return [_map_product(p, store) for p in raw]
    except Exception as e:
        print(f"[❌ Error en get_products()] {e}")
        return []


def get_product_by_title(keyword, origin=None):
    """
    Búsqueda contains (case-insensitive) en múltiples campos:
    title, body_html, sku, vendor, tags, product_type.
    """
    keyword = (keyword or "").lower()
    productos = get_products(limit=200, origin=origin)
    encontrados = []

    for p in productos:
        title = (p.get("title") or "").lower()
        body = (p.get("body_html") or "").lower()
        sku = (p.get("sku") or "").lower()
        vendor = (p.get("vendor") or "").lower()
        tags = (p.get("tags") or "").lower()
        ptype = (p.get("type") or "").lower()

        if any([
            keyword in title,
            keyword in body,
            keyword in sku,
            keyword in vendor,
            keyword in tags,
            keyword in ptype
        ]):
            encontrados.append({
                "id": p.get("id"),
                "title": p.get("title"),
                "type": p.get("type"),
                "price": p.get("price"),
                "image": p.get("image"),
                "link": p.get("link"),
                "body_html": p.get("body_html"),
                "variant_id": p.get("variant_id")
            })

    print(f"[DEBUG] Coincidencias para '{keyword}': {len(encontrados)}")
    return encontrados


def get_shopify_products(keyword="", origin=None):
    return get_product_by_title(keyword, origin) if keyword else get_products(origin=origin)


def get_product_details(product_id, origin=None):
    store, headers = get_shopify_context(origin)
    try:
        url = f"https://{store}/admin/api/{API_VERSION}/products/{product_id}.json"
        print(f"[DEBUG] GET detalles producto {product_id} → {url}")
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        return (response.json() or {}).get("product", {}) or {}
    except Exception as e:
        print(f"[❌ Error en get_product_details({product_id})] {e}")
        raise e


def get_inventory_by_variant_id(variant_id, origin=None):
    """
    Devuelve lista de {sucursal, cantidad} por variant_id.
    (Corregido: todas las llaves/paren cerradas.)
    """
    store, headers = get_shopify_context(origin)
    try:
        print(f"[DEBUG] Inventario para variant_id={variant_id} en {store}")

        # 1) Obtener inventory_item_id del variant
        variant_url = f"https://{store}/admin/api/{API_VERSION}/variants/{variant_id}.json"
        variant_res = requests.get(variant_url, headers=headers, timeout=20)
        variant_res.raise_for_status()
        inventory_item_id = (variant_res.json() or {}).get("variant", {}).get("inventory_item_id")

        # 2) Obtener niveles de inventario por locations
        levels_url = f"https://{store}/admin/api/{API_VERSION}/inventory_levels.json?inventory_item_ids={inventory_item_id}"
        levels_res = requests.get(levels_url, headers=headers, timeout=20)
        levels_res.raise_for_status()
        inventory_levels = (levels_res.json() or {}).get("inventory_levels", []) or []

        # 3) Mapear locations
        locs_url = f"https://{store}/admin/api/{API_VERSION}/locations.json"
        locs_res = requests.get(locs_url, headers=headers, timeout=20)
        locs_res.raise_for_status()
        locations = {loc["id"]: loc["name"] for loc in (locs_res.json() or {}).get("locations", [])}

        resultado = []
        for lvl in inventory_levels:
            loc_id = lvl.get("location_id")
            resultado.append({
                "sucursal": locations.get(loc_id, f"Loc {loc_id}"),
                "cantidad": lvl.get("available", 0)
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
