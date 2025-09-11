# -*- coding: utf-8 -*-
import os
import time
import requests


API_VERSION = "2024-04" # estable


class ShopifyClient:
def __init__(self, token: str = None, store_domain: str = None):
self.token = token or os.getenv("SHOPIFY_TOKEN")
self.store_domain = store_domain or os.getenv("SHOPIFY_STORE_DOMAIN")
if not self.token or not self.store_domain:
raise RuntimeError("Configura SHOPIFY_TOKEN y SHOPIFY_STORE_DOMAIN")
self.base = f"https://{self.store_domain}/admin/api/{API_VERSION}"
self.headers = {"X-Shopify-Access-Token": self.token}


# --- util paginado simple ---
def _get(self, path, params=None):
url = f"{self.base}{path}"
r = requests.get(url, headers=self.headers, params=params, timeout=40)
if r.status_code == 429:
time.sleep(1)
r = requests.get(url, headers=self.headers, params=params, timeout=40)
r.raise_for_status()
return r.json()


def list_locations(self):
data = self._get("/locations.json")
return data.get("locations", [])


def list_products(self):
# Trae todos los productos con campos necesarios
products = []
page = 1
limit = 250
while True:
params = {
"limit": limit,
"page_info": None,
"fields": "id,title,handle,body_html,images,variants,tags,vendor,status,published_scope,product_type"
}
data = self._get(f"/products.json", params={"limit": limit, "page": page})
batch = data.get("products", [])
products.extend(batch)
if len(batch) < limit:
break
page += 1
return products


def inventory_levels_for_items(self, inventory_item_ids):
# Shopify limita 50 ids por request
levels = []
chunk = 50
for i in range(0, len(inventory_item_ids), chunk):
ids = inventory_item_ids[i:i+chunk]
params = {"inventory_item_ids": ",".join(map(str, ids))}
data = self._get("/inventory_levels.json", params=params)
levels.extend(data.get("inventory_levels", []))
return levels