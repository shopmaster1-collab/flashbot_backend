# -*- coding: utf-8 -*-
"""Endpoints opcionales de pedidos por ORDEN_COMPRA.

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
        looks_like_bare_order,
        order_key,
        render_vertical_md,
    )
except Exception:  # pragma: no cover - fallback defensivo para despliegue
    OrdersSheetReader = None
    DEFAULT_ORDERS_PUBHTML_URL = ""

    def detect_order_number(text):
        return None

    def looks_like_bare_order(text):
        return bool(str(text or "").strip())

    def order_key(text):
        return str(text or "").strip()

    def render_vertical_md(rows, requested_order=""):
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
    """Consulta de estatus por número de pedido / ORDEN_COMPRA.

    Body aceptado: {"order":"702-..."}, {"orden":"..."}, {"order_no":"..."},
    {"folio":"..."} o {"message":"mi pedido es ..."}.
    """
    if _reader is None:
        return jsonify({"ok": False, "error": "Orders module not ready."}), 500

    data = request.get_json(silent=True) or {}
    raw = (
        data.get("order") or
        data.get("orden") or
        data.get("order_no") or
        data.get("folio") or
        data.get("message") or
        ""
    )
    raw = str(raw).strip()
    order_no = raw if looks_like_bare_order(raw) else (detect_order_number(raw) or raw)

    if not order_key(order_no):
        return jsonify({"ok": False, "error": "Número de pedido inválido."}), 400

    try:
        rows = _reader.find_by_order(order_no)
    except Exception as exc:
        logging.exception("orders lookup failed: %s", exc)
        return jsonify({"ok": False, "error": "Error consultando el reporte de pedidos."}), 500

    if not rows:
        return jsonify({
            "ok": True,
            "order": order_no,
            "answer": f"No encontramos información para el número de pedido {order_no}.",
            "rows_count": 0,
            "items": [],
        })

    return jsonify({
        "ok": True,
        "order": order_no,
        "answer": render_vertical_md(rows, order_no),
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
