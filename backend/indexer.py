# -*- coding: utf-8 -*-
"""
Indexer del catálogo Shopify -> SQLite (+FTS5) para el buscador del bot.

Reglas de filtrado (configurables por variables de entorno):
- REQUIRE_ACTIVE=1  -> exige product.status == "active"

(No se consideran imagen, SKU, stock ni tamaño de descripción.)

Tablas:
- products(id, handle, title, body, tags, vendor, product_type, image)
- variants(id, product_id, sku, price, compare_at_price, inventory_item_id)
- inventory(variant_id, location_id, location_name, available)
- products_fts (FTS5 sobre title/body/tags) si está disponible; fallback a LIKE.

Uso:
- build() descarga catálogo y crea/llena SQLite.
- search(q, k) consulta por FTS/LIKE y devuelve tarjetas listas para el widget.
- stats(), discard_stats(), sample_products() para endpoints admin.
"""
from __future__ import annotations

import os
import re
import json
import sqlite3
from typing import Any, Dict, List, Tuple, Optional
from .utils import strip_html

# ---------- Paths / Data dir ----------
BASE_DIR = os.path.dirname(__file__)
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")

# Si nos pasan una ruta completa de SQLite vía entorno, úsala (Render: SQLITE_PATH=/data/catalog.db)
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


class CatalogIndexer:
    def __init__(self, shop_client, store_base_url: str):
        self.client = shop_client
        self.store_base_url = store_base_url.rstrip("/")

        # Única regla: por defecto solo productos ACTIVO
        self.rules = {
            "REQUIRE_ACTIVE": os.getenv("REQUIRE_ACTIVE", "1") == "1",
        }

        self.db_path = DB_PATH
        self._fts_enabled = False

        # métricas del último build
        self._stats: Dict[str, int] = {
            "products": 0,
            "variants": 0,
            "inventory_levels": 0,
        }

        # diagnóstico de descartes
        self._discards_sample: List[Dict[str, Any]] = []
        self._discards_count: Dict[str, int] = {}

        # cachés auxiliares
        self._location_map: Dict[int, str] = {}            # location_id -> name
        self._inventory_map: Dict[int, List[Dict]] = {}    # inventory_item_id -> [{location_id, available}, ...]

    # ---------- conexión ----------
    def _conn_rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = _row_factory
        return conn

    def _conn_read(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = _row_factory
        return conn

    # ---------- util imágenes (solo para mostrar, NO para filtrar) ----------
    @staticmethod
    def _img_src(img_dict: Optional[Dict[str, Any]]) -> Optional[str]:
        if not img_dict:
            return None
        # REST: 'src'; GraphQL puede traer 'url'
        src = (img_dict.get("src") or img_dict.get("url") or "").strip()
        return src or None

    def _extract_hero_image(self, p: Dict[str, Any]) -> Optional[str]:
        """
        Selecciona una imagen 'hero' para tarjetas (si existe):
        1) product.image.src
        2) primera de la galería
        3) la primera imagen asociada a una variante (image_id)
        """
        # 1) destacada
        featured = self._img_src(p.get("image"))
        if featured:
            return featured

        # 2) primera de la galería
        for i in (p.get("images") or []):
            src = self._img_src(i)
            if src:
                return src

        # 3) por variante (mapeando image_id)
        images_by_id = {str(i.get("id")): i for i in (p.get("images") or [])}
        for v in (p.get("variants") or []):
            iid = str(v.get("image_id") or "")
            if iid:
                src = self._img_src(images_by_id.get(iid))
                if src:
                    return src

        return None

    # ---------- reglas ----------
    def _passes_product_rules(self, p: Dict[str, Any]) -> Tuple[bool, str]:
        """Devuelve (ok, reason). Única validación: estado activo (si se exige)."""
        if self.rules["REQUIRE_ACTIVE"] and p.get("status") != "active":
            return False, "status!=active"
        return True, ""

    def _select_valid_variants(self, variants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Selección mínima de variantes:
        - Requiere precio numérico (necesario para la tarjeta y buy_url).
        - NO exige SKU ni stock.
        """
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
                    {
                        "location_id": int(lv["location_id"]),
                        "available": int(lv.get("available") or 0),
                    }
                    for lv in inv_levels
                ],
            })
        return out

    # ---------- build ----------
    def build(self) -> None:
        """Descarga catálogo y levanta índice en SQLite."""
        # locations
        try:
            locations = self.client.list_locations()
        except Exception:
            locations = []
        self._location_map = {int(x["id"]): (x.get("name") or str(x["id"])) for x in locations}

        # productos (REST paginado) — asegúrate que el cliente incluya:
        # id, handle, title, status, body_html, tags, vendor, product_type, image, images, variants
        products = self.client.list_products()  # puede tardar

        # recoger todos los inventory_item_id
        all_inv_ids: List[int] = []
        for p in products:
            for v in p.get("variants") or []:
                if v.get("inventory_item_id"):
                    try:
                        all_inv_ids.append(int(v["inventory_item_id"]))
                    except Exception:
                        pass

        # inventario por item id (batched en el cliente)
        levels = self.client.inventory_levels_for_items(all_inv_ids) if all_inv_ids else []
        self._stats["inventory_levels"] = len(levels)

        # build inventory map
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

        # crear DB limpia
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

        conn = self._conn_rw()
        cur = conn.cursor()

        # tablas
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
            cur.execute(
                "CREATE VIRTUAL TABLE products_fts USING fts5(title, body, tags, content='products', content_rowid='id')"
            )
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
            # 1) Reglas de producto (solo estado activo si se exige)
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

            # 2) Variantes válidas (mínimo: tener precio numérico)
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
            hero_img = self._extract_hero_image(p)  # opcional, solo para mostrar

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

        conn = self._conn_read()
        cur = conn.cursor()

        terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1]
        fts_query = " ".join(terms) if terms else query
        ids: List[int] = []

        # 1) FTS
        if self._fts_enabled:
            fts_rows = list(cur.execute(
                "SELECT rowid FROM products_fts WHERE products_fts MATCH ? LIMIT ?",
                (fts_query, k * 3),
            ))
            ids.extend([int(r["rowid"]) for r in fts_rows])

        # 2) Fallback LIKE (AND por término)
        if len(ids) < k and terms:
            where_parts = []
            params: List[Any] = []
            for t in terms:
                like = f"%{t}%"
                where_parts.append("(title LIKE ? OR body LIKE ? OR tags LIKE ?)")
                params.extend([like, like, like])
            sql = f"SELECT id FROM products WHERE {' AND '.join(where_parts)} LIMIT ?"
            params.append(k * 3)
            like_rows = list(cur.execute(sql, tuple(params)))
            ids.extend([int(r["id"]) for r in like_rows])

        # únicos y top-k
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
                # No filtramos por stock ni SKU
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

            # Orden preferente por mayor disponibilidad total (si aplica)
            v_infos.sort(
                key=lambda vv: sum(ii["available"] for ii in vv["inventory"]) if vv["inventory"] else 0,
                reverse=True,
            )
            best = v_infos[0]

            product_url = f"{self.store_base_url}/products/{p['handle']}" if p.get("handle") else self.store_base_url
            buy_url = f"{self.store_base_url}/cart/{best['variant_id']}:1"

            results.append({
                "id": p["id"],
                "title": p["title"],
                "handle": p["handle"],
                "image": p["image"],
                "body": p["body"],
                "tags": p["tags"],
                "vendor": p["vendor"],
                "product_type": p["product_type"],
                "product_url": product_url,
                "buy_url": buy_url,
                "variant": best,
            })

        conn.close()
        return results

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
