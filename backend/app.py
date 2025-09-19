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
CORS(app)

# Shopify + Indexer
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
            '<code>GET /api/admin/preview?q=...</code>'
            "</p>")

@app.get("/health")
def health():
    return jsonify({"ok": True})

@app.post("/api/admin/reindex")
def reindex():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    def run():
        try:
            indexer.build(force=True)
        except Exception as e:
            print(f"[ERR] reindex: {e}", flush=True)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return jsonify({"ok": True, "started": True})

@app.get("/api/admin/stats")
def stats():
    return jsonify(indexer.stats())

@app.get("/api/admin/search")
def admin_search():
    q = (request.args.get("q") or "").strip()
    k = int(request.args.get("k") or 10)
    items = indexer.search(q, k=k)
    return jsonify(_cards_from_items(items))

@app.get("/static/<path:fn>")
def public_static(fn):
    return send_from_directory(os.path.join(os.path.dirname(__file__), "static"), fn)

def _intent_from_query(query: str) -> str|None:
    ql=(query or "").lower()
    # Señales fuertes de intención
    water = any(w in ql for w in ["agua","tinaco","cisterna","inundacion","inundación","fuga","nivel"])
    gas = ("gas" in ql) or any(w in ql for w in ["lp","propano","butano","estacionario","estacionaria","tanque"])
    if water and not gas: return "water"
    if gas and not water: return "gas"
    # si están ambos, ponderamos por más términos...
    wc=sum(ql.count(w) for w in ["agua","tinaco","cisterna","nivel","fuga"])
    gc=sum(ql.count(w) for w in ["gas","lp","propano","butano","tanque","estacionario","estacionaria"])
    if wc>gc: return "water"
    if gc>wc: return "gas"
    return None

# ----------------- Helpers de texto / puntuación -----------------
def _concat_fields(item: dict) -> str:
    parts = [
        (item.get("handle") or ""),
        (item.get("title") or ""),
        " ".join(item.get("tags") or []),
        (item.get("product_type") or ""),
        (item.get("vendor") or ""),
        (item.get("body_html") or ""),
        (item.get("description") or ""),
        (item.get("sku") or "")
    ]
    st = " ".join([p for p in parts if p])
    st = st.lower()
    st = re.sub(r"<[^>]+>", " ", st)
    st = re.sub(r"\s+", " ", st)
    return st

_WATER_ALLOW_FAMILIES = [
    "iot-water","iot water",
    "iot-waterv","iot waterv",
    "iot-waterp","iot waterp",
    "iot-waterultra","iot waterultra",
    "easy-water","easy water",
    "easy-waterultra","easy waterultra",
    "waterultra"
]

_WATER_ALLOW_KEYWORDS = [
    "agua","tinaco","cisterna","nivel","inundación","inundacion","fuga",
    "ultra","ultrason","ultrasónico","ultrasonico","presion","presión","valvula","válvula",
    "boya","flotador","electrónivel","electronivel"
]

_WATER_BLOCK = [
    "gas","estacionario","lp","propano","butano","monóxido","monoxido"
]

_GAS_ALLOW_FAMILIES = [
    "iot-gas","iot gas","iot-gassensor","iot gassensor","iot-gassensorv","iot gassensorv"
]

_GAS_ALLOW_KEYWORDS = [
    "gas","lp","propano","butano","tanque","estacionario","estacionaria","porcentaje","volumen","nivel","display","pantalla"
]

_GAS_BLOCK = [
    "agua","tinaco","cisterna","flotador","boya","ultrason","ultrasónico","ultrasonico"
]

def _score_family(st: str, ql: str, allow_words, allow_fams, extras: dict):
    score = 0
    has_family = any(f in st for f in allow_fams)

    # boost base si pertenece a familia permitida
    if has_family:
        score += 85

    # boost por coincidencia de subtipo (solo agua usa estos)
    if extras.get("want_ultra") and any(k in st for k in ["waterultra","easy-waterultra","easy waterultra","iot-waterultra","iot waterultra"]):
        score += 55
    if extras.get("want_pressure") and any(k in st for k in ["iot-waterp","iot waterp"]):
        score += 55
    if extras.get("want_valve") and any(k in st for k in ["iot-waterv","iot waterv"]):
        score += 55

    # conectividad
    if extras.get("want_bt") and any(k in st for k in extras.get("bt_fams", [])):
        score += 25
    if extras.get("want_wifi") and any(k in st for k in extras.get("wifi_fams", [])):
        score += 25

    # keywords explícitos en el texto
    for key in allow_words:
        if key in st:
            score += 15

    # señales positivas/negs específicas
    for pos in extras.get("pos_words", []):
        if pos in st: score += 12
    for fam in extras.get("pos_fams", []):
        if fam in st: score += 25
    for bundle in extras.get("pos_bundles", []):
        if all(b in st for b in bundle): score += 20
    for words in extras.get("combo_words", []):
        if all(w in ql for w in words): score += 14
    for famds in extras.get("combo_famds", []):
        for key in famds:
            if key in st: score += 25
    for neg in extras.get("neg_words", []):
        if neg in st: score -= 80
    return score, has_family

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
            "ultra_fams":["waterultra","easy-waterultra","easy waterultra","iot-waterultra","iot waterultra"],
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

    # Si el usuario pide un subtipo, recorta 'positives' SOLO a ese subtipo antes del hard filter
    if positives:
        if extras.get("want_ultra"):
            positives = [r for r in positives if any(k in _concat_fields(r[5]) for k in extras.get("ultra_fams", []))]
        elif extras.get("want_pressure"):
            positives = [r for r in positives if any(k in _concat_fields(r[5]) for k in extras.get("pressure_fams", []))]
        elif extras.get("want_valve"):
            positives = [r for r in positives if any(k in _concat_fields(r[5]) for k in extras.get("valve_fams", []))]

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
            "wifi_fams":["iot-gas","iot gas","iot-gassensor","iot gassensor","iot-gassensorv","iot gassensorv"],
            "pos_words":["estacionario","estacionaria","tanque","nivel","porcentaje","volumen","display","pantalla"],
            "pos_fams":["iot-gas","iot gas","iot-gassensor","iot gassensor","iot-gassensorv","iot gassensorv"],
            "combo_words":[["tanque","estacionario"],["gas","tanque"],["gas","lp"]],
            "combo_famds":["iot-gassensorv","iot gassensorv"],
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

    # HARD FILTER si hay positivos
    if positives:
        positives.sort(key=lambda x:x[0], reverse=True)
        if want_valve:
            wv=[r for r in positives if r[4]]; others=[r for r in positives if not r[4]]
            ordered=wv+others
        else:
            ordered=positives
        return [it for (_t,_s,_b,_hf,_valve,it) in ordered]

    # Fallback suave
    soft = []
    gas_words = ["gas","lp","propano","butano","tanque","estacionario","estacionaria"]
    for idx,it in enumerate(items):
        st=_concat_fields(it)
        if any(w in st for w in gas_words) and not any(b in st for b in _GAS_BLOCK):
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

# --------- PUERTA POST-RERANK PARA SUBTIPOS DE AGUA ----------
def _enforce_water_subtype_gate(query: str, items: list):
    """Compuerta adicional: si el usuario pide subtipo (ultra/presión/válvula),
    filtra estrictamente a ese subtipo si existen coincidencias; si no, deja la lista tal cual."""
    if not items:
        return items
    ql = (query or "").lower()
    want_ultra = any(w in ql for w in ["ultra","ultrason","ultrasónico","ultrasonico"])
    want_pressure = any(w in ql for w in ["presion","presión"])
    want_valve = ("valvula" in ql) or ("válvula" in ql)
    if not (want_ultra or want_pressure or want_valve):
        return items

    def has_any(st: str, keys: list) -> bool:
        return any(k in st for k in keys)

    ultra_fams = ["waterultra","easy-waterultra","easy waterultra","iot-waterultra","iot waterultra"]
    pressure_fams = ["iot-waterp","iot waterp"]
    valve_fams = ["iot-waterv","iot waterv"]

    filtered = []
    for it in items:
        st = _concat_fields(it)
        if want_ultra and has_any(st, ultra_fams):
            filtered.append(it)
        elif want_pressure and has_any(st, pressure_fams):
            filtered.append(it)
        elif want_valve and has_any(st, valve_fams):
            filtered.append(it)

    return filtered or items

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

def _cards_from_items(items: list[dict]):
    out=[]
    for it in items or []:
        price = it.get("price")
        out.append({
            "title": it.get("title"),
            "url": it.get("url"),
            "image": it.get("image"),
            "price": money(price) if price is not None else None,
            "sku": it.get("sku")
        })
    return out

# ----------------- Endpoints -----------------
@app.post("/api/chat")
def chat():
    data=request.get_json(force=True) or {}
    query=(data.get("message") or "").strip()
    k=int(data.get("k") or 5)
    if not query:
        return jsonify({"answer":"¿Qué producto buscas? Puedo ayudarte con soportes, antenas, controles, cables, sensores y más.","products":[]})

    # Pedimos más candidatos para rerank robusto, luego recortamos
    items=indexer.search(query, k=max(k,90))
    items=_apply_intent_rerank(query, items)
    items=_enforce_water_subtype_gate(query, items)
    items=_enforce_intent_gate(query, items)
    items=items[:k] if k and isinstance(items,list) else items

    if not items:
        return jsonify({"answer":"No encontré resultados directos. Intenta con nombre de producto o marca (ej. ‘soporte fijo 55”, ‘control Samsung’, ‘cable RCA audio video’).","products":[]})

    cards=_cards_from_items(items)
    base_answer=_format_answer(query, items)
    if deeps:
        try:
            pretty=deeps.chat(
                "Actúa como asesor de compras para una tienda retail de electrónica. Responde claro y en pocas líneas, listo para WhatsApp. Si no hay certeza, invita a ver los productos listados.",
                base_answer
            )
            answer = pretty.strip() or base_answer
        except Exception:
            answer = base_answer
    else:
        answer=base_answer

    return jsonify({"answer": answer, "products": cards})

@app.get("/api/admin/preview")
def admin_preview():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    query=(request.args.get("q") or "").strip()
    k=int(request.args.get("k") or 10)
    items=indexer.search(query, k=max(k,90))
    items=_apply_intent_rerank(query, items)
    items=_enforce_water_subtype_gate(query, items)
    return jsonify(_cards_from_items(items[:k]))

@app.get("/api/admin/discards")
def admin_discards():
    if not _admin_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return jsonify({"water_block": _WATER_BLOCK, "gas_block": _GAS_BLOCK})

def _detect_patterns(query: str):
    ql=(query or "").lower()
    pat={"water":False,"gas":False,"valve":False,"ultra":False,"pressure":False,"bt":False,"wifi":False,"display":False,"alarm":False}
    if any(w in ql for w in ["agua","tinaco","cisterna","inundacion","inundación","fuga","nivel"]): pat["water"]=True
    if ("gas" in ql) or any(w in ql for w in ["tanque","estacionario","estacionaria","lp","propano","butano"]): pat["gas"]=True
    if any(w in ql for w in ["valvula","válvula"]): pat["valve"]=True
    if any(w in ql for w in ["ultra","ultrason","ultrasónico","ultrasonico"]): pat["ultra"]=True
    if any(w in ql for w in ["presion","presión"]): pat["pressure"]=True
    if "bluetooth" in ql: pat["bt"]=True
    if ("wifi" in ql) or ("app" in ql): pat["wifi"]=True
    if any(w in ql for w in ["pantalla","display"]): pat["display"]=True
    if "alarma" in ql: pat["alarm"]=True
    return pat

def _format_answer(query: str, items: list) -> str:
    p=_detect_patterns(query); bits=[]
    if p.get("water"): bits.append("monitoreo de nivel de agua en tinacos/cisternas")
    if p.get("gas"): bits.append("medición/monitoreo de gas en tanque estacionario")
    if p.get("valve"): bits.append("con válvula")
    if p.get("ultra"): bits.append("tecnología ultrasónica")
    if p.get("pressure"): bits.append("medición por presión")
    if p.get("wifi"): bits.append("conectado a WiFi / App")
    if p.get("bt"): bits.append("Bluetooth")
    if p.get("display"): bits.append("con pantalla")
    if p.get("alarm"): bits.append("con alarma")
    tail=(" con " + ", ".join(bits)) if bits else ""
    return f"Te muestro las mejores opciones para tu búsqueda{tail}. Revisa las tarjetas y si necesitas afinar (marca, tamaño, precio o accesorios), dime."

