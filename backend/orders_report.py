# -*- coding: utf-8 -*-
"""Consulta de pedidos desde Google Sheets publicado.

Versión Maxter 2026-05-11.3
- La búsqueda principal se realiza por la columna ORDEN_COMPRA.
- La orden se trata como texto alfanumérico completo; no se recorta a números.
- Soporta órdenes como:
  702-7300318-1033843, 2000012817687573, v44851776ekt-01, #9188.307766427-A.
- Mantiene alias como DE_ORDEN por compatibilidad con hojas anteriores.
"""

from __future__ import annotations

import csv
import html
import io
import os
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

DEFAULT_ORDERS_PUBHTML_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vS7MFutb5ikOvAvWsxuc164Txu30GeVkCGZAY3U_fUVmS_0MKMn6ta2hbbNc-hcmFbV0fyAe8A-7PGG/"
    "pubhtml?gid=1842193501&single=true"
)

ORDERS_TTL_SECONDS = int(os.getenv("ORDERS_TTL_SECONDS", "45") or "45")

SOURCE_COLS = [
    "ORDEN_COMPRA",
    "DE_ORDEN",
    "ORDER_ID",
    "PEDIDO",
    "FOLIO",
    "CLAVE_ARTICULO",
    "UNIDADES",
    "TOTAL_CON_IVA",
    "REM_PAQUETERIA",
    "REM_GUIA",
]

DISPLAY_FIELDS = [
    ("Orden de compra", "ORDEN_COMPRA"),
    ("SKU de producto", "CLAVE_ARTICULO"),
    ("Cantidad", "UNIDADES"),
    ("Total", "TOTAL_CON_IVA"),
    ("Paquetería", "REM_PAQUETERIA"),
    ("Guía", "REM_GUIA"),
]

TABLE_FIELDS = ["Orden de compra", "SKU de producto", "Cantidad", "Total", "Paquetería", "Guía"]

HEADER_ALIASES = {
    "ORDEN_COMPRA": "ORDEN_COMPRA",
    "ORDEN_DE_COMPRA": "ORDEN_COMPRA",
    "ORDEN_DE_COMPRAS": "ORDEN_COMPRA",
    "ORDEN_DE_VENTA": "ORDEN_COMPRA",
    "NO_ORDEN": "ORDEN_COMPRA",
    "NUMERO_ORDEN": "ORDEN_COMPRA",
    "NUMERO_DE_ORDEN": "ORDEN_COMPRA",
    "NUMERO_DE_PEDIDO": "ORDEN_COMPRA",
    "NUMERO_PEDIDO": "ORDEN_COMPRA",
    "N_PEDIDO": "ORDEN_COMPRA",
    "NO_PEDIDO": "ORDEN_COMPRA",
    "PEDIDO": "ORDEN_COMPRA",
    "PEDIDO_CLIENTE": "ORDEN_COMPRA",
    "PEDIDO_MARKETPLACE": "ORDEN_COMPRA",
    "ORDER": "ORDEN_COMPRA",
    "ORDER_ID": "ORDEN_COMPRA",
    "ORDER_NUMBER": "ORDEN_COMPRA",
    "ORDER_NO": "ORDEN_COMPRA",
    "OC": "ORDEN_COMPRA",
    "DE_ORDEN": "ORDEN_COMPRA",
    "D_ORDEN": "ORDEN_COMPRA",

    "FOLIO": "FOLIO",
    "NO_FOLIO": "FOLIO",
    "NUMERO_FOLIO": "FOLIO",
    "NUMERO_DE_FOLIO": "FOLIO",
    "FOLIO_PEDIDO": "FOLIO",
    "FOLIO_DE_PEDIDO": "FOLIO",
    "PEDIDO_MICROSIP": "FOLIO",

    "CLAVE_ARTICULO": "CLAVE_ARTICULO",
    "CLAVE_DE_ARTICULO": "CLAVE_ARTICULO",
    "CLAVE": "CLAVE_ARTICULO",
    "SKU": "CLAVE_ARTICULO",
    "SKU_PRODUCTO": "CLAVE_ARTICULO",
    "SKU_DE_PRODUCTO": "CLAVE_ARTICULO",
    "ARTICULO": "CLAVE_ARTICULO",

    "UNIDADES": "UNIDADES",
    "UNIDAD": "UNIDADES",
    "CANTIDAD": "UNIDADES",
    "PIEZAS": "UNIDADES",
    "PZAS": "UNIDADES",

    "TOTAL_CON_IVA": "TOTAL_CON_IVA",
    "TOTAL": "TOTAL_CON_IVA",
    "TOTAL_IVA": "TOTAL_CON_IVA",
    "PRECIO_TOTAL": "TOTAL_CON_IVA",
    "IMPORTE": "TOTAL_CON_IVA",

    "REM_PAQUETERIA": "REM_PAQUETERIA",
    "PAQUETERIA": "REM_PAQUETERIA",
    "PAQUETERIA_REM": "REM_PAQUETERIA",
    "REMISION_PAQUETERIA": "REM_PAQUETERIA",

    "REM_GUIA": "REM_GUIA",
    "GUIA": "REM_GUIA",
    "NUMERO_GUIA": "REM_GUIA",
    "NUMERO_DE_GUIA": "REM_GUIA",
    "GUIA_REM": "REM_GUIA",
    "REMISION_GUIA": "REM_GUIA",
}

ORDER_TOKEN_RE = re.compile(
    r"(?:folio|pedido|orden|order|estatus|status|seguimiento|rastreo|gu[ií]a)\s*(?:es|:|#|-)?\s*(#?[A-Za-z0-9][A-Za-z0-9._\-#]{2,})",
    re.IGNORECASE,
)
ORDER_HASH_RE = re.compile(r"#\s*([A-Za-z0-9][A-Za-z0-9._\-]{2,})")
ORDER_BARE_RE = re.compile(r"^\s*(#?[A-Za-z0-9][A-Za-z0-9._\-#]{2,})\s*$")
ORDER_NUMERIC_RE = re.compile(r"(?:^|[^A-Za-z0-9])#?([0-9][0-9._\-]{2,24})(?:[^A-Za-z0-9]|$)")


def strip_accents(text: str) -> str:
    return (text or "").translate(str.maketrans({
        "Á": "A", "É": "E", "Í": "I", "Ó": "O", "Ú": "U", "Ü": "U", "Ñ": "N",
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ü": "u", "ñ": "n",
    }))


def clean_cell(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def normalize_header(text: str) -> str:
    t = strip_accents(html.unescape(str(text or "")).strip()).upper()
    t = re.sub(r"[^A-Z0-9]+", "_", t).strip("_")
    return HEADER_ALIASES.get(t, t)


def order_key(value) -> str:
    s = strip_accents(html.unescape(str(value or "")).strip()).upper()
    return re.sub(r"[^A-Z0-9]", "", s)


def folio_key(value) -> str:
    # Compatibilidad con importaciones antiguas.
    return order_key(value)


def order_matches(sheet_value, requested_value) -> bool:
    a = order_key(sheet_value)
    b = order_key(requested_value)
    if not a or not b:
        return False
    if a == b:
        return True
    if a.isdigit() and b.isdigit():
        return a.lstrip("0") == b.lstrip("0")
    return False


def folio_matches(sheet_value, requested_value) -> bool:
    return order_matches(sheet_value, requested_value)


def looks_like_bare_order(text: str) -> bool:
    s = str(text or "").strip()
    m = ORDER_BARE_RE.match(s)
    if not m:
        return False
    token = m.group(1)
    key = order_key(token)
    # Evita confundir consultas de SKUs cortos del catálogo con pedidos.
    return bool(len(key) >= 8 and any(ch.isdigit() for ch in token))


def detect_order_number(text: str) -> Optional[str]:
    if not text:
        return None
    s = str(text).strip()
    m = ORDER_TOKEN_RE.search(s)
    if m and any(ch.isdigit() for ch in m.group(1)):
        return m.group(1).strip()
    m = ORDER_HASH_RE.search(s)
    if m and any(ch.isdigit() for ch in m.group(1)):
        return m.group(1).strip()
    if looks_like_bare_order(s):
        return ORDER_BARE_RE.match(s).group(1).strip()
    m = ORDER_NUMERIC_RE.search(s)
    if m:
        return m.group(1).strip()
    return None


def format_value(source_key: str, value) -> str:
    val = clean_cell(value)
    if not val:
        return "—"
    if source_key == "TOTAL_CON_IVA":
        raw = val.replace("$", "").replace(",", "").strip()
        try:
            return f"${float(raw):,.2f}"
        except Exception:
            return val
    return val


def build_item(row: Dict[str, str]) -> Dict[str, str]:
    return {display: format_value(source, row.get(source, "")) for display, source in DISPLAY_FIELDS}


def candidate_urls(url: str) -> List[Tuple[str, str]]:
    if not url:
        return []
    candidates: List[Tuple[str, str]] = []

    def add(mode: str, candidate: str):
        if candidate and (mode, candidate) not in candidates:
            candidates.append((mode, candidate))

    add("html", url)
    parsed = urlsplit(url)
    qs = parse_qs(parsed.query)
    gid = (qs.get("gid") or [""])[0]
    path = parsed.path
    pub_path = path.replace("/pubhtml", "/pub") if "/pubhtml" in path else path

    if "/pub" in pub_path:
        base_pub = urlunsplit((parsed.scheme, parsed.netloc, pub_path, "", ""))
        if gid:
            add("csv", f"{base_pub}?gid={gid}&single=true&output=csv")
            add("csv", f"{base_pub}?output=csv&gid={gid}&single=true")
            add("csv", f"{base_pub}?gid={gid}&output=csv")
        add("csv", f"{base_pub}?single=true&output=csv")
        add("csv", f"{base_pub}?output=csv")

    if "/spreadsheets/d/e/" in path:
        base_dir = path.split("/pub", 1)[0]
        if gid:
            add("csv", urlunsplit((parsed.scheme, parsed.netloc, f"{base_dir}/gviz/tq", f"tqx=out:csv&gid={gid}", "")))
    return candidates


def csv_url_from_pubhtml(url: str) -> str:
    for mode, candidate in candidate_urls(url):
        if mode == "csv":
            return candidate
    return url


def header_score(cells: List[str]) -> int:
    normalized = [normalize_header(c) for c in cells]
    score = sum(1 for h in normalized if h in SOURCE_COLS)
    if "ORDEN_COMPRA" in normalized:
        score += 3
    return score


def matrix_from_html(html_text: str) -> List[List[str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.find_all("table")
    table = soup.find("table", {"class": "waffle"})
    if table is None and tables:
        table = max(tables, key=lambda t: len(t.find_all("tr")))
    if table is None:
        return []

    matrix: List[List[str]] = []
    for tr in table.find_all("tr"):
        cells: List[str] = []
        for index, cell in enumerate(tr.find_all(["td", "th"])):
            text = clean_cell(cell.get_text(" ", strip=True))
            classes = " ".join(cell.get("class") or [])
            is_visual_row_header = index == 0 and cell.name == "th" and (
                "row-headers" in classes or re.fullmatch(r"\d+", text or "") is not None
            )
            if is_visual_row_header:
                continue
            if cell.name == "th" and re.fullmatch(r"[A-Z]+", text or ""):
                continue
            cells.append(text)
        if any(cells):
            matrix.append(cells)
    return matrix


def rows_from_matrix(matrix: List[List[str]]) -> Tuple[List[str], List[Dict[str, str]]]:
    if not matrix:
        return [], []
    best_idx = -1
    best_score = -1
    for i, cells in enumerate(matrix[:250]):
        score = header_score(cells)
        if score > best_score:
            best_idx = i
            best_score = score
    if best_idx < 0 or best_score <= 0:
        return [], []

    headers = [normalize_header(h) for h in matrix[best_idx]]
    rows: List[Dict[str, str]] = []
    for arr in matrix[best_idx + 1:]:
        if not any(arr):
            continue
        row: Dict[str, str] = {}
        for index, value in enumerate(arr):
            if index < len(headers) and headers[index]:
                row[headers[index]] = clean_cell(value)
        if row and any(row.values()):
            rows.append(row)
    return headers, rows


def search_columns(headers: List[str]) -> List[str]:
    cols = []
    for col in ("ORDEN_COMPRA", "ORDER_ID", "PEDIDO", "DE_ORDEN"):
        if col in headers and col not in cols:
            cols.append(col)
    if not cols:
        cols.extend([h for h in headers if any(x in h for x in ("ORDEN", "ORDER", "PEDIDO"))])
    if not cols and "FOLIO" in headers:
        cols.append("FOLIO")
    return cols or headers


class OrdersSheetReader:
    """Lector con caché en memoria para consultar pedidos por ORDEN_COMPRA."""

    def __init__(self, url: str = "", ttl: int = ORDERS_TTL_SECONDS):
        self.url = url or os.getenv("ORDERS_PUBHTML_URL") or DEFAULT_ORDERS_PUBHTML_URL
        self.ttl = int(ttl or ORDERS_TTL_SECONDS)
        self._cache_ts = 0.0
        self._headers: List[str] = []
        self._rows: List[Dict[str, str]] = []
        self._mode = ""
        self._source_url = ""
        self._attempts: List[dict] = []

    def _fetch_html(self, url: str) -> Tuple[List[str], List[Dict[str, str]]]:
        response = requests.get(url, timeout=25, headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
        })
        response.raise_for_status()
        return rows_from_matrix(matrix_from_html(response.text or ""))

    def _fetch_csv(self, url: str) -> Tuple[List[str], List[Dict[str, str]]]:
        response = requests.get(url, timeout=25, headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/csv,text/plain,*/*;q=0.8",
            "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
        })
        response.raise_for_status()
        text = response.text or ""
        if not text.strip():
            return [], []
        if "<html" in text[:500].lower() or "<table" in text[:1000].lower():
            return rows_from_matrix(matrix_from_html(text))
        rows_raw = list(csv.reader(io.StringIO(text)))
        return rows_from_matrix(rows_raw)

    def refresh(self, force: bool = False) -> None:
        now = time.time()
        if not force and self._rows and (now - self._cache_ts) < self.ttl:
            return

        headers: List[str] = []
        rows: List[Dict[str, str]] = []
        mode = ""
        source_url = self.url
        attempts: List[dict] = []

        for candidate_mode, candidate_url in candidate_urls(self.url):
            try:
                if candidate_mode == "html":
                    h, r = self._fetch_html(candidate_url)
                else:
                    h, r = self._fetch_csv(candidate_url)
                attempts.append({"mode": candidate_mode, "url": candidate_url, "headers": h[:12], "rows": len(r)})
                if r:
                    headers, rows, mode, source_url = h, r, candidate_mode, candidate_url
                    break
            except Exception as exc:
                attempts.append({"mode": candidate_mode, "url": candidate_url, "error": repr(exc)})

        self._cache_ts = now
        self._headers = headers
        self._rows = rows
        self._mode = mode
        self._source_url = source_url
        self._attempts = attempts

    def rows(self, force: bool = False) -> List[Dict[str, str]]:
        self.refresh(force=force)
        return self._rows

    def find(self, order_no: str) -> List[Dict[str, str]]:
        return self.find_by_order(order_no)

    def find_by_order(self, order_no: str) -> List[Dict[str, str]]:
        self.refresh(force=True)
        cols = search_columns(self._headers)
        return [build_item(row) for row in self._rows if any(order_matches(row.get(col, ""), order_no) for col in cols)]

    def find_by_folio(self, folio: str) -> List[Dict[str, str]]:
        # Compatibilidad antigua: ahora también busca como ORDEN_COMPRA.
        return self.find_by_order(folio)

    def meta(self) -> Dict[str, object]:
        self.refresh(force=True)
        return {
            "url": self.url,
            "mode": self._mode,
            "source_url": self._source_url,
            "headers": self._headers,
            "rows_count": len(self._rows),
            "search_columns": search_columns(self._headers),
            "attempts": self._attempts,
        }

    def sample(self, limit: int = 3) -> List[Dict[str, str]]:
        self.refresh(force=True)
        return self._rows[:limit]


def render_vertical_md(rows: List[Dict[str, str]], requested_order: str = "") -> str:
    if not rows:
        return "No encontramos información con ese número de pedido. Verifica el número tal como aparece en tu comprobante."
    orden = rows[0].get("Orden de compra", requested_order or "—")
    parts = [f"Pedido correspondiente al pedido: {orden}"]
    for idx, row in enumerate(rows, 1):
        block = [f"Artículo {idx}"]
        for field in TABLE_FIELDS:
            block.append(f"- {field}: {row.get(field, '—')}")
        parts.append("\n".join(block))
    return "\n\n".join(parts)
