# -*- coding: utf-8 -*-
import os, re, threading
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

@app.get("/")
def home():
    return ("<h1>Maxter backend</h1>"
            "<p>OK ✅. Endpoints: "
            '<a href="/health">/health</a>, '
            '<code>POST /api/chat</code>, '
            '<code>POST /api/admin/reindex</code>, '
            '<code>GET /api/admin/stats</code>, '
            '<code>GET /api/admin/search?q=...</code>, '
            '<code>GET /api/admin/discards</code>, '
            '<code>GET /api/admin/products</code>, '
            '<code>GET /api/admin/diag</code>, '
            '<code>GET /api/admin/preview?q=...</code>'
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
    """Genera respuestas más contextuales y naturales basadas en la consulta"""
    ql = (query or "").lower()
    p = _detect_patterns(query)
    
    # Detectar tipo de producto buscado
    product_type = None
    brands = []
    size_mentioned = None
    
    # Detectar marcas mencionadas
    known_brands = ["sony", "samsung", "lg", "panasonic", "tcl", "hisense", "roku", "apple", "xiaomi"]
    for brand in known_brands:
        if brand in ql:
            brands.append(brand.capitalize())
    
    # Detectar tipos de productos específicos
    if any(w in ql for w in ["sensor", "detector", "medidor"]):
        if p.get("water"):
            product_type = "sensores de agua"
        elif p.get("gas"):
            product_type = "sensores de gas"
        else:
            product_type = "sensores"
    elif any(w in ql for w in ["control", "remoto"]):
        product_type = "controles remotos"
    elif any(w in ql for w in ["soporte", "bracket", "mount"]):
        product_type = "soportes"
    elif any(w in ql for w in ["cable", "cordon"]):
        product_type = "cables"
    elif any(w in ql for w in ["divisor", "splitter"]):
        product_type = "divisores"
    elif any(w in ql for w in ["antena"]):
        product_type = "antenas"
    elif any(w in ql for w in ["camara", "cámara"]):
        product_type = "cámaras"
    elif any(w in ql for w in ["bocina", "altavoz", "speaker"]):
        product_type = "bocinas"
    
    # Detectar tamaños mencionados
    sizes = re.findall(r'\b(\d{1,3})\s*["\'"pulgadas]?\b', ql)
    if sizes:
        size_mentioned = sizes[0]
    
    # Construir respuesta contextual
    response_parts = []
    
    # Saludo contextual específico para sensores de gas
    if product_type == "sensores de gas":
        response_parts.append("¡Perfecto! Tenemos una excelente selección de sensores de gas")
        
        # Analizar qué productos específicos están realmente en los resultados
        found_products = []
        product_titles = [item.get("title", "").lower() for item in items]
        
        for title in product_titles:
            if "iot-gassensorv" in title or "electroválvula" in title or "válvula" in title:
                found_products.append("con válvula electrónica")
            elif "iot-gassensor" in title or ("iot" in title and "sensor" in title and "gas" in title):
                found_products.append("con WiFi y app Master IOT")
            elif "easy-gas" in title or ("easy" in title and "gas" in title):
                found_products.append("con pantalla integrada")
            elif "connect-gas" in title or ("connect" in title and "gas" in title):
                found_products.append("con monitoreo remoto")
        
        # Solo mencionar características de productos que realmente están en los resultados
        if found_products:
            response_parts.append(" " + ", ".join(list(set(found_products))))
        else:
            # Respuesta genérica si no se identifican productos específicos
            response_parts.append(" para tanques estacionarios con diferentes características")
        
        # Información específica basada en la consulta
        additional_specs = []
        if p.get("valve") or any(w in ql for w in ["valvula", "válvula", "electrovalvula"]):
            additional_specs.append("priorizando modelos con válvula electrónica automática")
        if p.get("wifi") or "app" in ql:
            additional_specs.append("con conectividad WiFi y monitoreo desde app")
        if p.get("display") or any(w in ql for w in ["pantalla", "display"]):
            additional_specs.append("con pantalla integrada para lectura directa")
        if "alexa" in ql:
            additional_specs.append("compatibles con Alexa")
        
        if additional_specs:
            response_parts.append(", " + ", ".join(additional_specs))
    
    elif product_type == "sensores de agua":
        response_parts.append("¡Claro! Tenemos excelentes opciones en sensores de agua")
        specifics = []
        if p.get("valve"):
            specifics.append("con válvula automática (IOT-WATERV)")
        if p.get("ultra"):
            specifics.append("ultrasónicos de alta precisión (IOT-WATERULTRA)")
        if not specifics:
            specifics.append("de nuestras líneas IOT Water, Easy Water y Connect")
        response_parts.append(" " + ", ".join(specifics))
    
    elif product_type:
        if brands:
            response_parts.append(f"¡Perfecto! Para {product_type} de {', '.join(brands)}")
        else:
            response_parts.append(f"¡Claro! Tenemos excelentes opciones en {product_type}")
    else:
        response_parts.append("¡Hola! He encontrado estas opciones para ti")
    
    # Información específica basada en patrones adicionales
    additional_specs = []
    if p.get("matrix"):
        additional_specs.append(f"con matriz {p['matrix']}")
    elif size_mentioned:
        additional_specs.append(f"compatibles con pantallas de {size_mentioned}\"")
    elif p.get("inches"):
        additional_specs.append(f"para pantallas de {', '.join(p['inches'])}\"")
    
    if additional_specs:
        response_parts.append(" " + ", ".join(additional_specs))
    
    # Información de resultados
    if total_count > per_page:
        showing = min(per_page, len(items))
        response_parts.append(f". Mostrando {showing} de {total_count} productos disponibles")
    else:
        response_parts.append(f". Encontré {len(items)} productos que coinciden perfectamente")
    
    # Sugerencias adicionales para sensores
    if product_type in ["sensores de gas", "sensores de agua", "sensores"]:
        suggestions = []
        if p.get("valve"):
            suggestions.append("con válvula incluida")
        if p.get("wifi"):
            suggestions.append("con conectividad WiFi")
        if p.get("bt"):
            suggestions.append("con Bluetooth")
        if p.get("display"):
            suggestions.append("con pantalla")
        if p.get("alarm"):
            suggestions.append("con sistema de alertas")
        
        if suggestions:
            response_parts.append(f", incluyendo opciones {', '.join(suggestions)}")
    
    base_response = "".join(response_parts) + "."
    
    # Agregar call-to-action si hay más resultados
    if total_count > per_page:
        base_response += f" ¿Te gustaría ver más opciones o prefieres que filtre por alguna característica específica?"
    
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

# ---------- Señales / familias CORREGIDAS ----------
_WATER_ALLOW_FAMILIES = [
    "iot-waterv","iot-waterultra","iot-waterp","iot-water",
    "easy-waterultra","easy-water","iot waterv","iot waterultra","iot waterp","iot water","easy waterultra","easy water",
    "connect-water","connect water"
]
_WATER_ALLOW_KEYWORDS = ["tinaco","cisterna","nivel","agua","water","inundacion","inundación","flotador","boya"]

# FAMILIAS DE GAS CORREGIDAS - handles y nombres reales de productos
_GAS_ALLOW_FAMILIES = [
    # Handles reales de productos (encontrados en master.com.mx)
    "modulo-sensor-inteligente-de-nivel-de-gas",  # IOT-GASSENSOR
    "sensor-de-gas-inteligente-con-electrovalvula-y-alertas-en-tiempo-real",  # IOT-GASSENSORV
    "modulo-digital-de-nivel-de-gas-con-alcance-inalambrico-de-500-metros",  # CONNECT-GAS
    
    # SKUs y nombres de productos
    "iot-gassensorv","iot-gassensor","easy-gas","connect-gas",
    "iot gassensorv","iot gassensor","easy gas","connect gas",
    
    # Variaciones adicionales
    "sensor-inteligente-de-nivel-de-gas", "dispositivo-inteligente-sensor-gas",
    "modulo-sensor-gas", "sensor-gas-tanque-estacionario",
    "gassensorv","gassensor","gas-sensor"
]

_GAS_ALLOW_KEYWORDS = [
    # Keywords principales de gas
    "gas","tanque","estacionario","estacionaria","lp","propano","butano",
    "nivel","medidor","porcentaje","volumen",
    
    # Variaciones específicas de productos
    "gassensor","gas-sensor","sensor de gas","medidor de gas","detector de gas",
    "modulo sensor inteligente","dispositivo inteligente sensor",
    "sensor inteligente nivel gas","medidor inteligente gas",
    
    # Características específicas encontradas en descripciones
    "tanques estacionarios","nivel de gas","monitoreo gas",
    "alertas gas","app master iot","compatible alexa gas"
]

# Blocklist GAS REVISADA - más específica y menos agresiva
_GAS_BLOCK = [
    # Módulos Arduino específicos (no productos IOT principales)
    "ar-gasc","ar-flame","ar-photosensor","megasensor","ar-megasensor",
    # Módulos genéricos de desarrollo
    "arduino","módulo generico","modulo generico","mq-2","mq2","mq-","shield",
    # Productos de control de plagas
    "pest","plaga","mosquito","insect","insecto","pest-killer","pest killer",
    # Productos eléctricos (medidores de consumo)
    "easy-electric","easy electric","eléctrico","electrico","electricidad",
    "kwh","kw/h","consumo electrico","tarifa","electric meter","medidor de consumo","contador electrico",
    # Productos de lluvia/auto (no gas LP)
    "ar-rain","rain","lluvia","carsensor","bm-carsensor","auto","vehiculo","vehículo",
    # Solo familias de agua específicas, no todos los productos de agua
    "iot-waterv","iot-waterultra","iot-waterp","easy-waterultra"
]

# FAMILIAS DE AGUA - removemos bloqueadores demasiado amplios
_WATER_BLOCK = [
    # Solo productos de gas específicos
    "iot-gassensorv","iot-gassensor","easy-gas","connect-gas",
    # Productos de auto/lluvia
    "bm-carsensor","carsensor","car","auto","vehiculo","vehículo",
    "ar-rain","rain","lluvia",
    # Productos de otros gases
    "ar-gasc","gasc"," co2","humo","smoke",
    "ar-knock","knock","golpe"
]

def _concat_fields(it) -> str:
    """
    IMPORTANTE: incluye 'body' (descripción) para capturar señales como Alexa, IP67, válvula, alarma, WiFi, etc.
    """
    v = it.get("variant", {})
    body = (it.get("body") or "").lower()
    if len(body) > 1500:
        body = body[:1500]
    parts = [
        it.get("title") or "",
        it.get("handle") or "",
        it.get("tags") or "",
        it.get("vendor") or "",
        it.get("product_type") or "",
        v.get("sku") or "",
        body,
    ]
    if isinstance(it.get("skus"), (list, tuple)):
        parts.extend([x for x in it["skus"] if x])
    return " ".join(parts).lower()

# ---------- INTENCIÓN (mejorada para gas) ----------
def _intent_from_query(q: str):
    """
    Regla clara mejorada:
    - Si menciona 'gas' o señales inequívocas de gas (tanque, estacionario/a, lp, propano, butano, gassensor) => 'gas'
    - Si NO menciona 'gas' y sí menciona agua/tinaco/cisterna/inundación/boya/flotador => 'water'
    - 'nivel' o 'medidor' NO determinan la intención por sí solos (son ambiguos).
    """
    ql = (q or "").lower()

    # Señales fuertes de gas (expandidas)
    gas_hard = [
        "gas", "tanque", "estacionario", "estacionaria", "lp", "propano", "butano",
        "gassensor", "gas-sensor", "iot-gassensor", "easy-gas", "connect-gas"
    ]
    if any(w in ql for w in gas_hard):
        return "gas"

    # Señales fuertes de agua
    water_hard = ["agua", "tinaco", "cisterna", "inundacion", "inundación", "boya", "flotador"]
    if any(w in ql for w in water_hard):
        return "water"

    return None

def _score_family(st: str, ql: str, allow_keywords, allow_fams, extras) -> tuple[int, bool]:
    """Devuelve (score, has_family). Mejorado para detectar mejor las familias."""
    s=0
    has_family = any(fam in st for fam in allow_fams)
    
    # Scoring por keywords
    if any(w in st for w in allow_keywords): 
        s += 25  # Aumentado para keywords
    
    # Scoring fuerte por pertenencia a familia
    if has_family: 
        s += 100  # Aumentado significativamente
    
    # Extras específicos
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
    
    # Penalizaciones por términos negativos
    for neg in extras.get("neg_words", []):
        if neg in st: 
            s -= 80
    
    return s, has_family

# --------- Rerank/filtrado Agua (mejorado) ----------
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
        st=_concat_fields(it)
        blocked=any(b in st for b in _WATER_BLOCK)
        base=max(0,30-idx)
        score, has_fam = _score_family(st, ql, _WATER_ALLOW_KEYWORDS, _WATER_ALLOW_FAMILIES, extras)
        total=score+base-(120 if blocked else 0)
        is_wv=("iot-waterv" in st) or ("iot waterv" in st)
        rec=(total,score,blocked,has_fam,is_wv,it); rescored.append(rec)
        if has_fam and score>=60 and not blocked: positives.append(rec)

    # HARD FILTER si hay positivos
    if positives:
        positives.sort(key=lambda x:x[0], reverse=True)
        if want_valve:
            wv=[r for r in positives if r[4]]; others=[r for r in positives if not r[4]]
            ordered=wv+others
        else:
            ordered=positives
        return [it for (_t,_s,_b,_hf,_wv,it) in ordered]

    # Fallback suave
    soft = []
    water_words = ["agua","tinaco","cisterna","nivel","water"]
    for idx,it in enumerate(items):
        st=_concat_fields(it)
        if any(w in st for w in water_words) and not any(b in st for b in _WATER_BLOCK):
            soft.append((max(0,30-idx), it))
    if soft:
        soft.sort(key=lambda x:x[0], reverse=True)
        return [it for (_score, it) in soft]

    # Último recurso
    rescored.sort(key=lambda x:x[0], reverse=True)
    return [it for (_t,_s,_b,_hf,_wv,it) in rescored]

# --------- Rerank/filtrado Gas (CORREGIDO) ----------
def _rerank_for_gas(query: str, items: list):
    ql=(query or "").lower()
    if _intent_from_query(query)!="gas" or not items: return items
    
    want_valve=("valvula" in ql) or ("válvula" in ql) or ("electrovalvula" in ql)
    want_wifi=("wifi" in ql) or ("app" in ql) or ("inteligente" in ql) or ("iot" in ql)
    want_display=any(w in ql for w in ["pantalla","display","screen"])
    want_alexa="alexa" in ql
    
    extras={
        "want_valve": want_valve, 
        "want_bt": "bluetooth" in ql,
        "want_wifi": want_wifi,
        "want_display": want_display,
        "want_alarm": "alarma" in ql,
        "want_alexa": want_alexa,
        # Familias corregidas que coinciden con productos reales
        "valve_fams":["iot-gassensorv","iot gassensorv","gassensorv"],
        "bt_fams":["easy-gas","easy gas"],
        "wifi_fams":["iot-gassensor","iot gassensor","connect-gas","connect gas","iot-gassensorv","iot gassensorv"],
        "display_fams":["easy-gas","easy gas"],
        "alarm_words":["alarma","alerta","alert"],
        "alexa_fams":["iot-gassensor","iot-gassensorv","iot gassensor","iot gassensorv"],
        "neg_words":[]  # Minimizamos las palabras negativas
    }
    
    rescored=[]; positives=[]
    for idx,it in enumerate(items):
        st=_concat_fields(it)
        blocked=any(b in st for b in _GAS_BLOCK)
        base=max(0,30-idx)
        score, has_fam = _score_family(st, ql, _GAS_ALLOW_KEYWORDS, _GAS_ALLOW_FAMILIES, extras)
        
        # Boost extra para productos específicos de gas (CRÍTICO)
        if any(handle in st for handle in [
            "modulo-sensor-inteligente-de-nivel-de-gas",  # IOT-GASSENSOR
            "sensor-de-gas-inteligente-con-electrovalvula-y-alertas-en-tiempo-real"  # IOT-GASSENSORV
        ]):
            score += 150  # Boost masivo para productos principales
        elif any(fam in st for fam in ["iot-gassensor", "easy-gas", "connect-gas"]):
            score += 75   # Boost fuerte para otros productos de gas
        
        # Boost adicional por SKU/nombre de modelo específico
        if any(sku in st for sku in ["iot-gassensor", "iot-gassensorv"]):
            score += 100
        
        total=score+base-(140 if blocked else 0)
        is_valve=("iot-gassensorv" in st) or ("iot gassensorv" in st) or ("gassensorv" in st)
        rec=(total,score,blocked,has_fam,is_valve,it); rescored.append(rec)
        
        # Positivo SOLO si pertenece a familia de gas Y tiene score decente
        if has_fam and score>=40 and not blocked: positives.append(rec)

    # HARD FILTER si hay positivos -> solo familias de gas
    if positives:
        positives.sort(key=lambda x:x[0], reverse=True)
        if want_valve:
            vs=[r for r in positives if r[4]]; others=[r for r in positives if not r[4]]
            ordered=vs+others
        else:
            ordered=positives
        return [it for (_t,_s,_b,_hf,_valve,it) in ordered]

    # Fallback suave - buscar productos que contengan "gas" sin bloqueos
    soft = []
    for idx,it in enumerate(items):
        st=_concat_fields(it)
        if ("gas" in st) and not any(b in st for b in _GAS_BLOCK):
            soft.append((max(0,30-idx), it))
    if soft:
        soft.sort(key=lambda x:x[0], reverse=True)
        return [it for (_score, it) in soft]

    # Último recurso
    rescored.sort(key=lambda x:x[0], reverse=True)
    return [it for (_t,_s,_b,_hf,_valve,it) in rescored]

def _apply_intent_rerank(query: str, items: list):
    intent=_intent_from_query(query)
    if intent=="water": return _rerank_for_water(query, items)
    if intent=="gas":   return _rerank_for_gas(query, items)
    return items

# --------- PUERTA FINAL MEJORADA (HARD GATE) CONTRA MEZCLAS ---------
def _enforce_intent_gate(query: str, items: list):
    """Filtra de forma estricta resultados de la otra categoría si la intención está clara."""
    intent=_intent_from_query(query)
    if not intent or not items:
        return items

    filtered=[]
    for it in items:
        st=_concat_fields(it)
        if intent=="gas":
            # Filtrar productos de agua
            if any(fam in st for fam in _WATER_ALLOW_FAMILIES):
                continue
            # Ser más específico con las palabras de agua
            if any(w in st for w in ["tinaco","cisterna","inundacion","inundación","boya","flotador"]):
                continue
        elif intent=="water":
            # Filtrar productos de gas
            if any(fam in st for fam in _GAS_ALLOW_FAMILIES):
                continue
            # Filtrar si menciona gas explícitamente
            if any(w in st for w in ["gassensor","gas-sensor","iot-gassensor","easy-gas","connect-gas"]):
                continue
        filtered.append(it)

    return filtered or items  # Si no queda nada, devolver original

# ----------------- Endpoints -----------------
@app.post("/api/chat")
def chat():
    data=request.get_json(force=True) or {}
    query=(data.get("message") or "").strip()
    page=int(data.get("page") or 1)
    per_page=int(data.get("per_page") or 10)  # Aumentado de 5 a 10
    
    if not query:
        return jsonify({
            "answer":"¡Hola! Soy Maxter, tu asistente de compras de Master Electronics. ¿Qué producto estás buscando? Puedo ayudarte con soportes, antenas, controles, cables, sensores de agua, sensores de gas y mucho más.",
            "products":[],
            "pagination": {
                "page": 1,
                "per_page": per_page,
                "total": 0,
                "total_pages": 0,
                "has_next": False,
                "has_prev": False
            }
        })

    # Buscar muchos más candidatos para poder paginar
    max_search = 200  # Buscar hasta 200 productos
    all_items=indexer.search(query, k=max_search)
    all_items=_apply_intent_rerank(query, all_items)
    all_items=_enforce_intent_gate(query, all_items)
    
    total_count = len(all_items)
    
    if not all_items:
        # Mensaje mejorado para sensores de gas
        fallback_msg = "No encontré resultados directos para tu búsqueda. "
        if any(w in query.lower() for w in ["gas", "tanque", "estacionario", "gassensor"]):
            fallback_msg += "Para sensores de gas, prueba con: 'sensor gas tanque estacionario', 'IOT-GASSENSOR', 'sensor gas con válvula', 'medidor gas WiFi' o 'EASY-GAS'."
        elif any(w in query.lower() for w in ["agua", "tinaco", "cisterna"]):
            fallback_msg += "Para sensores de agua, prueba con: 'sensor agua tinaco', 'IOT-WATER', 'sensor nivel cisterna' o 'medidor agua WiFi'."
        else:
            fallback_msg += "Prueba con palabras clave específicas como 'divisor hdmi 1×4', 'soporte pared 55\"', 'control Samsung', 'sensor gas tanque' o 'sensor agua tinaco'."
        
        return jsonify({
            "answer": fallback_msg,
            "products":[],
            "pagination": {
                "page": 1,
                "per_page": per_page,
                "total": 0,
                "total_pages": 0,
                "has_next": False,
                "has_prev": False
            }
        })

    # Calcular paginación
    total_pages = (total_count + per_page - 1) // per_page  # ceil division
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    
    # Validar página
    if page < 1:
        page = 1
    elif page > total_pages:
        page = total_pages
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
    
    items = all_items[start_idx:end_idx]
    
    # Información de paginación
    pagination = {
        "page": page,
        "per_page": per_page,
        "total": total_count,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1
    }

    cards=_cards_from_items(items)
    
    # Generar respuesta contextual mejorada
    answer = _generate_contextual_answer(query, items, total_count, page, per_page)
    
    # Usar Deepseek para pulir la respuesta si está disponible
    if deeps and len(answer) > 50:
        try:
            enhanced_answer = deeps.chat(
                "Eres un asistente experto en productos electrónicos de Master Electronics México. Mejora esta respuesta para que sea más natural, específica y útil. Mantén toda la información técnica y de productos, pero hazla más conversacional y amigable. No inventes datos. Si se mencionan sensores de gas, destaca las líneas IOT-GASSENSOR, IOT-GASSENSORV, EASY-GAS y CONNECT-GAS:",
                answer
            )
            if enhanced_answer and len(enhanced_answer) > 40:
                answer = enhanced_answer
        except Exception as e:
            print(f"[WARN] Deepseek enhancement error: {e}", flush=True)
    
    return jsonify({
        "answer": answer, 
        "products": cards,
        "pagination": pagination
    })

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
    return {
        "ok": True,
        "env": {
            "STORE_BASE_URL": os.getenv("STORE_BASE_URL"),
            "FORCE_REST": os.getenv("FORCE_REST"),
            "REQUIRE_ACTIVE": os.getenv("REQUIRE_ACTIVE"),
            "CHAT_WRITER": CHAT_WRITER,
        }
    }

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

# --------- MAIN: arrancar el servidor en Render ---------
if __name__ == "__main__":
    # Render expone PORT; por defecto usamos 10000 para locales.
    port = int(os.getenv("PORT", "10000"))
    # host=0.0.0.0 para aceptar tráfico externo en Render
    app.run(host="0.0.0.0", port=port)
