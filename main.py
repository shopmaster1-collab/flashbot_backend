from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re
import os

# ---- Integraciones existentes ----
from integrations.shopify_api import (
    get_shopify_products,
    get_products,
    get_inventory_by_variant_id,
    get_product_details,
    extract_manual_url,
    get_shopify_context,
)

# API version (usa la de tu integración si existe)
try:
    from integrations.shopify_api import API_VERSION as SHOPIFY_API_VERSION
except Exception:
    SHOPIFY_API_VERSION = "2024-04"

# NLP (con fallback simple si el módulo no está)
try:
    from utils.nlp_tools import extract_keywords_from_text
except Exception:
    try:
        from nlp_tools import extract_keywords_from_text
    except Exception:
        def extract_keywords_from_text(text: str):
            words = re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ0-9\-]+", text or "")
            return [w.lower() for w in words if len(w) > 2]

# CORS centralizado (si config.py existe)
try:
    from config import ALLOWED_ORIGINS
    _CORS_ORIGINS = ALLOWED_ORIGINS
except Exception:
    _CORS_ORIGINS = [
        "https://master.mx", "https://www.master.mx",
        "https://master.com.mx", "https://www.master.com.mx"
    ]

# Parámetros de búsqueda tunables por ENV
MAX_PAGES = int(os.getenv("SEARCH_MAX_PAGES", "20"))           # por defecto 20 páginas * 250 = 5000 items
COLLECT_MAX = int(os.getenv("SEARCH_COLLECT_MAX", "40"))       # cortamos cuando tengamos 40 matches útiles
INCLUDE_DIAGNOSTIC = os.getenv("INCLUDE_DIAGNOSTIC", "false").lower() == "true"
MIN_TOKENS_MATCH = int(os.getenv("SEARCH_MIN_TOKENS_MATCH", "2"))  # cuántos tokens distintos deben aparecer

app = Flask(__name__)
CORS(app, origins=_CORS_ORIGINS, supports_credentials=True)

# ---------- Utilidades seguras de mapeo / normalización ----------

_ACCENTS = str.maketrans("áéíóúüÁÉÍÓÚÜñÑ", "aeiouuAEIOUUnN")
STOPWORDS = set("""
a al algo algun alguna algunos algunas ante antes como con contra cual cuales cuando de del desde donde dos el la los las un una unos unas en entre es esa eso esas esos esta este esto estas estos hay hasta hacia la lo los las le les mas menos mi mis muy no nos o os para pero por que se sin sobre su sus te tu tus y o en un/una unos/unas este esta estos estas aquel aquella aquellos aquellas esa ese esos esas esta ese estas estos
""".split())

def _normalize(s: str) -> str:
    return (s or "").translate(_ACCENTS).lower()

def _safe_first_variant(product: dict) -> dict:
    variants = product.get("variants") or []
    return variants[0] if isinstance(variants, list) and variants else {}

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

def _has_photo_raw(product: dict) -> bool:
    return bool(_choose_image_src(product))

def _map_product_for_cards(p: dict, store_domain: str) -> dict:
    v = _safe_first_variant(p)
    return {
        "id": p.get("id"),
        "title": p.get("title") or "",
        "type": p.get("product_type") or "",
        "price": v.get("price", "N/A"),
        "image": _choose_image_src(p),  # robusto
        "handle": (p.get("handle") or ""),
        "admin_link": f"https://{store_domain}/products/{p.get('handle') or ''}",
        "body_html": (p.get("body_html") or ""),
        "sku": (v.get("sku") or ""),
        "vendor": p.get("vendor") or "",
        "tags": p.get("tags") or "",
        "variant_id": v.get("id") or 0,
    }

# ---------- Helpers de texto / tokens / sinónimos ----------

SYNONYMS = {
    "router": ["ruteador", "access point", "ap", "wifi", "wi-fi", "inalambrico", "inalámbrico"],
    "soporte": ["bracket", "montaje", "montura", "base", "holder", "wall mount", "vesa"],
    "pantalla": ["tv", "televisor", "television", "monitor", "pantallas", "smart tv"],
    "camara": ["cámara", "seguridad", "ip", "cctv", "dvr", "nvr"],
}

def _tokens(text: str) -> list:
    parts = re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ0-9\-]+", text or "")
    toks = []
    seen = set()
    for p in parts:
        n = _normalize(p)
        if len(n) < 2 or n in STOPWORDS:
            continue
        if n not in seen:
            toks.append(n); seen.add(n)
        # sinónimos
        for syn in SYNONYMS.get(n, []):
            ns = _normalize(syn)
            if ns not in seen:
                toks.append(ns); seen.add(ns)
    return toks

def _score_product(tokens: list, p: dict) -> tuple[int, int]:
    """
    Devuelve (score, matched_tokens_count).
    Pondera campos para mejorar la precisión y exige mínimo de tokens.
    """
    # Campos normalizados
    title = _normalize(p.get("title"))
    body = _normalize(p.get("body_html"))
    sku = _normalize(p.get("sku"))
    vendor = _normalize(p.get("vendor"))
    tags = _normalize(p.get("tags"))
    ptype = _normalize(p.get("type"))
    handle = _normalize(p.get("handle"))

    fields = {
        "title": (title, 5),
        "handle": (handle, 4),
        "tags": (tags, 3),
        "type": (ptype, 3),
        "sku": (sku, 2),
        "body": (body, 1),
        "vendor": (vendor, 1),
    }

    matched_tokens = 0
    score = 0
    seen_tok = set()
    for t in tokens:
        present = False
        for (txt, w) in fields.values():
            if t and txt and t in txt:
                score += w
                present = True
        if present and t not in seen_tok:
            matched_tokens += 1
            seen_tok.add(t)

    # Bonus por frase (tokens unidos) en título/handle/tags
    if tokens:
        phrase = " ".join(tokens)
        for key in ("title", "handle", "tags"):
            txt, w = fields[key]
            if phrase and txt and phrase in txt:
                score += 8

    return score, matched_tokens

def _fetch_products_paginated_filtered(store: str, headers: dict, base_url: str,
                                       per_page: int = 250, max_pages: int = 20,
                                       filter_fn=None, collect_max: int = 40):
    """
    Descarga páginas usando page_info y aplica:
      - Filtro por foto
      - Filtro/score por texto (filter_fn)
      - Corte temprano (collect_max)
    Devuelve (collected, attempts)
    """
    attempts = []
    collected = []

    sep = "&" if "?" in base_url else "?"
    url_base = f"{base_url}{sep}limit={per_page}"

    page = 0
    page_info = None

    while page < max_pages:
        page += 1
        try:
            final_url = url_base if not page_info else f"{base_url}{sep}limit={per_page}&page_info={page_info}"
            resp = requests.get(final_url, headers=headers, timeout=25)
            code = resp.status_code
            if code != 200:
                attempts.append({"url": final_url, "status": code, "count": 0, "note": "non-200", "page": page})
                break

            payload = resp.json() or {}
            raw = payload.get("products") or []

            # Mapeo y filtro sin-foto
            page_items = []
            for rp in raw:
                if not _has_photo_raw(rp):
                    continue
                mp = _map_product_for_cards(rp, store_domain=store)
                page_items.append(mp)

            # Aplica filtro/score si procede
            if filter_fn:
                page_items = filter_fn(page_items)

            attempts.append({"url": final_url, "status": code, "count": len(page_items), "page": page})
            collected.extend(page_items)

            if len(collected) >= collect_max:
                break

            link_header = resp.headers.get("Link") or resp.headers.get("link") or ""
            m = re.search(r'page_info=([^&>]+).*?rel="next"', link_header)
            if not m:
                break
            page_info = m.group(1)

        except Exception as e:
            attempts.append({"url": final_url, "status": "exception", "count": 0, "error": str(e), "page": page})
            break

    return collected, attempts

def _shopify_fallback_search(user_text: str, origin: str,
                             collect_max: int = COLLECT_MAX, max_pages: int = MAX_PAGES):
    """
    Tokeniza, expande sinónimos, filtra por foto y pagina con corte temprano.
    Exige que al menos MIN_TOKENS_MATCH tokens distintos hagan match (mejor precisión).
    Rankea por score ponderado y devuelve top-N.
    """
    tokens = _tokens(user_text)
    if not tokens:
        return [], []

    store, headers = get_shopify_context(origin=origin)
    base_urls = [
        f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json",
        f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json?status=any",
        f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json?published_status=any",
    ]

    all_attempts = []
    per_page = 250

    # filter_fn hará score y threshold de tokens
    def filter_fn(items):
        scored = []
        for p in items:
            score, matched = _score_product(tokens, p)
            if matched >= MIN_TOKENS_MATCH and score > 0:
                scored.append((score, p))
        # ordena por score desc
        scored.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in scored]

    for base in base_urls:
        products, attempts = _fetch_products_paginated_filtered(
            store=store,
            headers=headers,
            base_url=base,
            per_page=per_page,
            max_pages=max_pages,
            filter_fn=filter_fn,
            collect_max=collect_max,
        )
        all_attempts.extend(attempts)
        if products:
            return products, all_attempts

    return [], all_attempts

# ---------- Detección de intención: SOPORTES/BRACKETS ----------

SUPPORT_SYNONYMS = {
    "soporte","soportes","bracket","montaje","montura","base","holder",
    "pared","techo","articulado","inclinable","esquinero","vesa","tv mount","wall mount"
}
EXCLUDE_FOR_SUPPORT = {
    "antena","antenas","adaptador","adaptadores","cable","cables","hdmi",
    "displayport","vga","convertidor","conversor","decodificador","sintonizador"
}

def detect_support_intent(text: str) -> bool:
    t = _normalize(text)
    # Debe mencionar soporte/bracket o vesa/pared/techo y algún término de pantalla/tv
    has_core = any(k in t for k in SUPPORT_SYNONYMS) or "soporte" in t or "bracket" in t
    has_tv = any(k in t for k in ("tv","televisor","pantalla","smart tv","monitor","vesa"))
    return bool(has_core or ("soporte" in t and has_tv))

def extract_inches(text: str):
    t = _normalize(text)
    m = re.search(r'(\d{2,3})\s*(?:["”]|pulg|pulgadas|in)\b', t)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None

def _looks_like_support_product(p: dict) -> bool:
    blob = _normalize(" ".join([
        p.get("title",""), p.get("type",""), p.get("vendor",""),
        p.get("tags","") if isinstance(p.get("tags"), str) else " ".join(p.get("tags") or []),
        p.get("body_html","") or ""
    ]))
    if any(bad in blob for bad in EXCLUDE_FOR_SUPPORT):
        return False
    keys = {"soporte","bracket","vesa","pared","techo","articulado","inclinable","esquinero","tv"}
    return any(k in blob for k in keys)

def _compatible_with_inches_local(p: dict, target: int) -> bool:
    if not target:
        return True
    blob = _normalize(" ".join([
        p.get("title",""), p.get("body_html","") or "",
        p.get("tags","") if isinstance(p.get("tags"), str) else " ".join(p.get("tags") or [])
    ]))
    # rangos 32-70 / 26–55
    for m in re.finditer(r'(\d{2,3})\s*[-–]\s*(\d{2,3})', blob):
        a, b = int(m.group(1)), int(m.group(2))
        if a <= target <= b:
            return True
    # "hasta 70"
    for m in re.finditer(r'(?:hasta|max(?:imo)?)\s+(\d{2,3})', blob):
        if target <= int(m.group(1)):
            return True
    # valores sueltos “para 45”
    for m in re.finditer(r'(?:para|de)\s+(\d{2,3})\s*(?:["”]|pulg|pulgadas|in)?\b', blob):
        if target == int(m.group(1)):
            return True
    # si no declara, no descartamos
    return True

def filter_support_products_locally(items: list, inches: int | None) -> list:
    out = []
    for p in items:
        if not _looks_like_support_product(p):
            continue
        if inches and not _compatible_with_inches_local(p, inches):
            # si parece incompatible, lo descartamos
            continue
        out.append(p)
    return out

# ---------- Rutas ----------

@app.route("/")
def home():
    return "✅ Chatbot backend activo."

@app.route("/chat", methods=["POST", "OPTIONS"])
def chat():
    if request.method == "OPTIONS":
        return "", 200

    try:
        data = request.get_json(force=True)
        user_message = (data.get("message") or "").strip()
        origin = data.get("origin") or request.headers.get("Origin", "")

        if not user_message:
            return jsonify({"success": False, "error": "Mensaje vacío"}), 400

        # --- Detección de intención SOPORTE / pulgadas ---
        is_support = detect_support_intent(user_message)
        inches = extract_inches(user_message)

        # 1) Búsqueda por integración (con require_photo)
        keywords = extract_keywords_from_text(user_message)
        print(f"[DEBUG] Keywords: {keywords} | Origin: {origin} | intent_support={is_support} | inches={inches}")

        encontrados = []

        # Si la intención es SOPORTE, intentamos pasar filtros avanzados a la integración.
        if is_support:
            try:
                encontrados = get_shopify_products(
                    user_message,
                    origin=origin,
                    require_photo=True,
                    # kwargs opcionales (si tu integración los soporta, se aplican)
                    must_include=list(SUPPORT_SYNONYMS),
                    must_match_any_of=[
                        "product_type:soporte","product_type:bracket",
                        "collection:soportes","tag:soporte","tag:bracket"
                    ],
                    exclude=list(EXCLUDE_FOR_SUPPORT),
                    prefer_in_stock=True,
                    inches_target=inches,
                    min_score=0.35
                )
                print("[DEBUG] get_shopify_products con filtros avanzados aplicado.")
            except TypeError:
                # Integración no soporta kwargs: caemos a keywords y filtrado local
                print("[WARN] Integración no soporta kwargs avanzados; usando keywords + filtro local.")
                for kw in keywords:
                    try:
                        encontrados.extend(get_shopify_products(kw, origin=origin, require_photo=True))
                    except Exception as e:
                        print(f"[WARN] get_shopify_products('{kw}') falló: {e}")
                # filtro local a soportes
                encontrados = filter_support_products_locally(encontrados, inches)
        else:
            # flujo normal sin intención específica
            for kw in keywords:
                try:
                    encontrados.extend(get_shopify_products(kw, origin=origin, require_photo=True))
                except Exception as e:
                    print(f"[WARN] get_shopify_products('{kw}') falló: {e}")

        productos = []
        for p in encontrados:
            handle = ""
            if p.get("link"):
                handle = p["link"].split("/products/")[-1]
            if not p.get("image"):
                continue
            productos.append({
                "id": p.get("id"),
                "title": p.get("title") or "",
                "type": p.get("type") or "",
                "price": p.get("price", "N/A"),
                "image": p.get("image", ""),
                "handle": handle,
                "admin_link": p.get("link", ""),
                "body_html": (p.get("body_html") or ""),
                "sku": p.get("sku", ""),
                "vendor": p.get("vendor", ""),
                "tags": p.get("tags", ""),
                "variant_id": p.get("variant_id", 0),
            })

        attempts = []
        if not productos:
            print("[DEBUG] Sin resultados por keywords; usando fallback paginado con scoring y filtro por foto...")
            productos, attempts = _shopify_fallback_search(
                user_text=user_message,
                origin=origin,
                collect_max=COLLECT_MAX,
                max_pages=MAX_PAGES
            )
            # Si la intención es SOPORTE, aplicar filtro local sobre el fallback
            if is_support and productos:
                productos = filter_support_products_locally(productos, inches)

        if not productos:
            meta = {"domain": origin or "desconocido", "ip": request.remote_addr}
            if INCLUDE_DIAGNOSTIC:
                meta["diagnostic"] = attempts
            return jsonify({
                "success": True,
                "response": "No encontré resultados para esa búsqueda. ¿Quieres intentar con otra palabra clave?",
                "meta": meta
            })

        # Evitar duplicados por título
        vistos = set()
        unicos = []
        for p in productos:
            t = p.get("title") or ""
            if t not in vistos:
                vistos.add(t)
                unicos.append(p)

        domain_for_links = "master.mx"
        if "master.com.mx" in (origin or ""):
            domain_for_links = "master.com.mx"

        cards = []
        for p in unicos[:5]:
            if not p.get("image"):
                continue
            title = p["title"]
            price = p.get("price", "N/A")
            image = p.get("image", "")
            handle = p.get("handle") or ""
            variant_id = p.get("variant_id", 0)
            product_id = p.get("id")

            url = f"https://{domain_for_links}/products/{handle}" if handle else f"https://{domain_for_links}"
            checkout_url = f"https://{domain_for_links}/cart/{variant_id}:1" if variant_id else f"https://{domain_for_links}/cart"

            # Manual de producto
            manual_url = None
            try:
                body_html = p.get("body_html") or ""
                if not body_html and product_id:
                    details = get_product_details(product_id, origin=origin)
                    body_html = (details.get("body_html") or "")
                manual_url = extract_manual_url(body_html)
            except Exception as e:
                print(f"[WARN] extract_manual_url falló: {e}")

            card_html = f"""
            <div style="display:flex;align-items:center;margin-bottom:12px;border-bottom:1px solid #ddd;padding-bottom:8px">
              <img src="{image}" alt="{title}" style="width:60px;height:60px;object-fit:cover;margin-right:12px;border-radius:8px">
              <div style="flex:1;">
                <div style="font-weight:bold">{title}</div>
                <div style="display:flex;align-items:center;gap:10px;margin:4px 0;">
                  <div style="color:#007bff;font-weight:bold">${price}</div>
                  <a href="{checkout_url}" target="_blank" style="background:#198754;color:#fff;padding:4px 10px;border-radius:6px;text-decoration:none;font-size:12px;">🛒 Comprar ahora</a>
                </div>
                <div style="display:flex;align-items:center;gap:10px;margin-top:4px;">
                  <a href="{url}" target="_blank" style="color:#007bff;text-decoration:underline;font-size:13px;">Ver producto</a>
                  {f'<a href="{manual_url}" target="_blank" style="color:#6c757d;text-decoration:underline;font-size:12px;">Manual de producto</a>' if manual_url else ''}
                </div>
            """
            # Inventario
            try:
                if variant_id:
                    inventario = get_inventory_by_variant_id(variant_id, origin=origin)
                    if inventario:
                        lista = "".join([f"<li>{i['sucursal']}: {i['cantidad']} disponibles</li>" for i in inventario])
                        card_html += f"<div style='font-size:12px;color:#007bff;margin-top:6px;'><strong>📦 Inventario:</strong><ul style='margin:4px 0 0 18px;padding:0;'>{lista}</ul></div>"
            except Exception as e:
                print(f"[WARN] inventario falló: {e}")

            card_html += "</div></div>"
            cards.append(card_html)

        html = "<div>🔍 Estos productos podrían interesarte:</div>" + "".join(cards)

        meta = {"domain": origin or "desconocido", "ip": request.remote_addr}
        if INCLUDE_DIAGNOSTIC:
            meta["diagnostic"] = attempts
        return jsonify({
            "success": True,
            "response": html,
            "meta": meta
        })

    except Exception as e:
        print(f"[ERROR /chat] {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/debug", methods=["GET"])
def debug_products():
    try:
        origin = request.headers.get("Origin", "")
        productos = get_products(limit=5, origin=origin, require_photo=True)
        return jsonify({"success": True, "productos": productos})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/debug/raw", methods=["GET"])
def debug_products_raw():
    """
    Diagnóstico crudo paginado: sólo aplica filtro de foto; no hace match de texto.
    Parámetro opcional: limit_total (por defecto COLLECT_MAX*2)
    """
    try:
        origin = request.headers.get("Origin", "")
        try:
            limit_total = int(request.args.get("limit_total", str(COLLECT_MAX * 2)))
        except Exception:
            limit_total = COLLECT_MAX * 2

        store, headers = get_shopify_context(origin=origin)
        base = f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json"
        products, attempts = _fetch_products_paginated_filtered(
            store=store,
            headers=headers,
            base_url=base,
            per_page=250,
            max_pages=MAX_PAGES,
            filter_fn=None,
            collect_max=limit_total
        )
        return jsonify({
            "success": True,
            "origin": origin or "no origin",
            "count": len(products),
            "attempts": attempts,
            "productos": products[:20]
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/inventario/<int:variant_id>", methods=["GET"])
def ver_inventario(variant_id):
    try:
        origin = request.headers.get("Origin", "")
        inventario = get_inventory_by_variant_id(variant_id, origin=origin)
        return jsonify({"success": True, "variant_id": variant_id, "inventario": inventario})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/debug/shopify-context", methods=["GET"])
def debug_shopify_context():
    try:
        origin = request.headers.get("Origin", "")
        store, headers = get_shopify_context(origin=origin)
        url = f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json?limit=1"
        response = requests.get(url, headers=headers, timeout=15)
        result = response.json() if response.status_code == 200 else {"status": response.status_code, "error": response.text}
        return jsonify({
            "success": True,
            "origin": origin or "no origin",
            "shopify_store": store,
            "token_used": "SHOPIFY_TOKEN_MASTER" if "master.com.mx" in (origin or "") else "SHOPIFY_TOKEN",
            "url": url,
            "response": result
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
