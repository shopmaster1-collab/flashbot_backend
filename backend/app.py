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

# =========================
#  NUEVO: servir widget estático desde /widget/*
# =========================
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# Busca la carpeta 'widget' en ubicaciones comunes (según cómo se ejecute el paquete)
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
    # Cache 7 días
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp
# =========================

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
            '<code>GET /api/admin/preview?q=...</code>, '
            '<code>GET /widget/widget.css</code>, '
            '<code>GET /widget/widget.js</code>'
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
            if "electroválvula" in title or "válvula" in title:
                found_products.append("con válvula electrónica")
            elif "easy" in title and "gas" in title:
                found_products.append("con pantalla integrada")
            elif "connect" in title and "gas" in title:
                found_products.append("con monitoreo remoto")
            elif "iot" in title and "gas" in title:
                found_products.append("con WiFi y app Master IOT")
        
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

# ---------- Señales / familias ULTRA PERMISIVAS ----------
_WATER_ALLOW_FAMILIES = [
    "iot-waterv","iot-waterultra","iot-waterp","iot-water",
    "easy-waterultra","easy-water","iot waterv","iot waterultra","iot waterp","iot water","easy waterultra","easy water",
    "connect-water","connect water"
]
_WATER_ALLOW_KEYWORDS = ["tinaco","cisterna","nivel","agua","water","inundacion","inundación","flotador","boya"]

# FAMILIAS DE GAS ULTRA PERMISIVAS - TODOS LOS HANDLES ENCONTRADOS
_GAS_ALLOW_FAMILIES = [
    # Handles reales encontrados en la web
    "modulo-sensor-inteligente-de-nivel-de-gas",
    "sensor-de-gas-inteligente-con-electrovalvula-y-alertas-en-tiempo-real",
    "modulo-de-nivel-de-volumen-y-cierre-para-tanques-estacionarios-de-gas-iot-gassensor-presentacion-sin-valvula",
    "modulo-digital-de-nivel-de-gas-con-alcance-inalambrico-de-500-metros",
    
    # SKUs y nombres de productos
    "iot-gassensorv","iot-gassensor","easy-gas","connect-gas",
    "iot gassensorv","iot gassensor","easy gas","connect gas",
    
    # Variaciones adicionales ultra permisivas
    "sensor-inteligente-de-nivel-de-gas", "dispositivo-inteligente-sensor-gas",
    "modulo-sensor-gas", "sensor-gas-tanque-estacionario",
    "gassensorv","gassensor","gas-sensor", "gasensor",
    "sensor-gas", "medidor-gas", "detector-gas",
    "nivel-gas", "tanque-gas", "estacionario-gas"
]

_GAS_ALLOW_KEYWORDS = [
    # Keywords principales ULTRA PERMISIVOS
    "gas","tanque","estacionario","estacionaria","lp","propano","butano",
    "nivel","medidor","porcentaje","volumen","gassensor","gasensor",
    
    # Variaciones específicas TODAS las posibles
    "gas-sensor","sensor de gas","medidor de gas","detector de gas",
    "modulo sensor inteligente","dispositivo inteligente sensor",
    "sensor inteligente nivel gas","medidor inteligente gas",
    "nivel de gas","monitoreo gas","alertas gas",
    "app master iot","compatible alexa gas",
    "tanques estacionarios","sensor gas wifi",
    "iot gas", "easy gas", "connect gas",
    "electrovalvula gas", "valvula gas"
]

# Blocklist GAS MÍNIMA - Solo lo absolutamente necesario
_GAS_BLOCK = [
    # Solo productos evidentemente no relacionados
    "ar-rain","rain","lluvia","carsensor","bm-carsensor","auto","vehiculo","vehículo",
    # Solo medidores eléctricos específicos
    "kwh","kw/h","consumo electrico","tarifa electrica","electric meter"
]

# FAMILIAS DE AGUA - Mínima también
_WATER_BLOCK = [
    # Solo productos evidentemente de gas
    "propano","butano","lp gas","tanque estacionario gas"
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

# ---------- INTENCIÓN ULTRA PERMISIVA para gas ----------
def _intent_from_query(q: str):
    """Regla mejorada para detectar gas con máxima sensibilidad."""
    ql = (q or "").lower()

    # Señales de gas ULTRA PERMISIVAS
    gas_signals = [
        "gas", "tanque", "estacionario", "estacionaria", "lp", "propano", "butano",
        "gassensor", "gas-sensor", "iot-gassensor", "easy-gas", "connect-gas",
        "gasensor", "sensor gas", "medidor gas", "detector gas", "nivel gas"
    ]
    if any(w in ql for w in gas_signals):
        return "gas"

    # Señales fuertes de agua
    water_hard = ["agua", "tinaco", "cisterna", "inundacion", "inundación", "boya", "flotador"]
    if any(w in ql for w in water_hard):
        return "water"

    return None

def _score_family(st: str, ql: str, allow_keywords, allow_fams, extras) -> tuple[int, bool]:
    """Scoring ULTRA PERMISIVO para gas."""
    s=0
    has_family = any(fam in st for fam in allow_fams)
    
    # Scoring permisivo por keywords
    if any(w in st for w in allow_keywords): 
        s += 50  # Aumentado masivamente
    
    # Scoring masivo por pertenencia a familia
    if has_family: 
        s += 200  # Aumentado a 200 para gas
    
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
    
    # Penalizaciones mínimas
    for neg in extras.get("neg_words", []):
        if neg in st: 
            s -= 30  # Reducido drásticamente
    
    return s, has_family

# --------- Rerank ULTRA PERMISIVO para Gas ----------
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
        # Familias ULTRA PERMISIVAS
        "valve_fams":["gassensorv", "electrovalvula", "valvula", "valve"],
        "bt_fams":["easy-gas","easy gas"],
        "wifi_fams":["iot", "inteligente", "smart", "wifi", "app"],
        "display_fams":["easy", "pantalla", "display"],
        "alarm_words":["alarma","alerta","alert"],
        "alexa_fams":["alexa", "iot"],
        "neg_words":[]  # Sin palabras negativas para gas
    }
    
    rescored=[]; positives=[]
    for idx,it in enumerate(items):
        st=_concat_fields(it)
        blocked=any(b in st for b in _GAS_BLOCK)
        base=max(0,30-idx)
        score, has_fam = _score_family(st, ql, _GAS_ALLOW_KEYWORDS, _GAS_ALLOW_FAMILIES, extras)
        
        # BOOST MASIVO para productos que contengan "gas" pero NO "agua"
        if "gas" in st and not any(water_word in st for water_word in ["agua", "tinaco", "cisterna", "water"]):
            score += 300  # Boost masivo solo para productos genuinos de gas
        
        # Boost adicional para handles específicos encontrados
        gas_handles = [
            "modulo-sensor-inteligente-de-nivel-de-gas",
            "sensor-de-gas-inteligente-con-electrovalvula-y-alertas-en-tiempo-real",
            "modulo-de-nivel-de-volumen-y-cierre-para-tanques-estacionarios-de-gas",
            "modulo-digital-de-nivel-de-gas-con-alcance-inalambrico-de-500-metros"
        ]
        if any(handle in st for handle in gas_handles):
            score += 500  # Boost ultra masivo
        
        total=score+base-(50 if blocked else 0)  # Penalización mínima por bloqueo
        is_valve=("valvula" in st) or ("válvula" in st) or ("electrovalvula" in st)
        rec=(total,score,blocked,has_fam,is_valve,it); rescored.append(rec)
        
        # Threshold ULTRA BAJO para inclusión
        if score >= 20:  # Threshold bajísimo
            positives.append(rec)

    # Si hay productos con "gas", priorizar esos
    if positives:
        positives.sort(key=lambda x:x[0], reverse=True)
        if want_valve:
            vs=[r for r in positives if r[4]]; others=[r for r in positives if not r[4]]
            ordered=vs+others
        else:
            ordered=positives
        return [it for (_t,_s,_b,_hf,_valve,it) in ordered]

    # Fallback super permisivo - CUALQUIER cosa con "gas"
    soft = []
    for idx,it in enumerate(items):
        st=_concat_fields(it)
        if "gas" in st:  # Sin más filtros
            soft.append((max(0,30-idx), it))
    if soft:
        soft.sort(key=lambda x:x[0], reverse=True)
        return [it for (_score, it) in soft]

    # Último recurso - devolver todos
    rescored.sort(key=lambda x:x[0], reverse=True)
    return [it for (_t,_s,_b,_hf,_valve,it) in rescored]

# --------- Rerank/filtrado Agua (sin cambios) ----------
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

    if positives:
        positives.sort(key=lambda x:x[0], reverse=True)
        if want_valve:
            wv=[r for r in positives if r[4]]; others=[r for r in positives if not r[4]]
            ordered=wv+others
        else:
            ordered=positives
        return [it for (_t,_s,_b,_hf,_wv,it) in ordered]

    soft = []
    water_words = ["agua","tinaco","cisterna","nivel","water"]
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

# --------- PUERTA FINAL MEJORADA (QUIRÚRGICA) ----------
def _enforce_intent_gate(query: str, items: list):
    """Filtra productos de categorías incorrectas con precisión quirúrgica."""
    intent=_intent_from_query(query)
    if not intent or not items:
        return items

    filtered=[]
    for it in items:
        st=_concat_fields(it)
        title = (it.get("title") or "").lower()
        handle = (it.get("handle") or "").lower()
        
        if intent=="gas":
            # FILTRAR productos evidentemente de AGUA cuando se busca GAS
            water_indicators = [
                "tinaco", "cisterna", "inundacion", "inundación", "flotador", "boya",
                "nivel de agua", "agua para", "water para", "tinacos y cisternas",
                "iot-waterv", "iot-waterp", "iot-water", "easy-water", "connect-water"
            ]
            
            # Si es claramente un producto de agua, filtrar
            if any(indicator in st for indicator in water_indicators):
                # Excepción: si también menciona gas explícitamente, mantener
                if not any(gas_word in st for gas_word in ["gas", "propano", "butano", "lp", "estacionario"]):
                    continue
            
        elif intent=="water":
            # FILTRAR productos evidentemente de GAS cuando se busca AGUA
            gas_indicators = [
                "gas", "propano", "butano", "lp", "estacionario", "estacionaria",
                "gassensor", "gas-sensor", "tanque estacionario",
                "iot-gassensor", "easy-gas", "connect-gas"
            ]
            
            # Si es claramente un producto de gas, filtrar
            if any(indicator in st for indicator in gas_indicators):
                continue
        
        filtered.append(it)

    return filtered or items

# =========================
#  NUEVO (ADICIONAL): ESTATUS DE PEDIDOS desde Google Sheets (pubhtml)
#  * No altera el flujo de productos *
# =========================
import time, html
try:
    import requests
    from bs4 import BeautifulSoup
except Exception:
    requests = None
    BeautifulSoup = None

ORDERS_PUBHTML_URL = os.getenv("ORDERS_PUBHTML_URL") or ""
ORDERS_AUTORELOAD = os.getenv("ORDERS_AUTORELOAD", "1")  # "1" lee siempre; "0" usa TTL
ORDERS_TTL_SECONDS = int(os.getenv("ORDERS_TTL_SECONDS", "45"))

_orders_cache = {"ts": 0.0, "rows": []}

_ORDER_COLS = [
    "# de Orden", "SKU", "Pzas", "Precio Unitario", "Precio Total",
    "Fecha Inicio", "EN PROCESO", "Paquetería", "Fecha envío", "Fecha Entrega"
]

_HEADER_MAP = {
    "# DE ORDEN": "# de Orden", "NO. DE ORDEN": "# de Orden", "NÚMERO DE ORDEN": "# de Orden",
    "NÚMERO DE PEDIDO": "# de Orden", "ORDEN": "# de Orden", "# ORDEN": "# de Orden", "#": "# de Orden",
    "SKU": "SKU", "PZAS": "Pzas", "PIEZAS": "Pzas", "CANTIDAD": "Pzas",
    "PRECIO UNITARIO": "Precio Unitario", "PRECIO": "Precio Unitario",
    "PRECIO TOTAL": "Precio Total", "TOTAL": "Precio Total",
    "FECHA INICIO": "Fecha Inicio",
    "EN PROCESO": "EN PROCESO",
    "PAQUETERÍA": "Paquetería", "PAQUETERIA": "Paquetería",
    "FECHA ENVÍO": "Fecha envío", "FECHA ENVIÓ": "Fecha envío", "FECHA ENVIO": "Fecha envío",
    "FECHA ENTREGA": "Fecha Entrega",
}

_ORDER_RE = re.compile(r"(?:^|[^0-9])#?\s*([0-9]{4,15})\b")

def _norm_header(t: str) -> str:
    t = (t or "").strip()
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t)
    u = t.upper().replace("Á","A").replace("É","E").replace("Í","I").replace("Ó","O").replace("Ú","U")
    return _HEADER_MAP.get(u, t)

def _fetch_order_rows(force: bool=False):
    global _orders_cache
    now = time.time()

    if ORDERS_AUTORELOAD != "1":
        if _orders_cache["rows"] and (now - _orders_cache["ts"] < ORDERS_TTL_SECONDS):
            return _orders_cache["rows"]

    if not (ORDERS_PUBHTML_URL and requests and BeautifulSoup):
        return []

    try:
        r = requests.get(ORDERS_PUBHTML_URL, timeout=20, headers={"Cache-Control":"no-cache"})
        r.raise_for_status()
    except Exception as e:
        print(f"[WARN] orders pubhtml fetch error: {e}", flush=True)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    trs = table.find_all("tr")
    if not trs:
        return []

    headers = [_norm_header(c.get_text(strip=True)) for c in trs[0].find_all(["th","td"])]

    rows=[]
    for tr in trs[1:]:
        tds = [c.get_text(strip=True) for c in tr.find_all(["td","th"])]
        if not any(tds):
            continue
        row={}
        for i, val in enumerate(tds):
            if i < len(headers):
                row[headers[i]] = val
        if row:
            rows.append(row)

    _orders_cache["rows"] = rows
    _orders_cache["ts"] = now
    return rows

def _detect_order_number(text: str):
    if not text: return None
    m = _ORDER_RE.search(text)
    return m.group(1) if m else None

def _looks_like_order_intent(text: str) -> bool:
    if not text: return False
    t = text.lower()
    keys = ("pedido","orden","order","estatus","status","seguimiento","rastreo","mi compra","mi pedido")
    return any(k in t for k in keys)

def _lookup_order(order_number: str):
    rows = _fetch_order_rows(force=True)
    if not rows:
        return []
    wanted=[]
    target = re.sub(r"\D+","", str(order_number))
    for r in rows:
        num = r.get("# de Orden") or r.get("# Orden") or r.get("#") or ""
        digits = re.sub(r"\D+","", str(num))
        if digits == target:
            item={}
            for col in _ORDER_COLS:
                item[col] = r.get(col, "") or "—"
            wanted.append(item)
    return wanted

def _render_order_vertical(rows: list) -> str:
    """Formato mobile-first (widget vertical): bloques de 'clave: valor' por ítem."""
    if not rows:
        return "No encontramos información con ese número de pedido. Verifica el número tal como aparece en tu comprobante."
    parts=[]
    for i, r in enumerate(rows, 1):
        blk=[f"**Artículo {i}**"]
        for k in _ORDER_COLS:
            blk.append(f"- **{k}:** {r.get(k,'—')}")
        parts.append("\n".join(blk))
    return "\n\n".join(parts)

# ----------------- Endpoints -----------------
@app.post("/api/chat")
def chat():
    data=request.get_json(force=True) or {}
    # Cambios mínimos: soportar 'message', 'q' y 'text'
    query=(data.get("message") or data.get("q") or data.get("text") or "").strip()
    page=int(data.get("page") or 1)
    per_page=int(data.get("per_page") or 10)
    
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

    # ---------- NUEVO: Desvío quirúrgico para ESTATUS DE PEDIDO ----------
    try:
        if _looks_like_order_intent(query):
            order_no = _detect_order_number(query)
            if order_no:
                rows = _lookup_order(order_no)  # lee siempre la publicación viva
                answer = _render_order_vertical(rows)
                return jsonify({
                    "answer": answer,
                    "products": [],
                    "pagination": {
                        "page": 1, "per_page": 10, "total": 0, "total_pages": 0,
                        "has_next": False, "has_prev": False
                    }
                })
    except Exception as e:
        # Si algo falla en pedidos, continuamos con el flujo normal de productos
        print(f"[WARN] order-status pipeline error: {e}", flush=True)
    # ---------- FIN desvío de pedidos ------------------------------------

    # Buscar muchos más candidatos para poder paginar
    max_search = 200
    all_items=indexer.search(query, k=max_search)
    all_items=_apply_intent_rerank(query, all_items)
    all_items=_enforce_intent_gate(query, all_items)
    
    total_count = len(all_items)
    
    if not all_items:
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
    total_pages = (total_count + per_page - 1) // per_page
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    
    if page < 1:
        page = 1
    elif page > total_pages:
        page = total_pages
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
    
    items = all_items[start_idx:end_idx]
    
    pagination = {
        "page": page,
        "per_page": per_page,
        "total": total_count,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1
    }

    cards=_cards_from_items(items)
    answer = _generate_contextual_answer(query, items, total_count, page, per_page)
    
    if deeps and len(answer) > 50:
        try:
            enhanced_answer = deeps.chat(
                "Eres un asistente experto en productos electrónicos de Master Electronics México. Mejora esta respuesta para que sea más natural, específica y útil. Mantén toda la información técnica y de productos, pero hazla más conversacional y amigable. No inventes datos. Si se mencionan sensores de gas, mantén las características específicas encontradas:",
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
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
