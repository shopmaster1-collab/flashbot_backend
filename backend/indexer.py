# -*- coding: utf-8 -*-
"""
Indexer Shopify -> SQLite (+FTS5) para el buscador del bot.

Reglas:
- REQUIRE_ACTIVE=1  -> exige product.status == "active" (único filtro)

Variables de entorno relevantes:
- SHOPIFY_SHOP               | SHOPIFY_STORE_DOMAIN
- SHOPIFY_ACCESS_TOKEN       | SHOPIFY_TOKEN
- SHOPIFY_API_VERSION        (default 2024-10)
- SQLITE_PATH                (p.ej. /data/catalog.db)
- FORCE_REST=1               (opcional; fuerza camino REST paginado)
- STORE_BASE_URL             (default https://master.com.mx)
- TAXONOMY_CSV_PATH          (opcional; CSV de taxonomía/colecciones/sinónimos)
"""

from __future__ import annotations

import os
import re
import csv
import json
import time
import sqlite3
import unicodedata
from typing import Any, Dict, List, Tuple, Optional
from urllib.parse import urlparse, parse_qs

import requests

from .utils import strip_html

# ---------- Paths ----------
BASE_DIR = os.path.dirname(__file__)
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")

SQLITE_PATH_ENV = (os.getenv("SQLITE_PATH") or "").strip()
if SQLITE_PATH_ENV:
    DATA_DIR = os.path.dirname(SQLITE_PATH_ENV)
    DB_PATH = SQLITE_PATH_ENV
else:
    DATA_DIR = DEFAULT_DATA_DIR
    DB_PATH = os.path.join(DATA_DIR, "catalog.sqlite3")

os.makedirs(DATA_DIR, exist_ok=True)


def _row_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


def _norm(s: str) -> str:
    """minúsculas + sin acentos (para comparaciones robustas)."""
    if not s:
        return ""
    s = s.lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return s


# ---------- REST nativo (paginación con page_info) ----------
class ShopifyREST:
    def __init__(self):
        store = (
            os.getenv("SHOPIFY_STORE_DOMAIN")
            or os.getenv("SHOPIFY_SHOP")
            or ""
        ).strip()
        if store.startswith("http"):
            store = urlparse(store).netloc

        token = (
            os.getenv("SHOPIFY_ACCESS_TOKEN")
            or os.getenv("SHOPIFY_TOKEN")
            or ""
        ).strip()
        api_ver = (os.getenv("SHOPIFY_API_VERSION") or "2024-10").strip()

        if not store or not token:
            raise RuntimeError("Faltan SHOPIFY_SHOP|STORE_DOMAIN o SHOPIFY_ACCESS_TOKEN|TOKEN")

        self.shop = store
        self.api_ver = api_ver
        self.base = f"https://{store}/admin/api/{api_ver}"
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    @staticmethod
    def _next_page_info(resp: requests.Response) -> Optional[str]:
        link = resp.headers.get("Link") or resp.headers.get("link") or ""
        for part in link.split(","):
            if 'rel="next"' in part:
                s = part.find("<")
                e = part.find(">")
                if s >= 0 and e > s:
                    url = part[s + 1:e]
                    qs = parse_qs(urlparse(url).query)
                    return (qs.get("page_info") or [None])[0]
        return None

    def _get(self, path: str, params: Dict[str, Any]) -> requests.Response:
        url = f"{self.base}{path}"
        for attempt in range(4):
            r = self.session.get(url, params=params, timeout=40)
            if r.status_code == 429:
                time.sleep(1.0 + attempt)
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()
        return r

    # Trae TODAS las páginas de /products.json SIN 'status' en el request.
    def list_products_all(self, limit: int = 250) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page: Optional[str] = None
        while True:
            params = {"limit": limit}
            if page:
                params["page_info"] = page
            r = self._get("/products.json", params)
            data = r.json() or {}
            items = data.get("products") or []
            out.extend(items)
            page = self._next_page_info(r)
            if not page or not items:
                break
        return out

    def list_locations(self) -> List[Dict[str, Any]]:
        r = self._get("/locations.json", {})
        return (r.json() or {}).get("locations") or []

    def inventory_levels_for_items(self, item_ids: List[int]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        CHUNK = 50
        for i in range(0, len(item_ids), CHUNK):
            chunk = item_ids[i:i + CHUNK]
            if not chunk:
                continue
            params = {"inventory_item_ids": ",".join(str(x) for x in chunk), "limit": 250}
            r = self._get("/inventory_levels.json", params)
            out.extend((r.json() or {}).get("inventory_levels") or [])
        return out


class CatalogIndexer:
    def __init__(self, shop_client, store_base_url: str):
        """
        shop_client: instancia de ShopifyClient (puede ser None).
        Si FORCE_REST=1 o faltan métodos en el cliente, se usa ShopifyREST.
        """
        self.client = shop_client
        self.store_base_url = (store_base_url or "").rstrip("/") or "https://master.com.mx"

        # ÚNICA REGLA
        self.rules = {"REQUIRE_ACTIVE": os.getenv("REQUIRE_ACTIVE", "1") == "1"}

        self.db_path = DB_PATH
        self._fts_enabled = False

        self._stats: Dict[str, int] = {"products": 0, "variants": 0, "inventory_levels": 0}
        self._discards_sample: List[Dict[str, Any]] = []
        self._discards_count: Dict[str, int] = {}

        self._location_map: Dict[int, str] = {}
        self._inventory_map: Dict[int, List[Dict]] = {}

        # ----- Taxonomía -----
        self._taxonomy_rows: int = 0
        self._taxonomy_terms: List[str] = []
        self._taxonomy_keywords: List[str] = []  # lista plana de palabras clave/sinónimos
        self._taxonomy_pairs: List[Tuple[str, str]] = []  # (categoria/subcategoria, termino)
        self._taxonomy_loaded: bool = False

        # REST fallback (si hay credenciales)
        self._rest_fallback: Optional[ShopifyREST] = None
        try:
            self._rest_fallback = ShopifyREST()
        except Exception:
            self._rest_fallback = None

        # Carga de taxonomía (si existe)
        self._load_taxonomy()

    # ---------- conexiones ----------
    def _conn_rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = _row_factory
        return conn

    def _conn_read(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = _row_factory
        return conn

    # ---------- util imágenes ----------
    @staticmethod
    def _img_src(img: Optional[Dict[str, Any]]) -> Optional[str]:
        if not img:
            return None
        src = (img.get("src") or img.get("url") or "").strip()
        return src or None

    def _extract_hero_image(self, p: Dict[str, Any]) -> Optional[str]:
        s = self._img_src(p.get("image"))
        if s:
            return s
        for i in (p.get("images") or []):
            s = self._img_src(i)
            if s:
                return s
        images_by_id = {str(i.get("id")): i for i in (p.get("images") or [])}
        for v in (p.get("variants") or []):
            iid = str(v.get("image_id") or "")
            if iid:
                s = self._img_src(images_by_id.get(iid))
                if s:
                    return s
        return None

    # ---------- Taxonomy helpers ----------
    def _load_taxonomy(self) -> None:
        """Lee TAXONOMY_CSV_PATH si existe y compila listas de términos/sinónimos."""
        path = (os.getenv("TAXONOMY_CSV_PATH") or "").strip()
        if not path or not os.path.exists(path):
            # nada que hacer; operamos normal
            self._taxonomy_loaded = False
            return

        rows = 0
        terms: List[str] = []
        pairs: List[Tuple[str, str]] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rd = csv.DictReader(fh)
                for r in rd:
                    rows += 1
                    cat = (r.get("categoria_principal") or "").strip()
                    sub = (r.get("subcategoria") or "").strip()
                    label = (sub or cat).strip() or "general"
                    # palabras clave/sinónimos -> coma separada
                    keys = (r.get("palabras_clave_sinonimos") or "").strip()
                    pool: List[str] = []
                    if keys:
                        pool.extend([x.strip() for x in keys.split(",") if x.strip()])
                    # también usamos partes de la URL (slug)
                    url = (r.get("url") or "").strip().lower()
                    if "/collections/" in url:
                        slug = url.split("/collections/", 1)[-1]
                        for piece in re.split(r"[-_/]", slug):
                            if piece and len(piece) > 2 and piece.isascii():
                                pool.append(piece.replace("%20", " ").strip())
                    # label como término
                    if label and len(label) > 1:
                        pool.append(label)

                    # limpiar y normalizar
                    pool_n = sorted(set(_norm(x) for x in pool if x))
                    for t in pool_n:
                        if t:
                            terms.append(t)
                            pairs.append((label, t))
        except Exception:
            # si hay problema de lectura, seguimos sin taxonomía
            self._taxonomy_loaded = False
            return

        terms = sorted(set(terms))
        self._taxonomy_rows = rows
        self._taxonomy_terms = terms
        self._taxonomy_keywords = terms[:]  # por ahora lista plana
        self._taxonomy_pairs = pairs
        self._taxonomy_loaded = True

    def taxonomy_meta(self) -> Dict[str, Any]:
        """Datos de diagnóstico para /api/admin/taxonomy (opcional en app)."""
        return {
            "ok": True,
            "rows": self._taxonomy_rows,
            "terms": self._taxonomy_terms[:200],  # trunc para seguridad
            "loaded": self._taxonomy_loaded,
        }

    # ---------- reglas ----------
    def _passes_product_rules(self, p: Dict[str, Any]) -> Tuple[bool, str]:
        if self.rules["REQUIRE_ACTIVE"] and p.get("status") != "active":
            return False, "status!=active"
        return True, ""

    def _select_valid_variants(self, variants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for v in variants or []:
            price = v.get("price")
            try:
                price_f = float(price) if price is not None else None
            except Exception:
                price_f = None
            if price_f is None:
                continue

            inv_item_id = v.get("inventory_item_id")
            inv_levels = self._inventory_map.get(int(inv_item_id), []) if inv_item_id else []

            cap = v.get("compare_at_price")
            try:
                cap_f = float(cap) if cap is not None else None
            except Exception:
                cap_f = None

            out.append({
                "id": int(v["id"]),
                "sku": (v.get("sku") or None),
                "price": price_f,
                "compare_at_price": cap_f,
                "inventory_item_id": int(inv_item_id) if inv_item_id else None,
                "inventory": [
                    {"location_id": int(lv["location_id"]), "available": int(lv.get("available") or 0)}
                    for lv in inv_levels
                ],
            })
        return out

    # ---------- fetch productos ----------
    def _fetch_all_active(self, limit: int = 250) -> List[Dict[str, Any]]:
        """Devuelve productos ACTIVOS. Usa REST paginado sin status en el request y filtra en Python."""
        force_rest = os.getenv("FORCE_REST", "0") == "1"

        # Preferimos REST con paginación robusta
        if force_rest and self._rest_fallback:
            all_items = self._rest_fallback.list_products_all(limit=limit)
            return [p for p in all_items if (p.get("status") == "active")]

        # Intento con cliente inyectado (si tiene paginación propia)
        try:
            if hasattr(self.client, "list_products"):
                acc: List[Dict[str, Any]] = []
                page = None
                while True:
                    # sin status en el request; filtramos después
                    resp = self.client.list_products(limit=limit, page_info=page)
                    if isinstance(resp, dict):
                        items = (resp.get("products") or resp.get("items") or []) or []
                        acc.extend(items)
                        page = resp.get("next_page_info")
                        if not page or not items:
                            break
                    else:
                        if isinstance(resp, list):
                            acc = list(resp)
                        break
                if acc:
                    return [p for p in acc if (p.get("status") == "active")]
        except Exception:
            pass

        # Fallback final a REST
        if self._rest_fallback:
            all_items = self._rest_fallback.list_products_all(limit=limit)
            return [p for p in all_items if (p.get("status") == "active")]
        return []

    # ---------- build ----------
    def build(self) -> None:
        """Crea esquema primero y luego llena datos (robusto)."""
        # reset archivo
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

        conn = self._conn_rw()
        cur = conn.cursor()

        # esquema
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

        # FTS ampliado (incluye handle, vendor, product_type) — permite "leer" más señales
        try:
            cur.execute("""
                CREATE VIRTUAL TABLE products_fts
                USING fts5(
                    title, body, tags, handle, vendor, product_type,
                    content='products', content_rowid='id'
                )
            """)
            self._fts_enabled = True
        except sqlite3.OperationalError:
            self._fts_enabled = False

        # fetch Shopify
        try:
            locations = self._rest_fallback.list_locations() if self._rest_fallback else (self.client.list_locations() if self.client else [])
        except Exception:
            locations = []
        self._location_map = {int(x["id"]): (x.get("name") or str(x["id"])) for x in locations}

        try:
            products = self._fetch_all_active(limit=250)
        except Exception as e:
            print(f"[INDEX] ERROR list_products: {e}", flush=True)
            products = []

        print(f"[INDEX] fetched: locations={len(locations)} products={len(products)}", flush=True)

        # inventory
        all_inv_ids: List[int] = []
        for p in products:
            for v in p.get("variants") or []:
                if v.get("inventory_item_id"):
                    try:
                        all_inv_ids.append(int(v["inventory_item_id"]))
                    except Exception:
                        pass

        try:
            if self._rest_fallback:
                levels = self._rest_fallback.inventory_levels_for_items(all_inv_ids) if all_inv_ids else []
            elif self.client:
                levels = self.client.inventory_levels_for_items(all_inv_ids) if all_inv_ids else []
            else:
                levels = []
        except Exception as e:
            print(f"[INDEX] ERROR inventory_levels: {e}", flush=True)
            levels = []

        self._stats["inventory_levels"] = len(levels)

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

        # volcado
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
                int(p["id"]),
                p.get("handle"),
                p.get("title"),
                body_text,
                tags,
                p.get("vendor"),
                p.get("product_type"),
                hero_img,
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
            # Poblar FTS con columnas ampliadas
            cur.execute("""
                INSERT INTO products_fts (rowid, title, body, tags, handle, vendor, product_type)
                SELECT id, title, body, tags, handle, vendor, product_type FROM products
            """)
            conn.commit()

        conn.close()

        self._stats["products"] = n_products
        self._stats["variants"] = n_variants
        self._discards_sample = discards_sample
        self._discards_count = discards_count

        print(f"[INDEX] done: products={n_products} variants={n_variants} inventory_levels={self._stats['inventory_levels']}", flush=True)

    # ---------- reporting ----------
    def stats(self) -> Dict[str, Any]:
        return dict(self._stats)

    def discard_stats(self) -> Dict[str, Any]:
        by_reason = [{"reason": k, "count": v} for k, v in sorted(self._discards_count.items(), key=lambda x: -x[1])]
        return {"ok": True, "by_reason": by_reason, "sample": self._discards_sample}

    # [Compat] algunos endpoints llaman indexer.discards()
    def discards(self) -> Dict[str, Any]:
        return self.discard_stats()

    def sample_products(self, limit: int = 10) -> List[Dict[str, Any]]:
        conn = self._conn_read()
        cur = conn.cursor()
        rows = list(cur.execute(
            "SELECT id, handle, title, vendor, product_type, image FROM products LIMIT ?",
            (int(limit),),
        ))
        conn.close()
        return rows

    # ---------- señales/familias Agua & Gas ----------
    _WATER_ALLOW_FAMILIES = [
        "iot-waterv","iot-waterultra","iot-waterp","iot-water",
        "easy-waterultra","easy-water","iot waterv","iot waterultra","iot waterp","iot water","easy waterultra","easy water",
    ]
    _WATER_ALLOW_KEYWORDS = ["tinaco","cisterna","nivel","agua"]
    _WATER_BLOCK = [
        "bm-carsensor","carsensor","car","auto","vehiculo","vehículo",
        "ar-rain","rain","lluvia","ar-gasc","gasc"," gas","co2","humo","smoke",
        "ar-knock","knock","golpe"
    ]

    _GAS_ALLOW_FAMILIES = [
        "iot-gassensorv","iot-gassensor","connect-gas","easy-gas",
        "iot gassensorv","iot gassensor","connect gas","easy gas",
    ]
    _GAS_ALLOW_KEYWORDS = ["gas","tanque","estacionario","estacionaria","lp","propano","butano","nivel","medidor","porcentaje","volumen"]
    _GAS_BLOCK = [
        "ar-gasc","ar-flame","ar-photosensor","photosensor","megasensor","ar-megasensor",
        "arduino","módulo","modulo","module","mq-","mq2","flame","co2","humo","smoke","luz","photo","shield",
        "pest","plaga","mosquito","insect","insecto","pest-killer","pest killer",
        "easy-electric","easy electric","eléctrico","electrico","electricidad","energia","energía",
        "kwh","kw/h","consumo","tarifa","electric meter","medidor de consumo","contador",
        "ar-rain","rain","lluvia","carsensor","bm-carsensor","auto","vehiculo","vehículo",
        "iot-water","iot-waterv","iot-waterultra","iot-waterp","easy-water","easy-waterultra"," water "
    ]

    @staticmethod
    def _concat_fields_item(it) -> str:
        v = it.get("variant", {})
        body = (it.get("body") or "").lower()
        if len(body) > 1500:
            body = body[:1500]
        parts = [
            it.get("title") or "",
            it.get("handle") or "",
            it.get("tags") or "",
            it.get("vendor") or "",
            it.get("product_type") or "",
            v.get("sku") or "",
            body,
        ]
        if isinstance(it.get("skus"), (list, tuple)):
            parts.extend([x for x in it["skus"] if x])
        return " ".join(parts).lower()

    @staticmethod
    def _intent_from_query(q: str) -> Optional[str]:
        ql = (q or "").lower()
        gas_hard = ["gas", "tanque", "estacionario", "estacionaria", "lp", "propano", "butano"]
        if any(w in ql for w in gas_hard):
            return "gas"
        water_hard = ["agua", "tinaco", "cisterna", "inundacion", "inundación", "boya", "flotador"]
        if any(w in ql for w in water_hard):
            return "water"
        return None

    @staticmethod
    def _score_family(st: str, ql: str, allow_keywords, allow_fams, extras) -> Tuple[int, bool]:
        s = 0
        has_family = any(fam in st for fam in allow_fams)
        if any(w in st for w in allow_keywords): s += 20
        if has_family: s += 85
        if extras.get("want_valve"):
            for key in extras.get("valve_fams", []):
                if key in st: s += extras.get("valve_bonus", 95)
        if extras.get("want_ultra"):
            for key in extras.get("ultra_fams", []):
                if key in st: s += 55
        if extras.get("want_pressure"):
            for key in extras.get("pressure_fams", []):
                if key in st: s += 55
        if extras.get("want_bt"):
            for key in extras.get("bt_fams", []):
                if key in st: s += 45
        if extras.get("want_wifi"):
            for key in extras.get("wifi_fams", []):
                if key in st: s += 45
        if extras.get("want_display"):
            for key in extras.get("display_fams", []):
                if key in st: s += 40
        if extras.get("want_alarm"):
            for key in extras.get("alarm_words", []):
                if key in st: s += 25
        for neg in extras.get("neg_words", []):
            if neg in st: s -= 80
        return s, has_family

    def _rerank_for_water(self, query: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        ql = (query or "").lower()
        if self._intent_from_query(query) != "water" or not items:
            return items
        want_valve = ("valvula" in ql) or ("válvula" in ql)
        extras = {
            "want_valve": want_valve,
            "want_ultra": any(w in ql for w in ["ultra","ultrason","ultrasónico","ultrasonico"]),
            "want_pressure": any(w in ql for w in ["presion","presión"]),
            "want_bt": "bluetooth" in ql,
            "want_wifi": ("wifi" in ql) or ("app" in ql),
            "valve_fams": ["iot-waterv","iot waterv"],
            "ultra_fams": ["waterultra","easy-waterultra","easy waterultra"],
            "pressure_fams": ["iot-waterp","iot waterp"],
            "bt_fams": ["easy-water","easy water","easy-waterultra","easy waterultra"],
            "wifi_fams": ["iot-water","iot water","iot-waterv","iot waterv","iot-waterultra","iot waterultra"],
        }
        rescored = []
        positives = []
        for idx, it in enumerate(items):
            st = self._concat_fields_item(it)
            blocked = any(b in st for b in self._WATER_BLOCK)
            base = max(0, 30 - idx)
            score, has_fam = self._score_family(st, ql, self._WATER_ALLOW_KEYWORDS, self._WATER_ALLOW_FAMILIES, extras)
            total = score + base - (120 if blocked else 0)
            is_wv = ("iot-waterv" in st) or ("iot waterv" in st)
            rec = (total, score, blocked, has_fam, is_wv, it)
            rescored.append(rec)
            if has_fam and score >= 60 and not blocked:
                positives.append(rec)

        if positives:
            positives.sort(key=lambda x: x[0], reverse=True)
            if want_valve:
                wv = [r for r in positives if r[4]]; others = [r for r in positives if not r[4]]
                ordered = wv + others
            else:
                ordered = positives
            return [it for (_t,_s,_b,_hf,_wv,it) in ordered]

        # Fallback suave
        soft = []
        water_words = ["agua","tinaco","cisterna","nivel"]
        for idx, it in enumerate(items):
            st = self._concat_fields_item(it)
            if any(w in st for w in water_words) and not any(b in st for b in self._WATER_BLOCK):
                soft.append((max(0, 30 - idx), it))
        if soft:
            soft.sort(key=lambda x: x[0], reverse=True)
            return [it for (_score, it) in soft]

        rescored.sort(key=lambda x: x[0], reverse=True)
        return [it for (_t,_s,_b,_hf,_wv,it) in rescored]

    def _rerank_for_gas(self, query: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        ql = (query or "").lower()
        if self._intent_from_query(query) != "gas" or not items:
            return items
        want_valve = ("valvula" in ql) or ("válvula" in ql)
        extras = {
            "want_valve": want_valve,
            "want_bt": "bluetooth" in ql,
            "want_wifi": ("wifi" in ql) or ("app" in ql),
            "want_display": any(w in ql for w in ["pantalla","display"]),
            "want_alarm": "alarma" in ql,
            "valve_fams": ["iot-gassensorv","iot gassensorv"],
            "bt_fams": ["easy-gas","easy gas"],
            "wifi_fams": ["iot-gassensor","iot gassensor","connect-gas","connect gas"],
            "display_fams": ["easy-gas","easy gas"],
            "alarm_words": ["alarma","alerta"],
            "neg_words": [],
        }
        rescored = []
        positives = []
        for idx, it in enumerate(items):
            st = self._concat_fields_item(it)
            blocked = any(b in st for b in self._GAS_BLOCK)
            base = max(0, 30 - idx)
            score, has_fam = self._score_family(st, ql, self._GAS_ALLOW_KEYWORDS, self._GAS_ALLOW_FAMILIES, extras)
            total = score + base - (140 if blocked else 0)
            is_valve = ("iot-gassensorv" in st) or ("iot gassensorv" in st)
            rec = (total, score, blocked, has_fam, is_valve, it)
            rescored.append(rec)
            if has_fam and score >= 60 and not blocked:
                positives.append(rec)

        if positives:
            positives.sort(key=lambda x: x[0], reverse=True)
            if want_valve:
                vs = [r for r in positives if r[4]]; others = [r for r in positives if not r[4]]
                ordered = vs + others
            else:
                ordered = positives
            return [it for (_t,_s,_b,_hf,_valve,it) in ordered]

        # Fallback suave
        soft = []
        for idx, it in enumerate(items):
            st = self._concat_fields_item(it)
            if ("gas" in st) and not any(b in st for b in self._GAS_BLOCK):
                soft.append((max(0, 30 - idx), it))
        if soft:
            soft.sort(key=lambda x: x[0], reverse=True)
            return [it for (_score, it) in soft]

        rescored.sort(key=lambda x: x[0], reverse=True)
        return [it for (_t,_s,_b,_hf,_valve,it) in rescored]

    # ---------- búsqueda ecommerce-aware ----------
    def search(self, query: str, k: int = 6) -> List[Dict[str, Any]]:
        if not query:
            return []

        q_norm = _norm(query)

        # Stopwords y sinónimos amplios
        STOP = {
            "el","la","los","las","un","una","unos","unas",
            "de","del","al","y","o","u","en","a","con","por","para",
            "que","cual","cuales","cual","cuales","donde","donde",
            "busco","busca","buscar","quiero","necesito","tienes","tienen","hay",
            "producto","productos"
        }
        SYN = {
            # vídeo / pantallas / soportes
            "tv": ["televisor","pantalla"],
            "pantalla": ["tv","televisor","monitor"],
            "soporte": ["base","bracket","montaje","mount","pared","techo","mural"],
            # cables / conectividad
            "cable": ["cordon","conector","conexion","conexión"],
            "hdmi": ["uhd","4k","8k","microhdmi","mini hdmi","arc","earc"],
            "rca": ["av","audio video","a/v"],
            "vga": ["dsub","d-sub"],
            "coaxial": ["rg6","rg59","f"],
            # divisores y switches
            "divisor": ["splitter","duplicador","repartidor","1x2","1x4","1×2","1×4","1 x 2","1 x 4"],
            "splitter": ["divisor","duplicador","repartidor","1x2","1x4","1×2","1×4","1 x 2","1 x 4"],
            "switch": ["conmutador","selector"],
            # antenas
            "antena": ["exterior","interior","uhf","vhf","aerea","aérea","digital","hd"],
            # controles
            "control": ["remoto","remote"],
            "remoto": ["control","remote"],
            # cámaras / seguridad
            "camara": ["cámara","ip","cctv","vigilancia","seguridad","poe","dvr","nvr"],
            "cámara": ["camara","ip","cctv","vigilancia","seguridad","poe","dvr","nvr"],
            # audio
            "bocina": ["parlante","altavoz","speaker"],
        }

        # expandir consulta con sinónimos (no agrego stopwords)
        tokens = [t for t in re.split(r"[^\wáéíóúñü]+", q_norm) if t and t not in STOP]
        expand = set(tokens)
        for t in list(tokens):
            if t in SYN:
                expand.update(SYN[t])

        # añadir taxonomía como términos válidos (boost controlado en scoring)
        taxo_terms = set(self._taxonomy_keywords) if self._taxonomy_loaded else set()

        # Query FTS (o LIKE si no hay FTS)
        conn = self._conn_read()
        cur = conn.cursor()

        def _like_escape(s: str) -> str:
            return s.replace("%", "\\%").replace("_", "\\_")

        candidates: List[Dict[str, Any]] = []

        if self._fts_enabled:
            # Construimos un MATCH flexible: AND de términos expandidos
            # (ej: 'soporte AND techo'...) — si falla, caemos a LIKE.
            try:
                clause = " AND ".join(f'"{t}"' for t in expand)
                q = f"SELECT p.id, p.handle, p.title, p.body, p.tags, p.vendor, p.product_type, p.image " \
                    f"FROM products_fts f JOIN products p ON p.id=f.rowid " \
                    f"WHERE products_fts MATCH ? LIMIT ?"
                rows = list(cur.execute(q, (clause, max(k, 120))))
            except Exception:
                rows = []
        else:
            rows = []

        if not rows:
            # LIKE amplio si no hay FTS o no hubo MATCH
            like = "%" + "%".join(_like_escape(t) for t in expand) + "%"
            q = ("SELECT id, handle, title, body, tags, vendor, product_type, image "
                 "FROM products WHERE _rowid_ IN (SELECT _rowid_ FROM products) "
                 "AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' "
                 "OR handle LIKE ? ESCAPE '\\' OR vendor LIKE ? ESCAPE '\\' OR product_type LIKE ? ESCAPE '\\') "
                 "LIMIT ?")
            rows = list(cur.execute(q, (like, like, like, like, like, like, max(k, 120))))

        # añadir variantes + inventario
        # (traemos la variante más barata por producto como representante)
        for r in rows:
            vs = list(cur.execute(
                "SELECT id, sku, price, compare_at_price, inventory_item_id FROM variants WHERE product_id=? ORDER BY price ASC LIMIT 1",
                (int(r["id"]),)
            ))
            if not vs:
                continue
            v = vs[0]
            inv = list(cur.execute(
                "SELECT location_id, location_name, available FROM inventory WHERE variant_id=?",
                (int(v["id"]),)
            ))
            variant = {
                "variant_id": int(v["id"]),
                "sku": v.get("sku"),
                "price": float(v["price"]),
                "compare_at_price": float(v["compare_at_price"]) if v.get("compare_at_price") is not None else None,
                "inventory": [{"location_id": int(x["location_id"]), "available": int(x["available"])} for x in inv],
            }
            candidates.append({
                "id": int(r["id"]),
                "handle": r.get("handle"),
                "title": r.get("title"),
                "body": r.get("body"),
                "tags": r.get("tags"),
                "vendor": r.get("vendor"),
                "product_type": r.get("product_type"),
                "image": r.get("image"),
                "variant": variant,
            })

        # ---------- Scoring ----------
        # Patrones especiales (matriz 1xN y pulgadas)
        PAT_ONE_BY_N = re.compile(r"\b(\d+)\s*[x×]\s*(\d+)\b", re.IGNORECASE)
        def _query_matrix(q: str) -> Optional[str]:
            m = PAT_ONE_BY_N.search(q or "")
            if m:
                return f"{m.group(1)}x{m.group(2)}"
            return None
        q_matrix = _query_matrix(query)

        inch = sorted(set(re.findall(r"\b(1[9]|[2-9]\d|100)\b", q_norm)))  # 19..100
        q_inches = set(inch) if inch else set()

        def _has_matrix(text: str, mx: str) -> bool:
            return bool(re.search(rf"\b{re.escape(mx).replace('x','[x×]')}\b", text, flags=re.IGNORECASE))

        def score_item(it: Dict[str, Any]) -> int:
            s = 0
            st = " ".join([_norm(it.get("title") or ""), _norm(it.get("tags") or ""),
                           _norm(it.get("vendor") or ""), _norm(it.get("product_type") or ""),
                           _norm(it.get("body") or ""), _norm(it.get("handle") or "")])

            # Match directo de tokens consulta
            for t in expand:
                if t and t in st:
                    s += 10

            # Boost por taxonomía (no destructivo)
            if taxo_terms:
                hits = 0
                for t in taxo_terms:
                    if t and t in st:
                        hits += 1
                if hits:
                    s += min(60, 5 * hits)  # 5 por término, máx 60

            # Tamaños en pulgadas
            if q_inches:
                # si el título o body mencionan las pulgadas, dar puntos
                for nn in q_inches:
                    if re.search(rf"\b{nn}\s*[\"”]?\b", st):
                        s += 6

            # Priorizar matriz exacta solicitada y penalizar matrices diferentes
            if q_matrix:
                st_full = _norm((it.get("title") or "") + " " + (it.get("handle") or "") + " " + (it.get("tags") or ""))
                if _has_matrix(st_full, q_matrix):
                    s += 60
                else:
                    other = re.findall(r"\b(\d+)\s*[x×]\s*(\d+)\b", st_full)
                    for a, b in other:
                        mx = f"{a}x{b}"
                        if mx != q_matrix:
                            s -= 12
                            break

            # Ligera preferencia por productos con inventario total > 0
            inv_total = sum(x.get("available", 0) for x in (it["variant"].get("inventory") or []))
            if inv_total > 0:
                s += 8

            return s

        candidates.sort(key=score_item, reverse=True)

        # Armar resultado final con URLs
        results: List[Dict[str, Any]] = []
        for it in candidates[:max(k, 12)]:
            v = it["variant"]
            product_url = f"{self.store_base_url}/products/{it['handle']}" if it.get('handle') else self.store_base_url
            buy_url = f"{self.store_base_url}/cart/{v['variant_id']}:1"
            results.append({
                "id": it["id"],
                "title": it["title"],
                "handle": it["handle"],
                "image": it["image"],
                "body": it["body"],
                "tags": it["tags"],
                "vendor": it["vendor"],
                "product_type": it["product_type"],
                "product_url": product_url,
                "buy_url": buy_url,
                "variant": v,
            })

        conn.close()
        return results[:k]

    # ---------- util para endpoints (LLM / Admin) ----------
    def mini_catalog_json(self, items: List[Dict[str, Any]]) -> str:
        out = []
        for it in items:
            v = it["variant"]
            out.append({
                "title": it["title"],
                "price": v["price"],
                "sku": v.get("sku"),
                "compare_at_price": v.get("compare_at_price"),
                "product_url": it["product_url"] if "product_url" in it else f"{self.store_base_url}/products/{it['handle']}",
                "buy_url": it["buy_url"] if "buy_url" in it else f"{self.store_base_url}/cart/{v['variant_id']}:1",
                "stock_total": sum(x["available"] for x in v["inventory"]) if v["inventory"] else 0,
                "image": it["image"],
            })
        return json.dumps(out, ensure_ascii=False)

    # ---------- API pública para app.py ----------
    def _apply_intent_rerank_public(self, query: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Rerank por intención (AGUA / GAS). Si no hay intención clara, retorna items igual."""
        items2 = self._rerank_for_water(query, items)
        items3 = self._rerank_for_gas(query, items2)
        return items3

    def _enforce_intent_gate_public(self, query: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Si la intención es clara, y existen 'positivos' suficientes, aplica gate suave."""
        intent = self._intent_from_query(query)
        if not intent or not items:
            return items

        # Gate solo si al menos 2 elementos cumplen señales de familia permitida
        ok_items: List[Dict[str, Any]] = []
        ql = (query or "").lower()

        if intent == "water":
            extras = {
                "want_valve": ("valvula" in ql) or ("válvula" in ql),
                "want_ultra": any(w in ql for w in ["ultra","ultrason","ultrasónico","ultrasonico"]),
                "want_pressure": any(w in ql for w in ["presion","presión"]),
            }
            for it in items:
                st = self._concat_fields_item(it)
                sc, has_fam = self._score_family(st, ql, self._WATER_ALLOW_KEYWORDS, self._WATER_ALLOW_FAMILIES, extras)
                if has_fam and sc >= 60 and not any(b in st for b in self._WATER_BLOCK):
                    ok_items.append(it)

        if intent == "gas":
            extras = {"want_valve": ("valvula" in ql) or ("válvula" in ql)}
            for it in items:
                st = self._concat_fields_item(it)
                sc, has_fam = self._score_family(st, ql, self._GAS_ALLOW_KEYWORDS, self._GAS_ALLOW_FAMILIES, extras)
                if has_fam and sc >= 60 and not any(b in st for b in self._GAS_BLOCK):
                    ok_items.append(it)

        if len(ok_items) >= 2:
            return ok_items[:len(items)]
        return items
