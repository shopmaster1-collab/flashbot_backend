# -*- coding: utf-8 -*-
"""
Crea un índice ligero del catálogo para búsquedas rápidas.
Aplica filtros configurables vía variables de entorno y
genera tarjetas listas para el widget.
"""

import os
import json
from collections import defaultdict
from rapidfuzz import fuzz, process
from .utils import strip_html

# Filtros (se configuran en Render → Environment)
REQUIRE_ACTIVE = os.getenv("REQUIRE_ACTIVE", "0") == "1"
REQUIRE_IMAGE  = os.getenv("REQUIRE_IMAGE",  "0") == "1"
REQUIRE_SKU    = os.getenv("REQUIRE_SKU",    "0") == "1"
REQUIRE_STOCK  = os.getenv("REQUIRE_STOCK",  "0") == "1"
MIN_BODY_CHARS = int(os.getenv("MIN_BODY_CHARS", "0"))

class CatalogIndexer:
    def __init__(self, shop_client, store_base_url: str):
        self.shop = shop_client
        self.store_base = store_base_url.rstrip("/")
        self.products = []          # lista de dicts para el chat
        self._norm_titles = []      # títulos normalizados para fuzzy search
        self._id_to_item = {}       # id producto -> item
        self._discard_reasons = []  # [(product_id, handle, reason, title)]
        self._inventory_cache = {}  # inventory_item_id -> [{location_id, available}, ...]

    # ------------------------ utilería interna ------------------------

    def _fetch_inventory_for_variants(self, variants):
        ids = [v.get("inventory_item_id") for v in variants if v.get("inventory_item_id")]
        ids = list({i for i in ids if i})
        if not ids:
            return {}
        levels = self.shop.inventory_levels_for_items(ids)
        m = defaultdict(list)
        for lv in levels:
            m[lv["inventory_item_id"]].append({
                "location_id": lv["location_id"],
                "available": lv.get("available") or 0,
            })
        return m

    def _variant_pick(self, variants):
        """
        Regla simple: el primer variant “publicable”.
        """
        if not variants:
            return None
        for v in variants:
            return v
        return variants[0]

    def _accepts(self, p, hero_img, variant, inventory_sum):
        """
        Aplica todas las reglas de filtrado y devuelve (ok, reason_str|None).
        """
        # Estado
        if REQUIRE_ACTIVE and (p.get("status") != "active"):
            return False, "status!=active"

        # Imagen: ahora validamos también hero_img que puede venir de p['image']
        if REQUIRE_IMAGE and not hero_img:
            return False, "no_image"

        # SKU
        if REQUIRE_SKU and not (variant.get("sku") or "").strip():
            return False, "no_sku"

        # Stock
        if REQUIRE_STOCK and inventory_sum <= 0:
            return False, "no_stock"

        # Cuerpo mínimo
        body_txt = strip_html(p.get("body_html") or "")
        if MIN_BODY_CHARS > 0 and len(body_txt) < MIN_BODY_CHARS:
            return False, "short_body"

        return True, None

    # ------------------------ API pública ------------------------

    def build(self):
        """Descarga productos, aplica filtros y produce el índice en memoria."""
        self.products.clear()
        self._norm_titles.clear()
        self._id_to_item.clear()
        self._discard_reasons.clear()
        self._inventory_cache.clear()

        raw_products = self.shop.list_products()

        for p in raw_products:
            # IMAGEN PRINCIPAL (robusto):
            # 1) featured image: product.image.src
            # 2) primera de la galería: product.images[0].src
            hero_img = None
            img_obj = p.get("image") or {}
            if img_obj.get("src"):
                hero_img = img_obj["src"]
            elif p.get("images"):
                first = p["images"][0] or {}
                hero_img = first.get("src")

            # VARIANT & INVENTARIO
            variants = p.get("variants") or []
            v = self._variant_pick(variants)
            if not v:
                self._discard_reasons.append((p.get("id"), p.get("handle"), "no_variant_complete", p.get("title")))
                continue

            inv_map = self._fetch_inventory_for_variants(variants)
            inv_list = inv_map.get(v.get("inventory_item_id"), [])
            inventory_sum = sum(x.get("available") or 0 for x in inv_list)

            ok, reason = self._accepts(p, hero_img, v, inventory_sum)
            if not ok:
                self._discard_reasons.append((p.get("id"), p.get("handle"), reason, p.get("title")))
                continue

            handle = p.get("handle")
            product_url = f"{self.store_base}/products/{handle}" if handle else self.store_base
            buy_url = product_url  # podrías cambiarlo a /cart/add si quieres “Comprar ahora”

            item = {
                "id": p.get("id"),
                "title": p.get("title") or "",
                "handle": handle,
                "body": strip_html(p.get("body_html") or ""),
                "image": hero_img,
                "product_url": product_url,
                "buy_url": buy_url,
                "vendor": p.get("vendor"),
                "product_type": p.get("product_type"),
                "tags": p.get("tags") or "",
                "status": p.get("status"),
                "variant": {
                    "id": v.get("id"),
                    "sku": (v.get("sku") or "").strip(),
                    "price": v.get("price"),
                    "compare_at_price": v.get("compare_at_price"),
                    "inventory": inv_list,  # [{location_id, available}]
                },
            }

            self.products.append(item)
            self._id_to_item[item["id"]] = item
            self._norm_titles.append(item["title"].lower())

    # ------------------------ consulta & utilidades ------------------------

    def stats(self):
        """Estadísticas rápidas para /api/admin/stats."""
        return {
            "products": len(self.products),
            "variants": len(self.products),  # 1 variant elegido por producto
            "inventory_levels": sum(len(p["variant"]["inventory"]) for p in self.products),
        }

    def discard_stats(self):
        """Resumen de descartes para /api/admin/discards."""
        by_reason = defaultdict(int)
        sample = []
        for pid, handle, reason, title in self._discard_reasons:
            by_reason[reason] += 1
        # muestreamos algunos para inspección
        for it in self._discard_reasons[:20]:
            pid, handle, reason, title = it
            sample.append({
                "product_id": pid,
                "handle": handle,
                "reason": reason,
                "title": title,
            })
        # ordenado
        res = [{"reason": r, "count": c} for r, c in sorted(by_reason.items(), key=lambda x: -x[1])]
        return {"by_reason": res, "sample": sample}

    def sample_products(self, limit=10):
        """Devuelve algunos productos del índice (para ver cómo quedaron)."""
        return self.products[: int(limit)]

    def mini_catalog_json(self, items):
        """Convierte items seleccionados a un JSON pequeño para el prompt del LLM."""
        out = []
        for it in items:
            v = it["variant"]
            out.append({
                "title": it["title"],
                "sku": v.get("sku"),
                "price": v.get("price"),
                "compare_at_price": v.get("compare_at_price"),
                "url": it["product_url"],
                "image": it["image"],
                "stock_total": sum(x["available"] for x in v["inventory"]),
            })
        return json.dumps(out, ensure_ascii=False)

    def search(self, query: str, k=5):
        """Búsqueda fuzzy por título (se puede enriquecer con más señales)."""
        if not query:
            return []
        q = query.lower().strip()

        # coincidencias por título
        res = process.extract(q, self._norm_titles, scorer=fuzz.WRatio, limit=max(k * 2, 8))
        # mapea a items
        picked = []
        seen = set()
        for score, idx, _ in [(r[1], r[2], r[0]) if isinstance(r, tuple) and len(r) == 3 else (r[1], r[2], r[0]) for r in res]:
            # r con forma (title_str, score, idx) depende de rapidfuzz; garantizamos idx/score
            title_idx = idx
            if 0 <= title_idx < len(self.products):
                item = self.products[title_idx]
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                picked.append(item)
                if len(picked) >= k:
                    break
        return picked
