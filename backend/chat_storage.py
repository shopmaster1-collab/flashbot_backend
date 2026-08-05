# -*- coding: utf-8 -*-
"""
Persistencia independiente para conversaciones escritas de Maxter.

Este módulo NO modifica ni comparte la base de datos del catálogo. Su objetivo es
registrar de forma tolerante a fallos las consultas realizadas desde las secciones
"Chat" y "Pedidos" del widget.
"""

# [MAXTER CHAT STORAGE - START: IMPORTS Y CONFIGURACIÓN]
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # PostgreSQL es opcional si se usa el fallback SQLite local.
    psycopg2 = None
    RealDictCursor = None
# [MAXTER CHAT STORAGE - END: IMPORTS Y CONFIGURACIÓN]


# [MAXTER CHAT STORAGE - START: UTILIDADES SEGURAS]
def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_text(value: Any, max_length: int = 20000) -> str:
    text = "" if value is None else str(value)
    return text[:max_length]


def _clean_identifier(value: Any, max_length: int = 160) -> str:
    text = _clean_text(value, max_length).strip()
    return text


def _json_text(value: Any, max_length: int = 200000) -> str:
    try:
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        serialized = "[]" if isinstance(value, list) else "{}"
    if len(serialized) <= max_length:
        return serialized
    # Nunca se corta JSON a la mitad. Se conserva una marca válida de truncamiento.
    return json.dumps({"truncated": True, "original_length": len(serialized)}, ensure_ascii=False)


def _safe_json_loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback
# [MAXTER CHAT STORAGE - END: UTILIDADES SEGURAS]


class ChatStorage:
    """Almacena conversaciones sin interrumpir el funcionamiento principal del bot."""

    # [MAXTER CHAT STORAGE - START: INICIALIZACIÓN]
    def __init__(self) -> None:
        self.enabled = _env_bool("CHAT_STORAGE_ENABLED", True)
        self.database_url = (os.getenv("DATABASE_URL") or "").strip()
        default_sqlite = os.path.join(os.path.dirname(__file__), "data", "maxter_chat_history.sqlite3")
        self.sqlite_path = os.path.abspath(os.getenv("CHAT_DB_PATH") or default_sqlite)
        self.backend = "disabled"
        self.last_error = ""
        self._sqlite_lock = threading.RLock()

        if not self.enabled:
            print("[MAXTER CHAT STORAGE] disabled by CHAT_STORAGE_ENABLED", flush=True)
            return

        if self.database_url and self.database_url.startswith(("postgres://", "postgresql://")):
            if psycopg2 is not None:
                self.backend = "postgresql"
            else:
                self.last_error = "DATABASE_URL está configurado, pero psycopg2 no está instalado."
                print(f"[MAXTER CHAT STORAGE][WARN] {self.last_error} Se usará SQLite.", flush=True)
                self.backend = "sqlite"
        else:
            self.backend = "sqlite"

        try:
            self._ensure_schema()
            print(f"[MAXTER CHAT STORAGE] ready backend={self.backend}", flush=True)
        except Exception as exc:
            self.last_error = str(exc)
            self.backend = "unavailable"
            print(f"[MAXTER CHAT STORAGE][WARN] initialization failed: {exc}", flush=True)
    # [MAXTER CHAT STORAGE - END: INICIALIZACIÓN]

    # [MAXTER CHAT STORAGE - START: CONEXIONES]
    def _postgres_connect(self):
        if psycopg2 is None:
            raise RuntimeError("psycopg2 no está disponible")
        return psycopg2.connect(self.database_url, connect_timeout=8)

    def _sqlite_connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.sqlite_path), exist_ok=True)
        conn = sqlite3.connect(self.sqlite_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except Exception:
            pass
        return conn
    # [MAXTER CHAT STORAGE - END: CONEXIONES]

    # [MAXTER CHAT STORAGE - START: ESQUEMA DE BASE DE DATOS]
    def _ensure_schema(self) -> None:
        if self.backend == "postgresql":
            self._ensure_postgresql_schema()
        elif self.backend == "sqlite":
            self._ensure_sqlite_schema()

    def _ensure_sqlite_schema(self) -> None:
        with self._sqlite_lock:
            conn = self._sqlite_connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS maxter_chat_sessions (
                        session_id TEXT PRIMARY KEY,
                        visitor_id TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        last_activity_at TEXT NOT NULL,
                        first_page_url TEXT,
                        last_page_url TEXT,
                        referrer TEXT,
                        user_agent TEXT
                    );

                    CREATE TABLE IF NOT EXISTS maxter_chat_exchanges (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request_id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL,
                        visitor_id TEXT NOT NULL,
                        user_message TEXT NOT NULL,
                        assistant_message TEXT NOT NULL,
                        effective_query TEXT,
                        page INTEGER NOT NULL DEFAULT 1,
                        products_json TEXT NOT NULL DEFAULT '[]',
                        page_url TEXT,
                        page_title TEXT,
                        referrer TEXT,
                        user_agent TEXT,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS maxter_order_queries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request_id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL,
                        visitor_id TEXT NOT NULL,
                        order_number TEXT NOT NULL,
                        found INTEGER NOT NULL DEFAULT 0,
                        items_count INTEGER NOT NULL DEFAULT 0,
                        answer TEXT,
                        items_json TEXT NOT NULL DEFAULT '[]',
                        page_url TEXT,
                        page_title TEXT,
                        referrer TEXT,
                        user_agent TEXT,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_maxter_chat_exchanges_session
                        ON maxter_chat_exchanges(session_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_maxter_chat_exchanges_created
                        ON maxter_chat_exchanges(created_at);
                    CREATE INDEX IF NOT EXISTS idx_maxter_order_queries_session
                        ON maxter_order_queries(session_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_maxter_order_queries_created
                        ON maxter_order_queries(created_at);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def _ensure_postgresql_schema(self) -> None:
        conn = self._postgres_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS maxter_chat_sessions (
                        session_id VARCHAR(160) PRIMARY KEY,
                        visitor_id VARCHAR(160) NOT NULL,
                        started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_activity_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        first_page_url TEXT,
                        last_page_url TEXT,
                        referrer TEXT,
                        user_agent TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS maxter_chat_exchanges (
                        id BIGSERIAL PRIMARY KEY,
                        request_id VARCHAR(200) NOT NULL UNIQUE,
                        session_id VARCHAR(160) NOT NULL,
                        visitor_id VARCHAR(160) NOT NULL,
                        user_message TEXT NOT NULL,
                        assistant_message TEXT NOT NULL,
                        effective_query TEXT,
                        page INTEGER NOT NULL DEFAULT 1,
                        products_json TEXT NOT NULL DEFAULT '[]',
                        page_url TEXT,
                        page_title TEXT,
                        referrer TEXT,
                        user_agent TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS maxter_order_queries (
                        id BIGSERIAL PRIMARY KEY,
                        request_id VARCHAR(200) NOT NULL UNIQUE,
                        session_id VARCHAR(160) NOT NULL,
                        visitor_id VARCHAR(160) NOT NULL,
                        order_number TEXT NOT NULL,
                        found BOOLEAN NOT NULL DEFAULT FALSE,
                        items_count INTEGER NOT NULL DEFAULT 0,
                        answer TEXT,
                        items_json TEXT NOT NULL DEFAULT '[]',
                        page_url TEXT,
                        page_title TEXT,
                        referrer TEXT,
                        user_agent TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_maxter_chat_exchanges_session ON maxter_chat_exchanges(session_id, created_at)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_maxter_chat_exchanges_created ON maxter_chat_exchanges(created_at)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_maxter_order_queries_session ON maxter_order_queries(session_id, created_at)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_maxter_order_queries_created ON maxter_order_queries(created_at)"
                )
            conn.commit()
        finally:
            conn.close()
    # [MAXTER CHAT STORAGE - END: ESQUEMA DE BASE DE DATOS]

    # [MAXTER CHAT STORAGE - START: SESIONES]
    def _upsert_session(self, metadata: Dict[str, Any], created_at: str) -> None:
        session_id = _clean_identifier(metadata.get("session_id"))
        visitor_id = _clean_identifier(metadata.get("visitor_id"))
        if not session_id or not visitor_id:
            return

        page_url = _clean_text(metadata.get("page_url"), 4000)
        referrer = _clean_text(metadata.get("referrer"), 4000)
        user_agent = _clean_text(metadata.get("user_agent"), 2000)

        if self.backend == "postgresql":
            conn = self._postgres_connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO maxter_chat_sessions (
                            session_id, visitor_id, started_at, last_activity_at,
                            first_page_url, last_page_url, referrer, user_agent
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (session_id) DO UPDATE SET
                            visitor_id = EXCLUDED.visitor_id,
                            last_activity_at = EXCLUDED.last_activity_at,
                            last_page_url = EXCLUDED.last_page_url,
                            referrer = CASE WHEN maxter_chat_sessions.referrer IS NULL OR maxter_chat_sessions.referrer = ''
                                THEN EXCLUDED.referrer ELSE maxter_chat_sessions.referrer END,
                            user_agent = EXCLUDED.user_agent
                        """,
                        (session_id, visitor_id, created_at, created_at, page_url, page_url, referrer, user_agent),
                    )
                conn.commit()
            finally:
                conn.close()
            return

        with self._sqlite_lock:
            conn = self._sqlite_connect()
            try:
                conn.execute(
                    """
                    INSERT INTO maxter_chat_sessions (
                        session_id, visitor_id, started_at, last_activity_at,
                        first_page_url, last_page_url, referrer, user_agent
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        visitor_id = excluded.visitor_id,
                        last_activity_at = excluded.last_activity_at,
                        last_page_url = excluded.last_page_url,
                        referrer = CASE WHEN maxter_chat_sessions.referrer IS NULL OR maxter_chat_sessions.referrer = ''
                            THEN excluded.referrer ELSE maxter_chat_sessions.referrer END,
                        user_agent = excluded.user_agent
                    """,
                    (session_id, visitor_id, created_at, created_at, page_url, page_url, referrer, user_agent),
                )
                conn.commit()
            finally:
                conn.close()
    # [MAXTER CHAT STORAGE - END: SESIONES]

    # [MAXTER CHAT STORAGE - START: REGISTRO DE CHAT]
    def record_chat_exchange(
        self,
        metadata: Dict[str, Any],
        user_message: str,
        assistant_message: str,
        effective_query: str = "",
        page: int = 1,
        products: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        if not self.enabled or self.backend not in {"postgresql", "sqlite"}:
            return False

        request_id = _clean_identifier(metadata.get("request_id"), 200)
        session_id = _clean_identifier(metadata.get("session_id"))
        visitor_id = _clean_identifier(metadata.get("visitor_id"))
        user_message = _clean_text(user_message).strip()
        if not request_id or not session_id or not visitor_id or not user_message:
            return False

        created_at = _utc_now_iso()
        row = (
            request_id,
            session_id,
            visitor_id,
            user_message,
            _clean_text(assistant_message),
            _clean_text(effective_query),
            max(1, int(page or 1)),
            _json_text(products or []),
            _clean_text(metadata.get("page_url"), 4000),
            _clean_text(metadata.get("page_title"), 1000),
            _clean_text(metadata.get("referrer"), 4000),
            _clean_text(metadata.get("user_agent"), 2000),
            created_at,
        )

        try:
            self._upsert_session(metadata, created_at)
            if self.backend == "postgresql":
                conn = self._postgres_connect()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO maxter_chat_exchanges (
                                request_id, session_id, visitor_id, user_message,
                                assistant_message, effective_query, page, products_json,
                                page_url, page_title, referrer, user_agent, created_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (request_id) DO NOTHING
                            """,
                            row,
                        )
                    conn.commit()
                finally:
                    conn.close()
            else:
                with self._sqlite_lock:
                    conn = self._sqlite_connect()
                    try:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO maxter_chat_exchanges (
                                request_id, session_id, visitor_id, user_message,
                                assistant_message, effective_query, page, products_json,
                                page_url, page_title, referrer, user_agent, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            row,
                        )
                        conn.commit()
                    finally:
                        conn.close()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            print(f"[MAXTER CHAT STORAGE][WARN] chat record failed: {exc}", flush=True)
            return False
    # [MAXTER CHAT STORAGE - END: REGISTRO DE CHAT]

    # [MAXTER CHAT STORAGE - START: REGISTRO DE PEDIDOS]
    def record_order_query(
        self,
        metadata: Dict[str, Any],
        order_number: str,
        found: bool,
        items: Optional[List[Dict[str, Any]]] = None,
        answer: str = "",
    ) -> bool:
        if not self.enabled or self.backend not in {"postgresql", "sqlite"}:
            return False

        request_id = _clean_identifier(metadata.get("request_id"), 200)
        session_id = _clean_identifier(metadata.get("session_id"))
        visitor_id = _clean_identifier(metadata.get("visitor_id"))
        order_number = _clean_text(order_number, 500).strip()
        if not request_id or not session_id or not visitor_id or not order_number:
            return False

        created_at = _utc_now_iso()
        items = items or []
        common_row = (
            request_id,
            session_id,
            visitor_id,
            order_number,
            bool(found),
            len(items),
            _clean_text(answer),
            _json_text(items),
            _clean_text(metadata.get("page_url"), 4000),
            _clean_text(metadata.get("page_title"), 1000),
            _clean_text(metadata.get("referrer"), 4000),
            _clean_text(metadata.get("user_agent"), 2000),
            created_at,
        )

        try:
            self._upsert_session(metadata, created_at)
            if self.backend == "postgresql":
                conn = self._postgres_connect()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO maxter_order_queries (
                                request_id, session_id, visitor_id, order_number,
                                found, items_count, answer, items_json,
                                page_url, page_title, referrer, user_agent, created_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (request_id) DO NOTHING
                            """,
                            common_row,
                        )
                    conn.commit()
                finally:
                    conn.close()
            else:
                sqlite_row = list(common_row)
                sqlite_row[4] = 1 if found else 0
                with self._sqlite_lock:
                    conn = self._sqlite_connect()
                    try:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO maxter_order_queries (
                                request_id, session_id, visitor_id, order_number,
                                found, items_count, answer, items_json,
                                page_url, page_title, referrer, user_agent, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            tuple(sqlite_row),
                        )
                        conn.commit()
                    finally:
                        conn.close()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            print(f"[MAXTER CHAT STORAGE][WARN] order record failed: {exc}", flush=True)
            return False
    # [MAXTER CHAT STORAGE - END: REGISTRO DE PEDIDOS]

    # [MAXTER CHAT STORAGE - START: CONSULTA ADMINISTRATIVA]
    def status(self) -> Dict[str, Any]:
        counts = {"chat_exchanges": 0, "order_queries": 0, "sessions": 0}
        if self.backend in {"postgresql", "sqlite"}:
            try:
                counts = self._count_rows()
            except Exception as exc:
                self.last_error = str(exc)
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "persistent_recommended": self.backend == "postgresql",
            "sqlite_path": self.sqlite_path if self.backend == "sqlite" else None,
            "counts": counts,
            "last_error": self.last_error or None,
        }

    def _count_rows(self) -> Dict[str, int]:
        table_map = {
            "chat_exchanges": "maxter_chat_exchanges",
            "order_queries": "maxter_order_queries",
            "sessions": "maxter_chat_sessions",
        }
        result: Dict[str, int] = {}
        if self.backend == "postgresql":
            conn = self._postgres_connect()
            try:
                with conn.cursor() as cur:
                    for key, table in table_map.items():
                        cur.execute(f"SELECT COUNT(*) FROM {table}")
                        result[key] = int(cur.fetchone()[0])
            finally:
                conn.close()
            return result

        with self._sqlite_lock:
            conn = self._sqlite_connect()
            try:
                for key, table in table_map.items():
                    result[key] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            finally:
                conn.close()
        return result

    def list_records(
        self,
        section: str,
        limit: int = 100,
        offset: int = 0,
        session_id: str = "",
        date_from: str = "",
        date_to: str = "",
    ) -> Dict[str, Any]:
        section = "orders" if section == "orders" else "chat"
        limit = min(max(int(limit or 100), 1), 1000)
        offset = max(int(offset or 0), 0)
        session_id = _clean_identifier(session_id)
        date_from = _clean_text(date_from, 64).strip()
        date_to = _clean_text(date_to, 64).strip()

        if self.backend == "postgresql":
            rows, total = self._list_postgresql(section, limit, offset, session_id, date_from, date_to)
        elif self.backend == "sqlite":
            rows, total = self._list_sqlite(section, limit, offset, session_id, date_from, date_to)
        else:
            rows, total = [], 0

        for row in rows:
            if section == "chat":
                row["products"] = _safe_json_loads(row.pop("products_json", "[]"), [])
            else:
                row["items"] = _safe_json_loads(row.pop("items_json", "[]"), [])
                row["found"] = bool(row.get("found"))
        return {"section": section, "total": total, "limit": limit, "offset": offset, "items": rows}

    def _where_clause(
        self,
        placeholder: str,
        session_id: str,
        date_from: str,
        date_to: str,
    ) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if session_id:
            clauses.append(f"session_id = {placeholder}")
            params.append(session_id)
        if date_from:
            clauses.append(f"created_at >= {placeholder}")
            params.append(date_from)
        if date_to:
            clauses.append(f"created_at <= {placeholder}")
            params.append(date_to)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def _list_sqlite(
        self,
        section: str,
        limit: int,
        offset: int,
        session_id: str,
        date_from: str,
        date_to: str,
    ) -> Tuple[List[Dict[str, Any]], int]:
        table = "maxter_order_queries" if section == "orders" else "maxter_chat_exchanges"
        where, params = self._where_clause("?", session_id, date_from, date_to)
        with self._sqlite_lock:
            conn = self._sqlite_connect()
            try:
                total = int(conn.execute(f"SELECT COUNT(*) FROM {table}{where}", params).fetchone()[0])
                query = f"SELECT * FROM {table}{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
                rows = [dict(row) for row in conn.execute(query, params + [limit, offset]).fetchall()]
            finally:
                conn.close()
        return rows, total

    def _list_postgresql(
        self,
        section: str,
        limit: int,
        offset: int,
        session_id: str,
        date_from: str,
        date_to: str,
    ) -> Tuple[List[Dict[str, Any]], int]:
        table = "maxter_order_queries" if section == "orders" else "maxter_chat_exchanges"
        where, params = self._where_clause("%s", session_id, date_from, date_to)
        conn = self._postgres_connect()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"SELECT COUNT(*) AS total FROM {table}{where}", params)
                total = int(cur.fetchone()["total"])
                cur.execute(
                    f"SELECT * FROM {table}{where} ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                    params + [limit, offset],
                )
                rows = [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()
        for row in rows:
            if row.get("created_at") is not None:
                row["created_at"] = row["created_at"].isoformat()
        return rows, total
    # [MAXTER CHAT STORAGE - END: CONSULTA ADMINISTRATIVA]
