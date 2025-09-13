# -*- coding: utf-8 -*-
"""
Indexer Shopify -> SQLite (+FTS5) para el buscador del bot.

Reglas:
- REQUIRE_ACTIVE=1  -> exige product.status == "active" (único filtro)
"""

from __future__ import annotations

import os, re, json, time, sqlite3, unicodedata
from typing import Any, Dict, List, Tuple, Optional
from urllib.parse import urlparse, parse_qs
import requests

from .utils import strip_html

# ---------- Paths ----------
BASE_DIR = os.path.dirname(__file__)
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")
SQLITE_PATH_ENV = (os.getenv("SQLITE_PATH") or "").strip()
if SQLITE_PATH_ENV:
    DATA_DIR = os.path.dirname(SQLITE_PATH_ENV)
    DB_PATH = SQLITE_PATH_ENV
else:
    DATA_DIR = DEFAULT_DATA_DIR
    DB_PATH = os.path.join(DATA_DIR, "catalog.sqlite3")
os.makedirs(DATA_DIR, exist_ok=True)

def _row_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}

def _norm(s: str) -> str:
    if not s: return ""
    s = s.lower()
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

# ---------- REST nativo ----------
class ShopifyREST:
    def __init__(self):
        store = (os.getenv("SHOPIFY_STORE_DOMAIN") or os.getenv("SHOPIFY_SHOP") or "").strip()
        if store.startswith("http"): store = urlparse(store).netloc
        token = (os.getenv("SHOPIFY_ACCESS_TOKEN") or os.getenv("SHOPIFY_TOKEN") or "").strip()
        api_ver = (os.getenv("SHOPIFY_API_VERSION") or "2024-10").strip()
        if not store or not token:
            raise RuntimeError("Faltan SHOPIFY_SHOP|STORE_DOMAIN o SHOPIFY_ACCESS_TOKEN|TOKEN")
        self.shop = store; self.api_ver = api_ver
        self.base = f"https://{store}/admin/api/{api_ver}"
        self.session = requests.Session()
        self.session.headers.update({"X-Shopify-Access-Token": token, "Content-Type":"application/json","Accept":"application/json"})

    @staticmethod
    def _next_page_info(resp: requests.Response) -> Optional[str]:
        link = resp.headers.get("Link") or resp.headers.get("link") or ""
        for part in link.split(","):
            if 'rel="next"' in part:
                s = part.find("<"); e = part.find(">")
                if s >= 0 and e > s:
                    url = part[s+1:e]
                    qs = parse_qs(urlparse(url).query)
                    return (qs.get("page_info") or [None])[0]
        return None

    def _get(self, path: str, params: Dict[str, Any]) -> requests.Response:
        url = f"{self.base}{path}"
        for attempt in range(4):
            r = self.session.get(url, params=params, timeout=40)
            if r.status_code == 429:
                time.sleep(1.0 + attempt); continue
            r.raise_for_status(); return r
        r.raise_for_status(); return r

    def list_products_all(self, limit: int = 250) -> List[Dict[str, Any]]:
        out=[]; page=None
        while True:
            params={"limit":limit}
            if page: params["page_info"]=page
            r=self._get("/products.json", params)
            items=(r.json() or {}).get("products") or []
            out.extend(items)
            page=self._next_page_info(r)
            if not page or not items: break
        return out

    def list_locations(self) -> List[Dict[str, Any]]:
        r=self._get("/locations.json", {}); return (r.json() or {}).get("locations") or []

    def inventory_levels_for_items(self, item_ids: List[int]) -> List[Dict[str, Any]]:
        out=[]; CHUNK=50
        for i in range(0, len(item_ids), CHUNK):
            chunk=item_ids[i:i+CHUNK]
            if not chunk: continue
            r=self._get("/inventory_levels.json", {"inventory_item_ids": ",".join(str(x) for x in chunk), "limit":250})
            out.extend((r.json() or {}).get("inventory_levels") or [])
        return out

# ----- familias / tokens -----
_WATER_ALLOW_FAMS = ["iot-waterv","iot-waterultra","iot-waterp","iot-water","easy-waterultra","easy-water",
                     "iot waterv","iot waterultra","iot waterp","iot water","easy waterultra","easy water"]
_WATER_KEYWORDS  = ["agua","nivel","tinaco","cisterna"]
_WATER_BLOCK     = ["bm-carsensor","carsensor","car","auto","vehiculo","vehículo","ar-rain","rain","lluvia",
                    "ar-gasc","gasc"," co2","humo","smoke","ar-knock","knock","golpe"]

_GAS_ALLOW_FAMS  = ["iot-gassensorv","iot-gassensor","connect-gas","easy-gas",
                    "iot gassensorv","iot gassensor","connect gas","easy gas"]
_GAS_FUELS       = ["lp","propano","butano"]
_GAS_BLOCK       = ["ar-gasc","ar-flame","ar-photosensor","photosensor","megasensor","ar-megasensor",
                    "arduino","mq-","mq2","flame","co2","humo","smoke","luz","photo","shield",
                    "pest","plaga","mosquito","insect","insecto","pest-killer","pest killer",
                    "easy-electric","easy electric","eléctrico","electrico","electricidad","energia","energía",
                    "kwh","kw/h","consumo","tarifa","electric meter","medidor de consumo","contador",
                    "ar-rain","rain","lluvia","carsensor","bm-carsensor","auto","vehiculo","vehículo",
                    "iot-water","iot-waterv","iot-waterultra","iot-waterp","easy-water","easy-waterultra"," water "]

def _intent_from_query(q: str) -> Optional[str]:
    qn = _norm(q)
    if "gas" in qn or any(w in qn for w in _GAS_FUELS): return "gas"
    if any(w in qn for w in _WATER_KEYWORDS):          return "water"
    if ("control" in qn or "remoto" in qn) and ("sony" in qn): return "control_sony"
    return None

# ---------- Indexer ----------
class CatalogIndexer:
    def __init__(self, shop_client, store_base_url: str):
        self.client = shop_client
        self.store_base_url = (store_base_url or "").rstrip("/") or "https://master.com.mx"
        self.rules = {"REQUIRE_ACTIVE": os.getenv("REQUIRE_ACTIVE","1")=="1"}
        self.db_path = DB_PATH
        self._fts_enabled = False
        self._stats={"products":0,"variants":0,"inventory_levels":0}
        self._discards_sample=[]; self._discards_count={}
        self._location_map={}; self._inventory_map={}
        self._rest_fallback: Optional[ShopifyREST] = None
        try: self._rest_fallback = ShopifyREST()
        except Exception: self._rest_fallback = None

    def _conn_rw(self): conn=sqlite3.connect(self.db_path); conn.row_factory=_row_factory; return conn
    def _conn_read(self): conn=sqlite3.connect(self.db_path); conn.row_factory=_row_factory; return conn

    @staticmethod
    def _img_src(img): 
        if not img: return None
        return (img.get("src") or img.get("url") or "").strip() or None

    def _extract_hero_image(self, p):
        s=self._img_src(p.get("image")); 
        if s: return s
        for i in (p.get("images") or []):
            s=self._img_src(i); 
            if s: return s
        images_by_id={str(i.get("id")): i for i in (p.get("images") or [])}
        for v in (p.get("variants") or []):
            iid=str(v.get("image_id") or "")
            if iid:
                s=self._img_src(images_by_id.get(iid))
                if s: return s
        return None

    def _passes_product_rules(self, p):
        if self.rules["REQUIRE_ACTIVE"] and p.get("status")!="active":
            return False,"status!=active"
        return True,""

    def _select_valid_variants(self, variants):
        out=[]
        for v in variants or []:
            try: price_f = float(v.get("price")) if v.get("price") is not None else None
            except Exception: price_f=None
            if price_f is None: continue
            inv_item_id=v.get("inventory_item_id")
            inv_levels=self._inventory_map.get(int(inv_item_id),[]) if inv_item_id else []
            try: cap_f = float(v.get("compare_at_price")) if v.get("compare_at_price") is not None else None
            except Exception: cap_f=None
            out.append({
                "id": int(v["id"]),
                "sku": (v.get("sku") or None),
                "price": price_f,
                "compare_at_price": cap_f,
                "inventory_item_id": int(inv_item_id) if inv_item_id else None,
                "inventory": [{"location_id": int(lv["location_id"]), "available": int(lv.get("available") or 0)} for lv in inv_levels],
            })
        return out

    def _fetch_all_active(self, limit=250):
        force_rest=os.getenv("FORCE_REST","0")=="1"
        if force_rest and self._rest_fallback:
            all_items=self._rest_fallback.list_products_all(limit=limit)
            return [p for p in all_items if p.get("status")=="active"]
        try:
            if hasattr(self.client,"list_products"):
                acc=[]; page=None
                while True:
                    resp=self.client.list_products(limit=limit, page_info=page)
                    if isinstance(resp, dict):
                        items=(resp.get("products") or resp.get("items") or []) or []
                        acc.extend(items); page=resp.get("next_page_info")
                        if not page or not items: break
                    else:
                        if isinstance(resp,list): acc=list(resp)
                        break
                if acc: return [p for p in acc if p.get("status")=="active"]
        except Exception: pass
        if self._rest_fallback:
            all_items=self._rest_fallback.list_products_all(limit=limit)
            return [p for p in all_items if p.get("status")=="active"]
        return []

    def build(self):
        if os.path.exists(self.db_path):
            try: os.remove(self.db_path)
            except OSError: pass

        conn=self._conn_rw(); cur=conn.cursor()
        cur.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE products (
              id INTEGER PRIMARY KEY,
              handle TEXT, title TEXT, body TEXT, tags TEXT,
              vendor TEXT, product_type TEXT, image TEXT
            );
            CREATE TABLE variants (
              id INTEGER PRIMARY KEY,
              product_id INTEGER, sku TEXT, price REAL,
              compare_at_price REAL, inventory_item_id INTEGER
            );
            CREATE TABLE inventory (
              variant_id INTEGER, location_id INTEGER,
              location_name TEXT, available INTEGER
            );
        """); conn.commit()

        try:
            cur.execute("""
                CREATE VIRTUAL TABLE products_fts USING fts5(
                    title, body, tags, handle, vendor, product_type,
                    content='products', content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2 tokenchars "-_/"'
                )
            """)
            self._fts_enabled=True
        except sqlite3.OperationalError:
            self._fts_enabled=False

        try:
            locations = self._rest_fallback.list_locations() if self._rest_fallback else (self.client.list_locations() if self.client else [])
        except Exception:
            locations=[]
        self._location_map={int(x["id"]): (x.get("name") or str(x["id"])) for x in locations}

        try: products=self._fetch_all_active(limit=250)
        except Exception as e:
            print(f"[INDEX] ERROR list_products: {e}", flush=True); products=[]

        all_inv_ids=[]
        for p in products:
            for v in (p.get("variants") or []):
                if v.get("inventory_item_id"):
                    try: all_inv_ids.append(int(v["inventory_item_id"]))
                    except Exception: pass

        try:
            if self._rest_fallback:
                levels=self._rest_fallback.inventory_levels_for_items(all_inv_ids) if all_inv_ids else []
            elif self.client:
                levels=self.client.inventory_levels_for_items(all_inv_ids) if all_inv_ids else []
            else:
                levels=[]
        except Exception as e:
            print(f"[INDEX] ERROR inventory_levels: {e}", flush=True); levels=[]

        self._stats["inventory_levels"]=len(levels)
        self._inventory_map={}
        for lev in levels:
            try:
                iid=int(lev["inventory_item_id"])
                self._inventory_map.setdefault(iid, []).append({
                    "location_id": int(lev["location_id"]),
                    "available": int(lev.get("available") or 0)
                })
            except Exception: continue

        ins_p="INSERT INTO products (id, handle, title, body, tags, vendor, product_type, image) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        ins_v="INSERT INTO variants (id, product_id, sku, price, compare_at_price, inventory_item_id) VALUES (?, ?, ?, ?, ?, ?)"
        ins_inv="INSERT INTO inventory (variant_id, location_id, location_name, available) VALUES (?, ?, ?, ?)"

        discards_sample=[]; discards_count={}; n_products=0; n_variants=0
        for p in products:
            ok,reason = self._passes_product_rules(p)
            if not ok:
                discards_count[reason]=discards_count.get(reason,0)+1
                if len(discards_sample)<20:
                    discards_sample.append({"product_id":p.get("id"),"handle":p.get("handle"),"title":p.get("title"),"reason":reason})
                continue

            valids=self._select_valid_variants(p.get("variants") or [])
            if not valids:
                discards_count["no_variant_complete"]=discards_count.get("no_variant_complete",0)+1
                if len(discards_sample)<20:
                    discards_sample.append({"product_id":p.get("id"),"handle":p.get("handle"),"title":p.get("title"),"reason":"no_variant_complete"})
                continue

            body_text=strip_html(p.get("body_html") or "")
            cur.execute(ins_p,(int(p["id"]), p.get("handle"), p.get("title"), body_text,
                               (p.get("tags") or "").strip(), p.get("vendor"), p.get("product_type"), self._extract_hero_image(p)))
            n_products+=1

            for v in valids:
                cur.execute(ins_v,(int(v["id"]), int(p["id"]), v.get("sku"), v.get("price"),
                                   v.get("compare_at_price"), int(v["inventory_item_id"]) if v.get("inventory_item_id") else None))
                n_variants+=1
                for lvl in v["inventory"]:
                    loc_id=int(lvl["location_id"])
                    cur.execute(ins_inv,(int(v["id"]), loc_id, self._location_map.get(loc_id,str(loc_id)), int(lvl.get("available") or 0)))

        conn.commit()
        if self._fts_enabled:
            cur.execute("""
                INSERT INTO products_fts (rowid, title, body, tags, handle, vendor, product_type)
                SELECT id, title, body, tags, handle, vendor, product_type FROM products
            """); conn.commit()
        conn.close()

        self._stats.update({"products":n_products,"variants":n_variants})
        self._discards_sample=discards_sample; self._discards_count=discards_count
        print(f"[INDEX] done: products={n_products} variants={n_variants} inventory_levels={self._stats['inventory_levels']}", flush=True)

    # --- reporting ---
    def stats(self): return dict(self._stats)
    def discard_stats(self):
        by_reason=[{"reason":k,"count":v} for k,v in sorted(self._discards_count.items(), key=lambda x:-x[1])]
        return {"ok":True,"by_reason":by_reason,"sample":self._discards_sample}
    def sample_products(self, limit=10):
        conn=self._conn_read(); cur=conn.cursor()
        rows=list(cur.execute("SELECT id, handle, title, vendor, product_type, image FROM products LIMIT ?", (int(limit),)))
        conn.close(); return rows

    # ---------- búsqueda ----------
    def search(self, query: str, k: int = 6) -> List[Dict[str, Any]]:
        if not query: return []
        q_norm=_norm(query); intent=_intent_from_query(query)

        STOP={"el","la","los","las","un","una","unos","unas","de","del","al","y","o","u","en","a","con","por","para",
              "que","cual","cuales","donde","busco","busca","buscar","quiero","necesito","tienes","tienen","hay",
              "producto","productos"}
        SYN={
            "tv":["televisor","pantalla"], "pantalla":["tv","televisor","monitor"],
            "soporte":["base","bracket","montaje","mount","pared","techo","mural"],
            "cable":["cordon","conector","conexion","conexión"], "hdmi":["uhd","4k","8k","microhdmi","mini hdmi","arc","earc"],
            "rca":["av","audio video","a/v"], "vga":["dsub","d-sub"], "coaxial":["rg6","rg59","f"],
            "divisor":["splitter","duplicador","repartidor","1x2","1x4","1×2","1×4","1 x 2","1 x 4"],
            "splitter":["divisor","duplicador","repartidor","1x2","1x4","1×2","1×4","1 x 2","1 x 4"],
            "switch":["conmutador","selector"], "antena":["tvant","exterior","interior","uhf","vhf","aerea","aérea","digital","hd"],
            "control":["remoto","remote"], "remoto":["control","remote"],
            "camara":["cámara","ip","cctv","vigilancia","seguridad","poe","dvr","nvr"], "cámara":["camara","ip","cctv","vigilancia","seguridad","poe","dvr","nvr"],
            "bocina":["parlante","altavoz","speaker"], "microfono":["micrófono","mic","micro"], "amplificador":["ampli","amp"],
            "sensor":["detector","sonda"], "movimiento":["pir"],
            "agua":["inundacion","inundación","fuga","nivel","liquido","líquido","water","leak","sumergible","boya","flotador","tinaco","cisterna"],
            "pila":["bateria","batería","aa","aaa","18650","9v"], "cargador":["charger","fuente","eliminador","adaptador","power"],
            "adaptador":["converter","convertidor"], "conector":["terminal","plug","jack"],
        }
        if "intemperie" in q_norm or "exterior" in q_norm: SYN.setdefault("ip67", ["impermeable","intemperie","exterior"])
        if "valvula" in q_norm or "válvula" in q_norm:   SYN.setdefault("valvula", ["válvula","valvula"])

        COMBOS=[({"divisor","splitter","duplicador","repartidor"},{"hdmi"},45),
                ({"soporte","bracket","mount","base"},{"tv","pantalla","monitor"},35),
                ({"antena"},{"tv","uhf","vhf","digital","hd"},25),
                ({"sensor","detector","sonda"},{"agua","inundacion","inundación","fuga","nivel","liquido","líquido","sumergible","boya","flotador","tinaco","cisterna"},40)]

        raw_terms=[t for t in re.findall(r"[\w]+", q_norm, re.UNICODE)]
        base_terms=[t for t in raw_terms if len(t)>=2 and t not in STOP] or [t for t in raw_terms if len(t)>=2]
        m_q=re.search(r"\b(\d+)\s*[x×]\s*(\d+)\b", q_norm)
        if m_q: base_terms.append(re.sub(r"\s+","",m_q.group(0)).replace("×","x"))
        q_matrix=f"{m_q.group(1)}x{m_q.group(2)}" if m_q else None

        seen=set(); expanded=[]
        for t in base_terms:
            if t not in seen: expanded.append(t); seen.add(t)
            for s in SYN.get(t, []):
                sn=_norm(s); 
                if sn not in seen: expanded.append(sn); seen.add(sn)
        clean_terms=expanded[:12] if expanded else []

        def detect_combo(tokens):
            tokset=set(tokens); hits=[]
            for A,B,bonus in COMBOS:
                if (tokset & A) and (tokset & B): hits.append((A,B,bonus))
            return hits
        combo_hits=detect_combo(clean_terms)

        conn=self._conn_read(); cur=conn.cursor()
        ids=[]

        # --- FTS (AND por intención) ---
        fts_q=None
        if self._fts_enabled:
            if intent=="gas":
                domain_any=["gas","tanque","estacionario","estacionaria"]+_GAS_FUELS+_GAS_ALLOW_FAMS
                attrs_any =["sensor","medidor","valvula","válvula","ip67","impermeable","intemperie","exterior","wifi","app","nivel","porcentaje","volumen"]
                fts_q="("+ " OR ".join(domain_any)+") AND ("+ " OR ".join(attrs_any)+")"
            elif intent=="water":
                domain_any=["agua","tinaco","cisterna","nivel"]+_WATER_ALLOW_FAMS
                attrs_any =["sensor","medidor","valvula","válvula","ip67","impermeable","intemperie","exterior","wifi","app","ultrasonico","ultrasónico","presion","presión","electrodos"]
                fts_q="("+ " OR ".join(domain_any)+") AND ("+ " OR ".join(attrs_any)+")"
            elif intent=="control_sony":
                fts_q="(sony) AND (control OR remoto OR bravia OR rm)"
            elif clean_terms:
                or_clause=" OR ".join(clean_terms)
                near_clause=f" OR ({clean_terms[0]} NEAR/6 {clean_terms[1]})" if len(clean_terms)>=2 else ""
                fts_q=f"({or_clause}){near_clause}"
            if fts_q:
                try:
                    rows=list(cur.execute("SELECT rowid FROM products_fts WHERE products_fts MATCH ? LIMIT ?", (fts_q, k*20)))
                    ids.extend([int(r["rowid"]) for r in rows])
                except Exception:
                    pass

        # --- Fallback LIKE con AND ---
        if len(ids) < max(8,k):
            def _like_group(terms):
                if not terms: return "", []
                preds=[]; params=[]
                tmpl="(title LIKE ? OR body LIKE ? OR tags LIKE ? OR handle LIKE ? OR vendor LIKE ? OR product_type LIKE ?)"
                for t in terms:
                    like=f"%{t}%"; preds.append(tmpl); params.extend([like,like,like,like,like,like])
                return "("+ " OR ".join(preds)+")", params

            if intent=="gas":
                domain_terms=["gas","tanque","estacionario","estacionaria"]+_GAS_FUELS+_GAS_ALLOW_FAMS
                attr_terms  =["sensor","medidor","valvula","válvula","ip67","impermeable","intemperie","exterior","wifi","app","nivel","porcentaje","volumen"]
            elif intent=="water":
                domain_terms=["agua","tinaco","cisterna","nivel"]+_WATER_ALLOW_FAMS
                attr_terms  =["sensor","medidor","valvula","válvula","ip67","impermeable","intemperie","exterior","wifi","app","ultrasonico","ultrasónico","presion","presión","electrodos"]
            elif intent=="control_sony":
                domain_terms=["sony"]; attr_terms=["control","remoto","bravia","rm"]
            else:
                domain_terms=clean_terms; attr_terms=[]

            where_parts=[]; params_all=[]
            dom_sql,dom_params=_like_group(domain_terms)
            if dom_sql: where_parts.append(dom_sql); params_all.extend(dom_params)
            att_sql,att_params=_like_group(attr_terms)
            if att_sql: where_parts.append(att_sql); params_all.extend(att_params)
            if where_parts:
                sql=f"SELECT id FROM products WHERE {' AND '.join(where_parts)} LIMIT ?"
                params_all.append(k*30)
                try:
                    like_rows=list(cur.execute(sql, tuple(params_all)))
                    ids.extend([int(r["id"]) for r in like_rows])
                except Exception:
                    pass

        # únicos
        seen2=set(); uniq_ids=[]
        for i in ids:
            if i not in seen2: seen2.add(i); uniq_ids.append(i)

        # cargar filas/variantes
        candidates=[]
        for pid in uniq_ids:
            p=cur.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not p: continue
            vars_=list(cur.execute("SELECT * FROM variants WHERE product_id=?", (pid,)))
            if not vars_: continue
            v_infos=[]
            for v in vars_:
                inv=list(cur.execute("SELECT location_name, available FROM inventory WHERE variant_id=?", (v["id"],)))
                v_infos.append({"variant_id":v["id"],"sku":(v.get("sku") or None),"price":v["price"],
                                "compare_at_price":v.get("compare_at_price"),
                                "inventory":[{"name":x["location_name"],"available":int(x["available"])} for x in inv]})
            if not v_infos: continue
            v_infos.sort(key=lambda vv: sum(ii["available"] for ii in vv["inventory"]) if vv["inventory"] else 0, reverse=True)
            best=v_infos[0]
            candidates.append({"id":p["id"],"title":p["title"] or "","handle":p.get("handle") or "","image":p.get("image"),
                               "body":p.get("body") or "","tags":p.get("tags") or "","vendor":p.get("vendor") or "",
                               "product_type":p.get("product_type") or "","variant":best,"skus":[x.get("sku") for x in v_infos if x.get("sku")]})

        # --- Filtro DURO por intención con límites de palabra ---
        def concat_all(it):
            parts=[it["title"],it["handle"],it["tags"],it.get("vendor",""),it.get("product_type",""),it.get("body","")]
            parts.extend([sku or "" for sku in (it.get("skus") or [])])
            return _norm(" ".join(parts))

        def has_token(st: str, tok: str) -> bool:
            # límite de palabra (no coincide 'vegas', sí ' gas ' o inicio/fin)
            return re.search(rf"(^|[^a-z0-9]){re.escape(tok)}([^a-z0-9]|$)", st) is not None

        if intent in ("gas","water") and candidates:
            pos=[]
            for it in candidates:
                st=concat_all(it)
                if intent=="gas":
                    blocked = any(b in st for b in _GAS_BLOCK)
                    ok = (has_token(st,"gas") or any(has_token(st,f) for f in _GAS_ALLOW_FAMS) or any(has_token(st,f) for f in _GAS_FUELS))
                    if ok and not blocked: pos.append(it)
                else:
                    blocked = any(b in st for b in _WATER_BLOCK)
                    ok = (any(has_token(st,w) for w in _WATER_KEYWORDS) or any(has_token(st,f) for f in _WATER_ALLOW_FAMS))
                    if ok and not blocked: pos.append(it)
            candidates = pos  # si queda vacío, preferimos “sin resultados” antes que colados

        # --- Subfiltros adicionales opcionales ---
        def strong_text(it): 
            return _norm(it["title"]+" "+it["handle"]+" "+it["tags"]+" "+it.get("product_type","")+" "+it.get("vendor",""))

        if candidates and intent=="control_sony":
            subset=[it for it in candidates if "sony" in (strong_text(it)+" "+_norm(it.get("body","")))]
            if subset: candidates=subset

        # --- Re-ranking final (igual que antes, con boosts por familia/atributos) ---
        def hits(text, term): return _norm(text).count(term)
        def _has_matrix(text_norm, mx): return (mx in text_norm) or (mx.replace("x","×") in text_norm)
        want_valve=("valvula" in q_norm or "válvula" in q_norm)
        want_ip67=("ip67" in q_norm) or ("intemperie" in q_norm) or ("exterior" in q_norm)
        want_wifi=("wifi" in q_norm) or ("app" in q_norm)

        def score_item(it):
            ttl,hdl,tgs,bdy = it["title"],it["handle"],it["tags"],it["body"]
            vendor=it["vendor"]; ptype=it["product_type"]
            s=0
            for t in clean_terms:
                s+=7*hits(ttl,t); s+=5*hits(hdl,t); s+=3*hits(tgs,t); s+=2*hits(ptype,t); s+=1*hits(vendor,t); s+=2*hits(bdy,t)
            st=strong_text(it)+" "+_norm(bdy)
            if intent=="gas":
                if any(f in st for f in _GAS_ALLOW_FAMS): s+=60
                if has_token(st,"gas"): s+=25
                if want_valve and any(x in st for x in ["gassensorv","válvula","valvula"]): s+=50
                if want_ip67 and any(x in st for x in ["ip67","impermeable","intemperie","exterior"]): s+=25
                if want_wifi and any(x in st for x in ["wifi","app"]): s+=12
            if intent=="water":
                if any(f in st for f in _WATER_ALLOW_FAMS): s+=60
                if any(has_token(st,w) for w in _WATER_KEYWORDS): s+=25
                if want_valve and any(x in st for x in ["waterv","válvula","valvula"]): s+=40
                if want_ip67 and any(x in st for x in ["ip67","impermeable","intemperie","exterior"]): s+=20
                if want_wifi and any(x in st for x in ["wifi","app"]): s+=10
            if clean_terms and _norm(ttl).startswith(clean_terms[0]): s+=6
            sku_set={_norm(sk) for sk in (it["skus"] or [])}
            if set(clean_terms) & sku_set: s+=25
            stock=sum(x["available"] for x in it["variant"]["inventory"]) if it["variant"]["inventory"] else 0
            if stock>0: s+=min(stock,20)
            if q_matrix:
                st_full=_norm(it["title"]+" "+it["handle"]+" "+it["tags"])
                if _has_matrix(st_full,q_matrix): s+=60
                else:
                    other=re.findall(r"\b(\d+)\s*[x×]\s*(\d+)\b", st_full)
                    for a,b in other:
                        if f"{a}x{b}"!=q_matrix: s-=12; break
            return s

        candidates.sort(key=score_item, reverse=True)

        results=[]
        for it in candidates[:max(k,12)]:
            v=it["variant"]
            product_url=f"{self.store_base_url}/products/{it['handle']}" if it["handle"] else self.store_base_url
            buy_url=f"{self.store_base_url}/cart/{v['variant_id']}:1"
            results.append({"id":it["id"],"title":it["title"],"handle":it["handle"],"image":it["image"],"body":it["body"],
                            "tags":it["tags"],"vendor":it["vendor"],"product_type":it["product_type"],
                            "product_url":product_url,"buy_url":buy_url,"variant":v})
        conn.close()
        return results[:k]
