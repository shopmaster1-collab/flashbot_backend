# -*- coding: utf-8 -*-
import os, re, threading
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
import requests
import csv
from io import StringIO
from datetime import datetime

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

# URL de Google Sheets publicada (formato CSV)
ORDERS_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSvtBeUGaoH4_9UOuVJUEh3hbWq1tLSloCQyB9Hxp3-Eg4bSRFFwArFbbFtGTPF98rAcukeYr_rYWXq/pub?gid=0&single=true&output=csv"

# Cache para los datos de pedidos (se actualiza cada cierto tiempo)
_orders_cache = {"data": None, "last_update": None}
_orders_cache_ttl = 300  # 5 minutos

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

# =========== FUNCIONES PARA CONSULTA DE PEDIDOS ===========

def _fetch_orders_data():
    """Obtiene los datos de pedidos desde Google Sheets"""
    global _orders_cache
    
    # Verificar si el cache es válido
    if _orders_cache["data"] is not None and _orders_cache["last_update"]:
        elapsed = (datetime.now() - _orders_cache["last_update"]).total_seconds()
        if elapsed < _orders_cache_ttl:
            return _orders_cache["data"]
    
    try:
        response = requests.get(ORDERS_SHEET_URL, timeout=10)
        response.raise_for_status()
        
        # Parsear CSV
        csv_data = StringIO(response.text)
        reader = csv.DictReader(csv_data)
        orders = list(reader)
        
        # Actualizar cache
        _orders_cache["data"] = orders
        _orders_cache["last_update"] = datetime.now()
        
        return orders
    except Exception as e:
        print(f"[ERROR] Failed to fetch orders: {e}", flush=True)
        return None

def _is_order_query(query: str) -> bool:
    """Detecta si la consulta es sobre un pedido"""
    ql = query.lower()
    
    # Palabras clave para consultas de pedidos
    order_keywords = [
        "pedido", "orden", "order", "compra", "envio", "envío",
        "rastreo", "seguimiento", "status", "estatus", "estado",
        "tracking", "entrega", "paquete", "paqueteria", "paquetería"
    ]
    
    return any(keyword in ql for keyword in order_keywords)

def _extract_order_number(query: str) -> str:
    """Extrae el número de pedido de la consulta"""
    # Buscar patrones como: #1234, orden 1234, pedido 1234, etc.
    patterns = [
        r'#\s*(\d+)',  # #1234 o # 1234
        r'(?:pedido|orden|order|compra)\s*[#:]?\s*(\d+)',  # pedido 1234, orden #1234
        r'\b(\d{4,})\b'  # Número de 4+ dígitos
    ]
    
    for pattern in patterns:
        match = re.search(pattern, query, re.IGNORECASE)
        if match:
            return match.group(1)
    
    return None

def _search_order(order_number: str) -> list:
    """Busca un pedido específico en los datos"""
    orders_data = _fetch_orders_data()
    
    if not orders_data:
        return None
    
    # Buscar todas las líneas que coincidan con el número de orden
    matching_orders = []
    for order in orders_data:
        # El campo puede llamarse "# de Orden" o similar
        order_id = str(order.get("# de Orden", "")).strip()
        if order_id == str(order_number).strip():
            matching_orders.append(order)
    
    return matching_orders if matching_orders else None

def _format_order_response(order_number: str, order_items: list) -> str:
    """Formatea la respuesta con los datos del pedido"""
    if not order_items:
        return (f"Lo siento, no encontré información sobre el pedido #{order_number}. "
                f"Por favor verifica el número de orden e intenta nuevamente.")
    
    # Construir respuesta detallada
    response = f"📦 **Información del Pedido #{order_number}**\n\n"
    
    # Si hay múltiples items en el pedido
    if len(order_items) > 1:
        response += f"Tu pedido contiene {len(order_items)} productos:\n\n"
    
    for idx, item in enumerate(order_items, 1):
        if len(order_items) > 1:
            response += f"**Producto {idx}:**\n"
        
        # SKU y cantidad
        sku = item.get("SKU", "N/A")
        pzas = item.get("Pzas", "N/A")
        response += f"• SKU: {sku}\n"
        response += f"• Cantidad: {pzas} pza(s)\n"
        
        # Precios
        precio_unit = item.get("Precio Unitario", "N/A")
        precio_total = item.get("Precio Total", "N/A")
        if precio_unit != "N/A":
            try:
                response += f"• Precio unitario: ${float(precio_unit):,.2f}\n"
            except:
                response += f"• Precio unitario: {precio_unit}\n"
        if precio_total != "N/A":
            try:
                response += f"• Precio total: ${float(precio_total):,.2f}\n"
            except:
                response += f"• Precio total: {precio_total}\n"
        
        # Fechas
        fecha_inicio = item.get("Fecha Inicio", "N/A")
        if fecha_inicio != "N/A":
            response += f"• Fecha de inicio: {fecha_inicio}\n"
        
        # Estado del pedido
        en_proceso = item.get("EN PROCESO", "N/A")
        if en_proceso != "N/A":
            response += f"• Estado: {en_proceso}\n"
        
        # Información de envío
        paqueteria = item.get("Paqueteria", "N/A")
        fecha_envio = item.get("Fecha envió", "N/A")  # Nota: tiene acento en "envió"
        fecha_entrega = item.get("Fecha Entrega", "N/A")
        
        if paqueteria != "N/A":
            response += f"• Paquetería: {paqueteria}\n"
        if fecha_envio != "N/A":
            response += f"• Fecha de envío: {fecha_envio}\n"
        if fecha_entrega != "N/A":
            response += f"• Fecha de entrega estimada: {fecha_entrega}\n"
        
        if len(order_items) > 1 and idx < len(order_items):
            response += "\n"
    
    response += "\n¿Necesitas más información sobre tu pedido?"
    
    return response

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

# ----------------- Endpoints -----------------
@app.post("/api/chat")
def chat():
    data=request.get_json(force=True) or {}
    query=(data.get("message") or "").strip()
    page=int(data.get("page") or 1)
    per_page=int(data.get("per_page") or 10)
    
    if not query:
        return jsonify({
            "answer":"¡Hola! Soy Maxter, tu asistente de compras de Master Electronics. ¿Qué producto estás buscando? Puedo ayudarte con soportes, antenas, controles, cables, sensores de agua, sensores de gas y mucho más. También puedes consultar el estado de tu pedido proporcionando tu número de orden.",
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

    # ===== PRIMERO: Verificar si es una consulta de pedido =====
    if _is_order_query(query):
        order_number = _extract_order_number(query)
        
        if order_number:
            order_items = _search_order(order_number)
            answer = _format_order_response(order_number, order_items)
            
            return jsonify({
                "answer": answer,
                "products": [],
                "pagination": {
                    "page": 1,
                    "per_page": per_page,
                    "total": 0,
                    "total_pages": 0,
                    "has_next": False,
                    "has_prev": False
                },
                "order_query": True
            })
        else:
            # Detectó que es consulta de pedido pero no encontró número
            return jsonify({
                "answer": "Para consultar tu pedido, por favor proporciona tu número de orden. Por ejemplo: 'pedido #1234' o 'orden 1234'.",
                "products": [],
                "pagination": {
                    "page": 1,
                    "per_page": per_page,
                    "total": 0,
                    "total_pages": 0,
                    "has_next": False,
                    "has_prev": False
                },
                "order_query": True
            })

    # ===== Si no es consulta de pedido, proceder con búsqueda de productos =====
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
