# -*- coding: utf-8 -*-
"""Utilidades comunes."""
from bs4 import BeautifulSoup


def strip_html(html: str) -> str:
if not html:
return ""
return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def money(v):
try:
return f"${float(v):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
except Exception:
return str(v)