# -*- coding: utf-8 -*-
"""
Cliente REST muy simple para Shopify.
- Usa paginación por since_id.
- Incluye 'image' (featured image) y 'images' (galería) para robustez.
"""

import os
import time
import requests

# Versión de API; puedes sobrescribirla con SHOPIFY_API_VERSION en Render.
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-10")

class ShopifyClient:
    def __init__(self, token: str = None, store_domain: str = None):
        self.token = token or os.getenv("SHOPIFY_TOKEN")
        self.store_domain = store_domain or os.getenv("SHOPIFY_STORE_DOMAIN")
        if not self.token or not self.store_domain:
            raise RuntimeError("Configura SHOPIFY_TOKEN y SHOPIFY_STORE_DOMAIN")

        self.base = f"https://{self.store_domain}/admin/api/{API_VERSION}"
        self.headers = {
            "X-Shopify-Access-Token": self.token,
            "Accept": "application/json",
        }

    def _get(self, path, params=None):
        """GET con un reintento simple si devuelve 429."""
        url = f"{self.base}{path}"
        r = requests.get(url, headers=self.headers, params=params, timeout=40)
        if r.status_code == 429:
            time.sleep(1.2)
            r = requests.get(url, headers=self.headers, params=params, timeout=40)
        r.raise_for_status()
        return r.json()

    def list_locations(self):
        data = self._get("/locations.json")
        return data.get("locations", [])

    def list_products(self):
        """
        Descarga productos en lotes usando since_id.
        IMPORTANTE: pedimos 'image' y 'images'.
        """
        products = []
        limit = 250
        since_id = 0

        # Pedimos sólo los campos que necesitamos para indexar
        fields = (
            "id,title,handle,body_html,"
            "image,images,"              # <-- featured y galería
            "variants,tags,vendor,status,product_type"
        )

        while True:
            params = {
                "limit": limit,
                "fields": fields,
            }
            if since_id:
                params["since_id"] = since_id

            data = self._get("/products.json", params=params)
            batch = data.get("products", [])
            if not batch:
                break

            products.extend(batch)
            since_id = batch[-1]["id"]
            if len(batch) < limit:
                break

        return products

    def inventory_levels_for_items(self, inventory_item_ids):
        """Obtiene inventario por inventory_item_id (máx. 50 por llamada)."""
        levels = []
        chunk = 50
        for i in range(0, len(inventory_item_ids), chunk):
            ids = inventory_item_ids[i : i + chunk]
            params = {"inventory_item_ids": ",".join(map(str, ids))}
            data = self._get("/inventory_levels.json", params=params)
            levels.extend(data.get("inventory_levels", []))
        return levels
