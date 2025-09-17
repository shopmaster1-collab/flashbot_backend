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
            '<code>GET /api/admin/preview?q=...</code>, '
            '<code>GET /api/admin/taxonomy</code>'
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

def _format_answer(query: str, items: list) -> str:
    p=_detect_patterns(query); bits=[]
    if p.get("water"): bits.append("monitoreo de nivel de agua en tinacos/cisternas")
    if p.get("gas"): bits.append("medición/monitoreo de gas en tanque estacionario")
    if p.get("valve"): bits.append("con válvula electrónica")
    if p.get("ultra"): bits.append("sensor ultrasónico")
    if p.get("pressure"): bits.append("sensor de presión")
    if p.get("bt"): bits.append("con Bluetooth")
    if p.get("wifi"): bits.append("con WiFi/App")
    if p.get("display"): bits.append("con pantalla/display")
    if p.get("alarm"): bits.append("con alarma de nivel bajo")
    if p.get("matrix"): bits.append(f"matriz {p['matrix']}")
    if p.get("inches"): bits.append(f"tamaños {', '.join(p['inches'])}”")
    if p.get("cats"): bits.append("categorías: " + ", ".join(p["cats"]))
    lines=[]
    if bits: lines.append("Consideré: " + "; ".join(bits) + ".")
    lines.append("Estas son las opciones más relevantes que encontré.")
    lines.append("¿Quieres acotar por marca, precio, disponibilidad o tipo?")
    return "\n".join(lines)

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
    items=indexer._apply_intent_rerank_public(query, items)   # (reusa lógica interna sin romper encapsulado)
    items=indexer._enforce_intent_gate_public(query, items)
    items=items[:k] if k and isinstance(items,list) else items

    if not items:
        return jsonify({"answer":"No encontré resultados directos. Prueba con palabras clave específicas (p. ej. ‘divisor hdmi 1×4’, ‘soporte pared 55”’, ‘antena exterior UHF’, ‘control Samsung’, ‘cable RCA audio video’).","products":[]})

    cards=_cards_from_items(items)
    base_answer=_format_answer(query, items)
    if deeps:
        try:
            pretty=deeps.chat(
                "Actúa como asesor de compras para una tienda retail de electrónica. Responde claro, breve (5-20 frases), sin inventar datos. No repitas precios; ya van en tarjetas.",
                base_answer)
            answer=pretty if (pretty and len(pretty)>40) else base_answer
        except Exception as e:
            print(f"[WARN] Deepseek chat error: {e}", flush=True)
            answer=base_answer
    else:
        answer=base_answer
    return jsonify({"answer":answer, "products":cards})

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
    s = indexer.stats()
    # adjuntamos resumen de taxonomía
    s["taxonomy_rows"] = indexer.taxonomy_meta().get("rows", 0)
    s["taxonomy_terms"] = len(indexer.taxonomy_meta().get("terms", []))
    return s

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
            "TAXONOMY_CSV_PATH": os.getenv("TAXONOMY_CSV_PATH"),
        }
    }

@app.get("/api/admin/preview")
def admin_preview():
    if not _admin_ok(request): return jsonify({"ok":False,"error":"unauthorized"}), 401
    q=(request.args.get("q") or "").strip(); k=int(request.args.get("k") or 12)
    items=indexer.search(q, k=max(k,90))
    items=indexer._apply_intent_rerank_public(q, items)
    items=indexer._enforce_intent_gate_public(q, items)
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

@app.get("/api/admin/taxonomy")
def admin_taxonomy():
    if not _admin_ok(request): return jsonify({"ok":False,"error":"unauthorized"}), 401
    return indexer.taxonomy_meta()

# --------- MAIN: arrancar el servidor en Render ---------
if __name__ == "__main__":
    # Render expone PORT; por defecto usamos 10000 para locales.
    port = int(os.getenv("PORT", "10000"))
    # host=0.0.0.0 para aceptar tráfico externo en Render
    app.run(host="0.0.0.0", port=port)
