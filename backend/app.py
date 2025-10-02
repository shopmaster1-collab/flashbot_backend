# -*- coding: utf-8 -*-
import os, re, threading, time, html
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

from .shopify_client import ShopifyClient
from .indexer import CatalogIndexer
from .utils import money

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

# ---- CORS
_allowed = [o.strip() for o in (os.getenv("ALLOWED_ORIGINS") or "*").split(",") if o.strip()]
CORS(app, resources={r"/*": {"origins": _allowed,
                             "allow_headers": ["Content-Type", "X-Admin-Secret"],
                             "methods": ["GET", "POST", "OPTIONS"]}})

# ---- Servicios (ShopifyClient lee envs internamente)
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
#  Servir widget estático desde /widget/*
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
    resp.headers["Cache-Control"] = "public, max-age=604800"  # 7 días
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

# --------- util de patrones para la redacción ----------
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
            "image": it["image"],
            "price": money(v.get("price")) if v.get("price") is not None else None,
            "compare_at_price": money(v.get("compare_at_price")) if v.get("compare_at_price") else None,
            "buy_url": it["buy_url"], "product_url": it["product_url"],
            "inventory": v.get("inventory"),
        })
    return cards

def _plain_items(items):
    out=[]
    for it in items:
        v=it["variant"]
        out.append({"title": it.get("title"), "sku": v.get("sku"),
                    "price": money(v.get("price")) if v.get("price") is not None else None,
                    "product_url": it.get("product_url"), "buy_url": it.get("buy_url")})
    return out

# ---------- Señales / familias (sin cambios de negocio) ----------
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

# =========================
#  ESTATUS DE PEDIDOS (Google Sheets pubhtml)
# =========================
ORDERS_PUBHTML_URL = os.getenv("ORDERS_PUBHTML_URL") or ""
ORDERS_AUTORELOAD = os.getenv("ORDERS_AUTORELOAD", "1")  # "1" = siempre recargar; "0" = caché según TTL
ORDERS_TTL_SECONDS = int(os.getenv("ORDERS_TTL_SECONDS", "45"))
_orders_cache = {"ts": 0.0, "rows": [], "headers": []}

# Columnas canónicas que devolveremos al usuario
_ORDER_COLS = [
    "Plataforma","SKU","Pzas","Precio Unitario","Precio Total","Envio",
    "Fecha Inicio","EN PROCESO","Fecha Termino","Almacen","Paqueteria",
    "Guia","Fecha envió","Fecha Entrega"
]

# Normalización de encabezados (acentos/variantes -> canónico)
_HEADER_MAP = {
    # Nº de orden (muchas variantes)
    "# DE ORDEN":"# de Orden","NO. DE ORDEN":"# de Orden","NÚMERO DE ORDEN":"# de Orden",
    "NUMERO DE ORDEN":"# de Orden","Nº DE ORDEN":"# de Orden","N° DE ORDEN":"# de Orden",
    "NÚM. DE ORDEN":"# de Orden","NUM. DE ORDEN":"# de Orden","NO DE ORDEN":"# de Orden",
    "NRO DE ORDEN":"# de Orden","ID DE ORDEN":"# de Orden","ORDEN":"# de Orden",
    "NÚMERO DE PEDIDO":"# de Orden","NUMERO DE PEDIDO":"# de Orden","PEDIDO":"# de Orden",
    "# ORDEN":"# de Orden","#":"# de Orden","ORDER ID":"# de Orden","ORDER":"# de Orden",

    # Otras columnas
    "PLATAFORMA":"Plataforma",
    "SKU":"SKU","PZAS":"Pzas","PIEZAS":"Pzas","CANTIDAD":"Pzas",
    "PRECIO UNITARIO":"Precio Unitario","PRECIO":"Precio Unitario",
    "PRECIO TOTAL":"Precio Total","TOTAL":"Precio Total",
    "ENVÍO":"Envio","ENVIO":"Envio",
    "FECHA INICIO":"Fecha Inicio","EN PROCESO":"EN PROCESO",
    "FECHA TÉRMINO":"Fecha Termino","FECHA TERMINO":"Fecha Termino",
    "FECHA ENTREGA":"Fecha Entrega",
    "ALMACÉN":"Almacen","ALMACEN":"Almacen",
    "PAQUETERÍA":"Paqueteria","PAQUETERIA":"Paqueteria",
    "GUÍA":"Guia","GUIA":"Guia",
    "FECHA ENVÍO":"Fecha envió","FECHA ENVIÓ":"Fecha envió","FECHA ENVIO":"Fecha envió",
}
_ORDER_RE = re.compile(r"(?:^|[^0-9])#?\s*([0-9]{3,15})\b")

def _norm_header(t: str) -> str:
    t=(t or "").strip()
    t=html.unescape(t)
    t=re.sub(r"\s+"," ", t)
    u=t.upper().replace("Á","A").replace("É","E").replace("Í","I").replace("Ó","O").replace("Ú","U").replace("Ñ","N")
    return _HEADER_MAP.get(u, t)

def _orders_int(val) -> int | None:
    """'6506'->6506, '6,506'->6506, '6506.0'/'6506,0'->6506."""
    if val is None: return None
    s = str(val).strip()
    if not s: return None
    m = re.match(r"^\s*([0-9]{1,})(?:[.,]0+)\s*$", s)
    if m:
        try: return int(m.group(1))
        except Exception: pass
    digits = re.sub(r"\D+", "", s)
    if not digits: return None
    try: return int(digits)
    except Exception: return None

def _fetch_order_rows(force: bool=False):
    """Lee la tabla 'waffle' del pubhtml y devuelve filas normalizadas."""
    global _orders_cache
    now=time.time()

    if ORDERS_AUTORELOAD!="1" and _orders_cache["rows"] and (now - _orders_cache["ts"] < ORDERS_TTL_SECONDS):
        return _orders_cache["rows"]

    if not (ORDERS_PUBHTML_URL and requests and BeautifulSoup):
        print("[ORDERS] missing deps or URL", flush=True)
        return []

    try:
        r=requests.get(ORDERS_PUBHTML_URL, timeout=25, headers={"Cache-Control":"no-cache", "Pragma":"no-cache"})
        r.raise_for_status()
    except Exception as e:
        print(f"[ORDERS] fetch error: {e}", flush=True)
        return []

    soup=BeautifulSoup(r.text, "html.parser")
    tables=soup.find_all("table")
    waffle=soup.find("table", {"class":"waffle"}) or (tables[-1] if tables else None)
    if not waffle:
        print(f"[ORDERS] no table found. total_tables={len(tables)}", flush=True)
        return []

    # Headers
    headers=[]
    thead=waffle.find("thead")
    if thead:
        thr=thead.find("tr")
        if thr:
            headers=[_norm_header(c.get_text(strip=True)) for c in thr.find_all(["th","td"])]
    if not headers:
        # fallback: primer tr con th
        first_th_tr=None
        for tr in waffle.find_all("tr"):
            if tr.find("th"):
                first_th_tr=tr; break
        if first_th_tr:
            headers=[_norm_header(c.get_text(strip=True)) for c in first_th_tr.find_all(["th","td"])]
    if not headers:
        # último fallback: primer tr
        tr=waffle.find("tr")
        if tr:
            headers=[_norm_header(c.get_text(strip=True)) for c in tr.find_all(["th","td"])]

    # Body rows
    rows=[]
    tbody=waffle.find("tbody")
    body_trs=tbody.find_all("tr") if tbody else [tr for tr in waffle.find_all("tr")]
    # si usamos thead, saltar la primera fila (encabezados)
    skip_first = bool(thead)
    for i, tr in enumerate(body_trs):
        if skip_first and i == 0:
            continue
        tds=[c.get_text(strip=True) for c in tr.find_all(["td","th"])]
        if not tds:
            continue
        # saltar filas idénticas a headers
        if headers and all(_norm_header(v) == headers[j] if j < len(headers) else False for j,v in enumerate(tds)):
            continue
        row={}
        for j,val in enumerate(tds):
            if j < len(headers):
                row[headers[j]] = val
        if row and any(v for v in row.values()):
            rows.append(row)

    _orders_cache.update({"ts": now, "rows": rows, "headers": headers})
    print(f"[ORDERS] parsed headers={headers} rows={len(rows)}", flush=True)
    return rows

def _detect_order_number(text: str):
    if not text: return None
    m=_ORDER_RE.search(text)
    return m.group(1) if m else None

def _looks_like_order_intent(text: str) -> bool:
    if not text: return False
    t=text.lower()
    keys=("pedido","orden","order","estatus","status","seguimiento","rastreo","mi compra","mi pedido","envio","envío","paqueteria","paquetería","guia","guía")
    return any(k in t for k in keys) or bool(_ORDER_RE.search(t))

def _order_candidate_columns(headers: list[str]) -> list[str]:
    """Prioriza '# de Orden' y luego cualquier columna cuyo nombre sugiera 'orden/pedido/order'."""
    cands=[]
    if "# de Orden" in headers: cands.append("# de Orden")
    for h in headers:
        u=h.upper()
        if h in cands: 
            continue
        if ("ORDEN" in u) or ("PEDIDO" in u) or ("ORDER" in u):
            cands.append(h)
    return cands or headers  # como último recurso, todas

def _lookup_order(order_number: str):
    rows=_fetch_order_rows(force=True)
    if not rows: 
        print("[ORDERS] no rows loaded", flush=True)
        return []
    target_int = _orders_int(order_number)
    if target_int is None:
        return []

    headers = _orders_cache.get("headers", [])
    cands = _order_candidate_columns(headers)
    skip_cols = set(["SKU","Precio Unitario","Precio Total","Pzas","Plataforma","Envio","Fecha Inicio","EN PROCESO","Fecha Termino","Almacen","Paqueteria","Guia","Fecha envió","Fecha Entrega"])

    # 1) Intento principal: solo columnas candidatas
    wanted=[]
    for r in rows:
        for key in cands:
            val = r.get(key)
            if val is None: 
                continue
            row_int = _orders_int(val)
            if row_int is None: 
                continue
            if row_int == target_int:
                item={col: (r.get(col, "") or "—") for col in _ORDER_COLS}
                wanted.append(item)
                break
    if wanted:
        print(f"[ORDERS] lookup (candidates) order={target_int} matches={len(wanted)}", flush=True)
        return wanted

    # 2) Fallback: escaneo controlado por todas las columnas (evitando precios/SKU)
    for r in rows:
        for key, val in r.items():
            if key in skip_cols: 
                continue
            row_int = _orders_int(val)
            if row_int is None: 
                continue
            if row_int == target_int:
                item={col: (r.get(col, "") or "—") for col in _ORDER_COLS}
                wanted.append(item)
                break
    print(f"[ORDERS] lookup (fallback) order={target_int} matches={len(wanted)}", flush=True)
    return wanted

def _render_order_vertical(rows: list) -> str:
    if not rows:
        return "No encontramos información con ese número de pedido. Verifica el número tal como aparece en tu comprobante."
    parts=[]
    for i,r in enumerate(rows,1):
        blk=[f"**Artículo {i}**"]
        for k in _ORDER_COLS:
            blk.append(f"- **{k}:** {r.get(k,'—')}")
        parts.append("\n".join(blk))
    return "\n\n".join(parts)

# --------- EXTRACTOR ROBUSTO ----------
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

# ----------------- Endpoints -----------------
@app.post("/api/chat")
def chat():
    data = request.get_json(force=True) or {}
    primary_text, all_text = _extract_text_and_all_strings(data)
    query = (primary_text or request.args.get("q") or "").strip()

    detected_from_all = _detect_order_number(all_text)
    order_intent = _looks_like_order_intent(query) or bool(detected_from_all)

    print(f"[CHAT] payload_keys={list(data.keys())} | extracted='{query}' | any_order='{detected_from_all}'", flush=True)

    page=int(data.get("page") or 1)
    per_page=int(data.get("per_page") or 10)

    if not query and not detected_from_all:
        return jsonify({
            "answer":"¡Hola! Soy Maxter, tu asistente de compras de Master Electronics. ¿Qué producto estás buscando? Puedo ayudarte con soportes, antenas, controles, cables, sensores de agua, sensores de gas y mucho más.",
            "products":[],
            "pagination":{"page":1,"per_page":per_page,"total":0,"total_pages":0,"has_next":False,"has_prev":False}
        })

    # ---------- DESVÍO: ESTATUS DE PEDIDO ----------
    try:
        if order_intent:
            order_no = _detect_order_number(query) or detected_from_all
            if order_no:
                rows = _lookup_order(order_no)
                answer = _render_order_vertical(rows)
                return jsonify({"answer": answer, "products": [],
                                "pagination": {"page":1,"per_page":10,"total":0,"total_pages":0,"has_next":False,"has_prev":False}})
    except Exception as e:
        print(f"[WARN] order-status pipeline error: {e}", flush=True)
    # ---------- FIN desvío de pedidos ----------

    # Flujo normal de productos (INTACTO)
    max_search = 200
    all_items=indexer.search(query, k=max_search)
    all_items=_apply_intent_rerank(query, all_items)
    all_items=_enforce_intent_gate(query, all_items)
    total_count=len(all_items)

    if not all_items:
        fallback_msg = "No encontré resultados directos para tu búsqueda. "
        if any(w in (query or "").lower() for w in ["gas","tanque","estacionario","gassensor"]):
            fallback_msg += "Para sensores de gas, prueba con: 'sensor gas tanque estacionario', 'IOT-GASSENSOR', 'sensor gas con válvula', 'medidor gas WiFi' o 'EASY-GAS'."
        elif any(w in (query or "").lower() for w in ["agua","tinaco","cisterna"]):
            fallback_msg += "Para sensores de agua, prueba con: 'sensor agua tinaco', 'IOT-WATER', 'sensor nivel cisterna' o 'medidor agua WiFi'."
        else:
            fallback_msg += "Prueba con palabras clave específicas como 'divisor hdmi 1×4', 'soporte pared 55\"', 'control Samsung', 'sensor gas tanque' o 'sensor agua tinaco'."
        return jsonify({"answer": fallback_msg,"products":[],
                        "pagination":{"page":1,"per_page":per_page,"total":0,"total_pages":0,"has_next":False,"has_prev":False}})

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
    if deeps and len(answer) > 50:
        try:
            enhanced_answer = deeps.chat(
                "Eres un asistente experto en productos electrónicos de Master Electronics México. Mejora esta respuesta para que sea más natural, específica y útil. Mantén toda la información técnica y de productos, pero hazla más conversacional y amigable. No inventes datos.",
                answer
            )
            if enhanced_answer and len(enhanced_answer) > 40:
                answer = enhanced_answer
        except Exception as e:
            print(f"[WARN] Deepseek enhancement error: {e}", flush=True)
    return jsonify({"answer": answer, "products": cards, "pagination": pagination})

# --- Endpoint de diagnóstico admin
@app.get("/api/admin/orders-ping")
def admin_orders_ping():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    rows=_fetch_order_rows(force=True)
    sample = rows[:2] if rows else []
    return {"ok": True,
            "url": ORDERS_PUBHTML_URL,
            "headers": _orders_cache.get("headers", []),
            "rows_count": len(rows),
            "sample": sample}

@app.get("/api/admin/orders-find")
def admin_orders_find():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    raw = (request.args.get("order") or "").strip()
    target = _orders_int(raw)
    if not target:
        return {"ok": False, "error": "bad order"}, 400
    rows = _fetch_order_rows(force=True)
    headers = _orders_cache.get("headers", [])
    cands = _order_candidate_columns(headers)
    matches = []
    for r in rows:
        for k in cands:
            vi = _orders_int(r.get(k))
            if vi and vi == target:
                matches.append(r); break
    # fallback
    if not matches:
        skip_cols = set(["SKU","Precio Unitario","Precio Total","Pzas","Plataforma","Envio","Fecha Inicio","EN PROCESO","Fecha Termino","Almacen","Paqueteria","Guia","Fecha envió","Fecha Entrega"])
        for r in rows:
            for k,v in r.items():
                if k in skip_cols: 
                    continue
                vi = _orders_int(v)
                if vi and vi == target:
                    matches.append(r); break
    return {"ok": True, "target": target, "headers": headers, "candidate_cols": cands,
            "rows_count": len(rows), "matched_count": len(matches), "matched_samples": matches[:3]}

# --- Endpoint de diagnóstico del chat (opcional)
@app.post("/api/admin/chat-debug")
def admin_chat_debug():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(force=True) or {}
    txt, all_txt = _extract_text_and_all_strings(payload)
    return {"ok": True, "extracted": txt, "all_strings": all_txt,
            "looks_order": _looks_like_order_intent(txt) or bool(_detect_order_number(all_txt)),
            "order_no_primary": _detect_order_number(txt),
            "order_no_any": _detect_order_number(all_txt),
            "payload_keys": list(payload.keys())}

# --- Reindex background
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
    items=indexer.search(q, k=max(k,90))
    items=_apply_intent_rerank(q, items)
    items=_enforce_intent_gate(q, items)
    items=items[:k]
    return {"q": q, "k": k, "items": _plain_items(items)}

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

# --- Endpoint dedicado: /api/orders (independiente del buscador de productos)
@app.route("/api/orders", methods=["POST", "OPTIONS"])
def api_orders():
    """Consulta de estatus de pedido por número (p. ej. 6506 o #6506)."""
    if request.method == "OPTIONS":
        return ("", 204)  # preflight OK

    try:
        data = request.get_json(force=True) or {}
    except Exception:
        data = {}

    raw = (data.get("order") or data.get("message") or data.get("q") or "").strip()
    if not raw:
        return jsonify({ "ok": False, "error": "missing order" }), 400

    order_no = _detect_order_number(raw) or raw
    try_int = _orders_int(order_no)
    if try_int is None:
        return jsonify({ "ok": False, "error": "invalid order format" }), 400

    try:
        rows = _lookup_order(str(try_int))
        answer = _render_order_vertical(rows)
        return jsonify({ "ok": True, "order": str(try_int), "items": rows, "answer": answer })
    except Exception as e:
        print(f"[ORDERS] /api/orders error: {e}", flush=True)
        return jsonify({ "ok": False, "error": "internal error" }), 500

# --------- MAIN ---------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
