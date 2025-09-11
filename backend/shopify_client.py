# -*- coding: utf-8 -*-
"""
Cliente REST mínimo para Shopify (Admin API).
- Maneja versión vía env SHOPIFY_API_VERSION (por defecto 2024-10).
- Paginación con since_id para /products.json
- Reintento simple en 429 (rate limit).
- Logs útiles cuando la API responde != 2xx.
- Incluye método 'probe()' para diagnóstico rápido desde /api/admin/diag.
"""

import os
import time
import requests

# Usa una versión estable. Si tu tienda soporta otra, cámbiala con SHOPIFY_API_VERSION en Render.
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

    # ----------------------- HTTP helpers -----------------------

    def _get(self, path: str, params: dict | None = None) -> dict:
        """GET con reintento simple y logging de errores (cuerpo incluido)."""
        url = f"{self.base}{path}"
        r = requests.get(url, headers=self.headers, params=params, timeout=40)

        # Reintento básico en 429 (rate limit)
        if r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", "1.2"))
            time.sleep(max(retry_after, 1.2))
            r = requests.get(url, headers=self.headers, params=params, timeout=40)

        if not r.ok:
            # Log CLI útil (Render logs) para saber por qué falla la llamada.
            body_preview = r.text[:800] if isinstance(r.text, str) else str(r.text)
            print(
                f"[SHOPIFY][GET] {r.status_code} {url} params={params} body={body_preview}",
                flush=True,
            )
        r.raise_for_status()
        return r.json()

    # ----------------------- Recursos usados -----------------------

    def list_locations(self) -> list[dict]:
        data = self._get("/locations.json")
        return data.get("locations", [])

    def list_products(self) -> list[dict]:
        """
        Paginación con since_id (válida en REST).
        Trae campos necesarios para indexación.
        """
        products: list[dict] = []
        limit = 250
        since_id = 0

        # IMPORTANTE: incluimos 'image' (singular) además de 'images'
        # porque muchas tiendas usan sólo el campo principal.
        fields = (
            "id,title,handle,body_html,images,image,variants,"
            "tags,vendor,status,product_type"
        )

        while True:
            params = {"limit": limit, "fields": fields}
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

    def inventory_levels_for_items(self, inventory_item_ids: list[int]) -> list[dict]:
        """Shopify limita 50 ids por request."""
        levels: list[dict] = []
        if not inventory_item_ids:
            return levels

        chunk = 50
        for i in range(0, len(inventory_item_ids), chunk):
            ids = inventory_item_ids[i : i + chunk]
            params = {"inventory_item_ids": ",".join(map(str, ids))}
            data = self._get("/inventory_levels.json", params=params)
            levels.extend(data.get("inventory_levels", []))
        return levels

    # ----------------------- Diagnóstico -----------------------

    def probe(self) -> dict:
        """
        Hace una llamada mínima para comprobar credenciales y versión.
        Devuelve muestra de productos y conteo de ubicaciones.
        """
        try:
            sample = self._get(
                "/products.json",
                params={"limit": 3, "fields": "id,title,status"},
            )
            locs = self.list_locations()
            return {
                "ok": True,
                "sample_products": [
                    {"id": p["id"], "title": p.get("title"), "status": p.get("status")}
                    for p in sample.get("products", [])
                ],
                "locations": len(locs),
                "api_version": API_VERSION,
                "store_domain": self.store_domain,
            }
        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
                "api_version": API_VERSION,
                "store_domain": self.store_domain,
            }
