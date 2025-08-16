# integrations/shopify_api.py

import os
import re
import math
import html
import json
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import request

# -----------------------------
# Configuration / Context
# -----------------------------

# Render envs (seen in screenshot):
# - DEEPSEEK_API_KEY (not used here)
# - SHOPIFY_TOKEN
# - SHOPIFY_TOKEN_MASTER
SHOPIFY_TOKEN_MX = os.getenv("SHOPIFY_TOKEN", "")
SHOPIFY_TOKEN_MASTER = os.getenv("SHOPIFY_TOKEN_MASTER", "")

# Admin stores
SHOPIFY_STORE_MX = os.getenv("SHOPIFY_STORE_MX", "airb2bsafe-8329.myshopify.com")
SHOPIFY_STORE_MASTER = os.getenv("SHOPIFY_STORE_MASTER", "master-electronicos.myshopify.com")

# Pin API version so both REST and GraphQL stay in sync
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-04")

# Optional: hard limit for per-request GraphQL page
DEFAULT_PAGE = 25


def get_shopify_context(origin: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
    """
    Decide which store/token to use based on Origin header or explicit origin.
    Falls back to master.com.mx store when in doubt (that's the production catalog).
    """
    if not origin:
        origin = request.headers.get("Origin", "") if request else ""

    if origin and "master.mx" in origin and "master.com.mx" not in origin:
        # Legacy domain
        store = SHOPIFY_STORE_MX
        token = SHOPIFY_TOKEN_MX or SHOPIFY_TOKEN_MASTER
    else:
        store = SHOPIFY_STORE_MASTER
        token = SHOPIFY_TOKEN_MASTER or SHOPIFY_TOKEN_MX

    if not token:
        raise RuntimeError("Falta token de Shopify. Define SHOPIFY_TOKEN_MASTER o SHOPIFY_TOKEN.")

    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return store, headers


# -----------------------------
# Utilities
# -----------------------------

_ws_re = re.compile(r"\s+", re.UNICODE)
_token_re = re.compile(r"[A-Za-zÁÉÍÓÚÜáéíóúüÑñ0-9\-\+]+", re.UNICODE)

def _norm(s: Optional[str]) -> str:
    s = s or ""
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _ws_re.sub(" ", s).strip()
    return s

def _tokens(s: str) -> List[str]:
    return [t.lower() for t in _token_re.findall(s or "") if len(t) > 1]

def _choose_image(product: Dict[str, Any]) -> Optional[str]:
    # featuredImage if using GraphQL data shape
    img = None
    if "featuredImage" in product and isinstance(product["featuredImage"], dict):
        img = product["featuredImage"].get("url") or product["featuredImage"].get("src")
    if not img:
        # REST shape
        if isinstance(product.get("image"), dict):
            img = product.get("image", {}).get("src") or product.get("image", {}).get("url")
    if not img:
        # REST images[]
        imgs = product.get("images") or []
        if imgs and isinstance(imgs, list) and isinstance(imgs[0], dict):
            img = imgs[0].get("src") or imgs[0].get("url")
    return img

def _score(query_tokens: List[str], product: Dict[str, Any]) -> float:
    """Simple deterministic score across title, tags, body, sku, vendor."""
    title = _norm(product.get("title"))
    body  = _norm(product.get("body_html") or product.get("description") or "")
    tags  = " ".join(product.get("tags") if isinstance(product.get("tags"), list) else [product.get("tags","")])
    vendor= _norm(product.get("vendor"))
    sku   = _norm(product.get("sku") or " ".join([v.get("sku","") for v in (product.get("variants") or []) if isinstance(v, dict)]))

    hay = f"{title} {tags} {body} {vendor} {sku}".lower()
    hits = 0.0
    for t in set(query_tokens):
        if not t: 
            continue
        if t in title.lower():   hits += 3.0
        if t in sku.lower():     hits += 4.0
        if t in tags.lower():    hits += 2.0
        if t in vendor.lower():  hits += 1.0
        if t in body.lower():    hits += 1.0
    # prefer in-stock if we have that flag populated later
    if product.get("_in_stock"): hits += 1.0
    return hits

def extract_manual_url(body_html: str) -> Optional[str]:
    """
    Try to find a PDF/manual link from the product description.
    """
    if not body_html:
        return None
    body = html.unescape(body_html or "")
    candidates = re.findall(r'href=["\']([^"\']+\.pdf)["\']', body, flags=re.IGNORECASE)
    if candidates:
        return candidates[0]
    # Spanish keywords
    links = re.findall(r'href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', body, flags=re.IGNORECASE|re.DOTALL)
    for href, text in links:
        if any(k in (text or "").lower() for k in ["manual","ficha","descargar","instructivo"]) and href:
            return href
    return None


# -----------------------------
# GraphQL
# -----------------------------

def _graphql(store: str, headers: Dict[str,str], query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
    resp = requests.post(url, headers=headers, json={"query": query, "variables": variables}, timeout=25)
    if resp.status_code != 200:
        raise RuntimeError(f"GraphQL {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data.get("data") or {}

def _build_shopify_query_string(q: str, extra_terms: Optional[List[str]] = None) -> str:
    """
    Build Shopify Admin search query. Supported keys in Admin: title, sku, tag, product_type, vendor, status.
    We force status:active and published_status:published to avoid drafts and legacy items.
    """
    tokens = _tokens(q)
    if extra_terms:
        tokens += extra_terms
    tokens = [t for t in tokens if t]

    clauses = ["status:active"]
    # published status is a REST concept; in GraphQL we narrow by status and rely on classic publications
    fields = ["title", "sku", "tag", "product_type", "vendor", "body"]
    ors = []
    for t in tokens:
        t_esc = re.sub(r'([":])', r'\\\1', t)
        ors.append(f"(title:{t_esc}* OR sku:{t_esc}* OR tag:{t_esc}* OR product_type:{t_esc}* OR vendor:{t_esc}* OR body:{t_esc}*)")
    if ors:
        clauses.append("(" + " AND ".join(ors) + ")")
    return " AND ".join(clauses) if clauses else "status:active"

def _collect_images(product_node: Dict[str, Any]) -> List[str]:
    out = []
    if product_node.get("featuredImage"):
        u = product_node["featuredImage"].get("url")
        if u: out.append(u)
    images = (((product_node.get("images") or {}).get("edges")) or [])
    for edge in images:
        node = edge.get("node") or {}
        u = node.get("url")
        if u: out.append(u)
    return out

def _edges_to_list(edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e.get("node") for e in (edges or []) if isinstance(e, dict)]

def _product_node_to_restish(node: Dict[str, Any]) -> Dict[str, Any]:
    # convert GraphQL node to a dict close to REST product shape our frontend expects
    variants = _edges_to_list(((node.get("variants") or {}).get("edges")) or [])
    flat_vars = []
    for v in variants[:50]:
        flat_vars.append({
            "id": v.get("id"),
            "sku": v.get("sku"),
            "title": v.get("title"),
            "price": (v.get("price") or {}).get("amount") if isinstance(v.get("price"), dict) else v.get("price"),
            "availableForSale": v.get("availableForSale"),
            "inventoryItemId": (v.get("inventoryItem") or {}).get("id") if isinstance(v.get("inventoryItem"), dict) else None,
        })
    imgs = _collect_images(node)
    return {
        "id": node.get("id"),
        "title": node.get("title"),
        "handle": node.get("handle"),
        "vendor": node.get("vendor"),
        "product_type": node.get("productType"),
        "tags": node.get("tags") or [],
        "body_html": node.get("descriptionHtml") or node.get("description") or "",
        "image": {"src": imgs[0]} if imgs else None,
        "images": [{"src": u} for u in imgs],
        "variants": flat_vars,
        "status": node.get("status"),
        "onlineStoreUrl": node.get("onlineStoreUrl"),
    }


def _graphql_product_search(store: str, headers: Dict[str,str], query: str, limit: int = DEFAULT_PAGE) -> List[Dict[str, Any]]:
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
            variants(first: 50) {
              edges {
                node {
                  id
                  title
                  sku
                  availableForSale
                  price
                  inventoryItem { id }
                }
              }
            }
            onlineStoreUrl
          }
        }
      }
    }
    """
    data = _graphql(store, headers, gql, {"first": int(limit), "query": query})
    edges = (((data.get("products") or {}).get("edges")) or [])
    out: List[Dict[str, Any]] = []
    for edge in edges:
        node = edge.get("node") or {}
        out.append(_product_node_to_restish(node))
    return out


# -----------------------------
# Public API (used by main.py)
# -----------------------------

def get_products(limit: int = 20, origin: Optional[str] = None, require_photo: bool = True) -> List[Dict[str, Any]]:
    """Simple latest products page (REST). Used for debug/fallback."""
    store, headers = get_shopify_context(origin)
    url = f"https://{store}/admin/api/{API_VERSION}/products.json?limit={int(limit)}&status=active&published_status=published"
    r = requests.get(url, headers=headers, timeout=20)
    items = (r.json() or {}).get("products") or []
    if require_photo:
        items = [p for p in items if _choose_image(p)]
    return items

def get_product_details(product_id: str, origin: Optional[str] = None) -> Dict[str, Any]:
    store, headers = get_shopify_context(origin)
    url = f"https://{store}/admin/api/{API_VERSION}/products/{product_id}.json"
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return (r.json() or {}).get("product") or {}

def get_inventory_by_variant_id(variant_id: str, origin: Optional[str] = None) -> Dict[str, Any]:
    """
    Return inventory by location for a given variant id (gid or numeric).
    """
    store, headers = get_shopify_context(origin)
    # Accept gid://shopify/ProductVariant/123 → extract 123
    m = re.search(r"(\d+)$", str(variant_id))
    var_id = m.group(1) if m else str(variant_id)

    # First we need the inventory_item_id
    url_variant = f"https://{store}/admin/api/{API_VERSION}/variants/{var_id}.json"
    rv = requests.get(url_variant, headers=headers, timeout=20)
    rv.raise_for_status()
    variant = (rv.json() or {}).get("variant") or {}
    inv_item_id = variant.get("inventory_item_id")
    if not inv_item_id:
        return {"levels": []}

    url_levels = f"https://{store}/admin/api/{API_VERSION}/inventory_levels.json?inventory_item_ids={inv_item_id}"
    rl = requests.get(url_levels, headers=headers, timeout=20)
    rl.raise_for_status()
    levels = (rl.json() or {}).get("inventory_levels") or []

    # Optionally fetch locations names
    locations_map: Dict[int, Dict[str, Any]] = {}
    try:
        url_locs = f"https://{store}/admin/api/{API_VERSION}/locations.json?limit=250"
        locs = requests.get(url_locs, headers=headers, timeout=20).json().get("locations") or []
        for loc in locs:
            locations_map[loc.get("id")] = loc
    except Exception:
        pass

    for lv in levels:
        loc = locations_map.get(lv.get("location_id"))
        if loc:
            lv["location_name"] = loc.get("name")

    return {"levels": levels}

def _apply_only_with_image(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [p for p in products if _choose_image(p)]

def _maybe_mark_in_stock(products: List[Dict[str, Any]]) -> None:
    for p in products:
        # mark as in stock if any variant availableForSale True, or totalInventory > 0 (GraphQL) or inventory_quantity > 0 (REST variant)
        flag = False
        variants = p.get("variants") or []
        for v in variants:
            if v.get("availableForSale") is True: 
                flag = True
                break
            qty = v.get("inventory_quantity")
            if isinstance(qty, int) and qty > 0:
                flag = True
                break
        p["_in_stock"] = flag

def get_shopify_products(
    keyword: str,
    origin: Optional[str] = None,
    require_photo: bool = True,
    **kwargs
) -> List[Dict[str, Any]]:
    """
    High-precision search:
      - Use Admin GraphQL 'products(query: ..)' to search across title, sku, tag, product_type, vendor, body.
      - Filter to status:active and require photo by default.
      - Deterministic scoring and stable sort to avoid erratic results.
      - Optional arguments (compatible with existing main.py):
          must_include: list[str] -> extra tokens that MUST appear (title/tags/body/sku)
          must_match_any_of: list[str] in shopify query syntax (e.g., 'product_type:sensor')
          exclude: list[str] -> tokens to exclude if found locally in the string blob
          prefer_in_stock: bool
          inches_target: Optional[int] (compat)
          min_score: float (default 0.25)
          limit: int (default 20)
          category_hint: Optional[str] (compat)
          location_ids: list[int] optional to filter stock by location
    """
    store, headers = get_shopify_context(origin)
    limit  = int(kwargs.get("limit", 20))
    must_include_tokens = [t.lower() for t in (kwargs.get("must_include") or []) if isinstance(t, str)]
    exclude_tokens      = [t.lower() for t in (kwargs.get("exclude") or []) if isinstance(t, str)]
    prefer_in_stock     = bool(kwargs.get("prefer_in_stock", False))
    min_score           = float(kwargs.get("min_score", 0.25))
    location_ids        = kwargs.get("location_ids") or []
    try:
        location_ids = [int(x) for x in location_ids if str(x).strip().isdigit()]
    except Exception:
        location_ids = []

    base_query = _build_shopify_query_string(keyword)

    # Inject any explicit match-any raw clauses (already shopify syntax)
    extra_clauses = []
    for raw in (kwargs.get("must_match_any_of") or []):
        raw = (raw or "").strip()
        if raw:
            extra_clauses.append(f"({raw})")
    if extra_clauses:
        base_query = f"{base_query} AND (" + " OR ".join(extra_clauses) + ")"

    products = _graphql_product_search(store, headers, base_query, limit=max(limit, DEFAULT_PAGE))
    if not products:
        # Fallback to REST list to never return empty when catalog is huge
        products = get_products(limit=limit, origin=origin, require_photo=require_photo)

    # Only products with images (default)
    if require_photo:
        products = _apply_only_with_image(products)

    # Apply must_include/exclude on normalized blob
    filtered: List[Dict[str, Any]] = []
    for p in products:
        blob = " ".join([
            _norm(p.get("title")),
            _norm(p.get("vendor")),
            _norm(p.get("product_type") or ""),
            _norm(" ".join(p.get("tags") if isinstance(p.get("tags"), list) else [p.get("tags","")])),
            _norm(p.get("body_html") or ""),
            _norm(p.get("sku") or ""),
        ]).lower()

        if must_include_tokens and not all(tok in blob for tok in must_include_tokens):
            continue
        if exclude_tokens and any(tok in blob for tok in exclude_tokens):
            continue
        filtered.append(p)

    # Optional stock-by-location filter (expensive → check first 40 x 6 variants)
    if location_ids:
        narrowed: List[Dict[str, Any]] = []
        for p in filtered[:40]:
            ok = False
            for v in (p.get("variants") or [])[:6]:
                try:
                    inv = get_inventory_by_variant_id(v.get("id"), origin=origin) or {}
                    for lv in (inv.get("levels") or []):
                        lid = lv.get("location_id")
                        try:
                            lid = int(lid)
                        except Exception:
                            continue
                        avail = lv.get("available", 0)
                        if isinstance(avail, str):
                            try:
                                avail = int(avail)
                            except Exception:
                                avail = 0
                        if lid in location_ids and isinstance(avail, int) and avail > 0:
                            ok = True
                            break
                    if ok: 
                        break
                except Exception:
                    continue
            if ok:
                narrowed.append(p)
        filtered = narrowed

    # Mark in-stock flag for gentle sorting
    _maybe_mark_in_stock(filtered)

    # Score & sort (stable)
    q_tokens = _tokens(keyword)
    scored = [( _score(q_tokens, p), str(p.get("id") or ""), p ) for p in filtered]
    # Prefer in-stock if requested
    if prefer_in_stock:
        scored.sort(key=lambda x: (0 if (x[2].get("_in_stock")) else 1, -x[0], x[1]))
    else:
        scored.sort(key=lambda x: (-x[0], x[1]))
    results = [p for s, _pid, p in scored if s >= min_score]
    return results[:limit]
