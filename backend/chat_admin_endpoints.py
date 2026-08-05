# -*- coding: utf-8 -*-
"""Endpoints administrativos protegidos para revisar y exportar chats de Maxter."""

# [MAXTER CHAT STORAGE ADMIN - START: IMPORTS]
import csv
import io
import os
from datetime import datetime, timezone
from typing import Any, Dict

from flask import Response, jsonify, request
# [MAXTER CHAT STORAGE ADMIN - END: IMPORTS]


# [MAXTER CHAT STORAGE ADMIN - START: REGISTRO DE ENDPOINTS]
def register_chat_admin_endpoints(app, admin_ok, storage) -> None:
    """Registra endpoints sin cambiar la lógica principal de app.py."""

    @app.get("/api/admin/chat-storage/status")
    def admin_chat_storage_status():
        if not admin_ok(request):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return jsonify({"ok": True, "storage": storage.status()})

    @app.get("/api/admin/conversations")
    def admin_conversations():
        if not admin_ok(request):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        try:
            result = storage.list_records(
                section=(request.args.get("section") or "chat").strip().lower(),
                limit=request.args.get("limit", 100, type=int),
                offset=request.args.get("offset", 0, type=int),
                session_id=(request.args.get("session_id") or "").strip(),
                date_from=(request.args.get("date_from") or "").strip(),
                date_to=(request.args.get("date_to") or "").strip(),
            )
            return jsonify({"ok": True, **result})
        except Exception as exc:
            print(f"[MAXTER CHAT STORAGE][WARN] admin list failed: {exc}", flush=True)
            return jsonify({"ok": False, "error": "storage query failed"}), 500

    @app.get("/api/admin/conversations/export.csv")
    def admin_conversations_export():
        if not admin_ok(request):
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        section = (request.args.get("section") or "chat").strip().lower()
        section = "orders" if section == "orders" else "chat"
        session_id = (request.args.get("session_id") or "").strip()
        date_from = (request.args.get("date_from") or "").strip()
        date_to = (request.args.get("date_to") or "").strip()
        start_offset = request.args.get("offset", 0, type=int) or 0
        max_rows = min(
            max(request.args.get("limit", int(os.getenv("CHAT_EXPORT_MAX_ROWS", "50000")), type=int) or 50000, 1),
            int(os.getenv("CHAT_EXPORT_MAX_ROWS", "50000")),
        )

        try:
            rows = []
            current_offset = start_offset
            while len(rows) < max_rows:
                batch = storage.list_records(
                    section=section,
                    limit=min(1000, max_rows - len(rows)),
                    offset=current_offset,
                    session_id=session_id,
                    date_from=date_from,
                    date_to=date_to,
                )
                batch_rows = batch.get("items", [])
                rows.extend(batch_rows)
                current_offset += len(batch_rows)
                if not batch_rows or current_offset >= int(batch.get("total", 0)):
                    break
        except Exception as exc:
            print(f"[MAXTER CHAT STORAGE][WARN] admin export failed: {exc}", flush=True)
            return jsonify({"ok": False, "error": "storage export failed"}), 500

        output = io.StringIO()
        output.write("\ufeff")  # BOM para que Excel abra correctamente acentos en UTF-8.
        fieldnames = _csv_fields(section)
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(_flatten_row(section, row))

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"maxter_{section}_{stamp}.csv"
        return Response(
            output.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
# [MAXTER CHAT STORAGE ADMIN - END: REGISTRO DE ENDPOINTS]


# [MAXTER CHAT STORAGE ADMIN - START: FORMATO CSV]
def _csv_fields(section: str):
    if section == "orders":
        return [
            "id", "created_at", "session_id", "visitor_id", "request_id",
            "order_number", "found", "items_count", "answer", "items",
            "page_url", "page_title", "referrer", "user_agent",
        ]
    return [
        "id", "created_at", "session_id", "visitor_id", "request_id",
        "user_message", "assistant_message", "effective_query", "page",
        "products", "page_url", "page_title", "referrer", "user_agent",
    ]


def _flatten_row(section: str, row: Dict[str, Any]) -> Dict[str, Any]:
    flattened = dict(row)
    if section == "orders":
        flattened["items"] = _compact_json(flattened.get("items", []))
    else:
        flattened["products"] = _compact_json(flattened.get("products", []))
    return flattened


def _compact_json(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
# [MAXTER CHAT STORAGE ADMIN - END: FORMATO CSV]
