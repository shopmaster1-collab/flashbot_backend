# -*- coding: utf-8 -*-
"""
orders_report.py
----------------
Lector solo-lectura para la hoja pública de Google Sheets usada por el widget
Master. La búsqueda se realiza por la columna FOLIO y la salida se normaliza a
las columnas solicitadas por el frontend:

- Orden de compra  <- ORDEN_COMPRA
- SKU de producto  <- CLAVE_ARTICULO
- Cantidad         <- UNIDADES
- Total            <- TOTAL_CON_IVA
- Paquetería       <- REM_PAQUETERIA
- Guía             <- REM_GUIA
"""

from __future__ import annotations

import csv
import html
import io
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

DEFAULT_ORDERS_PUBHTML_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vS7MFutb5ikOvAvWsxuc164Txu30GeVkCGZAY3U_fUVmS_0MKMn6ta2hbbNc-hcmFbV0fyAe8A-7PGG/"
    "pubhtml?gid=1842193501&single=true"
)
ORDERS_PUBHTML_URL = os.getenv("ORDERS_PUBHTML_URL") or os.getenv("ORDERS_PUBHTMl_URL") or DEFAULT_ORDERS_PUBHTML_URL
ORDERS_TTL_SECONDS = int(os.getenv("ORDERS_TTL_SECONDS", "45"))

SOURCE_COLS = ["FOLIO", "ORDEN_COMPRA", "CLAVE_ARTICULO", "UNIDADES", "TOTAL_CON_IVA", "REM_PAQUETERIA", "REM_GUIA"]
DISPLAY_FIELDS: List[Tuple[str, str]] = [
    ("Folio", "FOLIO"),
    ("Orden de compra", "ORDEN_COMPRA"),
    ("SKU de producto", "CLAVE_ARTICULO"),
    ("Cantidad", "UNIDADES"),
    ("Total", "TOTAL_CON_IVA"),
    ("Paquetería", "REM_PAQUETERIA"),
    ("Guía", "REM_GUIA"),
]
TABLE_FIELDS = ["Orden de compra", "SKU de producto", "Cantidad", "Total", "Paquetería", "Guía"]

HEADER_ALIASES = {
    "FOLIO": "FOLIO",
    "NO_FOLIO": "FOLIO",
    "NUMERO_FOLIO": "FOLIO",
    "NUMERO_DE_FOLIO": "FOLIO",
    "FOLIO_PEDIDO": "FOLIO",
    "FOLIO_DE_PEDIDO": "FOLIO",
    "ORDEN_COMPRA": "ORDEN_COMPRA",
    "ORDEN_DE_COMPRA": "ORDEN_COMPRA",
    "OC": "ORDEN_COMPRA",
    "ORDEN": "ORDEN_COMPRA",
    "CLAVE_ARTICULO": "CLAVE_ARTICULO",
    "CLAVE_DE_ARTICULO": "CLAVE_ARTICULO",
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
    "REMISION_PAQUETERIA": "REM_PAQUETERIA",
    "REM_GUIA": "REM_GUIA",
    "GUIA": "REM_GUIA",
    "NUMERO_GUIA": "REM_GUIA",
    "NUMERO_DE_GUIA": "REM_GUIA",
    "REMISION_GUIA": "REM_GUIA",
}

ORDER_TOKEN_RE = re.compile(
    r"(?:folio|pedido|orden|order|estatus|status|seguimiento|rastreo|gu[ií]a)\s*[:#\-]?\s*([A-Za-z0-9][A-Za-z0-9._\-]{2,})",
    re.IGNORECASE,
)
ORDER_HASH_RE = re.compile(r"#\s*([A-Za-z0-9][A-Za-z0-9._\-]{2,})")
ORDER_NUMERIC_RE = re.compile(r"(?:^|[^A-Za-z0-9])#?([0-9]{3,24})(?:[^A-Za-z0-9]|$)")


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


def folio_key(value) -> str:
    s = strip_accents(html.unescape(str(value or "")).strip()).upper()
    return re.sub(r"[^A-Z0-9]", "", s)


def folio_matches(sheet_value, requested_value) -> bool:
    a = folio_key(sheet_value)
    b = folio_key(requested_value)
    if not a or not b:
        return False
    if a == b:
        return True
    if a.isdigit() and b.isdigit():
        try:
            return int(a) == int(b)
        except Exception:
            return False
    return False


def detect_order_number(text: str) -> Optional[str]:
    if not text:
        return None
    s = str(text).strip()
    for pattern in (ORDER_TOKEN_RE, ORDER_HASH_RE, ORDER_NUMERIC_RE):
        m = pattern.search(s)
        if m:
            return m.group(1).strip()
    return None


def looks_like_order_intent(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    keys = (
        "folio", "pedido", "orden", "order", "estatus", "status", "seguimiento", "rastreo",
        "mi compra", "mi pedido", "envio", "envío", "paqueteria", "paquetería", "guia", "guía",
    )
    return any(k in t for k in keys) or bool(ORDER_TOKEN_RE.search(text)) or bool(ORDER_HASH_RE.search(text))


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


def csv_url_from_pubhtml(url: str) -> str:
    if not url:
        return ""
    if "/pubhtml" in url:
        base = url.replace("/pubhtml", "/pub")
        if "output=csv" not in base:
            sep = "&" if "?" in base else "?"
            base = f"{base}{sep}output=csv"
        return base
    if "/pub" in url and "output=csv" not in url:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}output=csv"
    return url


def header_score(cells: List[str]) -> int:
    normalized = [normalize_header(c) for c in cells]
    return sum(1 for h in normalized if h in SOURCE_COLS)


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
    for i, cells in enumerate(matrix[:25]):
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


class OrdersSheetReader:
    """Lector con caché en memoria para consultar pedidos por FOLIO."""

    def __init__(self, url: str = "", ttl: int = ORDERS_TTL_SECONDS):
        self.url = url or ORDERS_PUBHTML_URL
        self.ttl = int(ttl or ORDERS_TTL_SECONDS)
        self._cache_ts = 0.0
        self._headers: List[str] = []
        self._rows: List[Dict[str, str]] = []
        self._mode = ""
        self._source_url = ""

    def _fetch_html(self) -> Tuple[List[str], List[Dict[str, str]]]:
        response = requests.get(
            self.url,
            timeout=25,
            headers={
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
            },
        )
        response.raise_for_status()
        return rows_from_matrix(matrix_from_html(response.text or ""))

    def _fetch_csv(self) -> Tuple[List[str], List[Dict[str, str]]]:
        csv_url = csv_url_from_pubhtml(self.url)
        response = requests.get(
            csv_url,
            timeout=25,
            headers={
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/csv,*/*;q=0.8",
                "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
            },
        )
        response.raise_for_status()
        data = list(csv.reader(io.StringIO(response.text or "")))
        headers, rows = rows_from_matrix(data)
        if rows:
            self._source_url = csv_url
        return headers, rows

    def rows(self, force: bool = False) -> List[Dict[str, str]]:
        now = time.time()
        if not force and self._rows and (now - self._cache_ts) < self.ttl:
            return self._rows

        headers, rows = [], []
        self._mode = ""
        self._source_url = self.url
        try:
            headers, rows = self._fetch_html()
            if rows:
                self._mode = "html"
        except Exception:
            headers, rows = [], []

        if not rows:
            try:
                headers, rows = self._fetch_csv()
                if rows:
                    self._mode = "csv"
            except Exception:
                headers, rows = [], []

        self._headers = headers
        self._rows = rows
        self._cache_ts = now
        return self._rows

    def find_by_order(self, folio: str) -> List[Dict[str, str]]:
        return self.find_by_folio(folio)

    def find_by_folio(self, folio: str) -> List[Dict[str, str]]:
        return [build_item(row) for row in self.rows(force=True) if folio_matches(row.get("FOLIO", ""), folio)]

    def sample(self, limit: int = 3) -> List[Dict[str, str]]:
        return self.rows(force=True)[: max(0, int(limit))]

    def meta(self) -> Dict[str, object]:
        self.rows(force=True)
        return {
            "url": self.url,
            "source_url": self._source_url,
            "mode": self._mode,
            "headers": self._headers,
            "rows_count": len(self._rows),
        }


def render_vertical_md(rows: List[Dict[str, str]]) -> str:
    if not rows:
        return "No encontramos información con ese número de folio. Verifica el folio tal como aparece en tu comprobante."
    folio = rows[0].get("Folio", "—")
    orden = rows[0].get("Orden de compra", "—")
    parts = [f"Pedido correspondiente al folio: {folio}", f"Orden de compra: {orden}"]
    for index, row in enumerate(rows, 1):
        block = [f"Artículo {index}"]
        for key in TABLE_FIELDS:
            block.append(f"- {key}: {row.get(key, '—')}")
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def render_compact_table_md(rows: List[Dict[str, str]]) -> str:
    if not rows:
        return "No encontramos información con ese número de folio."
    header = "| " + " | ".join(TABLE_FIELDS) + " |"
    sep = "|" + "|".join(["---"] * len(TABLE_FIELDS)) + "|"
    body = ["| " + " | ".join(row.get(col, "—") for col in TABLE_FIELDS) + " |" for row in rows]
    return "\n".join([header, sep] + body)


def format_for_widget(rows: List[Dict[str, str]], prefer_vertical: bool = True) -> str:
    return render_vertical_md(rows) if prefer_vertical else render_compact_table_md(rows)
