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


# ================
#   GraphQL helpers
# ================
def _graphql(store: str, headers: dict, query: str, variables: dict):
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
    resp = requests.post(url, headers=headers, json={"query": query, "variables": variables}, timeout=25)
    if resp.status_code != 200:
        raise RuntimeError(f"GraphQL {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data.get('errors')}")
    return data.get("data") or {}

def _build_admin_query(keyword: str) -> str:
    """
    Construye la query de Shopify Admin con sinónimos y extracción de pulgadas/VESA/SKU.
    """
    text = (keyword or "").lower()

    # sinónimos básicos
    synonyms = {
        "soporte": ["soporte", "bracket", "mount", "montaje", "brackets"],
        "pantalla": ["pantalla", "tv", "televisor", "televisión", "monitor"],
    }

    tokens = set(re.findall(r"[a-z0-9]+", text))
    for key, syns in synonyms.items():
        if any(s in text for s in syns):
            tokens.add(key)

    clauses = ["status:active"]

    # SKU explícito
    msku = re.findall(r"\b[A-Z0-9]{3,}(?:[-_][A-Z0-9]+)+\b", (keyword or "").upper())
    if msku:
        sku = msku[0]
        clauses.append(f"(sku:{sku}*)")

    # pulgadas
    minch = re.findall(r"(\d{2,3})\s*(?:pulg|pulgadas|pulgada|in|\")", text)
    if minch:
        n = minch[0]
        clauses.append(f"(title:{n}* OR body:{n}* OR tag:{n}*)")

    # VESA (e.g., 600x400)
    mvesa = re.findall(r"(\d{2,4})\s*[xX]\s*(\d{2,4})", text)
    if mvesa:
        a,b = mvesa[0]
        pat = f"{a}x{b}"
        clauses.append(f"(title:{pat} OR body:{pat} OR tag:{pat})")

    # palabras generales
    if tokens:
        ors = []
        for t in sorted(tokens):
            ors.append(f"(title:{t}* OR sku:{t}* OR tag:{t}* OR product_type:{t}* OR vendor:{t}* OR body:{t}*)")
        clauses.append("(" + " AND ".join(ors) + ")")

    return " AND ".join(clauses)

def _graphql_product_search(store: str, headers: dict, query: str, limit: int = 40):
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
    nodes = [e.get("node") for e in edges if isinstance(e, dict)]
    return nodes

def _node_to_card(node: dict, store_public: str) -> dict:
    # image url
    img = ""
    if node.get("featuredImage"):
        img = node["featuredImage"].get("url") or ""
    if not img:
        edges = ((node.get("images") or {}).get("edges")) or []
        if edges:
            img = (edges[0].get("node") or {}).get("url") or ""

    # first variant
    v_edges = ((node.get("variants") or {}).get("edges")) or []
    v0 = v_edges[0].get("node") if v_edges else {}
    raw_price = v0.get("price")
    price = raw_price.get("amount") if isinstance(raw_price, dict) else (str(raw_price) if raw_price is not None else "N/A")
    sku = v0.get("sku") or ""
    # variant id numeric
    m = re.search(r"(\d+)$", str(v0.get("id") or ""))
    variant_id = m.group(1) if m else v0.get("id") or ""

    handle = node.get("handle") or ""
    link = f"https://{store_public}/products/{handle}" if handle else f"https://{store_public}"

    return {
        "id": node.get("id"),
        "title": node.get("title") or "",
        "type": node.get("productType") or "",
        "price": price or "N/A",
        "image": img,
        "link": link,
        "body_html": node.get("descriptionHtml") or "",
        "variant_id": variant_id,
        "sku": sku,
        "vendor": node.get("vendor") or "",
        "tags": node.get("tags") or [],
        "handle": handle,
        "_in_stock": True if v0.get("availableForSale") is True else False
    }


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
#   Utilidades varias
# ============================

def _norm(s: str) -> str:
    s = s or ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()

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
        src = image_obj.get("src") or image_obj.get("url") 
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
    return variants[0] if variants else {}

def _map_product(rest_product: dict, store: str) -> dict:
    """
    Normaliza REST → forma que espera el frontend.
    """
    v = _safe_first_variant(rest_product)
    handle = rest_product.get("handle") or ""
    # Dominio público para el enlace
    public = "master.com.mx" if store == SHOPIFY_STORE_COM_MX else "master.mx"
    return {
        "id": rest_product.get("id"),
        "title": rest_product.get("title") or "",
        "type": rest_product.get("product_type") or "",
        "price": v.get("price") or v.get("compare_at_price") or "N/A",
        "image": _choose_image_src(rest_product) or "",
        "link": f"https://{public}/products/{handle}" if handle else f"https://{public}",
        "body_html": rest_product.get("body_html") or "",
        "variant_id": v.get("id") or "",
        "sku": v.get("sku") or "",
        "vendor": rest_product.get("vendor") or "",
        "tags": rest_product.get("tags") or "",
        "handle": handle,
        "_in_stock": (isinstance(v.get("inventory_quantity"), int) and v.get("inventory_quantity") > 0)
    }

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
        raw = _fetch_products_multi(store, headers, limit=max(10, int(limit)))
        if not raw:
            return []
        out = []
        for rp in raw:
            if require_photo and not _has_photo(rp):
                continue
            out.append(_map_product(rp, store))
        return out[:int(limit)]
    except Exception as e:
        print(f"[❌ Error en get_products] {e}")
        return []

def _simple_keyword_match(keyword: str, origin=None, require_photo=False):
    """
    Modo simple: usa una página de productos y hace matching local.
    (Solo como fallback si GraphQL no está disponible).
    """
    store, headers = get_shopify_context(origin)
    raw = _fetch_products_multi(store, headers, limit=200)
    if not raw:
        return []
    mapped = []
    for p in raw:
        if require_photo and not _has_photo(p):
            continue
        mapped.append(_map_product(p, store))

    # matching local
    keyword = (keyword or "").lower().strip()
    if not keyword:
        return mapped[:20]

    encontrados = []
    for p in mapped:
        title = (p.get("title") or "").lower()
        body = (p.get("body_html") or "").lower()
        sku  = (p.get("sku") or "").lower()
        vendor = (p.get("vendor") or "").lower()
        tags = (p.get("tags") or "")
        if isinstance(tags, list):
            tags = " ".join(tags)
        tags = str(tags).lower()
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
#   Heurísticas de categorías
# ============================

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
        p.get("tags","") if isinstance(p.get("tags"), str) else " ".join(p.get("tags") or [])
    ]))
    keys = {"sensor","agua","tinaco","cisterna","nivel","bluetooth","conect","iot","water"}
    return any(k in t for k in keys)

def _looks_like_category(p: Dict[str, Any], hint: Optional[str]) -> bool:
    if not hint:
        return True
    if hint == "support":
        return _looks_like_support(p)
    if hint == "sensor_water":
        return _looks_like_sensor_water(p)
    return True

def _matches_all_meta(p: Dict[str, Any], tokens: List[str]) -> bool:
    if not tokens:
        return True
    blob = _norm(" ".join([
        p.get("title",""),
        p.get("type",""),
        p.get("vendor",""),
        p.get("sku",""),
        p.get("handle",""),
        p.get("tags","") if isinstance(p.get("tags"), str) else " ".join(p.get("tags") or []),
        p.get("body_html","") or ""
    ]))
    return all(t.lower() in blob for t in tokens)

def _matches_any_meta(p: Dict[str, Any], raw_clauses: List[str]) -> bool:
    if not raw_clauses:
        return True
    blob = _norm(" ".join([
        p.get("title",""),
        p.get("type",""),
        p.get("vendor",""),
        p.get("sku",""),
        p.get("handle",""),
        p.get("tags","") if isinstance(p.get("tags"), str) else " ".join(p.get("tags") or []),
        p.get("body_html","") or ""
    ]))
    # cada item en raw_clauses lo tratamos como un fragmento a buscar
    for clause in raw_clauses:
        clause = clause.strip().lower()
        if not clause:
            continue
        # si hay "x*y" tipo 600x400, solo busca literal
        if clause in blob:
            return True
    return False

def _compatible_with_inches(p: Dict[str, Any], inches: int) -> bool:
    """
    Regla simple: si el producto menciona N pulgadas y N >= inches.
    """
    blob = _norm(" ".join([p.get("title",""), p.get("body_html","") or "", p.get("tags","") if isinstance(p.get("tags"), str) else " ".join(p.get("tags") or [])]))
    # buscar 80, 80", 80 pulgadas
    pats = [
        rf"\b{inches}\b",
        rf"\b{inches}\"",
        rf"\b{inches}\s*(pulg|pulgadas|pulgada|in)\b"
    ]
    for pat in pats:
        if re.search(pat, blob):
            return True
    return False


# ============================
#   Búsqueda avanzada local (retrocompatible)
# ============================

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
            p.get("tags","") if isinstance(p.get("tags"), str) else " ".join(p.get("tags") or []),
            p.get("type",""),
            p.get("vendor",""),
            p.get("sku",""),
            p.get("handle",""),
        ])

        if exclude and any(x.lower() in meta_blob.lower() for x in exclude):
            continue

        if must_include and not _matches_all_meta(p, must_include):
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
    """
    Búsqueda de alta precisión.
    - Si hay keyword, usamos Admin GraphQL con query enriquecida (sinónimos, pulgadas, VESA, SKU).
    - Si falla GraphQL, caemos al flujo previo.
    - Retornamos en el formato que espera el frontend (image url string, price string, variant_id numérico).
    """
    advanced_keys = {
        "must_include", "must_match_any_of", "exclude",
        "prefer_in_stock", "inches_target", "min_score", "limit", "category_hint"
    }

    store, headers = get_shopify_context(origin)
    public_domain = "master.com.mx" if store == SHOPIFY_STORE_COM_MX else "master.mx"

    # 1) GraphQL when keyword provided
    if (keyword or "").strip():
        try:
            q = _build_admin_query(keyword)
            nodes = _graphql_product_search(store, headers, q, limit=int(kwargs.get("limit", 40)))
            products = [_node_to_card(n, public_domain) for n in nodes]
            if require_photo:
                products = [p for p in products if p.get("image")]
            # Simple scoring by token overlap with keyword and synonyms
            qtok = set(re.findall(r"[a-z0-9]+", (keyword or "").lower()))
            def sc(p):
                hay = " ".join([
                    str(p.get("title","")).lower(),
                    str(p.get("sku","")).lower(),
                    str(p.get("vendor","")).lower(),
                    " ".join(p.get("tags") if isinstance(p.get("tags"), list) else [str(p.get("tags",""))]).lower(),
                    str(p.get("body_html","")).lower(),
                ])
                s=0.0
                for t in qtok:
                    if t in p.get("title","").lower(): s+=3
                    if t and t in p.get("sku","").lower(): s+=4
                    if t in hay: s+=1
                if p.get("_in_stock"): s+=0.5
                return -s
            products.sort(key=sc)
            limit = int(kwargs.get("limit", 20))
            return products[:limit]
        except Exception as e:
            print(f"[WARN] GraphQL search failed: {e}. Falling back to legacy search.")

    # 2) Legacy behavior (retrocompatible)
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
            category_hint=kwargs.get("category_hint")
        )

    # default simple list + local filter
    return _simple_keyword_match(keyword, origin=origin, require_photo=require_photo)


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
        print(f"[DEBUG] Inventario para variant_id={variant_id} en {store}")

        variant_url = f"https://{store}/admin/api/{API_VERSION}/variants/{variant_id}.json"
        variant_res = requests.get(variant_url, headers=headers, timeout=20)
        variant_res.raise_for_status()
        inventory_item_id = (variant_res.json() or {}).get("variant", {}).get("inventory_item_id")

        levels_url = f"https://{store}/admin/api/{API_VERSION}/inventory_levels.json?inventory_item_ids={inventory_item_id}"
        levels_res = requests.get(levels_url, headers=headers, timeout=20)
        levels_res.raise_for_status()
        inventory_levels = (levels_res.json() or {}).get("inventory_levels", []) or []

        # Obtener nombres de locations
        locs_url = f"https://{store}/admin/api/{API_VERSION}/locations.json?limit=250"
        locs_res = requests.get(locs_url, headers=headers, timeout=20)
        locs_res.raise_for_status()
        locations = {l.get("id"): l.get("name") for l in (locs_res.json() or {}).get("locations", [])}

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
    if not description:
        return None
    match = re.search(r'(https?://[^\s"\']+\.pdf)', description)
    return match.group(1) if match else None
