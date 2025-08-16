import os
import re
import requests
from flask import request
from typing import List, Dict, Any, Optional, Iterable
import math

# Tokens para cada tienda
SHOPIFY_TOKEN_MX = os.getenv("SHOPIFY_TOKEN")               # master.mx
SHOPIFY_TOKEN_COM_MX = os.getenv("SHOPIFY_TOKEN_MASTER")    # master.com.mx

# Dominios internos de Shopify (admin)
SHOPIFY_STORE_MX = "airb2bsafe-8329.myshopify.com"
SHOPIFY_STORE_COM_MX = "master-electronicos.myshopify.com"

API_VERSION = "2024-04"


# ============================
#   Contexto Shopify
# ============================

def get_shopify_context(origin=None):
    """
    Detecta el dominio y retorna (store, headers) correctos según Origin.
    """
    if not origin:
        origin = request.headers.get("Origin", "")

    if "master.com.mx" in (origin or ""):
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


# ============================
#   Normalización / Utilidades
# ============================

_ACCENTS = str.maketrans("áéíóúüÁÉÍÓÚÜñÑ", "aeiouuAEIOUUnN")

def _norm(s: str) -> str:
    return (s or "").translate(_ACCENTS).lower().strip()

def _tokenize(s: str) -> set:
    s = _norm(s)
    toks = re.findall(r"[a-z0-9]+", s)
    return set(t for t in toks if len(t) > 1)

def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / math.sqrt(len(a) * len(b))

def _choose_image_src(product: dict) -> str:
    image_obj = product.get("image")
    if isinstance(image_obj, dict):
        src = image_obj.get("src") or image_obj.get("url") or ""
        if src:
            return src
    images = product.get("images") or []
    if isinstance(images, list) and images:
        for img in images:
            if isinstance(img, dict):
                src = img.get("src") or img.get("url") or ""
                if src:
                    return src
    return ""

def _has_photo(product: dict) -> bool:
    return bool(_choose_image_src(product))

def _safe_first_variant(product: dict) -> dict:
    variants = product.get("variants") or []
    return variants[0] if isinstance(variants, list) and variants else {}

def _map_product(product: dict, store: str) -> dict:
    v = _safe_first_variant(product)
    return {
        "id": product.get("id"),
        "title": product.get("title") or "",
        "type": product.get("product_type") or "",
        "price": v.get("price", "N/A"),
        "image": _choose_image_src(product),
        "link": f"https://{store}/products/{(product.get('handle') or '')}",
        "body_html": (product.get("body_html") or ""),
        "sku": (v.get("sku") or ""),
        "vendor": product.get("vendor") or "",
        "tags": product.get("tags") or "",
        "variant_id": v.get("id") or 0,
        "handle": product.get("handle") or "",
        # Señal muy básica de stock
        "_in_stock": not re.search(r"\b(agotado|sin\s*stock|out\s*of\s*stock)\b",
                                   _norm((product.get("tags") or "") + " " + (product.get("title") or "")) or "")
    }


# ============================
#   Fetch multi-estrategia
# ============================

def _fetch_products_multi(store: str, headers: dict, limit: int = 50):
    candidate_urls = [
        f"https://{store}/admin/api/{API_VERSION}/products.json?limit={limit}",
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


# ============================
#   API básica (retrocompatible)
# ============================

def get_products(limit=10, origin=None, require_photo=False):
    store, headers = get_shopify_context(origin)
    try:
        raw = _fetch_products_multi(store, headers, limit=limit)
        if require_photo:
            raw = [p for p in raw if _has_photo(p)]
        return [_map_product(p, store) for p in raw]
    except Exception as e:
        print(f"[❌ Error en get_products()] {e}")
        return []

def get_product_by_title(keyword, origin=None, require_photo=False):
    keyword = (keyword or "").lower()
    productos = get_products(limit=200, origin=origin, require_photo=require_photo)
    encontrados = []

    for p in productos:
        title = (p.get("title") or "").lower()
        body = (p.get("body_html") or "").lower()
        sku = (p.get("sku") or "").lower()
        vendor = (p.get("vendor") or "").lower()
        tags = (p.get("tags") or "").lower()
        ptype = (p.get("type") or "").lower()
        handle = (p.get("handle") or "").lower()

        if any([
            keyword in title,
            keyword in body,
            keyword in sku,
            keyword in vendor,
            keyword in tags,
            keyword in ptype,
            keyword in handle
        ]):
            encontrados.append({
                "id": p.get("id"),
                "title": p.get("title"),
                "type": p.get("type"),
                "price": p.get("price"),
                "image": p.get("image"),
                "link": p.get("link"),
                "body_html": p.get("body_html"),
                "variant_id": p.get("variant_id"),
                "sku": p.get("sku"),
                "vendor": p.get("vendor"),
                "tags": p.get("tags"),
                "handle": p.get("handle"),
            })

    print(f"[DEBUG] Coincidencias para '{keyword}' (require_photo={require_photo}): {len(encontrados)}")
    return encontrados


# ============================
#   Búsqueda avanzada (filtros + scoring)
# ============================

def _has_any(haystack: str, needles: Iterable[str]) -> bool:
    h = _norm(haystack)
    for n in needles or []:
        if _norm(n) in h:
            return True
    return False

def _looks_like_support(p: Dict[str, Any]) -> bool:
    t = _norm(" ".join([
        p.get("title",""),
        p.get("type",""),
        p.get("vendor",""),
        p.get("tags","") if isinstance(p.get("tags"), str) else " ".join(p.get("tags") or [])
    ]))
    keys = {"soporte","bracket","tv","vesa","pared","techo","articulado","inclinable","esquinero"}
    bad  = {"antena","adaptador","cable","hdmi","displayport","vga","decodificador","sintonizador","conversor"}
    if any(b in t for b in bad):
        return False
    return any(k in t for k in keys)

def _looks_like_sensor_water(p: Dict[str, Any]) -> bool:
    t = _norm(" ".join([
        p.get("title",""),
        p.get("type",""),
        p.get("vendor",""),
        p.get("tags","") if isinstance(p.get("tags"), str) else "",
        p.get("body_html","") or "",
        p.get("sku","") or ""
    ]))
    keys = {
        "sensor de agua","sensor agua","nivel de agua","tinaco","cisterna","pipa","aljibe",
        "iot-water","easy-water","connect-water","connect water","connectwater","bluetooth","wifi"
    }
    bad  = {"antena","hdmi","adaptador","displayport","soporte","bracket","cable","vga","decodificador"}
    if any(b in t for b in bad):
        return False
    return any(k in t for k in keys)

def _matches_any_meta(p: Dict[str, Any], patterns: Iterable[str]) -> bool:
    title = _norm(p.get("title",""))
    ptype = _norm(p.get("type",""))
    tags  = set(t.strip() for t in (_norm(p.get("tags","")).split(",") if isinstance(p.get("tags"), str) else []))

    for pat in patterns or []:
        pat = _norm(pat)
        if pat.startswith("product_type:"):
            want = pat.split(":",1)[1]
            if want and want in ptype:
                return True
        elif pat.startswith("tag:"):
            want = pat.split(":",1)[1]
            if want and want in tags:
                return True
        elif pat.startswith("collection:"):
            # No traemos colecciones en este endpoint
            continue
        else:
            if pat and pat in title:
                return True
    return False

def _compatible_with_inches(p: Dict[str, Any], target: Optional[int]) -> bool:
    if not target:
        return True
    text = _norm(" ".join([
        p.get("title",""),
        p.get("body_html","") or "",
        p.get("tags","") if isinstance(p.get("tags"), str) else ""
    ]))
    for m in re.finditer(r'(\d{2,3})\s*[-–]\s*(\d{2,3})', text):
        a, b = int(m.group(1)), int(m.group(2))
        if a <= target <= b:
            return True
    for m in re.finditer(r'(?:hasta|max(?:imo)?)\s+(\d{2,3})', text):
        if target <= int(m.group(1)):
            return True
    for m in re.finditer(r'(?:para|de)\s+(\d{2,3})\s*(?:["”]|pulg|pulgadas|in)?\b', text):
        if target == int(m.group(1)):
            return True
    return True

def _looks_like_category(p: Dict[str, Any], hint: Optional[str]) -> bool:
    if hint == "support":
        return _looks_like_support(p)
    if hint == "sensor_water":
        return _looks_like_sensor_water(p)
    return False

def _search_with_filters(
    query: str,
    *,
    origin: Optional[str] = None,
    require_photo: bool = False,
    must_include: Optional[List[str]] = None,
    must_match_any_of: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    prefer_in_stock: bool = False,
    inches_target: Optional[int] = None,
    min_score: float = 0.25,
    limit: int = 20,
    category_hint: Optional[str] = None
) -> List[Dict[str, Any]]:
    store, headers = get_shopify_context(origin)
    raw = _fetch_products_multi(store, headers, limit=200)
    if not raw:
        return []

    mapped = []
    for rp in raw:
        if require_photo and not _has_photo(rp):
            continue
        mapped.append(_map_product(rp, store))

    q_tokens = _tokenize(query)
    results: List[Dict[str, Any]] = []

    for p in mapped:
        meta_blob = " ".join([
            p.get("title",""),
            p.get("body_html","") or "",
            p.get("tags","") if isinstance(p.get("tags"), str) else "",
            p.get("type","") or "",
            p.get("sku","") or ""
        ])
        meta_norm = _norm(meta_blob)

        if exclude and _has_any(meta_norm, exclude):
            continue

        if must_include and not _has_any(meta_norm, must_include):
            if not _looks_like_category(p, category_hint):
                continue

        if must_match_any_of and not _matches_any_meta(p, must_match_any_of):
            if not _looks_like_category(p, category_hint):
                continue

        p_tokens = _tokenize(" ".join([p.get("title",""), p.get("tags",""), p.get("type",""), p.get("sku","")]))
        score = _overlap(q_tokens, p_tokens)

        if _looks_like_category(p, category_hint):
            score += 0.2

        if inches_target and category_hint == "support" and not _compatible_with_inches(p, inches_target):
            score -= 0.3

        if score < min_score:
            if not (_looks_like_category(p, category_hint) and len(q_tokens) <= 2 and score >= (min_score - 0.15)):
                continue

        p["_rank_score"] = round(float(score), 4)
        results.append(p)

    if prefer_in_stock:
        results.sort(key=lambda x: (0 if x.get("_in_stock") else 1, -x.get("_rank_score", 0.0)))
    else:
        results.sort(key=lambda x: -x.get("_rank_score", 0.0))

    return results[:max(1, limit)]


# ============================
#   API pública
# ============================

def get_shopify_products(
    keyword: str = "",
    origin: Optional[str] = None,
    require_photo: bool = False,
    **kwargs
):
    advanced_keys = {
        "must_include", "must_match_any_of", "exclude",
        "prefer_in_stock", "inches_target", "min_score", "limit", "category_hint"
    }
    if any(k in kwargs for k in advanced_keys):
        return _search_with_filters(
            query=keyword or "",
            origin=origin,
            require_photo=require_photo,
            must_include=kwargs.get("must_include"),
            must_match_any_of=kwargs.get("must_match_any_of"),
            exclude=kwargs.get("exclude"),
            prefer_in_stock=kwargs.get("prefer_in_stock", False),
            inches_target=kwargs.get("inches_target"),
            min_score=float(kwargs.get("min_score", 0.25)),
            limit=int(kwargs.get("limit", 20)),
            category_hint=kwargs.get("category_hint"),
        )

    return get_product_by_title(keyword, origin, require_photo=require_photo) if keyword else get_products(origin=origin, require_photo=require_photo)


# ============================
#   Detalles / Inventario / Manual
# ============================

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
    store, headers = get_shopify_context(origin)
    try:
        print(f"[DEBUG] Inventar
