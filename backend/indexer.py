# -*- coding: utf-8 -*-
"""
Indexer del catálogo Shopify -> SQLite (+FTS5) para el buscador del bot.

Reglas de filtrado (configurables por variables de entorno):
- REQUIRE_ACTIVE=1  -> exige product.status == "active"
- REQUIRE_IMAGE=1   -> exige al menos una imagen
- REQUIRE_STOCK=0/1 -> exige stock > 0 por variante (por defecto 1; en Opción B: 0)
- REQUIRE_SKU=0/1   -> exige SKU no vacío (por defecto 1; en Opción B: 0)
- MIN_BODY_CHARS=N  -> mínimo de caracteres de descripción tras limpiar HTML (por defecto 10; en Opción B: 0)

Tablas:
- products(id, handle, title, body, tags, vendor, product_type, image)
- variants(id, product_id, sku, price, compare_at_price, inventory_item_id)
- inventory(variant_id, location_id, location_name, available)
- products_fts (FTS5 sobre title/body/tags) si está disponible; si no, hay fallback LIKE.
"""
from __future__ import annotations

import os
import re
import json
import sqlite3
from typing import Any, Dict, List, Tuple, Optional

from .utils import strip_html


BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
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

        # Reglas desde entorno (ver docstring)
        self.rules = {
            "REQUIRE_ACTIVE": os.getenv("REQUIRE_ACTIVE", "1") == "1",
            "REQUIRE_IMAGE": os.getenv("REQUIRE_IMAGE", "1") == "1",
            "REQUIRE_STOCK": os.getenv("REQUIRE_STOCK", "1") == "1",
            "REQUIRE_SKU": os.getenv("REQUIRE_SKU", "1") == "1",
            "MIN_BODY_CHARS": int(os.getenv("MIN_BODY_CHARS", "10")),
        }

        self.db_path = os.path.join(DATA_DIR, "catalog.sqlite3")
        self._fts_enabled = False

        # métricas del último build
        self._stats: Dict[str, Any] = {
            "products": 0,
            "variants": 0,
            "inventory_levels": 0,
        }
        # descarte de productos (para diagnóstico)
        self._discards_sample: List[Dict[str, Any]] = []
        self._discards_count: Dict[str, int] = {}

        # cachés auxiliares
        self._location_map: Dict[int, str] = {}       # location_id -> name
        self._inventory_map: Dict[int, List[Dict]] = {}  # inventory_item_id -> [{location_id, available}, ...]

    # ---------- utils de conexión ----------
    def _conn_rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = _row_factory
        return conn

    def _conn_read(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = _row_factory
        return conn

    # ---------- reglas ----------
    def _passes_product_rules(self, p: Dict[str, Any]) -> Tuple[bool, str]:
        if self.rules["REQUIRE_ACTIVE"] and p.get("status") != "active":
            return False, "status!=active"
        images = p.get("images") or []
        if self.rules["REQUIRE_IMAGE"] and not images:
            return False, "no_image"

        if self.rules["MIN_BODY_CHARS"] > 0:
            text = strip_html(p.get("body_html") or "")
            if len(text) < self.rules["MIN_BODY_CHARS"]:
                return False, "no_body"

        return True, ""

    def _select_valid_variants(self, variants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Aplica reglas por variante (precio requerido; SKU/stock según banderas)."""
        out: List[Dict[str, Any]] = []
        for v in variants:
            price = v.get("price")
            sku = (v.get("sku") or "").strip()
            inv_item_id = v.get("inventory_item_id")
            inv_levels = self._inventory_map.get(inv_item_id, []) if inv_item_id else []
            total = sum(int(x.get("available") or 0) for x in inv_levels)

            if price is None:
                continue
            if self.rules["REQUIRE_SKU"] and not sku:
                continue
            if self.rules["REQUIRE_STOCK"] and total <= 0:
                continue

            out.append({
                "id": v["id"],
                "sku": sku or None,
                "price": float(price),
                "compare_at_price": float(v["compare_at_price"]) if v.get("compare_at_price") is not None else None,
                "inventory_item_id": inv_item_id,
                "inventory": inv_levels,  # se convierte a nombres más adelante
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
        self._location_map = {int(x["id"]): x.get("name") or str(x["id"]) for x in locations}

        # productos
        products = self.client.list_products()  # puede tardar
        # recoger todos los inventory_item_id
        all_inv_ids: List[int] = []
        for p in products:
            for v in p.get("variants") or []:
                if v.get("inventory_item_id"):
                    all_inv_ids.append(int(v["inventory_item_id"]))

        # inventario por item id
        levels = self.client.inventory_levels_for_items(all_inv_ids) if all_inv_ids else []
        self._stats["inventory_levels"] = len(levels)

        # construir índice en disco
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

        # build inventory map
        self._inventory_map = {}
        for lev in levels:
            iid = int(lev["inventory_item_id"])
            self._inventory_map.setdefault(iid, []).append({
                "location_id": int(lev["location_id"]),
                "available": int(lev.get("available") or 0),
            })

        conn = self._conn_rw()
        cur = conn.cursor()

        # tablas
        cur.executescript(
            """
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
            """
        )
        conn.commit()

        # Intentar crear FTS5 (si no está disponible, seguimos con LIKE)
        try:
            cur.execute("CREATE VIRTUAL TABLE products_fts USING fts5(title, body, tags, content='products', content_rowid='id')")
            self._fts_enabled = True
        except sqlite3.OperationalError:
            self._fts_enabled = False

        # inserción de productos/variantes
        discards_sample: List[Dict[str, Any]] = []
        discards_count: Dict[str, int] = {}

        ins_p = "INSERT INTO products (id, handle, title, body, tags, vendor, product_type, image) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        ins_v = "INSERT INTO variants (id, product_id, sku, price, compare_at_price, inventory_item_id) VALUES (?, ?, ?, ?, ?, ?)"
        ins_inv = "INSERT INTO inventory (variant_id, location_id, location_name, available) VALUES (?, ?, ?, ?)"

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

            # variantes válidas
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

            image = None
            if (p.get("images") or []):
                # primera imagen
                image = (p["images"][0].get("src") or "").strip() or None

            body_text = strip_html(p.get("body_html") or "")
            tags = ",".join((p.get("tags") or "").split(",")).strip()

            cur.execute(ins_p, (
                int(p["id"]),
                p.get("handle"),
                p.get("title"),
                body_text,
                tags,
                p.get("vendor"),
                p.get("product_type"),
                image,
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

                # inventario
                for lvl in v["inventory"]:
                    loc_id = int(lvl["location_id"])
                    cur.execute(ins_inv, (
                        int(v["id"]),
                        loc_id,
                        self._location_map.get(loc_id, str(loc_id)),
                        int(lvl.get("available") or 0),
                    ))

        conn.commit()

        # poblar FTS si está habilitado
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
        # transformar a lista ordenada para fácil consumo en consola
        by_reason = [{"reason": k, "count": v} for k, v in sorted(self._discards_count.items(), key=lambda x: -x[1])]
        return {"ok": True, "by_reason": by_reason, "sample": self._discards_sample}

    def sample_products(self, limit: int = 10) -> List[Dict[str, Any]]:
        conn = self._conn_read()
        cur = conn.cursor()
        rows = list(cur.execute("SELECT id, handle, title, vendor, product_type FROM products LIMIT ?", (int(limit),)))
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

        # 2) Fallback LIKE AND por término
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

        # únicos/top-k
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
            # filtrar variantes “presentables”: precio, (stock si se exige)
            v_infos: List[Dict[str, Any]] = []
            for v in vars_:
                inv = list(cur.execute("SELECT location_name, available FROM inventory WHERE variant_id=?", (v["id"],)))
                total = sum(int(x["available"]) for x in inv) if inv else 0
                if v["price"] is None:
                    continue
                if self.rules["REQUIRE_STOCK"] and total <= 0:
                    continue
                if self.rules["REQUIRE_SKU"] and not (v.get("sku") or "").strip():
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

            # ordenar por stock descendente (si no exigimos stock, mantiene orden de inserción)
            v_infos.sort(key=lambda vv: sum(ii["available"] for ii in vv["inventory"]) if vv["inventory"] else 0, reverse=True)
            best = v_infos[0]

            product_url = f"{self.store_base_url}/products/{p['handle']}"
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
                "variant": best,
                "product_url": product_url,
                "buy_url": buy_url,
            })

        conn.close()
        return results

    # ---------- util para LLM ----------
    def mini_catalog_json(self, items: List[Dict[str, Any]]) -> str:
        small = []
        for it in items:
            v = it["variant"]
            small.append({
                "title": it["title"],
                "price": v["price"],
                "compare_at_price": v.get("compare_at_price"),
                "product_url": it["product_url"],
                "buy_url": it["buy_url"],
                "stock_total": sum(x["available"] for x in v["inventory"]) if v["inventory"] else 0,
            })
        return json.dumps(small, ensure_ascii=False)
