def build(self) -> None:
    """Descarga catálogo y levanta índice en SQLite. (esquema primero)"""
    # --- 0) preparar DB y esquema ANTES de tocar Shopify ---
    if os.path.exists(self.db_path):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    conn = self._conn_rw()
    cur = conn.cursor()
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

    # --- 1) fetch Shopify (robusto, con logs) ---
    try:
        locations = self.client.list_locations()
    except Exception:
        locations = []
    self._location_map = {int(x["id"]): (x.get("name") or str(x["id"])) for x in locations}

    # productos activos (todas las páginas; respeta FORCE_REST si lo usas)
    try:
        products = self._fetch_all_active(limit=250)  # o tu ruta que ya usa FORCE_REST
    except Exception as e:
        print(f"[INDEX] ERROR list_products: {e}", flush=True)
        products = []

    print(f"[INDEX] fetched: locations={len(locations)} products={len(products)}", flush=True)

    # recoger todos los inventory_item_id
    all_inv_ids: List[int] = []
    for p in products:
        for v in p.get("variants") or []:
            if v.get("inventory_item_id"):
                try:
                    all_inv_ids.append(int(v["inventory_item_id"]))
                except Exception:
                    pass

    try:
        levels = self.client.inventory_levels_for_items(all_inv_ids) if all_inv_ids else []
    except Exception as e:
        print(f"[INDEX] ERROR inventory_levels: {e}", flush=True)
        levels = []

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

    # --- 2) volcado a SQLite ---
    ins_p = "INSERT INTO products (id, handle, title, body, tags, vendor, product_type, image) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    ins_v = "INSERT INTO variants (id, product_id, sku, price, compare_at_price, inventory_item_id) VALUES (?, ?, ?, ?, ?, ?)"
    ins_inv = "INSERT INTO inventory (variant_id, location_id, location_name, available) VALUES (?, ?, ?, ?)"

    discards_sample: List[Dict[str, Any]] = []
    discards_count: Dict[str, int] = {}
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
        hero_img = self._extract_hero_image(p)

        cur.execute(ins_p, (
            int(p["id"]), p.get("handle"), p.get("title"), body_text, tags,
            p.get("vendor"), p.get("product_type"), hero_img,
        ))
        n_products += 1

        for v in valids:
            cur.execute(ins_v, (
                int(v["id"]), int(p["id"]), v.get("sku"), v.get("price"),
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

    print(f"[INDEX] done: products={n_products} variants={n_variants} inventory_levels={self._stats['inventory_levels']}", flush=True)
