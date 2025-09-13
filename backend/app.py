# -*- coding: utf-8 -*-
import os, re, threading
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

# 👉 Importamos SOLO CatalogIndexer (ya no hay ShopifyClient en indexer.py)
from .indexer import CatalogIndexer
from .utils import money

# Deepseek opcional (si lo usas)
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

# ---- Servicios
# 👉 Pasamos None. Con FORCE_REST=1, el indexer usará su REST interno (ShopifyREST).
indexer = CatalogIndexer(
    shop_client=None,
    store_base_url=os.getenv("STORE_BASE_URL", "https://master.com.mx")
)

CHAT_WRITER = (os.getenv("CHAT_WRITER") or "none").strip().lower()
deeps = None
if CHAT_WRITER == "deepseek" and DeepseekClient:
    try:
        deeps = DeepseekClient()
    except Exception:
        deeps = None

# build inicial (no altera conteos/inventarios)
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

# ---- Detección ligera de intención (misma que ya venías usando)
_PAT_ONE_BY_N = re.compile(r"\b(\d+)\s*[x×]\s*(\d+)\b", re.IGNORECASE)
def _detect_patterns(q: str) -> dict:
    ql = (q or "").lower(); pat = {}
    m = _PAT_ONE_BY_N.search(ql)
    if m: pat["matrix"] = f"{m.group(1)}x{m.group(2)}"
    if any(w in ql for w in ["agua","nivel","cisterna","tinaco","boya","inundacion","inundación"]): pat["water"]=True
    if ("gas" in ql) or any(w in ql for w in ["tanque","estacionario","estacionaria","lp","propano","butano","medidor","nivel"]): pat["gas"]=True
    if any(w in ql for w in ["valvula","válvula"]): pat["valve"]=True
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
    if p.get("bt"): bits.append("con Bluetooth")
    if p.get("wifi"): bits.append("con WiFi/App")
    if p.get("display"): bits.append("con pantalla/display")
    if p.get("alarm"): bits.append("con alarma de nivel bajo")
    if p.get("matrix"): bits.append(f"matriz {p['matrix']}")
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
            "title": it["title"], "image": it["image"],
            "price": money(v.get("price")) if v.get("price") is not None else None,
            "compare_at_price": money(v.get("compare_at_price")) if v.get("compare_at_price") else None,
            "buy_url": f"{it['buy_url']}" if 'buy_url' in it else None,
            "product_url": it["product_url"],
            "inventory": v.get("inventory"),
        })
    return cards

def _plain_items(items):
    out=[]
    for it in items:
        v=it["variant"]
        out.append({"title": it.get("title"), "sku": v.get("sku"),
                    "price": money(v.get("price")) if v.get("price") is not None else None,
                    "product_url": it.get("product_url"), "buy_url": f"{it.get('buy_url')}"})
    return out

# ---------- Chat ----------
@app.post("/api/chat")
def chat():
    data=request.get_json(force=True) or {}
    query=(data.get("message") or "").strip()
    k=int(data.get("k") or 5)
    if not query:
        return jsonify({"answer":"¿Qué producto buscas? Puedo ayudarte con soportes, antenas, controles, cables, sensores y más.","products":[]})

    # Pedimos más candidatos; indexer ya re-rankea por intención y usa descripción
    items=indexer.search(query, k=max(k, 90))
    items=items[:k] if k and isinstance(items, list) else items

    if not items:
        return jsonify({"answer":"No encontré resultados directos. Puedes precisar marca, tipo o uso (p. ej. ‘sensor de gas con válvula para tanque’, ‘control Sony Bravia’).","products":[]})

    cards=_cards_from_items(items)
    answer=_format_answer(query, items)
    return jsonify({"answer":answer, "products":cards})

# --- Admin helpers ---
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

@app.get("/api/admin/search")
def admin_search():
    if not _admin_ok(request): return jsonify({"ok":False,"error":"unauthorized"}), 401
    q=(request.args.get("q") or "").strip(); k=int(request.args.get("k") or 12)
    items=indexer.search(q, k=k); return jsonify({"q":q,"k":k,"items":_plain_items(items)})

@app.get("/api/admin/discards")
def admin_discards():
    if not _admin_ok(request): return jsonify({"ok":False,"error":"unauthorized"}), 401
    return jsonify({"ok": True})

@app.get("/api/admin/products")
def admin_products():
    if not _admin_ok(request): return jsonify({"ok":False,"error":"unauthorized"}), 401
    page=int(request.args.get("page") or 1)
    size=max(1,min(100,int(request.args.get("size") or 20)))
    data={"ok":True,"page":page,"size":size,"items":indexer.sample_products(limit=size)}
    return jsonify(data)

@app.get("/api/admin/diag")
def admin_diag():
    if not _admin_ok(request): return jsonify({"ok":False,"error":"unauthorized"}), 401
    sqlite_path=os.getenv("SQLITE_PATH"); db_dir=os.path.dirname(sqlite_path) if sqlite_path else None
    info={"api_version":os.getenv("SHOPIFY_API_VERSION","2024-10"),
          "shop":os.getenv("SHOPIFY_STORE_DOMAIN",os.getenv("SHOPIFY_SHOP","")),
          "sqlite_path":sqlite_path or "(default)",
          "db_dir_exists":bool(db_dir and os.path.isdir(db_dir)),
          "db_dir_writable":bool(db_dir and os.path.isdir(db_dir) and os.access(db_dir, os.W_OK)),
          "db_file_exists":bool(sqlite_path and os.path.isfile(sqlite_path)),
          "force_rest":os.getenv("FORCE_REST","0")=="1",
          "require_active":os.getenv("REQUIRE_ACTIVE","1"),
          "token_present":bool(os.getenv("SHOPIFY_ACCESS_TOKEN") or os.getenv("SHOPIFY_TOKEN"))}
    return jsonify(info)

@app.get("/api/admin/preview")
def admin_preview():
    if not _admin_ok(request): return jsonify({"ok":False,"error":"unauthorized"}), 401
    q=(request.args.get("q") or "").strip(); k=int(request.args.get("k") or 12)
    raw=indexer.search(q, k=max(k,90))
    return jsonify({"q":q, "raw_titles":[i.get("title") for i in raw[:k]]})

# Estáticos del widget
BASE_DIR=os.path.dirname(os.path.abspath(__file__))
STATIC_DIR=os.path.join(BASE_DIR,"..","widget")
@app.get("/static/<path:fname>")
def static_files(fname): return send_from_directory(STATIC_DIR, fname)
@app.get("/widget/<path:fname>")
def widget_files(fname): return send_from_directory(STATIC_DIR, fname)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
