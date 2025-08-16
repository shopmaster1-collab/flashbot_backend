import os
import re
import html
import math
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import request

# =========================================
#   Configuración de Shopify (Admin API)
# =========================================

# Tokens (Render -> Environment)
SHOPIFY_TOKEN_MX = os.getenv("SHOPIFY_TOKEN", "")               # master.mx (legacy)
SHOPIFY_TOKEN_MASTER = os.getenv("SHOPIFY_TOKEN_MASTER", "")    # master.com.mx (principal)

# Dominios admin
SHOPIFY_STORE_MX = os.getenv("SHOPIFY_STORE_MX", "airb2bsafe-8329.myshopify.com")
SHOPIFY_STORE_MASTER = os.getenv("SHOPIFY_STORE_MASTER", "master-electronicos.myshopify.com")

# Versión API (usar una estable que coincida con permisos)
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-04")

# =========================================
#   Utilidades
# =========================================

_ws_re = re.compile(r"\s+", re.UNICODE)
_tok_re = re.compile(r"[A-Za-zÁÉÍÓÚÜáéíóúüÑñ0-9\-+%#]+", re.UNICODE)


def _norm(s: Optional[str]) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return _ws_re.sub(" ", s).strip()


def _tokens(s: str) -> List[str]:
    return [t.lower() for t in _tok_re.findall(_norm(s)) if len(t) > 1]


def _extract_numeric_gid(gid_or_num: Any) -> str:
    """
    Convierte 'gid://shopify/ProductVariant/49592845467947' -> '49592845467947'
    o deja el número tal cual.
    """
    m = re.search(r"(\d+)$", str(gid_or_num or ""))
    return m.group(1) if m else str(gid_or_num or "")


def _choose_image_src(product: Dict[str, Any]) -> Optional[str]:
    """
    Acepta estructura REST (product.image, product.images[]) o GraphQL (featuredImage, images.edges[]).
    Devuelve una URL directa (string) o None.
    """
    # GraphQL featuredImage
    if isinstance(product.get("featuredImage"), dict):
        u = product["featuredImage"].get("url") or product["featuredImage"].get("src")
        if u:
            return u

    # REST image
    if isinstance(product.get("image"), dict):
        u = product["image"].get("src") or product["image"].get("url")
        if u:
            return u

    # GraphQL images[]
    images = product.get("images") or []
    if isinstance(images, dict):  # GraphQL edges
        edges = images.get("edges") or []
        for e in edges:
            u = (e.get("node") or {}).get("url")
            if u:
                return u
    elif isinstance(images, list):  # REST images
        for img in images:
            if isinstance(img, dict):
                u = img.get("src") or img.get("url")
                if u:
                    return u

    return None


def _safe_first_variant(product: Dict[str, Any]) -> Dict[str, Any]:
    vs = product.get("variants") or []
    if isinstance(vs, dict):  # GraphQL edges
        edges = vs.get("edges") or []
        if edges:
            return edges[0].get("node") or {}
    elif isinstance(vs, list):
        return vs[0] if vs else {}
    return {}


def get_shopify_context(origin: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
    """
    Decide token/tienda por Origin.
    Por defecto prioriza master.com.mx (catálogo principal).
    """
    if not origin:
        origin = request.headers.get("Origin", "") if request else ""

    if origin and "master.mx" in origin and "master.com.mx" not in origin:
        store = SHOPIFY_STORE_MX
        token = SHOPIFY_TOKEN_MX or SHOPIFY_TOKEN_MASTER
    else:
        store = SHOPIFY_STORE_MASTER
        token = SHOPIFY_TOKEN_MASTER or SHOPIFY_TOKEN_MX

    if not token:
        raise RuntimeError("Falta token de Shopify. Configura SHOPIFY_TOKEN_MASTER o SHOPIFY_TOKEN.")

    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return store, headers

# =========================================
#   REST helpers (fallback / detalle)
# =========================================

def get_products(limit: int = 20, origin: Optional[str] = None, require_photo: bool = True) -> List[Dict[str, Any]]:
    store, headers = get_shopify_context(origin)
    url = f"https://{store}/admin/api/{API_VERSION}/products.json?limit={int(limit)}&status=active&published_status=published"
    r = requests.get(url, headers=headers, timeout=20)
    items = (r.json() or {}).get("products") or []
    if require_photo:
        items = [p for p in items if _choose_image_src(p)]
    # normalizar forma REST a forma que espera main.py
    out: List[Dict[str, Any]] = []
    for p in items:
        v = _safe_first_variant(p)
        out.append({
            "id": p.get("id"),
            "title": p.get("title") or "",
            "type": p.get("product_type") or "",
            "price": v.get("price") or v.get("compare_at_price") or "N/A",
            "image": _choose_image_src(p) or "",
            "link": f"https://{store}/products/{p.get('handle') or ''}",
            "body_html": p.get("body_html") or "",
            "sku": v.get("sku") or "",
            "vendor": p.get("vendor") or "",
            "tags": p.get("tags") or "",
            "variant_id": v.get("id") or 0,
            "handle": p.get("handle") or "",
            "_in_stock": (bool(v.get("inventory_quantity")) and int(v.get('inventory_quantity', 0)) > 0)
        })
    return out


def get_product_details(product_id: str, origin: Optional[str] = None) -> Dict[str, Any]:
    store, headers = get_shopify_context(origin)
    url = f"https://{store}/admin/api/{API_VERSION}/products/{product_id}.json"
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return (r.json() or {}).get("product") or {}


def get_inventory_by_variant_id(variant_id: str, origin: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Regresa niveles de inventario por sucursal para un variant_id (acepta gid o numérico).
    """
    store, headers = get_shopify_context(origin)
    var_id = _extract_numeric_gid(variant_id)

    # 1) obtener inventory_item_id
    v_url = f"https://{store}/admin/api/{API_VERSION}/variants/{var_id}.json"
    rv = requests.get(v_url, headers=headers, timeout=20)
    rv.raise_for_status()
    variant = (rv.json() or {}).get("variant") or {}
    inv_item_id = variant.get("inventory_item_id")
    if not inv_item_id:
        return []

    # 2) niveles
    lv_url = f"https://{store}/admin/api/{API_VERSION}/inventory_levels.json?inventory_item_ids={inv_item_id}"
    rl = requests.get(lv_url, headers=headers, timeout=20)
    rl.raise_for_status()
    levels = (rl.json() or {}).get("inventory_levels") or []

    # 3) nombres de location
    locations_map: Dict[int, Dict[str, Any]] = {}
    try:
        locs = requests.get(f"https://{store}/admin/api/{API_VERSION}/locations.json?limit=250",
                            headers=headers, timeout=20).json().get("locations") or []
        for loc in locs:
            locations_map[loc.get("id")] = loc
    except Exception:
        pass

    result: List[Dict[str, Any]] = []
    for lv in levels:
        loc_id = lv.get("location_id")
        qty = lv.get("available", 0)
        try:
            qty = int(qty)
        except Exception:
            qty = 0
        loc_name = (locations_map.get(loc_id) or {}).get("name") or f"Loc {loc_id}"
        result.append({"sucursal": loc_name, "cantidad": qty})
    return result

# =========================================
#   GraphQL Admin (búsqueda de alta precisión)
# =========================================

def _graphql(store: str, headers: Dict[str, str], query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
    r = requests.post(url, headers=headers, json={"query": query, "variables": variables}, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"GraphQL {r.status_code}: {r.text[:200]}")
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data.get("data") or {}


def _build_admin_query_string(q: str, extra_terms: Optional[List[str]] = None) -> str:
    toks = _tokens(q)
    if extra_terms:
        toks += [t for t in extra_terms if t]
    clauses = ["status:active"]
    ors = []
    for t in toks:
        t = re.sub(r'([":])', r"\\\1", t)
        ors.append(f'(title:{t}* OR sku:{t}* OR tag:{t}* OR product_type:{t}* OR vendor:{t}* OR body:{t}*)')
    if ors:
        clauses.append("(" + " AND ".join(ors) + ")")
    return " AND ".join(clauses)


def _gql_node_to_compat(node: Dict[str, Any], public_store_for_link: str) -> Dict[str, Any]:
    # imagen
    image_url = _choose_image_src(node) or ""

    # primer variant
    first_v = {}
    v_edges = ((node.get("variants") or {}).get("edges")) or []
    if v_edges:
        first_v = v_edges[0].get("node") or {}

    # precio
    raw_price = first_v.get("price")
    price = "N/A"
    if isinstance(raw_price, dict):
        price = raw_price.get("amount") or "N/A"
    elif isinstance(raw_price, (str, int, float)):
        price = str(raw_price)

    # sku
    sku = first_v.get("sku") or ""

    # variant id numérico
    variant_id = _extract_numeric_gid(first_v.get("id"))

    # stock (ligero): availableForSale o >0 en niveles si estuviera expuesto
    in_stock = True if first_v.get("availableForSale") is True else False

    handle = node.get("handle") or ""
    link = f"https://{public_store_for_link}/products/{handle}" if handle else f"https://{public_store_for_link}"

    return {
        "id": node.get("id"),
        "title": node.get("title") or "",
        "type": node.get("productType") or "",
        "price": price,
        "image": image_url,
        "link": link,
        "body_html": node.get("descriptionHtml") or node.get("description") or "",
        "sku": sku,
        "vendor": node.get("vendor") or "",
        "tags": node.get("tags") or [],
        "variant_id": variant_id,
        "handle": handle,
        "_in_stock": in_stock
    }


def _graphql_product_search(store: str, headers: Dict[str, str], query: str, limit: int = 40) -> List[Dict[str, Any]]:
    gql = """
    query($first:Int!, $query:String!) {
      products(first: $first, query: $query) {
        edges {
          node {
            id
            title
            handle
            vendor
            productType
            tags
            status
            featuredImage { url altText }
            images(first: 3) { edges { node { url altText } } }
            descriptionHtml
            variants(first: 25) {
              edges {
                node {
                  id
                  title
                  sku
                  availableForSale
                  price
                }
              }
            }
          }
        }
      }
    }
    """
    data = _graphql(store, headers, gql, {"first": int(limit), "query": query})
    edges = (((data.get("products") or {}).get("edges")) or [])
    return [e.get("node") for e in edges if isinstance(e, dict)]

# =========================================
#   Búsqueda pública usada por main.py
# =========================================

def get_shopify_products(
    keyword: str = "",
    origin: Optional[str] = None,
    require_photo: bool = True,
    **kwargs
) -> List[Dict[str, Any]]:
    """
    Estrategia:
      1) Admin GraphQL con query de alta precisión (title, sku, tag, product_type, vendor, body).
      2) Mapear a la forma que espera main.py (image string, price string, variant_id numérico, handle y link).
      3) Filtrar por foto si require_photo.
      4) Si no hay resultados, fallback REST (get_products).
    """
    store, headers = get_shopify_context(origin)
    public_domain = "master.com.mx" if store == SHOPIFY_STORE_MASTER else "master.mx"

    # Construir query
    base_query = _build_admin_query_string(keyword)
    # Añadir must_match_any_of crudo (sintaxis Admin) si viene de main.py
    extra = []
    for raw in (kwargs.get("must_match_any_of") or []):
        raw = (raw or "").strip()
        if raw:
            extra.append(f"({raw})")
    if extra:
        base_query = f"{base_query} AND (" + " OR ".join(extra) + ")"

    try:
        nodes = _graphql_product_search(store, headers, base_query, limit=max(40, int(kwargs.get("limit", 20))))
        products = [_gql_node_to_compat(n, public_domain) for n in nodes]
    except Exception as e:
        # Si GraphQL falla (permisos, etc.) hacemos fallback a REST directo
        print(f"[WARN] GraphQL search falló ({e}); usando REST fallback.")
        products = get_products(limit=int(kwargs.get("limit", 20)), origin=origin, require_photo=require_photo)

    # Filtros opcionales (compatibilidad con main.py)
    if require_photo:
        products = [p for p in products if p.get("image")]

    # must_include / exclude sobre blob normalizado
    blob_include = [t.lower() for t in (kwargs.get("must_include") or [])]
    blob_exclude = [t.lower() for t in (kwargs.get("exclude") or [])]

    filtered: List[Dict[str, Any]] = []
    for p in products:
        blob = " ".join([
            _norm(p.get("title")),
            _norm(p.get("vendor")),
            _norm(p.get("type")),
            _norm(" ".join(p.get("tags") if isinstance(p.get("tags"), list) else [p.get("tags","")])),
            _norm(p.get("body_html")),
            _norm(p.get("sku"))
        ]).lower()

        if blob_include and not all(tok in blob for tok in blob_include):
            continue
        if blob_exclude and any(tok in blob for tok in blob_exclude):
            continue
        filtered.append(p)

    # Scoring determinista sencillo
    q_tokens = _tokens(keyword)
    def score(p: Dict[str, Any]) -> float:
        hay = f"{p.get('title','').lower()} {p.get('sku','').lower()} {str(p.get('tags','')).lower()} {p.get('vendor','').lower()} {str(p.get('body_html','')).lower()}"
        s = 0.0
        for t in set(q_tokens):
            if t in p.get("title","").lower(): s += 3.0
            if t in p.get("sku","").lower():   s += 4.0
            if t in str(p.get("tags","")).lower(): s += 2.0
            if t in p.get("vendor","").lower(): s += 1.0
            if t in str(p.get("body_html","")).lower(): s += 1.0
        if p.get("_in_stock"): s += 1.0
        return s

    min_score = float(kwargs.get("min_score", 0.2))
    prefer_in_stock = bool(kwargs.get("prefer_in_stock", False))
    scored = [(score(p), p) for p in filtered]
    if prefer_in_stock:
        scored.sort(key=lambda x: (0 if x[1].get("_in_stock") else 1, -x[0], x[1].get("title","")))
    else:
        scored.sort(key=lambda x: (-x[0], x[1].get("title","")))
    out = [p for s,p in scored if s >= min_score]

    limit = int(kwargs.get("limit", 20))
    return out[:limit]

# =========================================
#   Utilidad: extraer manual (PDF)
# =========================================

def extract_manual_url(body_html: str) -> Optional[str]:
    """
    Busca enlace a PDF/Manual en la descripción.
    """
    body = html.unescape(body_html or "")
    # PDFs directos
    m = re.search(r'href=["\']([^"\']+\.pdf)["\']', body, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    # Palabras clave comunes
    for href, text in re.findall(r'href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', body, flags=re.IGNORECASE|re.DOTALL):
        if any(k in (text or "").lower() for k in ["manual","ficha","descargar","instructivo","datasheet"]):
            return href
    return None
