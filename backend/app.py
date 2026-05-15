# -*- coding: utf-8 -*-
import os, re, threading, time, html, io, csv
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

# --- internos
from .shopify_client import ShopifyClient
from .indexer import CatalogIndexer
try:
    from .catalog_intelligence import (
        analyze_query as _ci_analyze_query,
        build_search_queries as _ci_build_search_queries,
        apply_catalog_intelligence as _ci_apply_catalog_intelligence,
        build_catalog_answer as _ci_build_catalog_answer,
    )
except Exception as _ci_import_error:
    print(f"[WARN] catalog_intelligence disabled: {_ci_import_error}", flush=True)
    _ci_analyze_query = None
    _ci_build_search_queries = None
    _ci_apply_catalog_intelligence = None
    _ci_build_catalog_answer = None
try:
    from .utils import money  # si existe
except Exception:
    def money(x):  # fallback seguro
        if x is None:
            return None
        try:
            return f"${float(x):,.2f}"
        except Exception:
            return str(x)

# Deepseek opcional
try:
    from .deepseek_client import DeepseekClient
except Exception:
    DeepseekClient = None

# --- HTTP libs para Google Sheet
try:
    import requests
    from bs4 import BeautifulSoup
except Exception:
    requests = None
    BeautifulSoup = None

load_dotenv()
app = Flask(__name__)

# ---------- CORS ----------
_allowed = [o.strip() for o in (os.getenv("ALLOWED_ORIGINS") or "*").split(",") if o.strip()]
CORS(app, resources={
    r"/*": {
        "origins": _allowed,
        "allow_headers": ["Content-Type", "X-Admin-Secret"],
        "methods": ["GET", "POST", "OPTIONS"]
    }
})

# ---------- Servicios ----------
shop = ShopifyClient()
indexer = CatalogIndexer(shop, os.getenv("STORE_BASE_URL", "https://master.com.mx"))

CHAT_WRITER = (os.getenv("CHAT_WRITER") or "none").strip().lower()
deeps = None
if CHAT_WRITER == "deepseek" and DeepseekClient:
    try:
        deeps = DeepseekClient()
    except Exception:
        deeps = None

# Construcción inicial del índice (no caer si falla)
try:
    indexer.build()
except Exception as e:
    print(f"[WARN] Index build failed at startup: {e}", flush=True)

def _admin_ok(req) -> bool:
    return req.headers.get("X-Admin-Secret") == os.getenv("ADMIN_REINDEX_SECRET", "")

# =========================
#  Estáticos del widget
# =========================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
_WIDGET_CANDIDATES = [
    os.path.join(BASE_DIR, "widget"),
    os.path.join(os.path.dirname(BASE_DIR), "widget"),
    os.path.join(os.path.dirname(os.path.dirname(BASE_DIR)), "widget"),
]
WIDGET_DIR = next((p for p in _WIDGET_CANDIDATES if os.path.isdir(p)), _WIDGET_CANDIDATES[0])

@app.get("/widget/<path:filename>")
def serve_widget(filename):
    full = os.path.join(WIDGET_DIR, filename)
    if not os.path.isfile(full):
        return {"ok": False, "error": "not_found"}, 404
    resp = send_from_directory(WIDGET_DIR, filename)
    # En Shopify es mejor evitar caché agresivo durante correcciones del widget.
    # Además se puede usar ?v=YYYYMMDD_N en el script para forzar actualización.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

@app.get("/")
def home():
    return ("<h1>Maxter backend</h1>"
            "<p>OK ✅. Endpoints: "
            '<a href="/health">/health</a>, '
            '<code>POST /api/chat</code>, '
            '<code>POST /api/orders</code>, '
            '<code>POST /api/admin/reindex</code>, '
            '<code>GET /api/admin/stats</code>, '
            '<code>GET /api/admin/search?q=...</code>, '
            '<code>GET /api/admin/discards</code>, '
            '<code>GET /api/admin/products</code>, '
            '<code>GET /api/admin/diag</code>, '
            '<code>GET /api/admin/preview?q=...</code>, '
            '<code>GET /api/admin/orders-ping</code>, '
            '<code>GET /api/admin/orders-find?order=####</code>'
            "</p>")

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/api/health")
def api_health():
    return {"ok": True, "service": "flashbot"}

# ======================================================================
#  Utilidades de contexto/respuesta (Productos)  — (no modifican negocio)
# ======================================================================
_PAT_ONE_BY_N = re.compile(r"\b(\d+)\s*[x×]\s*(\d+)\b", re.IGNORECASE)

def _detect_patterns(q: str) -> dict:
    ql = (q or "").lower(); pat = {}
    m = _PAT_ONE_BY_N.search(ql)
    if m: pat["matrix"] = f"{m.group(1)}x{m.group(2)}"
    inch = re.findall(r"\b(1[9]|[2-9]\d|100)\b", ql)
    if inch: pat["inches"] = sorted(set(inch))
    cats = [k for k in ["hdmi","rca","coaxial","antena","soporte","control","cctv","vga","usb"] if k in ql]
    if cats: pat["cats"] = cats
    if any(w in ql for w in ["agua","nivel","cisterna","tinaco","boya","inundacion","inundación"]): pat["water"]=True
    if ("gas" in ql) or any(w in ql for w in ["tanque","estacionario","estacionaria","lp","propano","butano"]): pat["gas"]=True
    if any(w in ql for w in ["valvula","válvula"]): pat["valve"]=True
    if any(w in ql for w in ["ultra","ultrason","ultrasónico","ultrasonico"]): pat["ultra"]=True
    if any(w in ql for w in ["presion","presión"]): pat["pressure"]=True
    if "bluetooth" in ql: pat["bt"]=True
    if ("wifi" in ql) or ("app" in ql): pat["wifi"]=True
    if any(w in ql for w in ["pantalla","display"]): pat["display"]=True
    if "alarma" in ql: pat["alarm"]=True
    return pat

def _generate_contextual_answer(query: str, items: list, total_count: int, page: int, per_page: int) -> str:
    ql = (query or "").lower()
    p = _detect_patterns(query)
    product_type = None; brands = []; size_mentioned = None
    known_brands = ["sony", "samsung", "lg", "panasonic", "tcl", "hisense", "roku", "apple", "xiaomi"]
    for brand in known_brands:
        if brand in ql: brands.append(brand.capitalize())
    if any(w in ql for w in ["sensor","detector","medidor"]):
        product_type = "sensores de agua" if p.get("water") else ("sensores de gas" if p.get("gas") else "sensores")
    elif any(w in ql for w in ["control","remoto"]): product_type = "controles remotos"
    elif any(w in ql for w in ["soporte","bracket","mount"]): product_type = "soportes"
    elif any(w in ql for w in ["cable","cordon"]): product_type = "cables"
    elif any(w in ql for w in ["divisor","splitter"]): product_type = "divisores"
    elif any(w in ql for w in ["antena"]): product_type = "antenas"
    elif any(w in ql for w in ["camara","cámara"]): product_type = "cámaras"
    elif any(w in ql for w in ["bocina","altavoz","speaker"]): product_type = "bocinas"
    sizes = re.findall(r'\b(\d{1,3})\s*["\'"pulgadas]?\b', ql)
    if sizes: size_mentioned = sizes[0]
    response_parts = []
    if product_type == "sensores de gas":
        response_parts.append("¡Perfecto! Tenemos una excelente selección de sensores de gas")
        found_products = []
        product_titles = [item.get("title", "").lower() for item in items]
        for title in product_titles:
            if "electroválvula" in title or "válvula" in title: found_products.append("con válvula electrónica")
            elif "easy" in title and "gas" in title: found_products.append("con pantalla integrada")
            elif "connect" in title and "gas" in title: found_products.append("con monitoreo remoto")
            elif "iot" in title and "gas" in title: found_products.append("con WiFi y app Master IOT")
        if found_products: response_parts.append(" " + ", ".join(list(set(found_products))))
        else: response_parts.append(" para tanques estacionarios con diferentes características")
        additional_specs=[]
        if p.get("valve") or any(w in ql for w in ["valvula","válvula","electrovalvula"]): additional_specs.append("priorizando modelos con válvula electrónica automática")
        if p.get("wifi") or "app" in ql: additional_specs.append("con conectividad WiFi y monitoreo desde app")
        if p.get("display") or any(w in ql for w in ["pantalla","display"]): additional_specs.append("con pantalla integrada para lectura directa")
        if "alexa" in ql: additional_specs.append("compatibles con Alexa")
        if additional_specs: response_parts.append(", " + ", ".join(additional_specs))
    elif product_type == "sensores de agua":
        response_parts.append("¡Claro! Tenemos excelentes opciones en sensores de agua")
        specifics=[]
        if p.get("valve"): specifics.append("con válvula automática (IOT-WATERV)")
        if p.get("ultra"): specifics.append("ultrasónicos de alta precisión (IOT-WATERULTRA)")
        if not specifics: specifics.append("de nuestras líneas IOT Water, Easy Water y Connect")
        response_parts.append(" " + ", ".join(specifics))
    elif product_type:
        response_parts.append(f"¡Perfecto! Para {product_type} de {', '.join(brands)}" if brands else f"¡Claro! Tenemos excelentes opciones en {product_type}")
    else:
        response_parts.append("¡Hola! He encontrado estas opciones para ti")
    additional_specs=[]
    if p.get("matrix"): additional_specs.append(f"con matriz {p['matrix']}")
    elif size_mentioned: additional_specs.append(f"compatibles con pantallas de {size_mentioned}\"")
    elif p.get("inches"): additional_specs.append(f"para pantallas de {', '.join(p['inches'])}\"")
    if additional_specs: response_parts.append(" " + ", ".join(additional_specs))
    if total_count > per_page:
        showing = min(per_page, len(items))
        response_parts.append(f". Mostrando {showing} de {total_count} productos disponibles")
    else:
        response_parts.append(f". Encontré {len(items)} productos que coinciden perfectamente")
    if product_type in ["sensores de gas","sensores de agua","sensores"]:
        suggestions=[]
        if p.get("valve"): suggestions.append("con válvula incluida")
        if p.get("wifi"): suggestions.append("con conectividad WiFi")
        if p.get("bt"): suggestions.append("con Bluetooth")
        if p.get("display"): suggestions.append("con pantalla")
        if p.get("alarm"): suggestions.append("con sistema de alertas")
        if suggestions: response_parts.append(f", incluyendo opciones {', '.join(suggestions)}")
    base_response = "".join(response_parts) + "."
    if total_count > per_page:
        base_response += " ¿Te gustaría ver más opciones o prefieres que filtre por alguna característica específica?"
    return base_response

def _cards_from_items(items):
    cards=[]
    for it in items:
        v=it["variant"]
        cards.append({
            "title": it["title"],
            "handle": it.get("handle"),
            "sku": v.get("sku"),
            "variant_id": v.get("variant_id"),
            "image": it["image"],
            "price": money(v.get("price")) if v.get("price") is not None else None,
            "compare_at_price": money(v.get("compare_at_price")) if v.get("compare_at_price") else None,
            "buy_url": it["buy_url"],
            "product_url": it["product_url"],
            "inventory": v.get("inventory"),
            "compatibility": it.get("compatibility") or {},
            "catalog_specs": it.get("catalog_specs") or {},
        })
    return cards

def _plain_items(items):
    out=[]
    for it in items:
        v=it["variant"]
        out.append({"title": it.get("title"), "sku": v.get("sku"),
                    "variant_id": v.get("variant_id"),
                    "price": money(v.get("price")) if v.get("price") is not None else None,
                    "product_url": it.get("product_url"), "buy_url": it.get("buy_url"),
                    "compatibility": it.get("compatibility"),
                    "catalog_specs": it.get("catalog_specs")})
    return out

def _merge_unique_items(*groups):
    """Une resultados de varias búsquedas sin duplicar productos."""
    out=[]; seen=set()
    for group in groups:
        for it in group or []:
            key = it.get("id") or it.get("handle") or it.get("title")
            if key in seen:
                continue
            seen.add(key); out.append(it)
    return out

def _search_catalog_candidates(query: str, max_search: int = 200):
    """Busca candidatos ampliando términos cuando hay filtros técnicos."""
    if _ci_analyze_query and _ci_build_search_queries:
        try:
            analysis = _ci_analyze_query(query)
            queries = _ci_build_search_queries(query, analysis)
        except Exception as e:
            print(f"[WARN] catalog intelligence query analysis failed: {e}", flush=True)
            analysis = None; queries = [query]
    else:
        analysis = None; queries = [query]
    groups=[]
    for q in queries:
        try:
            groups.append(indexer.search(q, k=max_search))
        except Exception as e:
            print(f"[WARN] indexer search failed for '{q}': {e}", flush=True)
    return _merge_unique_items(*groups), analysis

def _apply_catalog_intelligence_safe(query: str, items: list):
    if not _ci_apply_catalog_intelligence:
        return items, {"analysis": None, "technical_filter_applied": False, "filtered_out": 0, "notes": []}
    try:
        return _ci_apply_catalog_intelligence(query, items)
    except Exception as e:
        print(f"[WARN] catalog intelligence ranking failed: {e}", flush=True)
        return items, {"analysis": None, "technical_filter_applied": False, "filtered_out": 0, "notes": []}


# ======================================================================
#  Candado estricto de catálogo
#  Objetivo: nunca mostrar tarjetas ni recomendaciones si la consulta no
#  corresponde a productos reales publicados en el catálogo indexado.
# ======================================================================
CATALOG_NO_MATCH_MESSAGE = "lo siento, no vendemos el artículo que solicitas en este sitio."

_CATALOG_STOPWORDS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al", "y", "o", "u",
    "en", "a", "con", "por", "para", "que", "qué", "cual", "cuál", "cuales", "cuáles", "donde",
    "dónde", "como", "cómo", "me", "mi", "mis", "tu", "tus", "su", "sus", "es", "son", "sea",
    "ser", "hay", "tiene", "tienen", "tendran", "tendrán", "manejan", "venden", "vendemos", "venta",
    "busco", "busca", "buscar", "buscando", "estoy", "estamos", "quiero", "queremos", "necesito",
    "necesitamos", "requiero", "requerimos", "ocupo", "ocupar", "interesa", "interesado", "interesada",
    "producto", "productos", "articulo", "artículo", "articulos", "artículos", "opcion", "opción",
    "opciones", "recomienda", "recomiendas", "recomendar", "cotizar", "cotizacion", "cotización",
    "precio", "precios", "costo", "costos", "favor", "porfa", "gracias", "hola", "buenos", "dias", "días",
    "tardes", "noches", "master", "maxter", "sitio", "pagina", "página", "tienda", "web"
}

_CATALOG_ATTRIBUTE_ONLY = {
    "techo", "pared", "muro", "piso", "mesa", "grande", "chico", "pequeno", "pequeño", "larga", "largo",
    "corto", "corta", "wifi", "inalambrico", "inalámbrico", "bluetooth", "app", "alexa", "google",
    "smart", "inteligente", "digital", "analogico", "analógico", "nuevo", "nueva", "compatible",
    "universal", "casa", "oficina", "hotel", "negocio", "exterior", "interior", "local", "remoto", "pulgada", "pulgadas", "inch", "inches", "nivel", "tanque", "deposito", "depósito", "pantalla", "pantallas", "tv", "televisor", "televisores", "television", "televisión", "senal", "señal", "digital"
}

_CATALOG_KNOWN_BRANDS = {"lg", "hp", "sony", "samsung", "tcl", "hisense", "roku", "panasonic", "daewoo", "philips", "sharp", "vizio", "xiaomi"}

_CATALOG_BLOCKED_NOUNS = {
    "ventilador", "ventiladores", "abanico", "abanicos", "alimento", "alimentos", "comida", "animal",
    "animales", "perro", "perros", "gato", "gatos", "mascota", "mascotas", "ropa", "playera",
    "zapatos", "tenis", "refrigerador", "refrigeradores", "lavadora", "lavadoras", "secadora", "secadoras",
    "estufa", "estufas", "microondas", "licuadora", "licuadoras", "colchon", "colchón", "colchones",
    "sillon", "sillón", "sillones", "sofa", "sofá", "celular", "celulares", "laptop", "laptops",
    "tablet", "tablets", "medicina", "medicamento", "medicamentos", "juguete", "juguetes"
}

_CATALOG_FAMILY_RULES = {
    "support": {
        "query_any": {"soporte", "soportes", "base", "bases", "bracket", "mount", "montaje", "vesa"},
        "item_any": {"soporte", "bracket", "mount", "montaje", "vesa", "pantalla", "televisor", "television", "tv"},
    },
    "support_projector": {
        "query_any": {"proyector", "proyectores", "projector"},
        "item_any": {"proyector", "proyectores", "projector"},
    },
    "antenna": {
        "query_any": {"antena", "antenas", "tvant", "uhf", "vhf"},
        "item_any": {"antena", "antenas", "tvant", "uhf", "vhf"},
    },
    "remote": {
        "query_any": {"control", "controles", "remoto", "remotos", "mando", "mandos"},
        "item_any": {"control", "controles", "remoto", "remotos", "mando", "atscontrol"},
    },
    "decoder": {
        # Convertidor/conversor sólo se trata como decodificador cuando el texto
        # también menciona TV/señal/TDT. Esto evita confundir adaptadores USB-C,
        # convertidores de audio o cables con decodificadores de televisión.
        "query_any": {"decodificador", "decodificadores", "decoder", "receptor", "sintonizador", "tdt", "isdb", "dtv"},
        "item_any": {"decodificador", "decoder", "receptor", "sintonizador", "tdt", "isdb", "dtv", "tdtplus", "mv-tdtplus"},
    },
    "cable_connector": {
        "query_any": {"cable", "cables", "cordon", "cordón", "hdmi", "usb", "rca", "aux", "vga", "coaxial", "rg6", "rg59", "adaptador", "adaptadores", "conector", "conectores", "plug", "jack", "splitter", "divisor", "divisores", "switch", "selector"},
        "item_any": {"cable", "cordon", "hdmi", "usb", "tipo-c", "tipo c", "type-c", "type c", "rca", "aux", "vga", "coaxial", "rg6", "rg59", "adaptador", "conector", "plug", "jack", "splitter", "divisor", "switch", "selector", "1x2", "1x4"},
    },
    "sensor_water": {
        "query_any": {"agua", "tinaco", "tinacos", "cisterna", "cisternas", "fuga", "fugas", "inundacion", "inundación", "boya", "flotador", "electronivel", "electroniveles", "ultrasonico", "ultrasonica", "ultrasónico", "ultrasónica", "presion", "presión", "water"},
        "item_any": {"agua", "tinaco", "tinacos", "cisterna", "cisternas", "water", "iot-water", "easy-water", "connect-water", "waterv", "waterultra", "waterp", "electronivel", "flotador", "boya", "presion", "ultrasonico"},
    },
    "sensor_gas": {
        "query_any": {"gas", "gassensor", "gasensor", "lp", "propano", "butano", "estacionario", "estacionaria"},
        "item_any": {"gas", "gassensor", "gasensor", "lp", "propano", "butano", "estacionario", "easy-gas", "connect-gas", "iot-gassensor"},
    },
    "sensor_environmental": {
        "query_any": {"ambiental", "ambientales", "ambiente", "temperatura", "humedad", "humo", "co2", "monoxido", "monóxido", "lluvia", "rain", "aire"},
        "item_any": {"ambiental", "ambiente", "temperatura", "humedad", "humo", "co2", "monoxido", "lluvia", "rain", "aire"},
    },
    "sensor_arduino": {
        "query_any": {"arduino", "raspberry", "prototipo", "prototipos", "robotica", "robótica"},
        "item_any": {"arduino", "raspberry", "prototipo", "robotica", "robótica", "maker"},
    },
    "sensor_general": {
        "query_any": {"sensor", "sensores", "detector", "detectores", "medidor", "medidores", "modulo", "módulo", "iot"},
        "item_any": {"sensor", "detector", "medidor", "modulo", "módulo", "iot", "smart"},
    },
    "camera_security": {
        "query_any": {"camara", "cámara", "camaras", "cámaras", "cctv", "vigilancia", "seguridad", "dvr", "nvr", "poe"},
        "item_any": {"camara", "cámara", "camaras", "cámaras", "cctv", "vigilancia", "seguridad", "dvr", "nvr", "poe"},
    },
    "audio": {
        "query_any": {"bocina", "bocinas", "parlante", "parlantes", "altavoz", "altavoces", "speaker", "microfono", "micrófono", "microfonos", "micrófonos", "amplificador", "amplificadores", "audio"},
        "item_any": {"bocina", "bocinas", "parlante", "altavoz", "speaker", "microfono", "micrófono", "amplificador", "audio", "m1"},
    },
    "power_energy": {
        "query_any": {"pila", "pilas", "bateria", "batería", "baterias", "baterías", "cargador", "cargadores", "fuente", "eliminador", "energia", "energía", "kwh", "watts", "voltaje", "contacto", "enchufe"},
        "item_any": {"pila", "bateria", "batería", "cargador", "fuente", "eliminador", "energia", "energía", "kwh", "watts", "voltaje", "contacto", "enchufe", "iote"},
    },
    "network": {
        "query_any": {"router", "routers", "modem", "módem", "repetidor", "extensor", "internet", "red", "ethernet", "rj45"},
        "item_any": {"router", "modem", "módem", "repetidor", "extensor", "internet", "ethernet", "rj45", "red"},
    },
}

# Marcas comunes de televisión/pantalla. Se usan para no mostrar controles de
# marcas distintas cuando el cliente pide explícitamente una marca.
_CATALOG_TV_BRANDS = {
    "lg", "sony", "samsung", "tcl", "hisense", "roku", "panasonic", "daewoo",
    "philips", "sharp", "vizio", "xiaomi", "sansui", "atvio", "jvc", "rca",
    "toshiba", "aoc", "hyundai", "onn", "sanyo", "benq", "epson"
}

_ANTENNA_CLARIFICATION_MESSAGE = "Contamos con antenas para interiores y para exteriores. ¿De qué tipo requieres?"
_CONTEXT_MAX_AGE_SECONDS = 30 * 60


def _catalog_norm(value: str) -> str:
    text = (value or "").lower()
    text = _strip_accents(text) if "_strip_accents" in globals() else text
    text = text.replace("×", "x")
    return re.sub(r"\s+", " ", text).strip()


def _catalog_tokens(query: str) -> list[str]:
    qn = _catalog_norm(query)
    raw = re.findall(r"[a-z0-9]+", qn, re.IGNORECASE)
    tokens = []
    for token in raw:
        if len(token) < 2 or token in _CATALOG_STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def _catalog_item_text(it: dict, include_body: bool = False) -> str:
    variant = it.get("variant") or {}
    parts = [
        it.get("title") or "", it.get("handle") or "", it.get("tags") or "",
        it.get("vendor") or "", it.get("product_type") or "", it.get("sku") or "",
        variant.get("sku") or "",
    ]
    skus = it.get("skus")
    if isinstance(skus, (list, tuple)):
        parts.extend([x for x in skus if x])
    if include_body:
        parts.append(it.get("body") or "")
    return _catalog_norm(" ".join(str(x) for x in parts if x))


def _query_has_any(qn: str, terms: set[str] | list[str] | tuple[str, ...]) -> bool:
    return any(term in qn for term in terms)


def _detect_catalog_constraints(query: str) -> dict:
    """Detecta subintenciones que deben respetarse de forma estricta."""
    qn = _catalog_norm(query)
    tokens = set(_catalog_tokens(query))
    constraints: dict = {}

    # Antenas: interior vs exterior. Si la consulta sólo dice "antena", el chat
    # debe preguntar antes de mostrar tarjetas para no mezclar ambas familias.
    has_antenna = _query_has_any(qn, ["antena", "antenas", "tvant"])
    cable_context = _query_has_any(qn, ["cable", "coaxial", "rg6", "rg59", "conector", "plug", "adaptador"])
    if has_antenna and not cable_context:
        interior_terms = {"interior", "interiores", "interna", "internas", "indoor", "adentro", "dentro", "habitacion", "habitación", "recamara", "recámara", "cuarto", "departamento"}
        exterior_terms = {"exterior", "exteriores", "externa", "externas", "outdoor", "afuera", "azotea", "techo", "tejado", "intemperie", "fachada", "muro", "pared exterior"}
        if _query_has_any(qn, exterior_terms):
            constraints["antenna_type"] = "exterior"
        elif _query_has_any(qn, interior_terms):
            constraints["antenna_type"] = "interior"
        else:
            constraints["antenna_needs_clarification"] = True

    # Soportes para pantalla/proyector: techo, fijo, articulado, inclinable y proyector.
    has_support = _query_has_any(qn, ["soporte", "soportes", "base", "bases", "bracket", "mount", "montaje", "vesa"])
    if has_support:
        if _query_has_any(qn, ["proyector", "proyectores", "projector"]):
            constraints["support_kind"] = "projector"
        elif _query_has_any(qn, ["techo", "ceiling"]):
            constraints["support_kind"] = "ceiling"
        elif _query_has_any(qn, ["articulado", "articulada", "articulable", "brazo", "brazos", "extendible", "movil", "móvil", "movimiento", "full motion"]):
            constraints["support_kind"] = "articulated"
        elif _query_has_any(qn, ["fijo", "fija", "fixed"]):
            constraints["support_kind"] = "fixed"
        elif _query_has_any(qn, ["inclinable", "inclinacion", "inclinación", "tilt", "tilting"]):
            constraints["support_kind"] = "tilt"

    # Controles remotos: marca específica y control para TV vs control de decodificador.
    remote_terms = {"control", "controles", "remoto", "remotos", "mando", "mandos"}
    if tokens & remote_terms:
        brands = sorted({brand for brand in _CATALOG_TV_BRANDS if re.search(rf"\b{re.escape(brand)}\b", qn)})
        if brands:
            constraints["remote_brand"] = brands[0]
        if _query_has_any(qn, ["pantalla", "pantallas", "tv", "televisor", "television", "televisión"]):
            constraints["remote_kind"] = "tv"
        elif _query_has_any(qn, ["decodificador", "decoder", "tdt", "sintonizador"]):
            constraints["remote_kind"] = "decoder"

    # Decodificador: sólo TV/señal/TDT. No debe arrastrar audio, cables, controles o adaptadores.
    decoder_core = _query_has_any(qn, ["decodificador", "decodificadores", "decoder", "sintonizador", "tdt", "isdb", "dtv"])
    converter_tv = _query_has_any(qn, ["convertidor", "conversor", "receptor"]) and _query_has_any(qn, ["tv", "televisor", "television", "televisión", "pantalla", "senal", "señal", "tdt", "digital"])
    if (decoder_core or converter_tv) and not (tokens & remote_terms):
        constraints["decoder_type"] = "tv"

    # Cables: tipo específico solicitado.
    cable_terms = {"cable", "cables", "cordon", "cordón", "conector", "conectores", "adaptador", "adaptadores"}
    if tokens & cable_terms or _query_has_any(qn, ["hdmi", "usb", "tipo c", "tipo-c", "type c", "type-c", "coaxial", "rg6", "rg59", "rca", "aux", "jack", "plug"]):
        if "hdmi" in qn:
            constraints["cable_type"] = "hdmi"
        elif _query_has_any(qn, ["usb", "tipo c", "tipo-c", "type c", "type-c", "micro usb", "microusb"]):
            constraints["cable_type"] = "usb"
        elif _query_has_any(qn, ["coaxial", "rg6", "rg59", "cable de antena", "cable para antena"]):
            constraints["cable_type"] = "coaxial"
        elif _query_has_any(qn, ["audio", "rca", "aux", "auxiliar", "jack", "plug", "3.5", "3 5"]):
            constraints["cable_type"] = "audio"
        if constraints.get("cable_type"):
            if _query_has_any(qn, ["adaptador", "adaptadores", "convertidor", "conversor"]):
                constraints["connection_form"] = "adapter"
            elif _query_has_any(qn, ["divisor", "splitter", "switch", "selector", "repartidor"]):
                constraints["connection_form"] = "distribution"
            elif _query_has_any(qn, ["cable", "cables", "cordon", "cordón"]):
                constraints["connection_form"] = "cable"

    # Sensores: familia y tecnología. En agua general permitimos varias tecnologías;
    # si el usuario pide una tecnología concreta, se filtra a esa tecnología.
    has_sensor = _query_has_any(qn, ["sensor", "sensores", "detector", "detectores", "medidor", "medidores", "modulo", "módulo"])
    if has_sensor:
        if _query_has_any(qn, ["arduino", "raspberry", "robotica", "robótica", "maker", "prototipo"]):
            constraints["sensor_kind"] = "arduino"
        elif _query_has_any(qn, ["ambiental", "ambientales", "ambiente", "temperatura", "humedad", "humo", "co2", "monoxido", "monóxido", "lluvia", "calidad de aire", "aire"]):
            constraints["sensor_kind"] = "environmental"
        elif _query_has_any(qn, ["gas", "lp", "propano", "butano", "estacionario", "estacionaria"]):
            constraints["sensor_kind"] = "gas"
        elif _query_has_any(qn, ["agua", "tinaco", "cisterna", "fuga", "inundacion", "inundación", "water", "nivel", "ultrasonico", "ultrasonica", "ultrasónico", "ultrasónica", "presion", "presión", "electronivel", "electroniveles"]):
            constraints["sensor_kind"] = "water"

        if constraints.get("sensor_kind") == "water" or _query_has_any(qn, ["agua", "tinaco", "cisterna", "water", "ultrasonico", "ultrasonica", "ultrasónico", "ultrasónica", "presion", "presión", "electronivel", "electroniveles"]):
            if _query_has_any(qn, ["ultrasonico", "ultrasonica", "ultrasónico", "ultrasónica", "ultra", "waterultra"]):
                constraints["sensor_water_tech"] = "ultrasonic"
            elif _query_has_any(qn, ["presion", "presión", "pressure", "waterp"]):
                constraints["sensor_water_tech"] = "pressure"
            elif _query_has_any(qn, ["electronivel", "electroniveles", "electro nivel", "electro-nivel", "electro niveles", "electro-niveles", "flotador", "boya"]):
                constraints["sensor_water_tech"] = "electronivel"

    return constraints


def _should_clarify_antenna_type(query: str) -> bool:
    constraints = _detect_catalog_constraints(query)
    return bool(constraints.get("antenna_needs_clarification"))


def _empty_pagination(per_page: int = 10) -> dict:
    return {"page": 1, "per_page": per_page, "total": 0, "total_pages": 0, "has_next": False, "has_prev": False}


def _now_ts() -> int:
    try:
        return int(time.time())
    except Exception:
        return 0


def _clear_conversation_context(user_query: str = "", effective_query: str = "") -> dict:
    return {
        "pending_clarification": None,
        "last_user_query": user_query or "",
        "last_effective_query": effective_query or user_query or "",
        "updated_at": _now_ts(),
    }


def _antenna_pending_context(base_query: str = "") -> dict:
    clean_base = (base_query or "antena").strip() or "antena"
    return {
        "pending_clarification": {
            "type": "antenna_type",
            "family": "antenna",
            "base_query": clean_base,
            "base_product": "antena",
        },
        "last_user_query": clean_base,
        "last_effective_query": clean_base,
        "updated_at": _now_ts(),
    }


def _antenna_clarification_payload(per_page: int = 10, base_query: str = "", answer: str | None = None) -> dict:
    return {
        "answer": answer or _ANTENNA_CLARIFICATION_MESSAGE,
        "products": [],
        "pagination": _empty_pagination(per_page),
        "needs_clarification": True,
        "clarification_type": "antenna_type",
        "quick_replies": ["Antenas para interiores", "Antenas para exteriores"],
        "conversation_context": _antenna_pending_context(base_query),
        "effective_query": base_query or "",
    }


def _get_conversation_context(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    for key in ("conversation_context", "chat_context", "context"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _conversation_context_expired(context: dict) -> bool:
    if not isinstance(context, dict):
        return True
    ts = context.get("updated_at")
    if not isinstance(ts, (int, float)):
        return False
    now = _now_ts()
    return bool(now and ts and (now - int(ts) > _CONTEXT_MAX_AGE_SECONDS))


def _detect_antenna_type_from_text(text: str) -> str | None:
    qn = _catalog_norm(text)
    if not qn:
        return None
    interior_terms = {
        "interior", "interiores", "interna", "internas", "indoor",
        "adentro", "dentro", "habitacion", "recamara", "cuarto", "departamento",
        "para interior", "para interiores", "de interior", "de interiores"
    }
    exterior_terms = {
        "exterior", "exteriores", "externa", "externas", "outdoor",
        "afuera", "azotea", "techo", "tejado", "intemperie", "fachada", "muro",
        "para exterior", "para exteriores", "de exterior", "de exteriores"
    }
    wants_interior = _query_has_any(qn, interior_terms)
    wants_exterior = _query_has_any(qn, exterior_terms)
    if wants_exterior and not wants_interior:
        return "exterior"
    if wants_interior and not wants_exterior:
        return "interior"
    return None


def _is_new_non_antenna_product_query(text: str) -> bool:
    qn = _catalog_norm(text)
    if not qn:
        return False
    if _query_has_any(qn, ["antena", "antenas", "tvant"]):
        return False
    product_terms = {
        "soporte", "soportes", "base", "bases", "control", "controles", "remoto",
        "sensor", "sensores", "detector", "medidor", "modulo", "módulo",
        "cable", "cables", "adaptador", "adaptadores", "conector", "conectores",
        "decodificador", "decodificadores", "receptor", "sintonizador", "tdt",
        "bocina", "bocinas", "audio", "microfono", "micrófono",
        "camara", "cámara", "camaras", "cámaras", "router", "modem", "módem",
        "pila", "pilas", "bateria", "batería", "cargador", "enchufe", "contacto"
    }
    return _query_has_any(qn, product_terms)


def _merge_antenna_clarification_query(base_query: str, user_query: str, antenna_type: str) -> str:
    base = _catalog_norm(base_query or "")
    user = (user_query or "").strip()
    qn_user = _catalog_norm(user)
    type_phrase = "exteriores" if antenna_type == "exterior" else "interiores"

    # Si el usuario ya escribió la familia completa, conservamos su texto para no
    # perder especificaciones adicionales: "antena exterior 4k", "antena para techo", etc.
    if _query_has_any(qn_user, ["antena", "antenas", "tvant"]):
        return user

    # Si la pregunta base ya decía antena, la convertimos en una consulta directa.
    if _query_has_any(base, ["antena", "antenas", "tvant"]):
        return f"antena para {type_phrase}"

    return f"antena para {type_phrase}"


def _resolve_contextual_product_query(query: str, incoming_context: dict) -> tuple[str, dict, dict]:
    """
    Reconstruye consultas cortas de aclaración usando el contexto enviado por el widget.

    Caso principal corregido:
    - Usuario: "busco una antena"
    - Bot: pregunta interior/exterior y devuelve pending_clarification.
    - Usuario: "para exteriores"
    - Backend convierte la búsqueda real en: "antena para exteriores".
    """
    original_query = (query or "").strip()
    if not incoming_context or _conversation_context_expired(incoming_context):
        return original_query, _clear_conversation_context(original_query, original_query), {"used_context": False}

    pending = incoming_context.get("pending_clarification")
    if not isinstance(pending, dict):
        return original_query, _clear_conversation_context(original_query, original_query), {"used_context": False}

    pending_type = pending.get("type")
    family = pending.get("family")

    if pending_type == "antenna_type" and family == "antenna":
        if _is_new_non_antenna_product_query(original_query):
            return original_query, _clear_conversation_context(original_query, original_query), {
                "used_context": False,
                "cleared_context": True,
                "reason": "new_product_query",
            }

        antenna_type = _detect_antenna_type_from_text(original_query)
        if antenna_type:
            effective_query = _merge_antenna_clarification_query(
                pending.get("base_query") or incoming_context.get("last_effective_query") or "antena",
                original_query,
                antenna_type,
            )
            return effective_query, _clear_conversation_context(original_query, effective_query), {
                "used_context": True,
                "resolved_clarification": "antenna_type",
                "antenna_type": antenna_type,
                "base_query": pending.get("base_query") or "antena",
            }

        # El usuario respondió algo como "cuál me recomiendas" o "la mejor"
        # sin elegir interior/exterior; mantenemos el contexto y pedimos la aclaración otra vez.
        return original_query, _antenna_pending_context(pending.get("base_query") or "antena"), {
            "used_context": False,
            "reask_clarification": "antenna_type",
        }

    return original_query, _clear_conversation_context(original_query, original_query), {"used_context": False}


def _item_has_any(text: str, terms: set[str] | list[str] | tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _item_matches_catalog_constraints(it: dict, constraints: dict) -> bool:
    if not constraints:
        return True
    text = _catalog_item_text(it, include_body=True)
    strong = _catalog_item_text(it, include_body=False)

    antenna_type = constraints.get("antenna_type")
    if antenna_type:
        if not _item_has_any(text, ["antena", "antenas", "tvant", "uhf", "vhf"]):
            return False
        interior_terms = ["interior", "interna", "indoor", "tvant-int", "int-4k", "para interiores"]
        exterior_terms = ["exterior", "externa", "outdoor", "azotea", "techo", "intemperie", "tvant-100", "tvant-16", "16elem", "aerea", "aérea", "para exteriores"]
        if antenna_type == "interior":
            return _item_has_any(text, interior_terms) and not (_item_has_any(strong, exterior_terms) and not _item_has_any(strong, interior_terms))
        if antenna_type == "exterior":
            return _item_has_any(text, exterior_terms) and not (_item_has_any(strong, interior_terms) and not _item_has_any(strong, exterior_terms))

    support_kind = constraints.get("support_kind")
    if support_kind:
        if support_kind == "projector":
            return _item_has_any(text, ["proyector", "proyectores", "projector"])
        if _item_has_any(text, ["proyector", "projector"]):
            return False
        if not _item_has_any(text, ["soporte", "bracket", "mount", "montaje", "vesa"]):
            return False
        if support_kind == "ceiling":
            return _item_has_any(text, ["techo", "ceiling"])
        if support_kind == "articulated":
            return _item_has_any(text, ["articulado", "articulada", "articulable", "brazo", "extendible", "movil", "móvil", "full motion"])
        if support_kind == "fixed":
            return _item_has_any(text, ["fijo", "fija", "fixed"]) and not _item_has_any(strong, ["articulado", "brazo", "extendible", "techo", "proyector"])
        if support_kind == "tilt":
            return _item_has_any(text, ["inclinable", "inclinacion", "inclinación", "tilt", "tilting"])

    remote_brand = constraints.get("remote_brand")
    if remote_brand:
        if not _item_has_any(text, ["control", "remoto", "mando", "atscontrol"]):
            return False
        if remote_brand not in text:
            return False
    if constraints.get("remote_kind") == "tv":
        if not _item_has_any(text, ["control", "remoto", "mando"]):
            return False
        if _item_has_any(strong, ["decodificador", "decoder", "tdt", "atscontrol", "sintonizador"]):
            return False

    if constraints.get("decoder_type") == "tv":
        if not _item_has_any(text, ["decodificador", "decoder", "sintonizador", "tdt", "isdb", "dtv", "mv-tdtplus", "tdtplus"]):
            return False
        if _item_has_any(strong, ["audio", "bocina", "amplificador", "microfono", "micrófono"]):
            return False
        if _item_has_any(strong, ["cable", "adaptador", "conector", "plug", "jack", "splitter", "divisor", "switch", "control remoto", "atscontrol"]):
            return False

    cable_type = constraints.get("cable_type")
    if cable_type:
        form = constraints.get("connection_form")
        if form == "cable" and _item_has_any(strong, ["splitter", "divisor", "switch", "selector", "adaptador", "conector"]):
            return False
        if form == "adapter" and not _item_has_any(text, ["adaptador", "convertidor", "conversor", "conector"]):
            return False
        if form == "distribution" and not _item_has_any(text, ["splitter", "divisor", "switch", "selector", "repartidor"]):
            return False
        if cable_type == "hdmi":
            return "hdmi" in text
        elif cable_type == "usb":
            return _item_has_any(text, ["usb", "tipo c", "tipo-c", "type c", "type-c", "micro usb", "microusb"])
        elif cable_type == "coaxial":
            return _item_has_any(text, ["coaxial", "rg6", "rg59", "cable de antena", "cable para antena", "conector f"])
        elif cable_type == "audio":
            return _item_has_any(text, ["audio", "rca", "aux", "auxiliar", "jack", "plug", "3.5", "3 5"])

    sensor_kind = constraints.get("sensor_kind")
    if sensor_kind:
        if sensor_kind == "water" and _item_has_any(strong, ["gas", "gassensor", "gasensor", "propano", "butano", "lp", "estacionario"]):
            return False
        if sensor_kind == "gas" and _item_has_any(strong, ["agua", "water", "tinaco", "cisterna", "waterv", "waterultra", "waterp"]):
            return False
        if sensor_kind == "environmental":
            if not _item_has_any(text, ["ambiental", "ambiente", "temperatura", "humedad", "humo", "co2", "monoxido", "lluvia", "rain", "aire"]):
                return False
            if _item_has_any(strong, ["agua", "water", "tinaco", "cisterna", "gas", "gassensor"]):
                return False
        if sensor_kind == "arduino":
            if not _item_has_any(text, ["arduino", "raspberry", "robotica", "robótica", "maker", "prototipo"]):
                return False

    water_tech = constraints.get("sensor_water_tech")
    if water_tech:
        if not _item_has_any(text, ["agua", "water", "tinaco", "cisterna", "nivel"]):
            return False
        if water_tech == "ultrasonic":
            return _item_has_any(text, ["ultrasonico", "ultrasonica", "ultrasónico", "ultrasónica", "waterultra", "ultra"])
        if water_tech == "pressure":
            return _item_has_any(text, ["presion", "presión", "pressure", "waterp"])
        if water_tech == "electronivel":
            return _item_has_any(text, ["electronivel", "electroniveles", "electro nivel", "electro-nivel", "electro niveles", "electro-niveles", "flotador", "boya"])

    return True


def _apply_specific_catalog_constraints(query: str, items: list) -> tuple[list, dict]:
    constraints = _detect_catalog_constraints(query)
    context = {"constraints": constraints, "input_count": len(items or []), "output_count": len(items or []), "rejected": False, "reason": ""}
    # La pregunta de antenas genéricas se maneja antes del buscador, no aquí.
    constraints = {k: v for k, v in constraints.items() if k != "antenna_needs_clarification"}
    if not constraints or not items:
        return items, context
    filtered = [it for it in items if _item_matches_catalog_constraints(it, constraints)]
    context["output_count"] = len(filtered)
    if not filtered:
        context.update({"rejected": True, "reason": "no_result_matches_specific_constraints"})
    return filtered, context


def _detect_catalog_families(query: str, analysis: dict | None = None) -> set[str]:
    tokens = set(_catalog_tokens(query))
    qn = _catalog_norm(query)
    families: set[str] = set()

    intent = (analysis or {}).get("intent") if isinstance(analysis, dict) else None
    if intent == "support":
        families.add("support")
    elif intent == "water":
        families.add("sensor_water")
    elif intent == "gas":
        families.add("sensor_gas")
    elif intent == "antenna":
        families.add("antenna")
    elif intent == "remote":
        families.add("remote")
    elif intent == "decoder_tv":
        families.add("decoder")
    elif intent == "cable":
        families.add("cable_connector")
    elif intent == "sensor_environmental":
        families.add("sensor_environmental")
    elif intent == "sensor_arduino":
        families.add("sensor_arduino")

    for family, rule in _CATALOG_FAMILY_RULES.items():
        query_any = rule.get("query_any") or set()
        if tokens & query_any or any(term in qn for term in query_any if len(term) > 3):
            families.add(family)

    # Convertidor/conversor/receptor es ambiguo. Sólo lo enviamos a decoder
    # cuando hay contexto de TV, señal digital o TDT.
    if (tokens & {"convertidor", "conversor", "receptor"}) and (
        tokens & {"tv", "televisor", "television", "pantalla", "senal", "señal", "tdt", "digital", "dtv"}
    ):
        families.add("decoder")

    if (tokens & {"pantalla", "pantallas", "tv", "televisor", "televisores", "monitor", "monitores", "vesa"}) and (
        re.search(r"\b(1[9]|[2-9]\d|10\d|11\d|120)\b", qn) or "vesa" in tokens
    ):
        families.add("support")

    constraints = _detect_catalog_constraints(query)
    if constraints.get("support_kind") == "projector":
        families.add("support_projector")
        families.discard("support")
    if constraints.get("sensor_kind") == "environmental":
        families.add("sensor_environmental")
        families.discard("sensor_general")
    if constraints.get("sensor_kind") == "arduino":
        families.add("sensor_arduino")
        families.discard("sensor_general")
    if constraints.get("decoder_type") == "tv":
        families.add("decoder")
    if constraints.get("cable_type"):
        families.add("cable_connector")
    if constraints.get("remote_brand") or constraints.get("remote_kind"):
        families.add("remote")

    # Si el usuario especifica agua, gas, ambiental o Arduino, no permitimos que
    # la familia genérica "sensor" arrastre sensores de otra categoría.
    if any(f in families for f in {"sensor_gas", "sensor_water", "sensor_environmental", "sensor_arduino"}):
        families.discard("sensor_general")

    # Si se pide control remoto de TV, no lo mezcles con decodificadores por la
    # presencia de SKUs como ATSCONTROL.
    if constraints.get("remote_kind") == "tv":
        families.discard("decoder")

    return families


def _item_matches_family(it: dict, family: str) -> bool:
    text = _catalog_item_text(it, include_body=False)
    body_text = _catalog_item_text(it, include_body=True)
    item_any = _CATALOG_FAMILY_RULES.get(family, {}).get("item_any") or set()
    if not item_any:
        return False
    source = body_text if family in {"sensor_water", "sensor_gas", "sensor_general"} else text
    return any(term in source for term in item_any)


def _has_direct_product_match(query_tokens: list[str], it: dict) -> bool:
    strong_text = _catalog_item_text(it, include_body=False)
    body_text = _catalog_item_text(it, include_body=True)
    meaningful = [t for t in query_tokens if t not in _CATALOG_ATTRIBUTE_ONLY and not t.isdigit() and (len(t) >= 3 or t in _CATALOG_KNOWN_BRANDS)]
    if not meaningful:
        return False
    if any(t in strong_text for t in meaningful):
        return True
    if any(t in body_text for t in meaningful if t in {"agua", "gas", "cisterna", "tinaco", "alexa", "wifi", "bluetooth", "vesa", "hdmi"}):
        return True
    return False


def _specific_query_tokens_for_families(query_tokens: list[str], families: set[str]) -> list[str]:
    family_terms = set()
    for family in families:
        family_terms.update(_CATALOG_FAMILY_RULES.get(family, {}).get("query_any") or set())
    return [
        t for t in query_tokens
        if t not in family_terms and t not in _CATALOG_ATTRIBUTE_ONLY and not t.isdigit() and (len(t) >= 3 or t in _CATALOG_KNOWN_BRANDS)
    ]


def _item_matches_specific_tokens(it: dict, specific_tokens: list[str]) -> bool:
    if not specific_tokens:
        return True
    strong_text = _catalog_item_text(it, include_body=False)
    body_text = _catalog_item_text(it, include_body=True)
    # Los tokens específicos no son todos obligatorios, pero al menos uno debe estar en la ficha.
    # Así evitamos que "sensor de movimiento" devuelva sensores de agua/gas si la palabra
    # movimiento no aparece en productos publicados.
    return any(token in strong_text or token in body_text for token in specific_tokens)


def _apply_strict_catalog_guard(query: str, items: list, analysis: dict | None = None):
    """Filtra o bloquea resultados débiles para evitar productos inventados."""
    tokens = _catalog_tokens(query)
    families = _detect_catalog_families(query, analysis)
    blocked_terms = sorted(set(tokens) & _CATALOG_BLOCKED_NOUNS)
    context = {
        "strict_catalog_guard": True,
        "query_tokens": tokens,
        "families": sorted(families),
        "blocked_terms": blocked_terms,
        "input_count": len(items or []),
        "output_count": 0,
        "rejected": False,
        "reason": "",
    }

    if not query or not items:
        context.update({"rejected": True, "reason": "empty_query_or_items"})
        return [], context

    if blocked_terms and not families:
        context.update({"rejected": True, "reason": "blocked_out_of_catalog_noun"})
        return [], context

    if families:
        specific_tokens = _specific_query_tokens_for_families(tokens, families)
        filtered = [
            it for it in items
            if any(_item_matches_family(it, fam) for fam in families)
            and _item_matches_specific_tokens(it, specific_tokens)
        ]
        context["specific_tokens"] = specific_tokens
        context["output_count"] = len(filtered)
        if not filtered:
            context.update({"rejected": True, "reason": "no_result_matches_detected_family"})
            return [], context
        constrained, constraint_context = _apply_specific_catalog_constraints(query, filtered)
        context["specific_constraints"] = constraint_context
        context["output_count"] = len(constrained)
        if not constrained:
            context.update({"rejected": True, "reason": constraint_context.get("reason") or "no_result_matches_specific_constraints"})
        return constrained, context

    filtered = [it for it in items if _has_direct_product_match(tokens, it)]
    constrained, constraint_context = _apply_specific_catalog_constraints(query, filtered)
    context["specific_constraints"] = constraint_context
    context["output_count"] = len(constrained)
    if not constrained:
        context.update({"rejected": True, "reason": constraint_context.get("reason") or "no_direct_catalog_match"})
    return constrained, context


def _catalog_no_match_payload(per_page: int = 10, user_query: str = "", effective_query: str = ""):
    return {
        "answer": CATALOG_NO_MATCH_MESSAGE,
        "products": [],
        "pagination": _empty_pagination(per_page),
        "conversation_context": _clear_conversation_context(user_query, effective_query or user_query),
        "effective_query": effective_query or user_query or "",
    }

# ---------- Señales / familias (idéntico enfoque) ----------
_WATER_ALLOW_FAMILIES = [
    "iot-waterv","iot-waterultra","iot-waterp","iot-water",
    "easy-waterultra","easy-water","iot waterv","iot waterultra","iot waterp","iot water","easy waterultra","easy water",
    "connect-water","connect water"
]
_WATER_ALLOW_KEYWORDS = ["tinaco","cisterna","nivel","agua","water","inundacion","inundación","flotador","boya"]

_GAS_ALLOW_FAMILIES = [
    "modulo-sensor-inteligente-de-nivel-de-gas",
    "sensor-de-gas-inteligente-con-electrovalvula-y-alertas-en-tiempo-real",
    "modulo-de-nivel-de-volumen-y-cierre-para-tanques-estacionarios-de-gas-iot-gassensor-presentacion-sin-valvula",
    "modulo-digital-de-nivel-de-gas-con-alcance-inalambrico-de-500-metros",
    "iot-gassensorv","iot-gassensor","easy-gas","connect-gas",
    "iot gassensorv","iot gassensor","easy gas","connect gas",
    "sensor-inteligente-de-nivel-de-gas","dispositivo-inteligente-sensor-gas",
    "modulo-sensor-gas","sensor-gas-tanque-estacionario",
    "gassensorv","gassensor","gas-sensor","gasensor",
    "sensor-gas","medidor-gas","detector-gas",
    "nivel-gas","tanque-gas","estacionario-gas"
]
_GAS_ALLOW_KEYWORDS = [
    "gas","tanque","estacionario","estacionaria","lp","propano","butano",
    "nivel","medidor","porcentaje","volumen","gassensor","gasensor",
    "gas-sensor","sensor de gas","medidor de gas","detector de gas",
    "modulo sensor inteligente","dispositivo inteligente sensor",
    "sensor inteligente nivel gas","medidor inteligente gas",
    "nivel de gas","monitoreo gas","alertas gas",
    "app master iot","compatible alexa gas",
    "tanques estacionarios","sensor gas wifi",
    "iot gas","easy gas","connect gas",
    "electrovalvula gas","valvula gas"
]
_GAS_BLOCK = ["ar-rain","rain","lluvia","carsensor","bm-carsensor","auto","vehiculo","vehículo","kwh","kw/h","consumo electrico","tarifa electrica","electric meter"]
_WATER_BLOCK = ["propano","butano","lp gas","tanque estacionario gas"]

def _concat_fields(it) -> str:
    v = it.get("variant", {})
    body = (it.get("body") or "").lower()
    if len(body) > 1500: body = body[:1500]
    parts = [it.get("title") or "", it.get("handle") or "", it.get("tags") or "",
             it.get("vendor") or "", it.get("product_type") or "", v.get("sku") or "", body]
    if isinstance(it.get("skus"), (list, tuple)):
        parts.extend([x for x in it["skus"] if x])
    return " ".join(parts).lower()

def _intent_from_query(q: str):
    ql = (q or "").lower()
    gas_signals = ["gas","tanque","estacionario","estacionaria","lp","propano","butano","gassensor","gas-sensor","iot-gassensor","easy-gas","connect-gas","gasensor","sensor gas","medidor gas","detector gas","nivel gas"]
    if any(w in ql for w in gas_signals): return "gas"
    water_hard = ["agua","tinaco","cisterna","inundacion","inundación","boya","flotador"]
    if any(w in ql for w in water_hard): return "water"
    return None

def _score_family(st: str, ql: str, allow_keywords, allow_fams, extras) -> tuple[int, bool]:
    s=0; has_family = any(fam in st for fam in allow_fams)
    if any(w in st for w in allow_keywords): s += 50
    if has_family: s += 200
    if extras.get("want_valve"):
        for key in extras.get("valve_fams", []):
            if key in st: s += extras.get("valve_bonus", 95)
    if extras.get("want_ultra"):
        for key in extras.get("ultra_fams", []):
            if key in st: s += 55
    if extras.get("want_pressure"):
        for key in extras.get("pressure_fams", []):
            if key in st: s += 55
    if extras.get("want_bt"):
        for key in extras.get("bt_fams", []):
            if key in st: s += 45
    if extras.get("want_wifi"):
        for key in extras.get("wifi_fams", []):
            if key in st: s += 45
    if extras.get("want_display"):
        for key in extras.get("display_fams", []):
            if key in st: s += 40
    if extras.get("want_alarm"):
        for key in extras.get("alarm_words", []):
            if key in st: s += 25
    for neg in extras.get("neg_words", []):
        if neg in st: s -= 30
    return s, has_family

def _rerank_for_gas(query: str, items: list):
    ql=(query or "").lower()
    if _intent_from_query(query)!="gas" or not items: return items
    want_valve=("valvula" in ql) or ("válvula" in ql) or ("electrovalvula" in ql)
    want_wifi=("wifi" in ql) or ("app" in ql) or ("inteligente" in ql) or ("iot" in ql)
    want_display=any(w in ql for w in ["pantalla","display","screen"])
    want_alexa="alexa" in ql
    extras={"want_valve": want_valve,"want_bt": "bluetooth" in ql,"want_wifi": want_wifi,"want_display": want_display,
            "want_alarm": "alarma" in ql,"want_alexa": want_alexa,
            "valve_fams":["gassensorv","electrovalvula","valvula","valve"],
            "bt_fams":["easy-gas","easy gas"],
            "wifi_fams":["iot","inteligente","smart","wifi","app"],
            "display_fams":["easy","pantalla","display"],
            "alarm_words":["alarma","alerta","alert"],
            "alexa_fams":["alexa","iot"],"neg_words":[]}
    rescored=[]; positives=[]
    for idx,it in enumerate(items):
        st=_concat_fields(it); blocked=any(b in st for b in _GAS_BLOCK); base=max(0,30-idx)
        score, has_fam = _score_family(st, ql, _GAS_ALLOW_KEYWORDS, _GAS_ALLOW_FAMILIES, extras)
        if "gas" in st and not any(w in st for w in ["agua","tinaco","cisterna","water"]): score += 300
        if any(h in st for h in [
            "modulo-sensor-inteligente-de-nivel-de-gas",
            "sensor-de-gas-inteligente-con-electrovalvula-y-alertas-en-tiempo-real",
            "modulo-de-nivel-de-volumen-y-cierre-para-tanques-estacionarios-de-gas",
            "modulo-digital-de-nivel-de-gas-con-alcance-inalambrico-de-500-metros"
        ]): score += 500
        total=score+base-(50 if blocked else 0)
        is_valve=("valvula" in st) or ("válvula" in st) or ("electrovalvula" in st)
        rec=(total,score,blocked,has_fam,is_valve,it); rescored.append(rec)
        if score>=20: positives.append(rec)
    if positives:
        positives.sort(key=lambda x:x[0], reverse=True)
        if want_valve:
            vs=[r for r in positives if r[4]]; others=[r for r in positives if not r[4]]
            ordered=vs+others
        else:
            ordered=positives
        return [it for (_t,_s,_b,_hf,_valve,it) in ordered]
    soft=[]; 
    for idx,it in enumerate(items):
        st=_concat_fields(it)
        if "gas" in st: soft.append((max(0,30-idx), it))
    if soft:
        soft.sort(key=lambda x:x[0], reverse=True)
        return [it for (_score, it) in soft]
    rescored.sort(key=lambda x:x[0], reverse=True)
    return [it for (_t,_s,_b,_hf,_valve,it) in rescored]

def _rerank_for_water(query: str, items: list):
    ql=(query or "").lower()
    if _intent_from_query(query)!="water" or not items: return items
    want_valve=("valvula" in ql) or ("válvula" in ql)
    extras={"want_valve": want_valve,
            "want_ultra": any(w in ql for w in ["ultra","ultrason","ultrasónico","ultrasonico"]),
            "want_pressure": any(w in ql for w in ["presion","presión"]),
            "want_bt": "bluetooth" in ql,
            "want_wifi": ("wifi" in ql) or ("app" in ql),
            "valve_fams":["iot-waterv","iot waterv"],
            "ultra_fams":["waterultra","easy-waterultra","easy waterultra"],
            "pressure_fams":["iot-waterp","iot waterp"],
            "bt_fams":["easy-water","easy water","easy-waterultra","easy waterultra"],
            "wifi_fams":["iot-water","iot water","iot-waterv","iot waterv","iot-waterultra","iot waterultra"]}
    rescored=[]; positives=[]
    for idx,it in enumerate(items):
        st=_concat_fields(it); blocked=any(b in st for b in _WATER_BLOCK); base=max(0,30-idx)
        score, has_fam = _score_family(st, ql, _WATER_ALLOW_KEYWORDS, _WATER_ALLOW_FAMILIES, extras)
        total=score+base-(120 if blocked else 0)
        is_wv=("iot-waterv" in st) or ("iot waterv" in st)
        rec=(total,score,blocked,has_fam,is_wv,it); rescored.append(rec)
        if has_fam and score>=60 and not blocked: positives.append(rec)
    if positives:
        positives.sort(key=lambda x:x[0], reverse=True)
        if want_valve:
            wv=[r for r in positives if r[4]]; others=[r for r in positives if not r[4]]
            ordered=wv+others
        else:
            ordered=positives
        return [it for (_t,_s,_b,_hf,_wv,it) in ordered]
    soft=[]; water_words=["agua","tinaco","cisterna","nivel","water"]
    for idx,it in enumerate(items):
        st=_concat_fields(it)
        if any(w in st for w in water_words) and not any(b in st for b in _WATER_BLOCK):
            soft.append((max(0,30-idx), it))
    if soft:
        soft.sort(key=lambda x:x[0], reverse=True)
        return [it for (_score, it) in soft]
    rescored.sort(key=lambda x:x[0], reverse=True)
    return [it for (_t,_s,_b,_hf,_wv,it) in rescored]

def _apply_intent_rerank(query: str, items: list):
    intent=_intent_from_query(query)
    if intent=="water": return _rerank_for_water(query, items)
    if intent=="gas":   return _rerank_for_gas(query, items)
    return items

def _enforce_intent_gate(query: str, items: list):
    intent=_intent_from_query(query)
    if not intent or not items: return items
    filtered=[]
    for it in items:
        st=_concat_fields(it)
        if intent=="gas":
            water_indicators=["tinaco","cisterna","inundacion","inundación","flotador","boya","nivel de agua","agua para","water para","tinacos y cisternas","iot-waterv","iot-waterp","iot-water","easy-water","connect-water"]
            if any(ind in st for ind in water_indicators):
                if not any(g in st for g in ["gas","propano","butano","lp","estacionario"]):
                    continue
        elif intent=="water":
            gas_indicators=["gas","propano","butano","lp","estacionario","estacionaria","gassensor","gas-sensor","tanque estacionario","iot-gassensor","easy-gas","connect-gas"]
            if any(ind in st for ind in gas_indicators): continue
        filtered.append(it)
    return filtered or items

# ===========================================================
#  ESTATUS DE PEDIDOS (Google Sheets publicado)
#  Búsqueda por ORDEN_COMPRA y respuesta normalizada para el widget.
# ===========================================================
_DEFAULT_ORDERS_PUBHTML_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vS7MFutb5ikOvAvWsxuc164Txu30GeVkCGZAY3U_fUVmS_0MKMn6ta2hbbNc-hcmFbV0fyAe8A-7PGG/"
    "pubhtml?gid=1842193501&single=true"
)
ORDERS_PUBHTML_URL = os.getenv("ORDERS_PUBHTML_URL") or _DEFAULT_ORDERS_PUBHTML_URL
ORDERS_AUTORELOAD  = os.getenv("ORDERS_AUTORELOAD", "1")  # "1" = siempre recargar; "0" = usa caché según TTL
ORDERS_TTL_SECONDS = int(os.getenv("ORDERS_TTL_SECONDS", "45"))

_orders_cache = {
    "ts": 0.0,
    "rows": [],
    "headers": [],
    "mode": None,
    "source_url": "",
    "attempts": [],
}

# Columnas normalizadas que necesita la sección Pedidos.
# La búsqueda principal es ORDEN_COMPRA. DE_ORDEN queda como alias por compatibilidad
# con versiones anteriores de la hoja publicada.
_ORDER_SEARCH_COLS = ["ORDEN_COMPRA", "DE_ORDEN", "ORDER_ID", "PEDIDO", "FOLIO"]
_ORDER_SOURCE_COLS = [
    "ORDEN_COMPRA",
    "DE_ORDEN",
    "ORDER_ID",
    "PEDIDO",
    "FOLIO",
    "CLAVE_ARTICULO",
    "UNIDADES",
    "TOTAL_CON_IVA",
    "REM_PAQUETERIA",
    "REM_GUIA",
]
_ORDER_DISPLAY_FIELDS = [
    ("Orden de compra", "ORDEN_COMPRA"),
    ("SKU de producto", "CLAVE_ARTICULO"),
    ("Cantidad", "UNIDADES"),
    ("Total", "TOTAL_CON_IVA"),
    ("Paquetería", "REM_PAQUETERIA"),
    ("Guía", "REM_GUIA"),
]
_ORDER_TABLE_FIELDS = ["Orden de compra", "SKU de producto", "Cantidad", "Total", "Paquetería", "Guía"]

_HEADER_ALIASES = {
    # Búsqueda principal
    "ORDEN_COMPRA": "ORDEN_COMPRA",
    "ORDEN_DE_COMPRA": "ORDEN_COMPRA",
    "ORDEN_DE_COMPRAS": "ORDEN_COMPRA",
    "ORDEN_DE_VENTA": "ORDEN_COMPRA",
    "NO_ORDEN": "ORDEN_COMPRA",
    "NUMERO_ORDEN": "ORDEN_COMPRA",
    "NUMERO_DE_ORDEN": "ORDEN_COMPRA",
    "NUMERO_DE_PEDIDO": "ORDEN_COMPRA",
    "NUMERO_PEDIDO": "ORDEN_COMPRA",
    "N_PEDIDO": "ORDEN_COMPRA",
    "NO_PEDIDO": "ORDEN_COMPRA",
    "PEDIDO": "ORDEN_COMPRA",
    "PEDIDO_CLIENTE": "ORDEN_COMPRA",
    "PEDIDO_MARKETPLACE": "ORDEN_COMPRA",
    "ORDER": "ORDEN_COMPRA",
    "ORDER_ID": "ORDEN_COMPRA",
    "ORDER_NUMBER": "ORDEN_COMPRA",
    "ORDER_NO": "ORDEN_COMPRA",
    "OC": "ORDEN_COMPRA",
    "DE_ORDEN": "ORDEN_COMPRA",
    "D_ORDEN": "ORDEN_COMPRA",

    # Compatibilidad/fallback; no es la búsqueda principal.
    "FOLIO": "FOLIO",
    "NO_FOLIO": "FOLIO",
    "NUMERO_FOLIO": "FOLIO",
    "NUMERO_DE_FOLIO": "FOLIO",
    "FOLIO_PEDIDO": "FOLIO",
    "FOLIO_DE_PEDIDO": "FOLIO",
    "PEDIDO_MICROSIP": "FOLIO",

    # Datos de salida
    "CLAVE_ARTICULO": "CLAVE_ARTICULO",
    "CLAVE_DE_ARTICULO": "CLAVE_ARTICULO",
    "CLAVE": "CLAVE_ARTICULO",
    "SKU": "CLAVE_ARTICULO",
    "SKU_PRODUCTO": "CLAVE_ARTICULO",
    "SKU_DE_PRODUCTO": "CLAVE_ARTICULO",
    "ARTICULO": "CLAVE_ARTICULO",

    "UNIDADES": "UNIDADES",
    "UNIDAD": "UNIDADES",
    "CANTIDAD": "UNIDADES",
    "PIEZAS": "UNIDADES",
    "PZAS": "UNIDADES",

    "TOTAL_CON_IVA": "TOTAL_CON_IVA",
    "TOTAL": "TOTAL_CON_IVA",
    "TOTAL_IVA": "TOTAL_CON_IVA",
    "PRECIO_TOTAL": "TOTAL_CON_IVA",
    "IMPORTE": "TOTAL_CON_IVA",

    "REM_PAQUETERIA": "REM_PAQUETERIA",
    "PAQUETERIA": "REM_PAQUETERIA",
    "PAQUETERIA_REM": "REM_PAQUETERIA",
    "REMISION_PAQUETERIA": "REM_PAQUETERIA",

    "REM_GUIA": "REM_GUIA",
    "GUIA": "REM_GUIA",
    "NUMERO_GUIA": "REM_GUIA",
    "NUMERO_DE_GUIA": "REM_GUIA",
    "GUIA_REM": "REM_GUIA",
    "REMISION_GUIA": "REM_GUIA",
}

_ORDER_TOKEN_RE = re.compile(
    r"(?:folio|pedido|orden|order|estatus|status|seguimiento|rastreo|gu[ií]a)\s*(?:es|:|#|-)?\s*(#?[A-Za-z0-9][A-Za-z0-9._\-#]{2,})",
    re.IGNORECASE,
)
_ORDER_HASH_RE = re.compile(r"#\s*([A-Za-z0-9][A-Za-z0-9._\-]{2,})")
_ORDER_BARE_RE = re.compile(r"^\s*(#?[A-Za-z0-9][A-Za-z0-9._\-#]{2,})\s*$")
_ORDER_NUMERIC_RE = re.compile(r"(?:^|[^A-Za-z0-9])#?([0-9][0-9._\-]{2,24})(?:[^A-Za-z0-9]|$)")


def _strip_accents(text: str) -> str:
    return (text or "").translate(str.maketrans({
        "Á":"A", "É":"E", "Í":"I", "Ó":"O", "Ú":"U", "Ü":"U", "Ñ":"N",
        "á":"a", "é":"e", "í":"i", "ó":"o", "ú":"u", "ü":"u", "ñ":"n",
    }))


def _clean_cell(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _canon_header(text: str) -> str:
    t = html.unescape(str(text or "")).strip()
    t = _strip_accents(t).upper()
    t = re.sub(r"[^A-Z0-9]+", "_", t).strip("_")
    return _HEADER_ALIASES.get(t, t)


def _norm_header(text: str) -> str:
    return _canon_header(text)


def _order_key(value) -> str:
    """Normaliza ORDEN_COMPRA conservando su naturaleza alfanumérica.

    Ejemplos:
    702-7300318-1033843 -> 70273003181033843
    v44851776ekt-01 -> V44851776EKT01
    #9188.307766427-A -> 9188307766427A
    """
    s = html.unescape(str(value or "")).strip().upper()
    s = _strip_accents(s)
    return re.sub(r"[^A-Z0-9]", "", s)


def _folio_key(value) -> str:
    # Alias mantenido para compatibilidad con código anterior.
    return _order_key(value)


def _order_matches(sheet_value, requested_value) -> bool:
    a = _order_key(sheet_value)
    b = _order_key(requested_value)
    if not a or not b:
        return False
    if a == b:
        return True
    # Fallback controlado para valores puramente numéricos con ceros iniciales.
    if a.isdigit() and b.isdigit():
        return a.lstrip("0") == b.lstrip("0")
    return False


def _folio_matches(sheet_value, requested_value) -> bool:
    # Alias mantenido para compatibilidad con código anterior.
    return _order_matches(sheet_value, requested_value)


def _looks_like_bare_order(text: str) -> bool:
    s = str(text or "").strip()
    m = _ORDER_BARE_RE.match(s)
    if not m:
        return False
    token = m.group(1)
    key = _order_key(token)
    # Evita confundir consultas de SKUs cortos del catálogo con pedidos.
    return bool(len(key) >= 8 and any(ch.isdigit() for ch in token))


def _format_order_value(key: str, value) -> str:
    val = _clean_cell(value)
    if not val:
        return "—"
    if key == "TOTAL_CON_IVA":
        raw = val.replace("$", "").replace(",", "").strip()
        try:
            return f"${float(raw):,.2f}"
        except Exception:
            return val
    return val


def _candidate_order_urls(url: str) -> list[tuple[str, str]]:
    """Genera rutas alternativas para hojas publicadas por Google Sheets.

    Algunas publicaciones responden bien en /pubhtml, otras sólo en /pub?output=csv.
    También agregamos variantes de orden de querystring porque Google puede ser sensible
    con hojas publicadas por pestaña/gid.
    """
    if not url:
        return []
    candidates: list[tuple[str, str]] = []

    def add(mode: str, candidate: str):
        if candidate and (mode, candidate) not in candidates:
            candidates.append((mode, candidate))

    add("html", url)

    from urllib.parse import parse_qs, urlsplit, urlunsplit
    parsed = urlsplit(url)
    qs = parse_qs(parsed.query)
    gid = (qs.get("gid") or [""])[0]
    path = parsed.path

    if "/pubhtml" in path:
        pub_path = path.replace("/pubhtml", "/pub")
    else:
        pub_path = path

    if "/pub" in pub_path:
        base_pub = urlunsplit((parsed.scheme, parsed.netloc, pub_path, "", ""))
        if gid:
            add("csv", f"{base_pub}?gid={gid}&single=true&output=csv")
            add("csv", f"{base_pub}?output=csv&gid={gid}&single=true")
            add("csv", f"{base_pub}?gid={gid}&output=csv")
        add("csv", f"{base_pub}?single=true&output=csv")
        add("csv", f"{base_pub}?output=csv")

    # Variante gviz; suele devolver CSV aunque la vista pubhtml no traiga tabla parseable.
    # Funciona con muchas hojas publicadas /d/e/<PUBLIC_ID>.
    if "/spreadsheets/d/e/" in path:
        base_dir = path.split("/pub", 1)[0]
        if gid:
            add("csv", urlunsplit((parsed.scheme, parsed.netloc, f"{base_dir}/gviz/tq", f"tqx=out:csv&gid={gid}", "")))

    return candidates


def _header_score(cells: list[str]) -> int:
    normalized = [_canon_header(c) for c in cells]
    score = sum(1 for h in normalized if h in _ORDER_SOURCE_COLS)
    # ORDEN_COMPRA debe pesar más porque ahora es el identificador oficial.
    if "ORDEN_COMPRA" in normalized:
        score += 3
    return score


def _extract_sheet_matrix_from_html(html_text: str) -> list[list[str]]:
    if not BeautifulSoup:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.find_all("table")
    table = soup.find("table", {"class": "waffle"})
    if table is None and tables:
        # En pubhtml de Google, la tabla útil suele ser la más grande.
        table = max(tables, key=lambda t: len(t.find_all("tr")))
    if table is None:
        return []

    matrix = []
    for tr in table.find_all("tr"):
        cells = []
        for index, cell in enumerate(tr.find_all(["td", "th"])):
            classes = " ".join(cell.get("class") or [])
            text = _clean_cell(cell.get_text(" ", strip=True))
            is_visual_row_header = (
                index == 0 and cell.name == "th" and (
                    "row-headers" in classes or
                    re.fullmatch(r"\d+", text or "") is not None
                )
            )
            if is_visual_row_header:
                continue
            # Evita letras A, B, C... de encabezados visuales de columna.
            if cell.name == "th" and re.fullmatch(r"[A-Z]+", text or ""):
                continue
            cells.append(text)
        if any(cells):
            matrix.append(cells)
    return matrix


def _rows_from_matrix(matrix: list[list[str]]):
    if not matrix:
        return [], []
    best_idx = -1
    best_score = -1
    # Google puede incluir títulos y filas vacías antes del encabezado real.
    for i, cells in enumerate(matrix[:250]):
        score = _header_score(cells)
        if score > best_score:
            best_idx, best_score = i, score
    if best_idx < 0 or best_score <= 0:
        return [], []

    headers = [_norm_header(h) for h in matrix[best_idx]]
    rows = []
    for arr in matrix[best_idx + 1:]:
        if not any(arr):
            continue
        row = {}
        for j, val in enumerate(arr):
            if j < len(headers) and headers[j]:
                row[headers[j]] = _clean_cell(val)
        if row and any(v for v in row.values()):
            rows.append(row)
    return headers, rows


def _fetch_orders_html(url: str):
    if not (url and requests and BeautifulSoup):
        return [], []
    headers_req = {
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
    }
    r = requests.get(url, timeout=25, headers=headers_req)
    print(f"[ORDERS][HTML] fetch status={r.status_code} len={len(r.text or '')}", flush=True)
    r.raise_for_status()
    return _rows_from_matrix(_extract_sheet_matrix_from_html(r.text or ""))


def _fetch_orders_csv(url: str):
    if not (url and requests):
        return [], []
    headers_req = {
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/csv,text/plain,*/*;q=0.8",
        "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
    }
    r = requests.get(url, timeout=25, headers=headers_req)
    print(f"[ORDERS][CSV]  fetch status={r.status_code} len={len(r.text or '')} url={url}", flush=True)
    r.raise_for_status()

    text = r.text or ""
    if not text.strip():
        return [], []

    # Si Google devuelve HTML aunque pedimos CSV, intentamos parsearlo como tabla.
    if "<html" in text[:500].lower() or "<table" in text[:1000].lower():
        return _rows_from_matrix(_extract_sheet_matrix_from_html(text))

    reader = csv.reader(io.StringIO(text))
    rows_raw = list(reader)
    if not rows_raw:
        return [], []

    best_idx = 0
    best_score = -1
    for i, arr in enumerate(rows_raw[:250]):
        score = _header_score(arr)
        if score > best_score:
            best_idx, best_score = i, score
    if best_score <= 0:
        return [], []

    headers = [_norm_header(h) for h in rows_raw[best_idx]]
    rows = []
    for arr in rows_raw[best_idx + 1:]:
        if not any(arr):
            continue
        row = {}
        for j, val in enumerate(arr):
            if j < len(headers) and headers[j]:
                row[headers[j]] = _clean_cell(val)
        if row and any(v for v in row.values()):
            rows.append(row)
    return headers, rows


def _fetch_order_rows(force: bool=False):
    global _orders_cache
    now = time.time()

    if not force and ORDERS_AUTORELOAD != "1" and _orders_cache["rows"] and (now - _orders_cache["ts"] < ORDERS_TTL_SECONDS):
        return _orders_cache["rows"]

    url = ORDERS_PUBHTML_URL
    if not url:
        print("[ORDERS] missing ORDERS_PUBHTML_URL", flush=True)
        _orders_cache.update({"ts": now, "rows": [], "headers": [], "mode": None, "source_url": "", "attempts": []})
        return []

    headers, rows, mode, source_url = [], [], None, url
    attempts = []
    for candidate_mode, candidate_url in _candidate_order_urls(url):
        try:
            if candidate_mode == "html":
                h, r = _fetch_orders_html(candidate_url)
            else:
                h, r = _fetch_orders_csv(candidate_url)
            attempts.append({"mode": candidate_mode, "url": candidate_url, "headers": h[:12], "rows": len(r)})
            if r:
                headers, rows, mode, source_url = h, r, candidate_mode, candidate_url
                break
        except Exception as e:
            attempts.append({"mode": candidate_mode, "url": candidate_url, "error": repr(e)})
            print(f"[ORDERS] {candidate_mode.upper()} error for {candidate_url}: {e}", flush=True)

    _orders_cache.update({"ts": now, "rows": rows, "headers": headers, "mode": mode, "source_url": source_url, "attempts": attempts})
    print(f"[ORDERS] parsed mode={mode} headers={headers} rows={len(rows)}", flush=True)
    return rows


def _detect_order_number(text: str):
    if not text:
        return None
    s = str(text).strip()
    m = _ORDER_TOKEN_RE.search(s)
    if m and any(ch.isdigit() for ch in m.group(1)):
        return m.group(1).strip()
    m = _ORDER_HASH_RE.search(s)
    if m and any(ch.isdigit() for ch in m.group(1)):
        return m.group(1).strip()
    if _looks_like_bare_order(s):
        return _ORDER_BARE_RE.match(s).group(1).strip()
    # En chat de texto se permite detectar pedidos puramente numéricos, sin cortar guiones.
    m = _ORDER_NUMERIC_RE.search(s)
    if m:
        return m.group(1).strip()
    return None


def _looks_like_order_intent(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    keys = (
        "folio", "pedido", "orden", "order", "estatus", "status", "seguimiento", "rastreo",
        "mi compra", "mi pedido", "envio", "envío", "paqueteria", "paquetería", "guia", "guía"
    )
    return any(k in t for k in keys) or bool(_ORDER_TOKEN_RE.search(text)) or bool(_ORDER_HASH_RE.search(text))


def _order_candidate_columns(headers: list[str]) -> list[str]:
    candidates = []
    for col in ("ORDEN_COMPRA", "ORDER_ID", "PEDIDO", "DE_ORDEN"):
        if col in headers and col not in candidates:
            candidates.append(col)
    # Fallback: sólo si no existe columna de orden, permite FOLIO/otros campos.
    if not candidates:
        candidates.extend([h for h in headers if any(x in h for x in ("ORDEN", "ORDER", "PEDIDO"))])
    if not candidates and "FOLIO" in headers:
        candidates.append("FOLIO")
    return candidates or headers


def _build_order_item(row: dict) -> dict:
    item = {}
    for display_key, source_key in _ORDER_DISPLAY_FIELDS:
        value = row.get(source_key, "")
        item[display_key] = _format_order_value(source_key, value)
    return item


def _lookup_order(order_no: str):
    rows = _fetch_order_rows(force=True)
    if not rows:
        print("[ORDERS] no rows loaded", flush=True)
        return []

    headers = _orders_cache.get("headers", [])
    search_cols = _order_candidate_columns(headers)
    wanted = []
    for row in rows:
        if any(_order_matches(row.get(col, ""), order_no) for col in search_cols):
            wanted.append(_build_order_item(row))

    print(f"[ORDERS] lookup order={order_no} cols={search_cols} matches={len(wanted)}", flush=True)
    return wanted


def _render_order_vertical(rows: list, requested_order: str = "") -> str:
    if not rows:
        return "No encontramos información con ese número de pedido. Verifica el número tal como aparece en tu comprobante."
    orden = rows[0].get("Orden de compra", requested_order or "—")
    parts = [f"Pedido correspondiente al pedido: {orden}"]
    for i, row in enumerate(rows, 1):
        block = [f"Artículo {i}"]
        for key in _ORDER_TABLE_FIELDS:
            block.append(f"- {key}: {row.get(key, '—')}")
        parts.append("\n".join(block))
    return "\n\n".join(parts)

# ============== EXTRACTOR ROBUSTO (chat) ==============
def _extract_text_and_all_strings(payload):
    strings=[]
    for k in ("message","q","text","query","prompt","content","user_input"):
        v = payload.get(k) if isinstance(payload, dict) else None
        if isinstance(v, str) and v.strip():
            return v.strip(), v.strip()
        if isinstance(v, dict):
            for kk in ("message","q","text","query","prompt","content","user_input"):
                vv=v.get(kk)
                if isinstance(vv,str) and vv.strip():
                    return vv.strip(), vv.strip()
    def walk(o):
        if isinstance(o,str):
            s=o.strip()
            if s: strings.append(s)
        elif isinstance(o,dict):
            for vv in o.values(): walk(vv)
        elif isinstance(o,list):
            for it in o: walk(it)
    walk(payload)
    if not strings: return "", ""
    for s in strings:
        if _looks_like_order_intent(s):
            return s, " ".join(strings)
    for s in strings:
        if _detect_order_number(s):
            return s, " ".join(strings)
    return strings[0], " ".join(strings)

# =========================
#  Endpoints
# =========================
@app.post("/api/chat")
def chat():
    data = request.get_json(force=True) or {}
    primary_text, all_text = _extract_text_and_all_strings(data)
    original_query = (primary_text or request.args.get("q") or "").strip()

    detected_from_all = _detect_order_number(all_text)
    bare_order_token = _looks_like_bare_order(original_query or "")
    # El chat de texto se mantiene separado de Pedidos. Sólo desviamos si el usuario
    # menciona explícitamente pedido/orden/seguimiento o si escribe únicamente un número de pedido.
    order_intent = _looks_like_order_intent(original_query) or (bool(detected_from_all) and bare_order_token)

    incoming_context = _get_conversation_context(data)
    query, outgoing_context, context_resolution = _resolve_contextual_product_query(original_query, incoming_context)

    print(
        f"[CHAT] payload_keys={list(data.keys())} | extracted='{original_query}' | effective='{query}' | "
        f"context={context_resolution} | any_order='{detected_from_all}'",
        flush=True,
    )

    page=int(data.get("page") or 1)
    per_page=int(data.get("per_page") or 10)

    if not original_query and not detected_from_all:
        return jsonify({
            "answer":"¡Hola! Soy Maxter, tu asistente de compras de Master Electronics. ¿Qué producto estás buscando? Puedo ayudarte con soportes, antenas, controles, cables, sensores de agua, sensores de gas y mucho más.",
            "products":[],
            "pagination": _empty_pagination(per_page),
            "conversation_context": _clear_conversation_context(),
            "effective_query": "",
        })

    # ---------- DESVÍO: ESTATUS DE PEDIDO ----------
    try:
        if order_intent:
            order_no = _detect_order_number(original_query) or detected_from_all
            if order_no:
                rows = _lookup_order(order_no)
                answer = _render_order_vertical(rows, order_no)
                return jsonify({
                    "answer": answer,
                    "products": [],
                    "pagination": _empty_pagination(10),
                    "conversation_context": _clear_conversation_context(original_query, original_query),
                    "effective_query": original_query,
                })
    except Exception as e:
        print(f"[WARN] order-status pipeline error: {e}", flush=True)
    # ---------- FIN desvío de pedidos ----------

    if context_resolution.get("reask_clarification") == "antenna_type":
        return jsonify(_antenna_clarification_payload(
            per_page,
            base_query=(incoming_context.get("pending_clarification") or {}).get("base_query") or "antena",
            answer="Para continuar con la búsqueda de antenas, dime si la necesitas para interiores o para exteriores.",
        ))

    # Antenas genéricas: si no especifica interior/exterior, preguntamos antes
    # de mostrar productos para evitar mezclar familias incompatibles.
    if _should_clarify_antenna_type(query):
        return jsonify(_antenna_clarification_payload(per_page, base_query=query))

    # Flujo normal de productos + inteligencia técnica de catálogo.
    max_search = 200
    all_items, query_analysis = _search_catalog_candidates(query, max_search=max_search)
    all_items=_apply_intent_rerank(query, all_items)
    all_items=_enforce_intent_gate(query, all_items)
    all_items, compatibility_context = _apply_catalog_intelligence_safe(query, all_items)
    all_items, strict_guard_context = _apply_strict_catalog_guard(query, all_items, query_analysis)
    total_count=len(all_items)

    if not all_items:
        print(f"[CHAT][CATALOG_GUARD] rejected query='{query}' reason={strict_guard_context.get('reason')} families={strict_guard_context.get('families')} blocked={strict_guard_context.get('blocked_terms')}", flush=True)
        return jsonify(_catalog_no_match_payload(per_page, original_query, query))

    total_pages=(total_count + per_page - 1)//per_page
    start_idx=(page-1)*per_page; end_idx=start_idx+per_page
    if page<1: page=1
    elif page>total_pages:
        page=total_pages; start_idx=(page-1)*per_page; end_idx=start_idx+per_page
    items=all_items[start_idx:end_idx]
    pagination={"page":page,"per_page":per_page,"total":total_count,"total_pages":total_pages,
                "has_next": page < total_pages, "has_prev": page > 1}

    cards=_cards_from_items(items)
    answer=_generate_contextual_answer(query, items, total_count, page, per_page)
    if _ci_build_catalog_answer:
        try:
            smart_answer = _ci_build_catalog_answer(query, items, total_count, page, per_page, compatibility_context)
            if smart_answer:
                answer = smart_answer
        except Exception as e:
            print(f"[WARN] catalog intelligence answer failed: {e}", flush=True)
    if deeps and len(answer) > 50:
        try:
            enhanced_answer = deeps.chat(
                "Eres un asistente experto en productos electrónicos de Master Electronics México. Mejora esta respuesta para que sea más natural, específica y útil. Mantén intactos los criterios de compatibilidad técnica, advertencias y recomendaciones. No inventes precios, existencias, productos, categorías ni características. Si el texto recibido es una negativa de catálogo, consérvala exactamente y no agregues alternativas.",
                answer
            )
            if enhanced_answer and len(enhanced_answer) > 40:
                answer = enhanced_answer
        except Exception as e:
            print(f"[WARN] Deepseek enhancement error: {e}", flush=True)
    return jsonify({
        "answer": answer,
        "products": cards,
        "pagination": pagination,
        "conversation_context": _clear_conversation_context(original_query, query),
        "effective_query": query,
        "original_query": original_query,
    })

# ---------- Admin: diagnóstico de pedidos ----------
@app.get("/api/admin/orders-ping")
def admin_orders_ping():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    rows = _fetch_order_rows(force=True)
    sample = rows[:2] if rows else []
    return {
        "ok": True,
        "url": ORDERS_PUBHTML_URL,
        "mode": _orders_cache.get("mode"),
        "source_url": _orders_cache.get("source_url"),
        "headers": _orders_cache.get("headers", []),
        "rows_count": len(rows),
        "search_columns": _order_candidate_columns(_orders_cache.get("headers", [])),
        "attempts": _orders_cache.get("attempts", []),
        "sample": sample,
    }

@app.get("/api/admin/orders-find")
def admin_orders_find():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    raw = (request.args.get("order") or request.args.get("orden") or request.args.get("folio") or request.args.get("q") or "").strip()
    order_no = raw if _looks_like_bare_order(raw) else (_detect_order_number(raw) or raw)
    if not _order_key(order_no):
        return {"ok": False, "error": "bad order"}, 400
    rows = _fetch_order_rows(force=True)
    headers = _orders_cache.get("headers", [])
    search_cols = _order_candidate_columns(headers)
    matches = [r for r in rows if any(_order_matches(r.get(col, ""), order_no) for col in search_cols)]
    return {
        "ok": True,
        "target_order": order_no,
        "target_key": _order_key(order_no),
        "mode": _orders_cache.get("mode"),
        "source_url": _orders_cache.get("source_url"),
        "headers": headers,
        "search_columns": search_cols,
        "rows_count": len(rows),
        "matched_count": len(matches),
        "matched_samples": matches[:3],
        "normalized_items": [_build_order_item(r) for r in matches[:10]],
    }

# ---------- Admin varios ----------
def _do_reindex():
    try:
        print("[INDEX] Reindex started", flush=True); indexer.build()
        print("[INDEX] Reindex finished", flush=True); print(f"[INDEX] Stats: {indexer.stats()}", flush=True)
    except Exception as e:
        import traceback; print(f"[INDEX] Reindex failed: {e}\n{traceback.format_exc()}", flush=True)

@app.post("/api/admin/reindex")
def reindex():
    if not _admin_ok(request): return jsonify({"ok":False,"error":"unauthorized"}), 401
    threading.Thread(target=_do_reindex, daemon=True).start(); return {"ok": True}

@app.get("/api/admin/stats")
def admin_stats():
    if not _admin_ok(request): return jsonify({"ok":False,"error":"unauthorized"}), 401
    return indexer.stats()

@app.get("/api/admin/diag")
def admin_diag():
    if not _admin_ok(request): return jsonify({"ok":False,"error":"unauthorized"}), 401
    return {"ok": True, "env": {"STORE_BASE_URL": os.getenv("STORE_BASE_URL"),
                                 "FORCE_REST": os.getenv("FORCE_REST"),
                                 "REQUIRE_ACTIVE": os.getenv("REQUIRE_ACTIVE"),
                                 "CHAT_WRITER": CHAT_WRITER}}

@app.get("/api/admin/preview")
def admin_preview():
    if not _admin_ok(request): return jsonify({"ok":False,"error":"unauthorized"}), 401
    q=(request.args.get("q") or "").strip(); k=int(request.args.get("k") or 12)
    items, _analysis = _search_catalog_candidates(q, max_search=max(k,90))
    items=_apply_intent_rerank(q, items)
    items=_enforce_intent_gate(q, items)
    items, _compatibility_context = _apply_catalog_intelligence_safe(q, items)
    items, strict_guard_context = _apply_strict_catalog_guard(q, items, _analysis)
    items=items[:k]
    return {"q": q, "k": k, "strict_guard": strict_guard_context, "items": _plain_items(items)}

@app.get("/api/admin/search")
def admin_search():
    if not _admin_ok(request): return jsonify({"ok":False,"error":"unauthorized"}), 401
    q=(request.args.get("q") or "").strip(); k=int(request.args.get("k") or 12)
    items=indexer.search(q, k=max(k,90))
    return {"q": q, "k": k, "items": _plain_items(items)}

@app.get("/api/admin/products")
def admin_products():
    if not _admin_ok(request): return jsonify({"ok":False,"error":"unauthorized"}), 401
    return {"items": indexer.sample_products(20)}

@app.get("/api/admin/discards")
def admin_discards():
    if not _admin_ok(request): return jsonify({"ok":False,"error":"unauthorized"}), 401
    return indexer.discard_stats()

# ---------- Endpoint dedicado de pedidos (independiente al buscador/DeepSeek) ----------
@app.route("/api/orders", methods=["POST", "OPTIONS"])
def api_orders():
    """Consulta de pedido por ORDEN_COMPRA. Acepta {order}, {orden}, {order_no}, {folio}, {message} o {q}."""
    if request.method == "OPTIONS":
        return ("", 204)

    try:
        data = request.get_json(force=True) or {}
    except Exception:
        data = {}

    raw = (
        data.get("order") or
        data.get("orden") or
        data.get("order_no") or
        data.get("folio") or
        data.get("message") or
        data.get("q") or
        ""
    )
    raw = str(raw).strip()
    if not raw:
        return jsonify({"ok": False, "error": "missing order"}), 400

    # En el widget el usuario escribe directamente la ORDEN_COMPRA, por eso conservamos
    # el texto completo. Sólo extraemos con regex si el mensaje viene con frases como
    # "mi pedido es 702-...".
    order_no = raw if _looks_like_bare_order(raw) else (_detect_order_number(raw) or raw)
    if not _order_key(order_no):
        return jsonify({"ok": False, "error": "invalid order format"}), 400

    try:
        rows = _lookup_order(order_no)
        answer = _render_order_vertical(rows, order_no)
        return jsonify({
            "ok": True,
            "order": order_no,
            "folio": order_no,  # compatibilidad con versiones anteriores del widget
            "items": rows,
            "answer": answer,
            "fields": _ORDER_TABLE_FIELDS,
        })
    except Exception as e:
        print(f"[ORDERS] /api/orders error: {e}", flush=True)
        return jsonify({"ok": False, "error": "internal error"}), 500

# ---------- MAIN ----------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
