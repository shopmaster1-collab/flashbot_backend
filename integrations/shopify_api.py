import os
import re
import html
import math
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import request

# =========================================
#   Shopify Admin configuration
# =========================================

API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-04")

SHOPIFY_TOKEN_MX = os.getenv("SHOPIFY_TOKEN", "")
SHOPIFY_TOKEN_MASTER = os.getenv("SHOPIFY_TOKEN_MASTER", "")

SHOPIFY_STORE_MX = os.getenv("SHOPIFY_STORE_MX", "airb2bsafe-8329.myshopify.com")
SHOPIFY_STORE_MASTER = os.getenv("SHOPIFY_STORE_MASTER", "master-electronicos.myshopify.com")


# =========================================
#   Small utils
# =========================================

_WS = re.compile(r"\s+", re.UNICODE)

def _norm(s: Optional[str]) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return _WS.sub(" ", s).strip()

def _tokens(s: str) -> List[str]:
    return [t.lower() for t in re.findall(r"[a-z0-9áéíóúüñ]+", _norm(s), flags=re.IGNORECASE)]

def _extract_numeric_gid(gid_or_num: Any) -> str:
    m = re.search(r"(\d+)$", str(gid_or_num or ""))
    return m.group(1) if m else str(gid_or_num or "")

def _public_domain_for_store(store: str) -> str:
    return "master.com.mx" if store == SHOPIFY_STORE_MASTER else "master.mx"

def get_shopify_context(origin: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
    """
    Decide store & token from request Origin.
    Defaults to master.com.mx catalog.
    """
    if not origin and request:
        origin = request.headers.get("Origin", "")

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
#   REST fallbacks / detail
# =========================================

def get_products(limit: int = 20, origin: Optional[str] = None, require_photo: bool = True) -> List[Dict[str, Any]]:
    store, headers = get_shopify_context(origin)
    url = f"https://{store}/admin/api/{API_VERSION}/products.json?limit={int(limit)}&status=active&published_status=published"
    r = requests.get(url, headers=headers, timeout=20)
    items = (r.json() or {}).get("products") or []
    out: List[Dict[str, Any]] = []
    for p in items:
        image = ""
        if isinstance(p.get("image"), dict):
            image = p["image"].get("src") or p["image"].get("url") or ""
        if not image:
            imgs = p.get("images") or []
            if imgs and isinstance(imgs, list) and isinstance(imgs[0], dict):
                image = imgs[0].get("src") or imgs[0].get("url") or ""
        if require_photo and not image:
            continue
        v = (p.get("variants") or [{}])[0] if isinstance(p.get("variants"), list) and p["variants"] else {}
        price = v.get("price") or v.get("compare_at_price") or "N/A"
        out.append({
            "id": p.get("id"),
            "title": p.get("title") or "",
            "type": p.get("product_type") or "",
            "price": price if isinstance(price, str) else str(price),
            "image": image,
            "link": f"https://{_public_domain_for_store(store)}/products/{p.get('handle') or ''}",
            "body_html": p.get("body_html") or "",
            "sku": v.get("sku") or "",
            "vendor": p.get("vendor") or "",
            "tags": p.get("tags") or [],
            "variant_id": _extract_numeric_gid(v.get("id") or ""),
            "handle": p.get("handle") or "",
            "_in_stock": bool(v.get("inventory_quantity")) and int(v.get("inventory_quantity", 0)) > 0
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
    Return inventory per location for a given variant id (gid or numeric).
    """
    store, headers = get_shopify_context(origin)
    var_id = _extract_numeric_gid(variant_id)

    v_url = f"https://{store}/admin/api/{API_VERSION}/variants/{var_id}.json"
    rv = requests.get(v_url, headers=headers, timeout=20)
    rv.raise_for_status()
    variant = (rv.json() or {}).get("variant") or {}
    inv_item_id = variant.get("inventory_item_id")
    if not inv_item_id:
        return []

    lv_url = f"https://{store}/admin/api/{API_VERSION}/inventory_levels.json?inventory_item_ids={inv_item_id}"
    rl = requests.get(lv_url, headers=headers, timeout=20)
    rl.raise_for_status()
    levels = (rl.json() or {}).get("inventory_levels") or []

    # Fetch locations names
    names = {}
    try:
        locs = requests.get(f"https://{store}/admin/api/{API_VERSION}/locations.json?limit=250",
                            headers=headers, timeout=20).json().get("locations") or []
        for loc in locs:
            names[loc.get("id")] = loc.get("name")
    except Exception:
        pass

    out: List[Dict[str, Any]] = []
    for lv in levels:
        lid = lv.get("location_id")
        qty = lv.get("available", 0)
        try:
            qty = int(qty)
        except Exception:
            qty = 0
        out.append({"sucursal": names.get(lid, f"Loc {lid}"), "cantidad": qty})
    return out


# =========================================
#   Admin GraphQL search
# =========================================

def _graphql(store: str, headers: Dict[str, str], query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
    resp = requests.post(url, headers=headers, json={"query": query, "variables": variables}, timeout=25)
    if resp.status_code != 200:
        raise RuntimeError(f"GraphQL {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data.get("data") or {}

# ---- Domain intents ----
NEG_GENERIC = {"amplificador","booster","cable","antena","adaptador","conversor","displayport","vga","hdmi",
               "splitter","extensor","switch","apagador","toma corriente","toma-corriente","tomacorriente",
               "bocina","bafle","foco","lámpara","lampara"}

WATER_POS = {"sensor","nivel","agua","tinaco","cisterna","aljibe","pipa","water","tank","float"}
GAS_POS   = {"sensor","gas","estacionario","fugas","tanque","lp","propano","butano"}
ENER_POS  = {"medidor","consumo","energía","energia","kwh","kw","corriente","voltaje","electricidad","eléctrica","electrica","amperaje"}

def _detect_intent(text: str) -> Optional[str]:
    t = _norm(text).lower()
    # simple discriminative checks
    if any(w in t for w in ["tinaco","cisterna","agua","nivel de agua","nivel","water"]):
        return "sensor_water"
    if any(w in t for w in ["gas","fuga","estacionario","tanque"]):
        return "sensor_gas"
    if any(w in t for w in ["consumo de energia","consumo de energía","kwh","medidor de energia","medidor de energía","electricidad","consumo eléctrico","energia electrica"]):
        return "sensor_energy"
    # fallbacks by keyword + "sensor"/"medidor"
    if "sensor" in t and any(w in t for w in ["agua","tinaco","cisterna"]):
        return "sensor_water"
    if "sensor" in t and "gas" in t:
        return "sensor_gas"
    if "medidor" in t or "kwh" in t:
        return "sensor_energy"
    return None

def _build_admin_query(keyword: str, intent: Optional[str]) -> str:
    toks = _tokens(keyword)

    ors = []
    for t in toks:
        t = re.sub(r'([":])', r"\\\1", t)
        ors.append(f'(title:{t}* OR sku:{t}* OR tag:{t}* OR product_type:{t}* OR vendor:{t}* OR body:{t}*)')

    # inches / vesa keep for other categories if needed
    for m in re.finditer(r"(\d{2,3})\s*(?:\"|pulg|pulgadas|in)\b", keyword.lower()):
        n = m.group(1)
        ors.append(f'(title:{n}* OR body:{n}* OR tag:{n}*)')
    for m in re.finditer(r"(\d{2,4})\s*[xX]\s*(\d{2,4})", keyword):
        pat = f"{m.group(1)}x{m.group(2)}"
        ors.append(f'(title:{pat} OR body:{pat} OR tag:{pat})')

    q = ["status:active"]
    if ors:
        q.append("(" + " AND ".join(ors) + ")")

    # Intent-specific strengthening
    if intent == "sensor_water":
        q.append('(product_type:sensor OR title:sensor* OR tag:sensor)')
        q.append('(title:agua* OR body:agua OR tag:agua OR title:tinaco* OR body:tinaco OR tag:tinaco OR title:cisterna* OR body:cisterna OR tag:cisterna OR tag:nivel)')
    elif intent == "sensor_gas":
        q.append('(product_type:sensor OR title:sensor* OR tag:sensor)')
        q.append('(title:gas* OR body:gas OR tag:gas OR tag:estacionario OR title:tanque* OR body:tanque)')
    elif intent == "sensor_energy":
        q.append('(title:medidor* OR product_type:medidor OR tag:medidor OR body:medidor OR title:kwh* OR body:kwh OR tag:kwh OR title:energia* OR body:energia OR tag:energia)')

    return " AND ".join(q)

def _graphql_product_search(store: str, headers: Dict[str,str], query: str, first: int = 80) -> List[Dict[str, Any]]:
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
            featuredImage { url }
            images(first: 3) { edges { node { url } } }
            descriptionHtml
            variants(first: 25) {
              edges { node { id title sku availableForSale price } }
            }
            collections(first: 10) { edges { node { title handle } } }
          }
        }
      }
    }
    """
    data = _graphql(store, headers, gql, {"first": int(first), "query": query})
    edges = (((data.get("products") or {}).get("edges")) or [])
    return [e.get("node") for e in edges if isinstance(e, dict)]

def _node_to_card(n: Dict[str, Any], public_domain: str) -> Dict[str, Any]:
    # image
    image = ""
    if n.get("featuredImage"):
        image = n["featuredImage"].get("url") or ""
    if not image:
        edges = ((n.get("images") or {}).get("edges")) or []
        if edges:
            image = (edges[0].get("node") or {}).get("url") or ""

    # variant
    v_edges = ((n.get("variants") or {}).get("edges")) or []
    v0 = v_edges[0].get("node") if v_edges else {}
    raw_price = v0.get("price")
    price = raw_price.get("amount") if isinstance(raw_price, dict) else (str(raw_price) if raw_price is not None else "N/A")
    sku = v0.get("sku") or ""
    variant_id = _extract_numeric_gid(v0.get("id") or "")

    handle = n.get("handle") or ""
    link = f"https://{public_domain}/products/{handle}" if handle else f"https://{public_domain}"

    # collections list
    cols = []
    c_edges = ((n.get("collections") or {}).get("edges")) or []
    for e in c_edges:
        node = e.get("node") or {}
        title = (node.get("title") or "").strip()
        if title:
            cols.append(title)

    return {
        "id": n.get("id"),
        "title": n.get("title") or "",
        "type": n.get("productType") or "",
        "price": price or "N/A",
        "image": image,
        "link": link,
        "body_html": n.get("descriptionHtml") or n.get("description") or "",
        "sku": sku,
        "vendor": n.get("vendor") or "",
        "tags": n.get("tags") or [],
        "variant_id": variant_id,
        "handle": handle,
        "_in_stock": True if v0.get("availableForSale") else False,
        "_collections": cols,
    }

# Negative words per intent to avoid false positives
NEG_PER_INTENT = {
    "sensor_water": NEG_GENERIC | {"interruptor","switch","apagador","contacto"},
    "sensor_gas":   NEG_GENERIC | {"detector de humo"},  # humo may be separate
    "sensor_energy": NEG_GENERIC | {"apagador","switch","contacto","tomacorriente"},
}

# Boost collections for relevance
TARGET_COLLECTIONS = {"soportes para tv","video y tv","ofertas relámpago","ofertas relampago",
                      "sensores","iot","hogar inteligente","medidores"}

def _score_product(p: Dict[str, Any], keyword: str, intent: Optional[str]) -> float:
    s = 0.0
    text = " ".join([
        _norm(p.get("title")), _norm(p.get("body_html")),
        " ".join(p.get("tags") if isinstance(p.get("tags"), list) else [str(p.get("tags",""))]),
        _norm(p.get("vendor")), _norm(p.get("sku"))
    ]).lower()

    for t in set(_tokens(keyword)):
        if t in (p.get("title","").lower()): s += 3.0
        if t in (p.get("sku","").lower()):   s += 4.0
        if t and t in text:                  s += 1.0

    if intent == "sensor_water":
        if any(w in text for w in WATER_POS): s += 2.0
    elif intent == "sensor_gas":
        if any(w in text for w in GAS_POS): s += 2.0
    elif intent == "sensor_energy":
        if any(w in text for w in ENER_POS): s += 2.0

    # collection boost
    cols = [c.lower() for c in (p.get("_collections") or [])]
    if any(c in TARGET_COLLECTIONS for c in cols):
        s += 0.8

    if p.get("_in_stock"):
        s += 0.3

    # punish negatives
    if intent and any(w in text for w in NEG_PER_INTENT.get(intent, set())):
        s -= 3.0
    else:
        if any(w in text for w in NEG_GENERIC):
            s -= 1.5
    return s

# =========================================
#   Public search
# =========================================

def get_shopify_products(
    keyword: str = "",
    origin: Optional[str] = None,
    require_photo: bool = True,
    **kwargs
) -> List[Dict[str, Any]]:
    """
    Precise product search across 4k+ items with:
      - Admin GraphQL search (title, sku, tag, product_type, vendor, body)
      - Intent recognition for sensors (agua/tinaco, gas, energía)
      - Collections awareness
      - Deterministic scoring; only items with image
    Requires: read_products (and read_inventory/read_locations if you call inventory endpoints).
    """
    store, headers = get_shopify_context(origin)
    public_domain = _public_domain_for_store(store)

    intent = _detect_intent(keyword)
    base_query = _build_admin_query(keyword, intent)

    try:
        nodes = _graphql_product_search(store, headers, base_query, first=max(60, int(kwargs.get("limit", 20))*3))
        products = [_node_to_card(n, public_domain) for n in nodes]
    except Exception as e:
        # Fallback to REST list to avoid empty responses
        print(f"[WARN] GraphQL search falló ({e}); usando REST fallback.")
        products = get_products(limit=int(kwargs.get("limit", 20)), origin=origin, require_photo=require_photo)

    # require image
    if require_photo:
        products = [p for p in products if p.get("image")]

    # If we have an intent, filter out false positives aggressively
    if intent:
        filtered = []
        negs = NEG_PER_INTENT.get(intent, set())
        for p in products:
            text = " ".join([
                _norm(p.get("title")), _norm(p.get("body_html")),
                " ".join(p.get("tags") if isinstance(p.get("tags"), list) else [str(p.get("tags",""))]),
                _norm(p.get("vendor")), _norm(p.get("sku"))
            ]).lower()
            if any(w in text for w in negs):
                continue
            filtered.append(p)
        products = filtered

    # Score & sort
    scored = [( _score_product(p, keyword, intent), p.get("title",""), p ) for p in products]
    scored.sort(key=lambda x: (-x[0], x[1]))
    limit = int(kwargs.get("limit", 20))
    return [p for _,__,p in scored][:limit]


# =========================================
#   Manual URL
# =========================================

def extract_manual_url(body_html: str) -> Optional[str]:
    if not body_html:
        return None
    body = html.unescape(body_html or "")
    m = re.search(r'href=["\']([^"\']+\.pdf)["\']', body, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    for href, text in re.findall(r'href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', body, flags=re.IGNORECASE|re.DOTALL):
        if any(k in (text or "").lower() for k in ["manual","ficha","descargar","instructivo","datasheet"]):
            return href
    return None
