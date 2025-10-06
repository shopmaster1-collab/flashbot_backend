# -*- coding: utf-8 -*-
"""
orders_report.py
----------------
Lectura *solo-lectura* de la publicación de Google Sheets en formato `pubhtml`
para generar reportes de pedidos por "# de Orden".

Diseñado para ser "plug-and-play" sin romper nada del bot existente.
No requiere nuevas dependencias fuera de `requests` y `beautifulsoup4`.
"""

import os
import re
import time
import html
import logging
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

# ENV
ORDERS_PUBHTML_URL = os.getenv("ORDERS_PUBHTML_URL") or os.getenv("ORDERS_PUBHTMl_URL") or ""
ORDERS_AUTORELOAD = os.getenv("ORDERS_AUTORELOAD", "1")  # "1" para leer siempre; "0" cachea por TTL corto
ORDERS_TTL_SECONDS = int(os.getenv("ORDERS_TTL_SECONDS", "45"))

# Cache muy ligero en memoria de proceso
_cache = {"ts": 0.0, "rows": []}

# Normalización flexible de encabezados
_HEADER_MAP = {
    "# DE ORDEN": "# de Orden",
    "NO. DE ORDEN": "# de Orden",
    "NÚMERO DE ORDEN": "# de Orden",
    "NÚMERO DE PEDIDO": "# de Orden",
    "ORDEN": "# de Orden",
    "# ORDEN": "# de Orden",
    "#": "# de Orden",
    "SKU": "SKU",
    "PZAS": "Pzas",
    "PIEZAS": "Pzas",
    "CANTIDAD": "Pzas",
    "PRECIO UNITARIO": "Precio Unitario",
    "PRECIO": "Precio Unitario",
    "PRECIO TOTAL": "Precio Total",
    "TOTAL": "Precio Total",
    "FECHA INICIO": "Fecha Inicio",
    "EN PROCESO": "EN PROCESO",
    "PAQUETERÍA": "Paquetería",
    "PAQUETERIA": "Paquetería",
    "FECHA ENVÍO": "Fecha envío",
    "FECHA ENVIO": "Fecha envío",
    "FECHA ENVIÓ": "Fecha envío",
    "FECHA ENTREGA": "Fecha Entrega",
}

_WANTED_COLS = [
    "# de Orden", "SKU", "Pzas", "Precio Unitario", "Precio Total",
    "Fecha Inicio", "EN PROCESO", "Paquetería", "Fecha envío", "Fecha Entrega"
]

_ORDER_RE = re.compile(r"\d{3,}")#?\s*([0-9]{4,15})\b")

def _normalize_header(text: str) -> str:
    t = (text or "").strip()
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t)
    t_upper = t.upper()
    # quitar acentos para el mapeo laxo
    t_upper = (
        t_upper.replace("Á", "A").replace("É", "E")
        .replace("Í", "I").replace("Ó", "O").replace("Ú", "U")
    )
    return _HEADER_MAP.get(t_upper, t)

def _fetch_rows(force: bool=False) -> List[Dict[str, str]]:
    """Lee la publicación pubhtml y devuelve una lista de dicts (una por fila)."""
    global _cache
    now = time.time()
    if not force and ORDERS_AUTORELOAD != "1":
        if _cache["rows"] and (now - _cache["ts"] < ORDERS_TTL_SECONDS):
            return _cache["rows"]

    if not ORDERS_PUBHTML_URL:
        logging.warning("ORDERS_PUBHTML_URL no definida")
        return []

    try:
        r = requests.get(ORDERS_PUBHTML_URL, timeout=20, headers={"Cache-Control":"no-cache"})
        r.raise_for_status()
    except Exception as e:
        logging.exception("Error leyendo pubhtml: %s", e)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if not table:
        logging.warning("No se encontró <table> en pubhtml")
        return []

    # Extraer filas
    rows = []
    ths = [c.get_text(strip=True) for c in table.find_all("th")]
    if ths:
        headers = [ _normalize_header(h) for h in ths ]
        body_rows = table.find_all("tr")[1:]
    else:
        trs = table.find_all("tr")
        if not trs: return []
        first = trs[0]
        headers = [ _normalize_header(c.get_text(strip=True)) for c in first.find_all(["td","th"]) ]
        body_rows = trs[1:]

    for tr in body_rows:
        cells = [c.get_text(strip=True) for c in tr.find_all(["td","th"])]
        if not any(cells): 
            continue
        row = {}
        for i, val in enumerate(cells):
            if i < len(headers):
                row[headers[i]] = val
        if any(k for k in row.keys()):
            rows.append(row)

    _cache["rows"] = rows
    _cache["ts"] = now
    return rows

def detect_order_number(text: str) -> Optional[str]:
    """Devuelve el número de pedido si se encuentra en el texto, de lo contrario None."""
    if not text:
        return None
    m = _ORDER_RE.search(text)
    if not m:
        return None
    return m.group(1)

def looks_like_order_intent(text: str) -> bool:
    if not text: return False
    t = text.lower()
    keys = ("pedido", "orden", "order", "estatus", "status", "seguimiento", "rastreo", "mi compra", "mi pedido")
    return any(k in t for k in keys)

def lookup_order(order_number: str, force: bool=False) -> List[Dict[str, str]]:
    rows = _fetch_rows(force=force)
    if not rows:
        return []
    out = []
    for r in rows:
        num = r.get("# de Orden") or r.get("# Orden") or r.get("#")
        if not num: 
            for k in r.keys():
                if k.strip().lower().startswith("# de orden"):
                    num = r.get(k)
                    break
        if not num:
            continue
        digits = re.sub(r"\D+", "", str(num))
        if digits == re.sub(r"\D+", "", str(order_number)):
            item = {}
            for col in _WANTED_COLS:
                item[col] = r.get(col, "") or "—"
            out.append(item)
    return out

def render_vertical_md(rows: List[Dict[str, str]]) -> str:
    """Formato mobile-first (widget vertical): bloques por ítem con pares etiqueta: valor."""
    if not rows:
        return "No encontramos información con ese número de pedido. Verifica el número tal como aparece en tu comprobante."
    parts = []
    for i, r in enumerate(rows, 1):
        blk = []
        blk.append(f"**Artículo {i}**")
        for key in _WANTED_COLS:
            val = r.get(key, "—")
            blk.append(f"- **{key}:** {val}")
        parts.append("\n".join(blk))
    return "\n\n".join(parts)

def render_compact_table_md(rows: List[Dict[str, str]]) -> str:
    """Tabla compacta (si el widget soporta scroll horizontal)."""
    if not rows:
        return "No encontramos información con ese número de pedido."
    hdr = "| " + " | ".join(_WANTED_COLS) + " |"
    sep = "|" + "|".join(["---"]*len(_WANTED_COLS)) + "|"
    body = []
    for r in rows:
        body.append("| " + " | ".join(r.get(c, "—") for c in _WANTED_COLS) + " |")
    return "\n".join([hdr, sep] + body)

def format_for_widget(rows: List[Dict[str, str]], prefer_vertical: bool=True) -> str:
    """Devuelve el markdown ideal para tu widget en vertical."""
    if prefer_vertical:
        return render_vertical_md(rows)
    return render_compact_table_md(rows)

# --- Patched: robust order detection for hyphenated/long IDs (Amazon/Coppel/Elektra) ---
def detect_order_number(text: str) -> Optional[str]:  # type: ignore[override]
    if not text:
        return None
    runs = re.findall(r"\d{3,}", text)
    if runs:
        runs.sort(key=lambda x: (-len(x), text.find(x)))
        return runs[0]
    digits = re.sub(r"\D+", "", text)
    return digits if len(digits) >= 3 else None
