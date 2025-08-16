import os
import re
import html
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import request

# ============================================================
#   Shopify Admin configuration
# ============================================================

API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-04")

SHOPIFY_TOKEN_MX = os.getenv("SHOPIFY_TOKEN", "")
SHOPIFY_TOKEN_MASTER = os.getenv("SHOPIFY_TOKEN_MASTER", "")

SHOPIFY_STORE_MX = os.getenv("SHOPIFY_STORE_MX", "airb2bsafe-8329.myshopify.com")
SHOPIFY_STORE_MASTER = os.getenv("SHOPIFY_STORE_MASTER", "master-electronicos.myshopify.com")


# ============================================================
#   Helpers
# ============================================================

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


# ============================================================
#   REST fallbacks
# ============================================================

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


# ============================================================
#   GraphQL Admin search
# ============================================================

def _graphql(store: str, headers: Dict[str, str], query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
    resp = requests.post(url, headers=headers, json={"query": query, "variables": variables}, timeout=25)
    if resp.status_code != 200:
        raise RuntimeError(f"GraphQL {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data.get("data") or {}

SUPPORT_NEGATIVE_WORDS = {
    "amplificador","booster","cable","antena","adaptador","conversor","displayport","vga","hdmi","splitter",
    "extensor","switch","conmutador","sintonizador","decodificador","tuner","bocina","bafle",
}

SUPPORT_POSITIVE_WORDS = {
    "soporte","bracket","montaje","montura","holder","vesa","pared","techo","articulado","inclinable","esquinero","tv","pantalla","monitor"
}

TARGET_COLLECTIONS = {"soportes para tv","video y tv","ofertas relámpago","ofertas relampago"}

def _build_admin_query(keyword: str, extra: Optional[List[str]] = None) -> str:
    toks = _tokens(keyword)
    # expand synonyms quickly
    if "bracket" in toks or "montaje" in toks or "montura" in toks:
        toks.append("soporte")
    if "tv" in toks or "televisor" in toks or "pantalla" in toks:
        toks.append("pantalla")

    ors = []
    for t in toks:
        t = re.sub(r'([":])', r"\\\1", t)
        ors.append(f'(title:{t}* OR sku:{t}* OR tag:{t}* OR product_type:{t}* OR vendor:{t}* OR body:{t}*)')

    # inches
    for m in re.finditer(r"(\d{2,3})\s*(?:\"|pulg|pulgadas|in)\b", keyword.lower()):
        n = m.group(1)
        ors.append(f'(title:{n}* OR body:{n}* OR tag:{n}*)')

    # vesa patterns
    for m in re.finditer(r"(\d{2,4})\s*[xX]\s*(\d{2,4})", keyword):
        pat = f"{m.group(1)}x{m.group(2)}"
        ors.append(f'(title:{pat} OR body:{pat} OR tag:{pat})')

    q = ["status:active"]
    if ors:
        q.append("(" + " AND ".join(ors) + ")")

    if extra:
        # raw clauses (already admin syntax)
        q.append("(" + " OR ".join(extra) + ")")

    return " AND ".join(q)

def _product_node_to_card(n: Dict[str, Any], public_domain: str) -> Dict[str, Any]:
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

    collections = []
    c_edges = ((n.get("collections") or {}).get("edges")) or []
    for e in c_edges:
        c = e.get("node") or {}
        title = (c.get("title") or "").strip()
        if title:
            collections.append(title)

    handle = n.get("handle") or ""
    link = f"https://{public_domain}/products/{handle}" if handle else f"https://{public_domain}"

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
        "_collections": collections,
    }

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
            collections(first: 10) {
              edges { node { id title handle } }
            }
          }
        }
      }
    }
    """
    data = _graphql(store, headers, gql, {"first": int(first), "query": query})
    edges = (((data.get("products") or {}).get("edges")) or [])
    return [e.get("node") for e in edges if isinstance(e, dict)]


# ============================================================
#   Public API
# ============================================================

def _is_support_intent(text: str) -> bool:
    t = _norm(text).lower()
    if any(w in t for w in SUPPORT_POSITIVE_WORDS):
        if not any(b in t for b in SUPPORT_NEGATIVE_WORDS):
            return True
    # ask for VESA or inches implies support intent
    if re.search(r"\bvesa\b", t) or re.search(r"\d{2,3}\s*(?:\"|pulg|pulgadas|in)\b", t):
        return True
    return False

def _boost_if_support(p: Dict[str, Any], query: str) -> float:
    score = 0.0
    text = " ".join([
        _norm(p.get("title")),
        _norm(p.get("body_html")),
        " ".join(p.get("tags") if isinstance(p.get("tags"), list) else [str(p.get("tags",""))]).lower(),
        _norm(p.get("vendor")),
        _norm(p.get("sku")),
    ]).lower()

    for t in set(_tokens(query)):
        if t and t in (p.get("title","").lower()):
            score += 3.0
        if t and t in (p.get("sku","").lower()):
            score += 4.0
        if t and t in text:
            score += 1.0

    # prefer supports
    if any(w in text for w in SUPPORT_POSITIVE_WORDS):
        score += 1.5
    if any(w in text for w in SUPPORT_NEGATIVE_WORDS):
        score -= 2.0

    # boost if belongs to desired collections
    cols = [c.lower() for c in (p.get("_collections") or [])]
    if any(c in TARGET_COLLECTIONS for c in cols):
        score += 1.5

    # inches match / ranges
    for m in re.finditer(r"(\d{2,3})\s*(?:\"|pulg|pulgadas|in)\b", query.lower()):
        target = int(m.group(1))
        blob = text
        ok = False
        # ranges like 32-80
        for mm in re.finditer(r"(\d{2,3})\s*[-–]\s*(\d{2,3})", blob):
            a, b = int(mm.group(1)), int(mm.group(2))
            if a <= target <= b:
                ok = True; break
        # "hasta 80" or "max 80"
        if not ok:
            for mm in re.finditer(r"(?:hasta|max(?:imo)?)\s*(\d{2,3})", blob):
                if target <= int(mm.group(1)):
                    ok = True; break
        # "para 80"
        if not ok:
            if re.search(rf"(?:para|de)\s*{target}\s*(?:\"|pulg|pulgadas|in)?\b", blob):
                ok = True
        if ok: score += 1.0

    # vesa exact match boost
    for m in re.finditer(r"(\d{2,4})\s*[xX]\s*(\d{2,4})", query):
        pat = f"{m.group(1)}x{m.group(2)}"
        if pat in text:
            score += 1.2

    if p.get("_in_stock"):
        score += 0.5

    return score


def get_shopify_products(
    keyword: str = "",
    origin: Optional[str] = None,
    require_photo: bool = True,
    **kwargs
) -> List[Dict[str, Any]]:
    """
    Precise product search across 4k+ items with:
      - Admin GraphQL search (title, sku, tag, product_type, vendor, body)
      - Collections awareness (Soportes para TV, Video y TV, OFERTAS RELÁMPAGO)
      - VESA and inches parsing
      - Deterministic scoring; only items with image
    Requires: read_products (and read_inventory/read_locations if you call inventory endpoints).
    """
    store, headers = get_shopify_context(origin)
    public_domain = _public_domain_for_store(store)

    # Build admin query
    extra = []
    # If intent is support, bias query with extra raw terms that help Shopify search
    if _is_support_intent(keyword):
        extra.append('(tag:soporte OR product_type:soporte OR title:soporte*)')

    base_query = _build_admin_query(keyword, extra=extra)

    try:
        nodes = _graphql_product_search(store, headers, base_query, first=max(60, int(kwargs.get("limit", 20))*3))
        products = [_product_node_to_card(n, public_domain) for n in nodes]
    except Exception as e:
        # Fallback to REST list (first page) to avoid empty responses
        print(f"[WARN] GraphQL search falló ({e}); usando REST fallback.")
        products = get_products(limit=int(kwargs.get("limit", 20)), origin=origin, require_photo=require_photo)

    # require image
    if require_photo:
        products = [p for p in products if p.get("image")]

    # If support intent, filter hard negatives and boost good collections
    if _is_support_intent(keyword):
        filtered = []
        for p in products:
            blob = " ".join([
                _norm(p.get("title")), _norm(p.get("body_html")),
                " ".join(p.get("tags") if isinstance(p.get("tags"), list) else [str(p.get("tags",""))]).lower()
            ]).lower()
            if any(w in blob for w in SUPPORT_NEGATIVE_WORDS):
                continue
            filtered.append(p)
        products = filtered

    # Scoring & stable sorting
    scored = []
    for p in products:
        s = _boost_if_support(p, keyword) if _is_support_intent(keyword) else 0.0
        # generic token score
        if not _is_support_intent(keyword):
            text = " ".join([
                _norm(p.get("title")), _norm(p.get("body_html")),
                _norm(p.get("vendor")), _norm(p.get("sku"))
            ]).lower()
            for t in set(_tokens(keyword)):
                if t in p.get("title","").lower(): s += 3.0
                if t in p.get("sku","").lower(): s += 4.0
                if t in text: s += 1.0
        scored.append((s, p.get("title",""), p))

    scored.sort(key=lambda x: (-x[0], x[1]))
    limit = int(kwargs.get("limit", 20))
    return [p for _,__,p in scored][:limit]


# ============================================================
#   Manual URL
# ============================================================

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
