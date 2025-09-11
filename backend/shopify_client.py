# -*- coding: utf-8 -*-
import os
import time
import requests

# Usa una versión estable reciente. Si tu tienda aún no tiene esta disponible, puedes bajar a 2024-10.
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-01")

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

    # --- GET con reintento simple para 429 ---
    def _get(self, path, params=None):
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
        Paginación con since_id (válida en REST).
        Trae campos necesarios para indexación.
        """
        products = []
        limit = 250
        since_id = 0
        fields = (
            "id,title,handle,body_html,images,variants,"
            "tags,vendor,status,product_type"
        )
        while True:
            params = {
                "limit": limit,
                "fields": fields,
                "since_id": since_id or None,  # omitido si 0
                # Opcional: "status": "active",
            }
            # Elimina clave None para evitar ?since_id=None en la URL
            params = {k: v for k, v in params.items() if v is not None}

            data = self._get("/products.json", params=params)
            batch = data.get("products", [])
            if not batch:
                break

            products.extend(batch)
            # Avanza since_id al último id del batch
            since_id = batch[-1]["id"]
            if len(batch) < limit:
                break

        return products

    def inventory_levels_for_items(self, inventory_item_ids):
        # Shopify limita 50 ids por request
        levels = []
        chunk = 50
        for i in range(0, len(inventory_item_ids), chunk):
            ids = inventory_item_ids[i : i + chunk]
            params = {"inventory_item_ids": ",".join(map(str, ids))}
            data = self._get("/inventory_levels.json", params=params)
            levels.extend(data.get("inventory_levels", []))
        return levels
