# -*- coding: utf-8 -*-
"""
CatalogIndexer
--------------
Crea un índice local (SQLite + FTS5) del catálogo de Shopify para responder
rápido a búsquedas y armar tarjetas de producto para el chatbot.

Tablas:
- products(id, title, handle, body, image, tags, vendor, product_type)
- variants(id, product_id, sku, price, compare_at_price, inventory_item_id)
- locations(id, name)
- inventory_levels(inventory_item_id, location_id, available)
- products_fts (FTS5 sobre title/body/tags)
- debug_discard(product_id, title, handle, reason) -> motivos por los que se descartó

Requisitos de "producto completo":
- status == active
- alguna imagen (product.image, product.images o imagen en variante)
- body_html con mínimo contenido
- al menos 1 variante con: SKU, price y stock total > 0
"""

import os
import sqlite3
from typing import List, Dict, Any, Tuple
from .utils import strip_html

# Carpeta/archivo de la base
DB_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DB_DIR, "catalog.db")


class CatalogIndexer:
    def __init__(self, shopify_client, store_base_url: str):
        self.client = shopify_client
        self.store_base_url = store_base_url.rstrip("/")
        os.makedirs(DB_DIR, exist_ok=True)

    # ----------------------------- DB helpers -----------------------------

    def _conn(self) -> sqlite3.Connection:
        """Abre una conexión nueva (row_factory=Row)."""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def _conn_read(self) -> sqlite3.Connection:
        """Alias de _conn; separada por claridad si se desea caching futuro."""
        return self._conn()

    # ----------------------------- Build index -----------------------------

    def build(self) -> None:
        """
        Descarga catálogo desde Shopify, aplica filtros de calidad y construye
        todas las tablas + índice FTS5. Borra/recrea el esquema cada vez.
        """
        conn = self._conn()
        cur = conn.cursor()

        cur.executescript(
            """
            PRAGMA journal_mode=WAL;

            DROP TABLE IF EXISTS products;
            DROP TABLE IF EXISTS variants;
            DROP TABLE IF EXISTS locations;
            DROP TABLE IF EXISTS inventory_levels;
            DROP TABLE IF EXISTS products_fts;
            DROP TABLE IF EXISTS debug_discard;

            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                title TEXT,
                handle TEXT,
                body TEXT,
                image TEXT,
                tags TEXT,
                vendor TEXT,
                product_type TEXT
            );

            CREATE TABLE variants (
                id INTEGER PRIMARY KEY,
                product_id INTEGER,
                sku TEXT,
                price REAL,
                compare_at_price REAL,
                inventory_item_id INTEGER,
                FOREIGN KEY(product_id) REFERENCES products(id)
            );

            CREATE TABLE locations (
                id INTEGER PRIMARY KEY,
                name TEXT
            );

            CREATE TABLE inventory_levels (
                inventory_item_id INTEGER,
                location_id INTEGER,
                available INTEGER
            );

            -- Índice de texto completo para título/cuerpo/tags
            CREATE VIRTUAL TABLE products_fts USING fts5(
                title, body, tags, content='products', content_rowid='id'
            );

            -- Depuración: por qué se descartó un producto
            CREATE TABLE debug_discard (
                product_id INTEGER,
                title TEXT,
                handle TEXT,
                reason TEXT
            );
            """
        )
        conn.commit()

        # --- Locations
        locations = self.client.list_locations()
        if locations:
            cur.executemany(
                "INSERT INTO locations(id, name) VALUES (?, ?)",
                [(loc["id"], loc["name"]) for loc in locations],
            )

        # --- Productos
        products = self.client.list_products()

        # Recolectar todos los inventory_item_id de las variantes
        all_inv_items: List[int] = []
        for p in products:
            for v in p.get("variants", []):
                if v.get("inventory_item_id"):
                    all_inv_items.append(v["inventory_item_id"])

        # --- Niveles de inventario (puede ser vacío)
        levels = (
            self.client.inventory_levels_for_items(all_inv_items) if all_inv_items else []
        )
        if levels:
            cur.executemany(
                "INSERT INTO inventory_levels(inventory_item_id, location_id, available) VALUES (?,?,?)",
                [
                    (
                        lv["inventory_item_id"],
                        lv["location_id"],
                        int(lv.get("available", 0)),
                    )
                    for lv in levels
                ],
            )

        def stock_for(inv_item_id: int) -> int:
            """Suma de stock disponible por inventory_item_id."""
            if not inv_item_id:
                return 0
            return sum(
                lv.get("available", 0) for lv in levels if lv["inventory_item_id"] == inv_item_id
            )

        # ---------- Reglas de aceptación + indexado ----------
        def product_check_and_reason(p: Dict[str, Any]) -> Tuple[bool, str]:
            if p.get("status") != "active":
                return False, "status!=active"

            # Imagen: principal o alguna imagen en galería o en variantes
            has_image = bool((p.get("image") or {}).get("src")) or bool(p.get("images"))
            if not has_image:
                has_variant_image = any((v.get("image_id") is not None) for v in p.get("variants", []))
                if not has_variant_image:
                    return False, "no_image"

            # Descripción mínima
            body = strip_html(p.get("body_html", "") or "")
            if len(body.strip()) < 10:
                return False, "no_body"

            # Al menos una variante con SKU, price y stock > 0
            ok_variant = False
            for v in p.get("variants", []):
                sku_ok = bool(v.get("sku"))
                price_ok = v.get("price") not in (None, "")
                inv_item = v.get("inventory_item_id")
                total = stock_for(inv_item) if inv_item else 0
                if sku_ok and price_ok and total > 0:
                    ok_variant = True
                    break
            if not ok_variant:
                return False, "no_variant_complete"

            return True, "ok"

        inserted = 0

        for p in products:
            is_ok, reason = product_check_and_reason(p)
            if not is_ok:
                # Guardar motivo de descarte
                cur.execute(
                    "INSERT INTO debug_discard(product_id, title, handle, reason) VALUES (?,?,?,?)",
                    (p.get("id"), p.get("title"), p.get("handle"), reason),
                )
                continue

            # Campos base
            body = strip_html(p.get("body_html", "") or "")
            # Imagen: toma principal si existe, de lo contrario primera de images
            image = (p.get("image") or {}).get("src") or (
                (p.get("images", [{}])[0].get("src")) if p.get("images") else None
            )

            cur.execute(
                "INSERT INTO products(id, title, handle, body, image, tags, vendor, product_type) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    p["id"],
                    p.get("title"),
                    p.get("handle"),
                    body,
                    image,
                    p.get("tags", ""),
                    p.get("vendor", ""),
                    p.get("product_type", ""),
                ),
            )
            inserted += 1

            # Variantes (guardamos todas, filtramos en búsqueda)
            v_rows = []
            for v in p.get("variants", []):
                v_rows.append(
                    (
                        v["id"],
                        p["id"],
                        v.get("sku", ""),
                        float(v["price"]) if v.get("price") not in (None, "") else None,
                        float(v["compare_at_price"])
                        if v.get("compare_at_price") not in (None, "")
                        else None,
                        v.get("inventory_item_id"),
                    )
                )
            if v_rows:
                cur.executemany(
                    "INSERT INTO variants(id, product_id, sku, price, compare_at_price, inventory_item_id) "
                    "VALUES (?,?,?,?,?,?)",
                    v_rows,
                )

            # Índice de texto
            cur.execute(
                "INSERT INTO products_fts(rowid, title, body, tags) VALUES (?,?,?,?)",
                (p["id"], p.get("title", ""), body, p.get("tags", "")),
            )

        conn.commit()
        print(f"[INDEX] Inserted products: {inserted}", flush=True)
        conn.close()

    # ----------------------------- Query helpers -----------------------------

    def _top_inventory_by_variant(self, conn: sqlite3.Connection, inventory_item_id: int) -> List[Dict[str, Any]]:
        """Retorna lista de {location, available} para un inventory_item_id."""
        q = """
        SELECT locations.name AS location, inventory_levels.available AS available
        FROM inventory_levels
        JOIN locations ON locations.id = inventory_levels.location_id
        WHERE inventory_levels.inventory_item_id = ? AND inventory_levels.available > 0
        ORDER BY locations.name ASC
        """
        return [dict(row) for row in conn.execute(q, (inventory_item_id,))]

    # ----------------------------- Search API -----------------------------

    def search(self, query: str, k: int = 6) -> List[Dict[str, Any]]:
        """Busca por FTS5 con fallback LIKE; arma tarjetas con la mejor variante en stock."""
        if not query:
            return []

        conn = self._conn_read()
        cur = conn.cursor()

        # Búsqueda en FTS
        fts_rows = list(
            cur.execute(
                "SELECT rowid FROM products_fts WHERE products_fts MATCH ? LIMIT ?",
                (query, k * 2),
            )
        )
        ids = [r[0] for r in fts_rows]

        # Fallback: LIKE cuando FTS devuelve poco
        if len(ids) < k:
            like = f"%{query}%"
            like_rows = list(
                cur.execute(
                    "SELECT id FROM products WHERE title LIKE ? OR body LIKE ? OR tags LIKE ? LIMIT ?",
                    (like, like, like, k * 2),
                )
            )
            ids += [r[0] for r in like_rows]

        # Únicos y top-k
        seen, ordered = set(), []
        for i in ids:
            if i not in seen:
                seen.add(i)
                ordered.append(i)
        ids = ordered[:k]

        results: List[Dict[str, Any]] = []

        for pid in ids:
            p = cur.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not p:
                continue

            variants = cur.execute(
                "SELECT * FROM variants WHERE product_id=?", (pid,)
            ).fetchall()

            # Filtrar variantes válidas para mostrar (con stock y precio y sku)
            v_infos = []
            for v in variants:
                inv = (
                    self._top_inventory_by_variant(conn, v["inventory_item_id"])
                    if v["inventory_item_id"]
                    else []
                )
                total_stock = sum(x["available"] for x in inv)
                if total_stock > 0 and v["price"] is not None and v["sku"]:
                    v_infos.append(
                        {
                            "variant_id": v["id"],
                            "sku": v["sku"],
                            "price": v["price"],
                            "compare_at_price": v["compare_at_price"],
                            "inventory": inv,
                        }
                    )
            if not v_infos:
                # No hay variantes mostrables -> saltamos producto
                continue

            # Ordenar variantes por stock total desc y tomar la mejor
            v_infos.sort(
                key=lambda x: sum(i["available"] for i in x["inventory"]), reverse=True
            )
            best = v_infos[0]

            product_url = f"{self.store_base_url}/products/{p['handle']}"
            buy_url = f"{self.store_base_url}/cart/{best['variant_id']}:1"

            results.append(
                {
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
                }
            )

        conn.close()
        return results

    # ----------------------------- Stats & samples -----------------------------

    def stats(self) -> Dict[str, int]:
        conn = self._conn()
        cur = conn.cursor()
        p = cur.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        v = cur.execute("SELECT COUNT(*) FROM variants").fetchone()[0]
        il = cur.execute("SELECT COUNT(*) FROM inventory_levels").fetchone()[0]
        conn.close()
        return {"products": p, "variants": v, "inventory_levels": il}

    def discard_stats(self) -> Dict[str, Any]:
        conn = self._conn()
        cur = conn.cursor()
        data = cur.execute(
            "SELECT reason, COUNT(*) AS c FROM debug_discard GROUP BY reason ORDER BY c DESC"
        ).fetchall()
        sample = cur.execute(
            "SELECT product_id, title, handle, reason FROM debug_discard LIMIT 20"
        ).fetchall()
        conn.close()
        return {
            "by_reason": [{"reason": r["reason"], "count": r["c"]} for r in data],
            "sample": [dict(x) for x in sample],
        }

    def sample_products(self, limit: int = 10) -> List[Dict[str, Any]]:
        conn = self._conn()
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id, title, handle FROM products LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ----------------------------- LLM context -----------------------------

    def mini_catalog_json(self, items: List[Dict[str, Any]]) -> str:
        """Devuelve un JSON pequeño con datos relevantes para el prompt del LLM."""
        import json

        safe = []
        for it in items:
            safe.append(
                {
                    "title": it["title"],
                    "short": (it.get("body") or "")[:400],
                    "tags": it.get("tags"),
                    "price": it["variant"]["price"],
                    "compare_at_price": it["variant"]["compare_at_price"],
                    "sku": it["variant"]["sku"],
                }
            )
        return json.dumps(safe, ensure_ascii=False, indent=2)
