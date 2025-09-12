# -*- coding: utf-8 -*-
"""
Indexer Shopify -> SQLite (+FTS5) para el buscador del bot.

Reglas:
- REQUIRE_ACTIVE=1  -> exige product.status == "active" (único filtro)

Variables de entorno relevantes:
- SHOPIFY_SHOP               | SHOPIFY_STORE_DOMAIN
- SHOPIFY_ACCESS_TOKEN       | SHOPIFY_TOKEN
- SHOPIFY_API_VERSION        (default 2024-10)
- SQLITE_PATH                (p.ej. /data/catalog.db)
- FORCE_REST=1               (opcional; fuerza camino REST paginado)
"""

from __future__ import annotations

import os
import re
import json
import time
import sqlite3
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
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


# ---------- REST nativo (paginación con page_info) ----------
class ShopifyREST:
    def __init__(self):
        store = (
            os.getenv("SHOPIFY_STORE_DOMAIN")
            or os.getenv("SHOPIFY_SHOP")
            or ""
        ).strip()
        if store.startswith("http"):
            store = urlparse(store).netloc

        token = (
            os.getenv("SHOPIFY_ACCESS_TOKEN")
            or os.getenv("SHOPIFY_TOKEN")
            or ""
        ).strip()
        api_ver = (os.getenv("SHOPIFY_API_VERSION") or "2024-10").strip()

        if not store or not token:
            raise RuntimeError("Faltan SHOPIFY_SHOP|STORE_DOMAIN o SHOPIFY_ACCESS_TOKEN|TOKEN")

        self.shop = store
        self.api_ver = api_ver
        self.base = f"https://{store}/admin/api/{api_ver}"
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    @staticmethod
    def _next_page_info(resp: requests.Response) -> Optional[str]:
        link = resp.headers.get("Link") or resp.headers.get("link") or ""
        for part in link.split(","):
            if 'rel="next"' in part:
                s = part.find("<")
                e = part.find(">")
                if s >= 0 and e > s:
                    url = part[s + 1:e]
                    qs = parse_qs(urlparse(url).query)
                    return (qs.get("page_info") or [None])[0]
        return None

    def _get(self, path: str, params: Dict[str, Any]) -> requests.Response:
        url = f"{self.base}{path}"
        for attempt in range(4):
            r = self.session.get(url, params=params, timeout=40)
            if r.status_code == 429:
                time.sleep(1.0 + attempt)
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()
        return r

    # Trae TODAS las páginas de /products.json SIN 'status' en el request.
    def list_products_all(self, limit: int = 250) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page: Optional[str] = None
        while True:
            params = {"limit": limit}
            if page:
                params["page_info"] = page
            r = self._get("/products.json", params)
            data = r.json() or {}
            items = data.get("products") or []
            out.extend(items)
            page = self._next_page_info(r)
            if not page or not items:
                break
        return out

    def list_locations(self) -> List[Dict[str, Any]]:
        r = self._get("/locations.json", {})
        return (r.json() or {}).get("locations") or []

    def inventory_levels_for_items(self, item_ids: List[int]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        CHUNK = 50
        for i in range(0, len(item_ids), CHUNK):
            chunk = item_ids[i:i + CHUNK]
            if not chunk:
                continue
            params = {"inventory_item_ids": ",".join(str(x) for x in chunk), "limit": 250}
            r = self._get("/inventory_levels.json", params)
            out.extend((r.json() or {}).get("inventory_levels") or [])
        return out


class CatalogIndexer:
    def __init__(self, shop_client, store_base_url: str):
        """
        shop_client: instancia de ShopifyClient (puede ser None).
        Si FORCE_REST=1 o faltan métodos en el cliente, se usa ShopifyREST.
        """
        self.client = shop_client
        self.store_base_url = (store_base_url or "").rstrip("/") or "https://master.com.mx"

        # ÚNICA REGLA
        self.rules = {"REQUIRE_ACTIVE": os.getenv("REQUIRE_ACTIVE", "1") == "1"}

        self.db_path = DB_PATH
        self._fts_enabled = False

        self._stats: Dict[str, int] = {"products": 0, "variants": 0, "inventory_levels": 0}
        self._discards_sample: List[Dict[str, Any]] = []
        self._discards_count: Dict[str, int] = {}

        self._location_map: Dict[int, str] = {}
        self._inventory_map: Dict[int, List[Dict]] = {}

        # REST fallback (si hay credenciales)
        self._rest_fallback: Optional[ShopifyREST] = None
        try:
            self._rest_fallback = ShopifyREST()
        except Exception:
            self._rest_fallback = None

    # ---------- conexiones ----------
    def _conn_rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = _row_factory
        return conn

    def _conn_read(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = _row_factory
        return conn

    # ---------- util imágenes ----------
    @staticmethod
    def _img_src(img: Optional[Dict[str, Any]]) -> Optional[str]:
        if not img:
            return None
        src = (img.get("src") or img.get("url") or "").strip()
        return src or None

    def _extract_hero_image(self, p: Dict[str, Any]) -> Optional[str]:
        s = self._img_src(p.get("image"))
        if s:
            return s
        for i in (p.get("images") or []):
            s = self._img_src(i)
            if s:
                return s
        images_by_id = {str(i.get("id")): i for i in (p.get("images") or [])}
        for v in (p.get("variants") or []):
            iid = str(v.get("image_id") or "")
            if iid:
                s = self._img_src(images_by_id.get(iid))
                if s:
                    return s
        return None

    # ---------- reglas ----------
    def _passes_product_rules(self, p: Dict[str, Any]) -> Tuple[bool, str]:
        if self.rules["REQUIRE_ACTIVE"] and p.get("status") != "active":
            return False, "status!=active"
        return True, ""

    def _select_valid_variants(self, variants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for v in variants or []:
            price = v.get("price")
            try:
                price_f = float(price) if price is not None else None
            except Exception:
                price_f = None
            if price_f is None:
                continue

            inv_item_id = v.get("inventory_item_id")
            inv_levels = self._inventory_map.get(int(inv_item_id), []) if inv_item_id else []

            cap = v.get("compare_at_price")
            try:
                cap_f = float(cap) if cap is not None else None
            except Exception:
                cap_f = None

            out.append({
                "id": int(v["id"]),
                "sku": (v.get("sku") or None),
                "price": price_f,
                "compare_at_price": cap_f,
                "inventory_item_id": int(inv_item_id) if inv_item_id else None,
                "inventory": [
                    {"location_id": int(lv["location_id"]), "available": int(lv.get("available") or 0)}
                    for lv in inv_levels
                ],
            })
        return out

    # ---------- fetch productos ----------
    def _fetch_all_active(self, limit: int = 250) -> List[Dict[str, Any]]:
        """Devuelve productos ACTIVOS. Usa REST paginado sin status en el request y filtra en Python."""
        force_rest = os.getenv("FORCE_REST", "0") == "1"

        # Preferimos REST con paginación robusta
        if force_rest and self._rest_fallback:
            all_items = self._rest_fallback.list_products_all(limit=limit)
            return [p for p in all_items if (p.get("status") == "active")]

        # Intento con cliente inyectado (si tiene paginación propia)
        try:
            if hasattr(self.client, "list_products"):
                acc: List[Dict[str, Any]] = []
                page = None
                while True:
                    # sin status en el request; filtramos después
                    resp = self.client.list_products(limit=limit, page_info=page)
                    if isinstance(resp, dict):
                        items = (resp.get("products") or resp.get("items") or []) or []
                        acc.extend(items)
                        page = resp.get("next_page_info")
                        if not page or not items:
                            break
                    else:
                        if isinstance(resp, list):
                            acc = list(resp)
                        break
                if acc:
                    return [p for p in acc if (p.get("status") == "active")]
        except Exception:
            pass

        # Fallback final a REST
        if self._rest_fallback:
            all_items = self._rest_fallback.list_products_all(limit=limit)
            return [p for p in all_items if (p.get("status") == "active")]
        return []

    # ---------- build ----------
    def build(self) -> None:
        """Crea esquema primero y luego llena datos (robusto)."""
        # reset archivo
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

        conn = self._conn_rw()
        cur = conn.cursor()

        # esquema
        cur.executescript("""
            PRAGMA journal_mode=WAL;

            CREATE TABLE products (
              id INTEGER PRIMARY KEY,
              handle TEXT,
              title TEXT,
              body TEXT,
              tags TEXT,
              vendor TEXT,
              product_type TEXT,
              image TEXT
            );

            CREATE TABLE variants (
              id INTEGER PRIMARY KEY,
              product_id INTEGER,
              sku TEXT,
              price REAL,
              compare_at_price REAL,
              inventory_item_id INTEGER
            );

            CREATE TABLE inventory (
              variant_id INTEGER,
              location_id INTEGER,
              location_name TEXT,
              available INTEGER
            );
        """)
        conn.commit()

        # FTS opcional
        try:
            cur.execute("CREATE VIRTUAL TABLE products_fts USING fts5(title, body, tags, content='products', content_rowid='id')")
            self._fts_enabled = True
        except sqlite3.OperationalError:
            self._fts_enabled = False

        # fetch Shopify
        try:
            locations = self._rest_fallback.list_locations() if self._rest_fallback else (self.client.list_locations() if self.client else [])
        except Exception:
            locations = []
        self._location_map = {int(x["id"]): (x.get("name") or str(x["id"])) for x in locations}

        try:
            products = self._fetch_all_active(limit=250)
        except Exception as e:
            print(f"[INDEX] ERROR list_products: {e}", flush=True)
            products = []

        print(f"[INDEX] fetched: locations={len(locations)} products={len(products)}", flush=True)

        # inventory
        all_inv_ids: List[int] = []
        for p in products:
            for v in p.get("variants") or []:
                if v.get("inventory_item_id"):
                    try:
                        all_inv_ids.append(int(v["inventory_item_id"]))
                    except Exception:
                        pass

        try:
            if self._rest_fallback:
                levels = self._rest_fallback.inventory_levels_for_items(all_inv_ids) if all_inv_ids else []
            elif self.client:
                levels = self.client.inventory_levels_for_items(all_inv_ids) if all_inv_ids else []
            else:
                levels = []
        except Exception as e:
            print(f"[INDEX] ERROR inventory_levels: {e}", flush=True)
            levels = []

        self._stats["inventory_levels"] = len(levels)

        self._inventory_map = {}
        for lev in levels:
            try:
                iid = int(lev["inventory_item_id"])
                self._inventory_map.setdefault(iid, []).append({
                    "location_id": int(lev["location_id"]),
                    "available": int(lv.get("available") or 0),
                })
            except Exception:
                continue

        # volcado
        ins_p = "INSERT INTO products (id, handle, title, body, tags, vendor, product_type, image) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        ins_v = "INSERT INTO variants (id, product_id, sku, price, compare_at_price, inventory_item_id) VALUES (?, ?, ?, ?, ?, ?)"
        ins_inv = "INSERT INTO inventory (variant_id, location_id, location_name, available) VALUES (?, ?, ?, ?)"

        discards_sample: List[Dict[str, Any]] = []
        discards_count: Dict[str, int] = {}
        n_products = 0
        n_variants = 0

        for p in products:
            ok, reason = self._passes_product_rules(p)
            if not ok:
                discards_count[reason] = discards_count.get(reason, 0) + 1
                if len(discards_sample) < 20:
                    discards_sample.append({
                        "product_id": p.get("id"),
                        "handle": p.get("handle"),
                        "title": p.get("title"),
                        "reason": reason,
                    })
                continue

            valids = self._select_valid_variants(p.get("variants") or [])
            if not valids:
                discards_count["no_variant_complete"] = discards_count.get("no_variant_complete", 0) + 1
                if len(discards_sample) < 20:
                    discards_sample.append({
                        "product_id": p.get("id"),
                        "handle": p.get("handle"),
                        "title": p.get("title"),
                        "reason": "no_variant_complete",
                    })
                continue

            body_text = strip_html(p.get("body_html") or "")
            tags = (p.get("tags") or "").strip()
            hero_img = self._extract_hero_image(p)

            cur.execute(ins_p, (
                int(p["id"]),
                p.get("handle"),
                p.get("title"),
                body_text,
                tags,
                p.get("vendor"),
                p.get("product_type"),
                hero_img,
            ))
            n_products += 1

            for v in valids:
                cur.execute(ins_v, (
                    int(v["id"]),
                    int(p["id"]),
                    v.get("sku"),
                    v.get("price"),
                    v.get("compare_at_price"),
                    int(v["inventory_item_id"]) if v.get("inventory_item_id") else None,
                ))
                n_variants += 1

                for lvl in v["inventory"]:
                    loc_id = int(lvl["location_id"])
                    cur.execute(ins_inv, (
                        int(v["id"]),
                        loc_id,
                        self._location_map.get(loc_id, str(loc_id)),
                        int(lvl.get("available") or 0),
                    ))

        conn.commit()

        if self._fts_enabled:
            cur.execute("INSERT INTO products_fts (rowid, title, body, tags) SELECT id, title, body, tags FROM products")
            conn.commit()

        conn.close()

        self._stats["products"] = n_products
        self._stats["variants"] = n_variants
        self._discards_sample = discards_sample
        self._discards_count = discards_count

        print(f"[INDEX] done: products={n_products} variants={n_variants} inventory_levels={self._stats['inventory_levels']}", flush=True)

    # ---------- reporting ----------
    def stats(self) -> Dict[str, Any]:
        return dict(self._stats)

    def discard_stats(self) -> Dict[str, Any]:
        by_reason = [{"reason": k, "count": v} for k, v in sorted(self._discards_count.items(), key=lambda x: -x[1])]
        return {"ok": True, "by_reason": by_reason, "sample": self._discards_sample}

    def sample_products(self, limit: int = 10) -> List[Dict[str, Any]]:
        conn = self._conn_read()
        cur = conn.cursor()
        rows = list(cur.execute(
            "SELECT id, handle, title, vendor, product_type, image FROM products LIMIT ?",
            (int(limit),),
        ))
        conn.close()
        return rows

    # ---------- búsqueda ----------
        def search(self, query: str, k: int = 6) -> List[Dict[str, Any]]:
        if not query:
            return []

        # Stopwords y sinónimos
        STOP = {
            "el","la","los","las","un","una","unos","unas",
            "de","del","al","y","o","u","en","a","con","por","para",
            "que","cual","cuales","cuál","cuáles","donde","dónde",
            "busco","busca","buscar","quiero","necesito","tienes","tienen","hay",
            "producto","productos"
        }
        SYN = {
            "divisor": ["splitter","duplicador","repartidor"],
            "splitter": ["divisor","duplicador","repartidor"],
            "agua": ["inundacion","inundación","fuga","nivel","liquido","líquido","water","leak"],
            "sensor": ["detector","sonda"],
            "hdmi": ["hdmi"],
            "cable": ["cordon","cordón"],
        }
        WATER = {"agua","inundacion","inundación","fuga","nivel","liquido","líquido","water","leak"}
        SENSOR = {"sensor","detector","sonda"}

        raw_terms = [t.strip().lower() for t in re.findall(r"\w+", query, re.UNICODE)]
        base_terms = [t for t in raw_terms if len(t) >= 3 and t not in STOP] or [t for t in raw_terms if len(t) >= 3]

        # Expansión de sinónimos
        seen = set()
        expanded: List[str] = []
        for t in base_terms:
            if t not in seen:
                expanded.append(t); seen.add(t)
            for s in SYN.get(t, []):
                if s not in seen:
                    expanded.append(s); seen.add(s)
        clean_terms = expanded[:8] if expanded else []

        # Señal “combo sensor+agua” en la intención
        wants_water_sensor = any(t in WATER for t in clean_terms) and any(t in SENSOR for t in clean_terms)

        conn = self._conn_read()
        cur = conn.cursor()

        ids: List[int] = []

        # FTS5 (OR + NEAR entre los 2 primeros términos)
        if self._fts_enabled and clean_terms:
            or_clause = " OR ".join(clean_terms)
            near_clause = f" OR ({clean_terms[0]} NEAR/6 {clean_terms[1]})" if len(clean_terms) >= 2 else ""
            fts_q = f"({or_clause}){near_clause}"
            try:
                rows = list(cur.execute(
                    "SELECT rowid FROM products_fts WHERE products_fts MATCH ? LIMIT ?",
                    (fts_q, k * 8),
                ))
                ids.extend([int(r["rowid"]) for r in rows])
            except Exception:
                pass

        # Fallback LIKE (OR) incluyendo handle
        if len(ids) < k and clean_terms:
            where_parts, params = [], []
            for t in clean_terms:
                like = f"%{t}%"
                where_parts.append("(title LIKE ? OR body LIKE ? OR tags LIKE ? OR handle LIKE ?)")
                params.extend([like, like, like, like])
            sql = f"SELECT id FROM products WHERE {' OR '.join(where_parts)} LIMIT ?"
            params.append(k * 8)
            try:
                like_rows = list(cur.execute(sql, tuple(params)))
                ids.extend([int(r["id"]) for r in like_rows])
            except Exception:
                pass

        # Únicos (margen amplio para reordenar)
        uniq_ids: List[int] = []
        seen2 = set()
        for i in ids:
            if i not in seen2:
                seen2.add(i)
                uniq_ids.append(i)

        # Construye candidatos completos
        candidates: List[Dict[str, Any]] = []
        for pid in uniq_ids:
            p = cur.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not p:
                continue
            vars_ = list(cur.execute("SELECT * FROM variants WHERE product_id=?", (pid,)))
            if not vars_:
                continue
            v_infos: List[Dict[str, Any]] = []
            for v in vars_:
                inv = list(cur.execute("SELECT location_name, available FROM inventory WHERE variant_id=?", (v["id"],)))
                v_infos.append({
                    "variant_id": v["id"],
                    "sku": (v.get("sku") or None),
                    "price": v["price"],
                    "compare_at_price": v.get("compare_at_price"),
                    "inventory": [{"name": x["location_name"], "available": int(x["available"])} for x in inv],
                })
            if not v_infos:
                continue

            # stock total para desempate
            v_infos.sort(key=lambda vv: sum(ii["available"] for ii in vv["inventory"]) if vv["inventory"] else 0, reverse=True)
            best = v_infos[0]

            candidates.append({
                "id": p["id"],
                "title": p["title"] or "",
                "handle": p.get("handle") or "",
                "image": p.get("image"),
                "body": p.get("body") or "",
                "tags": p.get("tags") or "",
                "vendor": p.get("vendor"),
                "product_type": p.get("product_type"),
                "variant": best,
            })

        # ---- Re-ranking por relevancia semántica ligera ----
        def hits(text: str, term: str) -> int:
            t = text.lower()
            try:
                return t.count(term.lower())
            except Exception:
                return 0

        def score_item(it: Dict[str, Any]) -> int:
            ttl, hdl, tgs, bdy = it["title"], it["handle"], it["tags"], it["body"]
            s = 0
            for t in clean_terms:
                s += 6 * hits(ttl, t)
                s += 4 * hits(hdl, t)
                s += 3 * hits(tgs, t)
                s += 1 * hits(bdy, t)
            # Bonus por aparición completa “sensor + agua” (o sinónimos) en campos fuertes
            if wants_water_sensor:
                title_tokens = set(re.findall(r"\w+", ttl.lower()))
                handle_tokens = set(re.findall(r"\w+", hdl.lower()))
                tag_tokens = set(re.findall(r"\w+", tgs.lower()))
                strong = title_tokens | handle_tokens | tag_tokens
                if (strong & WATER) and (strong & SENSOR):
                    s += 40  # gran boost si cumple la intención exacta
            # Bonus leve si el término principal aparece al inicio del título
            if clean_terms:
                first = clean_terms[0]
                if ttl.lower().startswith(first):
                    s += 5
            # Bonus por stock
            stock = sum(x["available"] for x in it["variant"]["inventory"]) if it["variant"]["inventory"] else 0
            if stock > 0:
                s += min(stock, 20)  # cap
            return s

        candidates.sort(key=score_item, reverse=True)

        # Armar resultado final con URLs
        results: List[Dict[str, Any]] = []
        for it in candidates[:max(k, 10)]:
            v = it["variant"]
            product_url = f"{self.store_base_url}/products/{it['handle']}" if it["handle"] else self.store_base_url
            buy_url = f"{self.store_base_url}/cart/{v['variant_id']}:1"
            results.append({
                **it,
                "product_url": product_url,
                "buy_url": buy_url,
            })

        conn.close()
        return results[:k]

    # ---------- util para LLM ----------
    def mini_catalog_json(self, items: List[Dict[str, Any]]) -> str:
        out = []
        for it in items:
            v = it["variant"]
            out.append({
                "title": it["title"],
                "price": v["price"],
                "sku": v.get("sku"),
                "compare_at_price": v.get("compare_at_price"),
                "product_url": it["product_url"],
                "buy_url": it["buy_url"],
                "stock_total": sum(x["available"] for x in v["inventory"]) if v["inventory"] else 0,
                "image": it["image"],
            })
        return json.dumps(out, ensure_ascii=False)
