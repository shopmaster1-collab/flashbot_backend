# -*- coding: utf-8 -*-
ORDER BY locations.name ASC
"""
return [dict(row) for row in conn.execute(q, (inventory_item_id,))]


def search(self, query: str, k: int = 6) -> List[Dict[str, Any]]:
conn = self._conn()
cur = conn.cursor()
# 1) Búsqueda FTS5
fts = list(cur.execute(
"SELECT rowid FROM products_fts WHERE products_fts MATCH ? LIMIT ?", (query, k*2)
))
ids = [r[0] for r in fts]
# 2) Fallback: LIKE por si el FTS no trae suficiente
if len(ids) < k:
like = list(cur.execute(
"SELECT id FROM products WHERE title LIKE ? OR body LIKE ? LIMIT ?",
(f"%{query}%", f"%{query}%", k*2)
))
ids += [r[0] for r in like]
# uniq pero preservando orden
seen = set(); ordered = []
for i in ids:
if i not in seen:
seen.add(i); ordered.append(i)
ids = ordered[:k]
results = []
for pid in ids:
p = cur.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
if not p: continue
# variantes con stock > 0 (sumado)
variants = cur.execute("SELECT * FROM variants WHERE product_id=?", (pid,)).fetchall()
v_infos = []
for v in variants:
inv = self._top_inventory_by_variant(conn, v["inventory_item_id"]) if v["inventory_item_id"] else []
total_stock = sum(x["available"] for x in inv)
if total_stock > 0 and v["price"] is not None and v["sku"]:
v_infos.append({
"variant_id": v["id"],
"sku": v["sku"],
"price": v["price"],
"compare_at_price": v["compare_at_price"],
"inventory": inv,
})
if not v_infos:
continue
# elegir variante preferida: mayor stock
v_infos.sort(key=lambda x: sum(i["available"] for i in x["inventory"]), reverse=True)
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


def mini_catalog_json(self, items: List[Dict[str,Any]]) -> str:
import json
# Reducido para mandar al modelo sin credenciales ni urls privadas
safe = []
for it in items:
safe.append({
"title": it["title"],
"short": it["body"][:400],
"tags": it["tags"],
"price": it["variant"]["price"],
"compare_at_price": it["variant"]["compare_at_price"],
"sku": it["variant"]["sku"],
})
return json.dumps(safe, ensure_ascii=False, indent=2)