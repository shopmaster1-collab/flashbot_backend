# -*- coding: utf-8 -*-
"""
ShopifyClient: cliente REST mínimo con paginación (page_info) y compatibilidad de variables.

Variables aceptadas (cualquiera de los alias):
- Dominio:
    SHOPIFY_STORE_DOMAIN   | SHOPIFY_SHOP
- Token:
    SHOPIFY_TOKEN          | SHOPIFY_ACCESS_TOKEN
- Versión API (opcional, default 2024-10):
    SHOPIFY_API_VERSION
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs
import requests


class ShopifyClient:
    def __init__(self):
        # ---- dominios/alias aceptados ----
        store_domain = (
            os.getenv("SHOPIFY_STORE_DOMAIN")
            or os.getenv("SHOPIFY_SHOP")
            or ""
        ).strip()
        token = (
            os.getenv("SHOPIFY_TOKEN")
            or os.getenv("SHOPIFY_ACCESS_TOKEN")
            or ""
        ).strip()
        api_ver = (os.getenv("SHOPIFY_API_VERSION") or "2024-10").strip()

        if store_domain.startswith("https://") or store_domain.startswith("http://"):
            store_domain = urlparse(store_domain).netloc

        if not store_domain or not token:
            raise RuntimeError(
                "Configura SHOPIFY_TOKEN/SHOPIFY_ACCESS_TOKEN y "
                "SHOPIFY_STORE_DOMAIN/SHOPIFY_SHOP"
            )

        self.store_domain = store_domain
        self.api_ver = api_ver
        self.base = f"https://{self.store_domain}/admin/api/{self.api_ver}"
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # ------------- helpers internos -------------

    def _get(self, path: str, params: Dict[str, Any]) -> requests.Response:
        url = f"{self.base}{path}"
        # manejo simple de rate limits
        for attempt in range(4):
            r = self.session.get(url, params=params, timeout=40)
            if r.status_code == 429:
                time.sleep(1.0 + attempt)
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()
        return r  # nunca llega

    @staticmethod
    def _next_page_info(resp: requests.Response) -> Optional[str]:
        """
        Lee el header Link para extraer el cursor page_info de la 'next' page.
        Formato:
          <https://...page_info=AAA>; rel="previous", <https://...page_info=BBB>; rel="next"
        """
        link = resp.headers.get("Link") or resp.headers.get("link") or ""
        for part in link.split(","):
            if 'rel="next"' in part:
                start = part.find("<")
                end = part.find(">")
                if start >= 0 and end > start:
                    url = part[start + 1:end]
                    qs = parse_qs(urlparse(url).query)
                    return (qs.get("page_info") or [None])[0]
        return None

    # ------------- API públicas usadas por el indexer -------------

    def list_products(self, status: Optional[str] = None, limit: int = 250,
                      page_info: Optional[str] = None) -> Dict[str, Any]:
        """
        Devuelve un dict {'products': [...], 'next_page_info': '...'} para facilitar la paginación
        desde el indexador actual.
        """
        params: Dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if page_info:
            params["page_info"] = page_info

        r = self._get("/products.json", params=params)
        data = r.json() or {}
        products = data.get("products") or []
        next_pi = self._next_page_info(r)

        return {"products": products, "next_page_info": next_pi}

    def list_locations(self) -> List[Dict[str, Any]]:
        r = self._get("/locations.json", params={})
        return (r.json() or {}).get("locations") or []

    def inventory_levels_for_items(self, item_ids: List[int]) -> List[Dict[str, Any]]:
        """
        Llama /inventory_levels.json en lotes para evitar URIs largas.
        """
        out: List[Dict[str, Any]] = []
        CHUNK = 50
        for i in range(0, len(item_ids), CHUNK):
            chunk = item_ids[i:i + CHUNK]
            if not chunk:
                continue
            params = {"inventory_item_ids": ",".join(str(x) for x in chunk), "limit": 250}
            r = self._get("/inventory_levels.json", params=params)
            out.extend((r.json() or {}).get("inventory_levels") or [])
        return out
