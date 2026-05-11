# -*- coding: utf-8 -*-
"""Endpoints opcionales de pedidos por FOLIO.

El endpoint principal del proyecto está en app.py como POST /api/orders.
Este blueprint queda actualizado por compatibilidad con instalaciones que lo
registren por separado.
"""

import logging
import os
from flask import Blueprint, jsonify, request

try:
    from .orders_report import (
        DEFAULT_ORDERS_PUBHTML_URL,
        OrdersSheetReader,
        detect_order_number,
        folio_key,
        render_vertical_md,
    )
except Exception:  # pragma: no cover - fallback defensivo para despliegue
    OrdersSheetReader = None
    DEFAULT_ORDERS_PUBHTML_URL = ""

    def detect_order_number(text):
        return None

    def folio_key(text):
        return str(text or "").strip()

    def render_vertical_md(rows):
        return "Sin renderer disponible."


bp_orders = Blueprint("bp_orders", __name__)

_ORDERS_URL = (os.getenv("ORDERS_PUBHTML_URL") or DEFAULT_ORDERS_PUBHTML_URL or "").strip()
_TTL = int(os.getenv("ORDERS_TTL_SECONDS", "45") or "45")
_reader = None
if OrdersSheetReader and _ORDERS_URL:
    try:
        _reader = OrdersSheetReader(_ORDERS_URL, ttl=_TTL)
    except Exception as exc:
        logging.exception("OrdersSheetReader init failed: %s", exc)
else:
    logging.warning("orders_endpoints: missing deps or ORDERS_PUBHTML_URL")


@bp_orders.post("/api/orders/status")
def order_status():
    """Consulta de estatus por folio.

    Body aceptado: {"folio":"A1BC3"}, {"order":"A1BC3"} u {"order_no":"A1BC3"}.
    """
    if _reader is None:
        return jsonify({"ok": False, "error": "Orders module not ready."}), 500

    data = request.get_json(silent=True) or {}
    raw = (data.get("folio") or data.get("order") or data.get("order_no") or data.get("message") or "")
    raw = str(raw).strip()
    folio = detect_order_number(raw) or raw

    if not folio_key(folio):
        return jsonify({"ok": False, "error": "Folio inválido."}), 400

    try:
        rows = _reader.find_by_folio(folio)
    except Exception as exc:
        logging.exception("orders lookup failed: %s", exc)
        return jsonify({"ok": False, "error": "Error consultando el reporte de pedidos."}), 500

    if not rows:
        return jsonify({
            "ok": True,
            "folio": folio,
            "answer": f"No encontramos información para el folio {folio}.",
            "rows_count": 0,
            "items": [],
        })

    return jsonify({
        "ok": True,
        "folio": folio,
        "answer": render_vertical_md(rows),
        "rows_count": len(rows),
        "items": rows,
    })


@bp_orders.get("/api/admin/orders-ping")
def orders_ping():
    """Diagnóstico simple de la hoja publicada."""
    if _reader is None:
        return jsonify({"ok": False, "error": "Orders module not ready"}), 500
    try:
        return jsonify({"ok": True, "meta": _reader.meta(), "sample": _reader.sample(3)})
    except Exception as exc:
        logging.exception("orders ping failed: %s", exc)
        return jsonify({"ok": False, "error": repr(exc)}), 500
