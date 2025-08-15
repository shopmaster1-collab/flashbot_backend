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
MAX_PAGES = int(os.getenv("SEARCH_MAX_PAGES", "10"))           # 10 páginas * 250 = 2500 items
COLLECT_MAX = int(os.getenv("SEARCH_COLLECT_MAX", "40"))       # cortamos cuando tengamos 40 matches
INCLUDE_DIAGNOSTIC = os.getenv("INCLUDE_DIAGNOSTIC", "false").lower() == "true"

app = Flask(__name__)
CORS(app, origins=_CORS_ORIGINS, supports_credentials=True)

# ---------- Utilidades seguras de mapeo ----------

def _safe_first_variant(product: dict) -> dict:
    variants = product.get("variants") or []
    return variants[0] if isinstance(variants, list) and variants else {}

def _choose_image_src(product: dict) -> str:
    # Igual que en integraciones: intenta product.image, luego product.images[]
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

# ---------- Helpers de texto ----------

def _tokens(text: str) -> list:
    parts = re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ0-9\-]+", text or "")
    toks = [p.lower() for p in parts if len(p) >= 2]
    # único preservando orden
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out

def _hay_match(tokens: list, p: dict) -> bool:
    """
    True si ALGÚN token aparece en cualquiera de los campos buscados.
    Campos: title, body_html, sku, vendor, tags, type, handle
    """
    fields = [
        (p.get("title") or "").lower(),
        (p.get("body_html") or "").lower(),
        (p.get("sku") or "").lower(),
        (p.get("vendor") or "").lower(),
        (p.get("tags") or "").lower(),
        (p.get("type") or "").lower(),
        (p.get("handle") or "").lower(),
    ]
    for t in tokens:
        for f in fields:
            if t in f:
                return True
    return False

# ---------- Fallback multi-estrategia + paginación + filtro por foto ----------

def _parse_next_page_info(link_header: str) -> str | None:
    """
    Extrae page_info del Link header (rel="next") de Shopify.
    """
    if not link_header:
        return None
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            m = re.search(r"page_info=([^&>]+)", part)
            if m:
                return m.group(1)
    return None

def _fetch_products_paginated_filtered(store: str, headers: dict, base_url: str,
                                       per_page: int = 250, max_pages: int = 10,
                                       filter_fn=None, collect_max: int = 40):
    """
    Descarga varias páginas usando page_info y aplica:
      - Filtro por foto (implícito en filter_fn).
      - Corte temprano (collect_max).
    Devuelve (collected, attempts).
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
            # Mapeo uno a uno y filtro "sin foto" ANTES de aplicar filter_fn (por seguridad).
            mapped_page = []
            for rp in raw:
                if not _has_photo_raw(rp):
                    continue  # descarta sin foto
                mp = _map_product_for_cards(rp, store_domain=store)
                mapped_page.append(mp)

            # Aplica filtro de texto si existe
            if filter_fn:
                mapped_page = [m for m in mapped_page if filter_fn(m)]

            attempts.append({"url": final_url, "status": code, "count": len(mapped_page), "page": page})
            collected.extend(mapped_page)

            if len(collected) >= collect_max:
                break

            # Página siguiente
            link_header = resp.headers.get("Link") or resp.headers.get("link") or ""
            next_pi = _parse_next_page_info(link_header)
            if not next_pi:
                break
            page_info = next_pi

        except Exception as e:
            attempts.append({"url": final_url, "status": "exception", "count": 0, "error": str(e), "page": page})
            break

    return collected, attempts

def _fetch_products_multi(origin: str, tokens: list, collect_max: int = COLLECT_MAX, max_pages: int = MAX_PAGES):
    """
    Llama a Shopify probando varias combinaciones y paginando.
    Retorna (products, attempts), ya filtrados por:
      - Tiene foto
      - Hace match con los tokens (OR)
    """
    store, headers = get_shopify_context(origin=origin)

    base_urls = [
        f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json",                     # (esta te devuelve datos hoy)
        f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json?status=any",
        f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json?published_status=any",
    ]

    all_attempts = []
    per_page = 250
    filter_fn = (lambda m: _hay_match(tokens, m)) if tokens else None

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

    # Si ninguna variante trajo resultados
    return [], all_attempts

def _shopify_fallback_search(user_text: str, origin: str,
                             collect_max: int = COLLECT_MAX, max_pages: int = MAX_PAGES):
    """
    Fallback literal: tokeniza y busca 'contains' (OR) sobre:
    title, body_html, sku, vendor, tags, product_type, handle.
    Paginado con corte temprano y filtrado por foto.
    Retorna (resultados, attempts).
    """
    toks = _tokens(user_text)
    if not toks:
        return [], []
    return _fetch_products_multi(origin=origin, tokens=toks, collect_max=collect_max, max_pages=max_pages)

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

        # 1) Búsqueda original por keywords vía integración (ya con require_photo en origen si deseas)
        keywords = extract_keywords_from_text(user_message)
        print(f"[DEBUG] Keywords: {keywords} | Origin: {origin}")

        encontrados = []
        for kw in keywords:
            try:
                # Nota: si quieres forzar que esta capa también filtre por foto, pasa require_photo=True
                encontrados.extend(get_shopify_products(kw, origin=origin, require_photo=True))
            except Exception as e:
                print(f"[WARN] get_shopify_products('{kw}') falló: {e}")

        # Normaliza estructura si viene del integrador
        productos = []
        for p in encontrados:
            handle = ""
            if p.get("link"):
                handle = p["link"].split("/products/")[-1]
            # Descarta sin foto por seguridad
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

        # 2) Fallback literal (paginado + corte temprano + sin foto)
        attempts = []
        if not productos:
            print("[DEBUG] Sin resultados por keywords; usando fallback paginado (tokens + filtro por foto)...")
            productos, attempts = _shopify_fallback_search(
                user_text=user_message,
                origin=origin,
                collect_max=COLLECT_MAX,
                max_pages=MAX_PAGES
            )

        if not productos:
            meta = {"domain": origin or "desconocido", "ip": request.remote_addr}
            if INCLUDE_DIAGNOSTIC:
                meta["diagnostic"] = attempts
            return jsonify({
                "success": True,
                "response": "No encontré resultados para esa búsqueda. ¿Quieres intentar con otra palabra clave?",
                "meta": meta
            })

        # Evita duplicados por título
        vistos = set()
        unicos = []
        for p in productos:
            t = p.get("title") or ""
            if t not in vistos:
                vistos.add(t)
                unicos.append(p)

        # Dominio público para links visibles
        domain_for_links = "master.mx"
        if "master.com.mx" in (origin or ""):
            domain_for_links = "master.com.mx"

        # Render tarjetas (ya sólo llegan con foto)
        cards = []
        for p in unicos[:5]:
            if not p.get("image"):  # extra guard
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
            # Inventario por sucursal
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
        # ya filtramos por foto aquí también para consistencia visual
        productos = get_products(limit=5, origin=origin, require_photo=True)
        return jsonify({"success": True, "productos": productos})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/debug/raw", methods=["GET"])
def debug_products_raw():
    """
    Diagnóstico crudo paginado con corte temprano y filtro por foto.
    Parámetros:
      - limit_total (opcional): tope de coincidencias a recolectar (por defecto, COLLECT_MAX * 2)
    """
    try:
        origin = request.headers.get("Origin", "")
        try:
            limit_total = int(request.args.get("limit_total", str(COLLECT_MAX * 2)))
        except Exception:
            limit_total = COLLECT_MAX * 2

        # Para inspección, no hacemos match por tokens: sólo juntamos con foto
        # usando _fetch_products_paginated_filtered con filter_fn=None
        store, headers = get_shopify_context(origin=origin)
        base = f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/products.json"
        products, attempts = _fetch_products_paginated_filtered(
            store=store,
            headers=headers,
            base_url=base,
            per_page=250,
            max_pages=MAX_PAGES,
            filter_fn=None,            # sin filtro de texto
            collect_max=limit_total    # sólo tope por cantidad
        )
        # 'products' ya llega sin foto filtrada y mapeada
        return jsonify({
            "success": True,
            "origin": origin or "no origin",
            "count": len(products),
            "attempts": attempts,
            "productos": products[:20]  # muestra 20 para no saturar
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
