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
    
    # Detectar tipos de productos
    if any(w in ql for w in ["sensor", "detector"]):
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
    
    # Saludo contextual
    if product_type:
        if brands:
            response_parts.append(f"¡Perfecto! Para {product_type} de {', '.join(brands)}")
        else:
            response_parts.append(f"¡Claro! Tenemos excelentes opciones en {product_type}")
    else:
        response_parts.append("¡Hola! He encontrado estas opciones para ti")
    
    # Información específica basada en patrones
    specifics = []
    if p.get("water"):
        specifics.append("de nuestras líneas IOT Water, Easy Water y Connect")
    elif p.get("gas"):
        specifics.append("de nuestras líneas IOT Gas Sensor, Easy Gas y Connect Gas")
    elif p.get("matrix"):
        specifics.append(f"con matriz {p['matrix']}")
    elif size_mentioned:
        specifics.append(f"compatibles con pantallas de {size_mentioned}\"")
    elif p.get("inches"):
        specifics.append(f"para pantallas de {', '.join(p['inches'])}\"")
    
    if specifics:
        response_parts.append(" " + ", ".join(specifics))
    
    # Información de resultados
    if total_count > per_page:
        showing = min(per_page, len(items))
        response_parts.append(f". Mostrando {showing} de {total_count} productos disponibles")
    else:
        response_parts.append(f". Encontré {len(items)} productos que coinciden")
    
    # Sugerencias adicionales
    suggestions = []
    if p.get("valve"):
        suggestions.append("con válvula incluida")
    if p.get("wifi"):
        suggestions.append("con conectividad WiFi")
    if p.get("bt"):
        suggestions.append("con Bluetooth")
    if p.get("display"):
        suggestions.append("con pantalla")
    
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

# ---------- Señales / familias ----------
_WATER_ALLOW_FAMILIES = [
    "iot-waterv","iot-waterultra","iot-waterp","iot-water",
    "easy-waterultra","easy-water","iot waterv","iot waterultra","iot waterp","iot water","easy waterultra","easy water",
]
_WATER_ALLOW_KEYWORDS = ["tinaco","cisterna","nivel","agua"]
_WATER_BLOCK = ["bm-carsensor","carsensor","car","auto","vehiculo","vehículo",
                "ar-rain","rain","lluvia","ar-gasc","gasc"," gas","co2","humo","smoke",
                "ar-knock","knock","golpe"]

_GAS_ALLOW_FAMILIES = [
    "iot-gassensorv","iot-gassensor","connect-gas","easy-gas",
    "iot gassensorv","iot gassensor","connect gas","easy gas",
]
_GAS_ALLOW_KEYWORDS = ["gas","tanque","estacionario","estacionaria","lp","propano","butano","nivel","medidor","porcentaje","volumen"]

# Blocklist GAS (evita EASY-ELECTRIC, PEST-KILLER y módulos) + familias de agua
_GAS_BLOCK = [
    "ar-gasc","ar-flame","ar-photosensor","photosensor","megasensor","ar-megasensor",
    "arduino","módulo","modulo","module","mq-","mq2","flame","co2","humo","smoke","luz","photo","shield",
    "pest","plaga","mosquito","insect","insecto","pest-killer","pest killer",
    "easy-electric","easy electric","eléctrico","electrico","electricidad","energia","energía",
    "kwh","kw/h","consumo","tarifa","electric meter","medidor de consumo","contador",
    "ar-rain","rain","lluvia","carsensor","bm-carsensor","auto","vehiculo","vehículo",
    "iot-water","iot-waterv","iot-waterultra","iot-waterp","easy-water","easy-waterultra"," water "
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

# ---------- INTENCIÓN (estricta y no ambigua) ----------
def _intent_from_query(q: str):
    """
    Regla clara:
    - Si menciona 'gas' o señales inequívocas de gas (tanque, estacionario/a, lp, propano, butano) => 'gas'
    - Si NO menciona 'gas' y sí menciona agua/tinaco/cisterna/inundación/boya/flotador => 'water'
    - 'nivel' o 'medidor' NO determinan la intención por sí solos (son ambiguos).
    """
    ql = (q or "").lower()

    gas_hard = ["gas", "tanque", "estacionario", "estacionaria", "lp", "propano", "butano"]
    if any(w in ql for w in gas_hard):
        return "gas"

    water_hard = ["agua", "tinaco", "cisterna", "inundacion", "inundación", "boya", "flotador"]
    if any(w in ql for w in water_hard):
        return "water"

    return None

def _score_family(st: str, ql: str, allow_keywords, allow_fams, extras) -> tuple[int, bool]:
    """Devuelve (score, has_family)."""
    s=0
    has_family = any(fam in st for fam in allow_fams)
    if any(w in st for w in allow_keywords): s+=20
    if has_family: s+=85
    if extras.get("want_valve"):
        for key in extras.get("valve_fams", []):
            if key in st: s+=extras.get("valve_bonus", 95)
    if extras.get("want_ultra"):
        for key in extras.get("ultra_fams", []):
            if key in st: s+=55
    if extras.get("want_pressure"):
        for key in extras.get("pressure_fams", []):
            if key in st: s+=55
    if extras.get("want_bt"):
        for key in extras.get("bt_fams", []):
            if key in st: s+=45
    if extras.get("want_wifi"):
        for key in extras.get("wifi_fams", []):
            if key in st: s+=45
    if extras.get("want_display"):
        for key in extras.get("display_fams", []):
            if key in st: s+=40
    if extras.get("want_alarm"):
        for key in extras.get("alarm_words", []):
            if key in st: s+=25
    for neg in extras.get("neg_words", []):
        if neg in st: s-=80
    return s, has_family

# --------- Rerank/filtrado Agua ----------
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
    water_words = ["agua","tinaco","cisterna","nivel"]
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

# --------- Rerank/filtrado Gas ----------
def _rerank_for_gas(query: str, items: list):
    ql=(query or "").lower()
    if _intent_from_query(query)!="gas" or not items: return items
    want_valve=("valvula" in ql) or ("válvula" in ql)
    extras={"want_valve": want_valve, "want_bt": "bluetooth" in ql,
            "want_wifi": ("wifi" in ql) or ("app" in ql),
            "want_display": any(w in ql for w in ["pantalla","display"]),
            "want_alarm": "alarma" in ql,
            "valve_fams":["iot-gassensorv","iot gassensorv"],
            "bt_fams":["easy-gas","easy gas"],
            "wifi_fams":["iot-gassensor","iot gassensor","connect-gas","connect gas"],
            "display_fams":["easy-gas","easy gas"],
            "alarm_words":["alarma","alerta"],
            "neg_words":[]}
    rescored=[]; positives=[]
    for idx,it in enumerate(items):
        st=_concat_fields(it)
        blocked=any(b in st for b in _GAS_BLOCK)
        base=max(0,30-idx)
        score, has_fam = _score_family(st, ql, _GAS_ALLOW_KEYWORDS, _GAS_ALLOW_FAMILIES, extras)
        total=score+base-(140 if blocked else 0)
        is_valve=("iot-gassensorv" in st) or ("iot gassensorv" in st)
        rec=(total,score,blocked,has_fam,is_valve,it); rescored.append(rec)
        # Positivo SOLO si pertenece a familia de gas
        if has_fam and score>=60 and not blocked: positives.append(rec)

    # HARD FILTER si hay positivos -> solo familias de gas
    if positives:
        positives.sort(key=lambda x:x[0], reverse=True)
        if want_valve:
            vs=[r for r in positives if r[4]]; others=[r for r in positives if not r[4]]
            ordered=vs+others
        else:
            ordered=positives
        return [it for (_t,_s,_b,_hf,_valve,it) in ordered]

    # Fallback suave
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

# --------- PUERTA FINAL (HARD GATE) CONTRA MEZCLAS ---------
def _enforce_intent_gate(query: str, items: list):
    """Filtra de forma estricta resultados de la otra categoría si la intención está clara."""
    intent=_intent_from_query(query)
    if not intent or not items:
        return items

    filtered=[]
    for it in items:
        st=_concat_fields(it)
        if intent=="gas":
            if any(fam in st for fam in _WATER_ALLOW_FAMILIES):
                continue
            if any(w in st for w in _WATER_ALLOW_KEYWORDS):
                continue
        elif intent=="water":
            if any(fam in st for fam in _GAS_ALLOW_FAMILIES):
                continue
            if "gas" in st or any(w in st for w in ["lp","propano","butano","estacionario","estacionaria"]):
                continue
        filtered.append(it)

    return filtered or items

# ----------------- Endpoints -----------------
@app.post("/api/chat")
def chat():
    data=request.get_json(force=True) or {}
    query=(data.get("message") or "").strip()
    page=int(data.get("page") or 1)
    per_page=int(data.get("per_page") or 10)  # Aumentado de 5 a 10
    
    if not query:
        return jsonify({
            "answer":"¡Hola! Soy Maxter, tu asistente de compras de Master Electronics. ¿Qué producto estás buscando? Puedo ayudarte con soportes, antenas, controles, cables, sensores y mucho más.",
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
        return jsonify({
            "answer":"No encontré resultados directos para tu búsqueda. Prueba con palabras clave más específicas como 'divisor hdmi 1×4', 'soporte pared 55\"', 'antena exterior UHF', 'control Samsung', 'cable RCA audio video', 'sensor agua tinaco' o 'sensor gas estacionario'.",
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
                "Eres un asistente experto en productos electrónicos de Master Electronics México. Mejora esta respuesta para que sea más natural, específica y útil. Mantén toda la información técnica y de productos, pero hazla más conversacional y amigable. No inventes datos:",
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
