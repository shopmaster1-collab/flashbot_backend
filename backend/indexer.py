# -*- coding: utf-8 -*-
"""
Indexer Shopify -> SQLite (+FTS5)

Reglas (solo una):
- REQUIRE_ACTIVE=1  -> exige product.status == "active"

No se consideran imagen, SKU, stock ni tamaño de descripción.

Variables de entorno usadas (para fallback REST directo a Shopify):
- SHOPIFY_SHOP               (p.ej. master-electronicos.myshopify.com)
- SHOPIFY_ACCESS_TOKEN       (Admin API access token)
- SHOPIFY_API_VERSION        (ej. 2024-10)
- SQLITE_PATH                (p.ej. /data/catalog.db)
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

# ---------------- Paths / DB ----------------
BASE_DIR = os.path.dirname(__file__)
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")

SQLITE_PATH_ENV = os.getenv("SQLITE_PATH", "").strip()
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


# ---------------- Shopify REST helpers ----------------
class ShopifyREST:
    """Fallback REST client para recorrer TODAS las páginas via Link: rel=next."""
    def __init__(self):
        self.shop = (os.getenv("SHOPIFY_SHOP") or "").strip().rstrip("/")
        self.token = (os.getenv("SHOPIFY_ACCESS_TOKEN") or "").strip()
        self.api_ver = (os.getenv("SHOPIFY_API_VERSION") or "2024-10").strip()
        if self.shop.startswith("https://"):
            self.shop = urlparse(self.shop).netloc
        if not self.shop:
            raise RuntimeError("SHOPIFY_SHOP no configurado")
        if not self.token:
            raise RuntimeError("SHOPIFY_ACCESS_TOKEN no configurado")

        self.base = f"https://{self.shop}/admin/api/{self.api_ver}"
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _get(self, path: str, params: Dict[str, Any]) -> requests.Response:
        url = f"{self.base}{path}"
        # Respeta límites de tasa básicos
        for attempt in range(3):
            r = self.session.get(url, params=params, timeout=40)
            if r.status_code == 429:
                time.sleep(1.0 + attempt)
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()
        return r

    @staticmethod
    def _next_page_info(resp: requests.Response) -> Optional[str]:
        link = resp.headers.get("Link") or resp.headers.get("link") or ""
        # Ejemplo: <...page_info=xxxxx>; rel="previous", <...page_info=yyyyy>; rel="next"
        for part in link.split(","):
            if 'rel="next"' in part:
                start = part.find("<")
                end = part.find(">")
                if start >= 0 and end > start:
                    url = part[start + 1:end]
                    qs = parse_qs(urlparse(url).query)
                    p = qs.get("page_info", [None])[0]
                    return p
        return None

    def list_products_active_all(self, limit: int = 250) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page_info: Optional[str] = None
        while True:
            params = {"limit": limit, "status": "active"}
            if page_info:
                params["page_info"] = page_info
            r = self._get("/products.json", params=params)
            data = r.json() or {}
            items = data.get("products") or []
            out.extend(items)
            nxt = self._next_page_info(r)
            if not nxt or not items:
                break
            page_info = nxt
        return out

    def list_locations(self) -> List[Dict[str, Any]]:
        r = self._get("/locations.json", params={})
        return (r.json() or {}).get("locations") or []

    def inventory_levels_for_items(self, item_ids: List[int]) -> List[Dict[str, Any]]:
        """Consulta inventario por lotes (Shopify permite lista separada por coma)."""
        out: List[Dict[str, Any]] = []
        CHUNK = 50  # seguro
        for i in range(0, len(item_ids), CHUNK):
            chunk = item_ids[i:i + CHUNK]
            if not chunk:
                continue
            params = {"inventory_item_ids": ",".join(str(x) for x in chunk), "limit": 250}
            r = self._get("/inventory_levels.json", params=params)
            levels = (r.json() or {}).get("inventory_levels") or []
            out.extend(levels)
        return out


class CatalogIndexer:
    def __init__(self, shop_client, store_base_url: str):
        """
        shop_client: si tu cliente propio NO maneja paginación, este indexer
        usará automáticamente el fallback REST nativo (ShopifyREST).
        """
        self.client = shop_client
        self.rest_fallback: Optional[ShopifyREST] = None
        try:
            # Si hay token/tienda, prepararnos para fallback
            self.rest_fallback = ShopifyREST()
        except Exception:
            # Si faltan credenciales, solo se usará el cliente inyectado
            self.rest_fallback = None

        self.store_base_url = store_base_url.rstrip("/")

        # ÚNICA regla (default=1)
        self.rules = {
            "REQUIRE_ACTIVE": os.getenv("REQUIRE_ACTIVE", "1") == "1",
        }

        self.db_path = DB_PATH
        self._fts_enabled = False

        self._stats: Dict[str, int] = {"products": 0, "variants": 0, "inventory_levels": 0}
        self._discards_sample: List[Dict[str, Any]] = []
        self._discards_count: Dict[str, int] = {}
        self._location_map: Dict[int, str] = {}
        self._inventory_map: Dict[int, List[Dict]] = {}

    # ------------- conexión -------------
    def _conn_rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = _row_factory
        return conn

    def _conn_read(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = _row_factory
        return conn

    # ------------- imágenes (solo para mostrar) -------------
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

    # ------------- reglas -------------
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

    # ------------- fetch productos / locations / inventario -------------
    def _fetch_products_active_all(self) -> List[Dict[str, Any]]:
        """
        Intenta primero con el cliente inyectado (si EXPRESAMENTE soporta paginación).
        Si detectamos 'solo 1 página' o no hay soporte, usa fallback REST nativo.
        """
        # Intento con cliente inyectado (si tiene método extendido)
        try:
            products = self.client.list_products(status="active", limit=250, page_info=None)
            # Si es dict, intenta leer 'next_page_info'
            if isinstance(products, dict) and products.get("next_page_info"):
                items = products.get("items") or products.get("products") or []
                acc = list(items)
                cursor = products.get("next_page_info")
                while cursor:
                    batch = self.client.list_products(status="active", limit=250, page_info=cursor)
                    items = (batch.get("items") or batch.get("products") or [])
                    acc.extend(items or [])
                    cursor = batch.get("next_page_info")
                if acc:
                    return acc
            # Si es lista, asume una página (posible 1ª)
            if isinstance(products, list) and len(products) > 5:
                return products
        except Exception:
            pass

        # Fallback REST nativo (solución robusta)
        if self.rest_fallback:
            return self.rest_fallback.list_products_active_all(limit=250)

        # Último recurso: lo que sea que devuelva el cliente (probable 1 página)
        try:
            any_products = self.client.list_products()
            if isinstance(any_products, dict):
                return any_products.get("items") or any_products.get("products") or []
            return list(any_products or [])
        except Exception:
            return []

    def _fetch_locations(self) -> List[Dict[str, Any]]:
        try:
            return self.client.list_locations()
        except Exception:
            if self.rest_fallback:
                return self.rest_fallback.list_locations()
            return []

    def _fetch_inventory_levels(self, item_ids: List[int]) -> List[Dict[str, Any]]:
        try:
            return self.client.inventory_levels_for_items(item_ids)
        except Exception:
            if self.rest_fallback:
                return self.rest_fallback.inventory_levels_for_items(item_ids)
            return []

    # ------------- build -------------
    def build(self) -> None:
        # locations
        locations = self._fetch_locations()
        self._location_map = {int(x["id"]): (x.get("name") or str(x["id"])) for x in locations}

        # productos ACTIVO (todas las páginas)
        products = self._fetch_products_active_all()

        # inventory_item_ids
        all_inv_ids: List[int] = []
        for p in products:
            for v in p.get("variants") or []:
                if v.get("inventory_item_id"):
                    try:
                        all_inv_ids.append(int(v["inventory_item_id"]))
                    except Exception:
                        pass

        # inventario
        levels = self._fetch_inventory_levels(all_inv_ids) if all_inv_ids else []
        self._stats["inventory_levels"] = len(levels)

        self._inventory_map = {}
        for lev in levels:
            try:
                iid = int(lev["inventory_item_id"])
                self._inventory_map.setdefault(iid, []).append({
                    "location_id": int(lev["location_id"]),
                    "available": int(lev.get("available") or 0),
                })
            except Exception:
                continue

        # reset DB
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

        conn = self._conn_rw()
        cur = conn.cursor()

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
                    discards_sample.append({"product_id": p.get("id"), "handle": p.get("handle"), "title": p.get("title"), "reason": reason})
                continue

            valids = self._select_valid_variants(p.get("variants") or [])
            if not valids:
                discards_count["no_variant_complete"] = discards_count.get("no_variant_complete", 0) + 1
                if len(discards_sample) < 20:
                    discards_sample.append({"product_id": p.get("id"), "handle": p.get("handle"), "title": p.get("title"), "reason": "no_variant_complete"})
                continue

            body_text = strip_html(p.get("body_html") or "")
            tags = (p.get("tags") or "").strip()
            hero_img = self._extract_hero_image(p)

            cur.execute(ins_p, (int(p["id"]), p.get("handle"), p.get("title"), body_text, tags, p.get("vendor"), p.get("product_type"), hero_img))
            n_products += 1

            for v in valids:
                cur.execute(ins_v, (int(v["id"]), int(p["id"]), v.get("sku"), v.get("price"), v.get("compare_at_price"),
                                    int(v["inventory_item_id"]) if v.get("inventory_item_id") else None))
                n_variants += 1

                for lvl in v["inventory"]:
                    loc_id = int(lvl["location_id"])
                    cur.execute(ins_inv, (int(v["id"]), loc_id, self._location_map.get(loc_id, str(loc_id)), int(lvl.get("available") or 0)))

        conn.commit()

        if self._fts_enabled:
            cur.execute("INSERT INTO products_fts (rowid, title, body, tags) SELECT id, title, body, tags FROM products")
            conn.commit()

        conn.close()

        self._stats["products"] = n_products
        self._stats["variants"] = n_variants
        self._discards_sample = discards_sample
        self._discards_count = discards_count

    # ------------- reporting -------------
    def stats(self) -> Dict[str, Any]:
        return dict(self._stats)

    def discard_stats(self) -> Dict[str, Any]:
        by_reason = [{"reason": k, "count": v} for k, v in sorted(self._discards_count.items(), key=lambda x: -x[1])]
        return {"ok": True, "by_reason": by_reason, "sample": self._discards_sample}

    def sample_products(self, limit: int = 10) -> List[Dict[str, Any]]:
        conn = self._conn_read()
        cur = conn.cursor()
        rows = list(cur.execute("SELECT id, handle, title, vendor, product_type, image FROM products LIMIT ?", (int(limit),)))
        conn.close()
        return rows

    # ------------- búsqueda -------------
    def search(self, query: str, k: int = 6) -> List[Dict[str, Any]]:
        if not query:
            return []
        conn = self._conn_read()
        cur = conn.cursor()

        terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1]
        fts_query = " ".join(terms) if terms else query
        ids: List[int] = []

        if self._fts_enabled:
            rows = list(cur.execute("SELECT rowid FROM products_fts WHERE products_fts MATCH ? LIMIT ?", (fts_query, k * 3)))
            ids.extend([int(r["rowid"]) for r in rows])

        if len(ids) < k and terms:
            where, params = [], []
            for t in terms:
                like = f"%{t}%"
                where.append("(title LIKE ? OR body LIKE ? OR tags LIKE ?)")
                params += [like, like, like]
            sql = f"SELECT id FROM products WHERE {' AND '.join(where)} LIMIT ?"
            params.append(k * 3)
            rows = list(cur.execute(sql, tuple(params)))
            ids.extend([int(r["id"]) for r in rows])

        seen, order = set(), []
        for i in ids:
            if i not in seen:
                seen.add(i)
                order.append(i)
        ids = order[:k]

        results: List[Dict[str, Any]] = []
        for pid in ids:
            p = cur.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not p:
                continue
            vars_ = list(cur.execute("SELECT * FROM variants WHERE product_id=?", (pid,)))
            v_infos: List[Dict[str, Any]] = []
            for v in vars_:
                inv = list(cur.execute("SELECT location_name, available FROM inventory WHERE variant_id=?", (v["id"],)))
                if v["price"] is None:
                    continue
                v_infos.append({
                    "variant_id": v["id"],
                    "sku": (v.get("sku") or None),
                    "price": v["price"],
                    "compare_at_price": v.get("compare_at_price"),
                    "inventory": [{"name": x["location_name"], "available": int(x["available"])} for x in inv],
                })
            if not v_infos:
                continue
            v_infos.sort(key=lambda vv: sum(ii["available"] for ii in vv["inventory"]) if vv["inventory"] else 0, reverse=True)
            best = v_infos[0]

            product_url = f"{self.store_base_url}/products/{p['handle']}" if p.get("handle") else self.store_base_url
            buy_url = f"{self.store_base_url}/cart/{best['variant_id']}:1"

            results.append({
                "id": p["id"], "title": p["title"], "handle": p["handle"], "image": p["image"],
                "body": p["body"], "tags": p["tags"], "vendor": p["vendor"], "product_type": p["product_type"],
                "product_url": product_url, "buy_url": buy_url, "variant": best,
            })

        conn.close()
        return results

    # ------------- util LLM -------------
    def mini_catalog_json(self, items: List[Dict[str, Any]]) -> str:
        out = []
        for it in items:
            v = it["variant"]
            out.append({
                "title": it["title"], "price": v["price"], "sku": v.get("sku"),
                "compare_at_price": v.get("compare_at_price"),
                "product_url": it["product_url"], "buy_url": it["buy_url"],
                "stock_total": sum(x["available"] for x in v["inventory"]) if v["inventory"] else 0,
                "image": it["image"],
            })
        return json.dumps(out, ensure_ascii=False)
