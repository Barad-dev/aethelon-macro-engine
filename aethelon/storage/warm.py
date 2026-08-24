# -*- coding: utf-8 -*-
"""
L2 Warm Store — local SQLite in WAL mode for recent transactional history.

Responsibilities
----------------
Open the AppData SQLite database safely, enable Write-Ahead Logging,
report health, and run short-lived parameterized SQL.

``query`` is read-only (SELECT / WITH / PRAGMA / EXPLAIN).
``execute`` allows INSERT / UPDATE / REPLACE / CREATE TABLE|INDEX only.
DROP, DELETE, TRUNCATE, ALTER, and multi-statement SQL are refused so
existing analytical user data cannot be wiped through these helpers.

This phase does **not** rewrite schema or ``news_engine.py``.

Default path (via ``paths.get_db_path``):
  Windows : %APPDATA%\\Aethelon\\data\\news_engine_store.db
  Other   : ~/.aethelon/data/news_engine_store.db
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence, Union

from aethelon.core.logger import get_logger
from aethelon.storage.exceptions import StorageError
from aethelon.storage.timeutil import utc_now_iso_z

__all__ = [
    "BUSY_TIMEOUT_SECONDS",
    "WarmHealth",
    "WarmStore",
    "default_warm_db_path",
]

log = get_logger(__name__)

PathLike = Union[str, Path]
SqlParams = Union[Sequence[Any], Mapping[str, Any]]

# sqlite3.connect timeout is seconds; also applied as PRAGMA busy_timeout (ms).
BUSY_TIMEOUT_SECONDS: float = 5.0

_READ_OK = frozenset({"select", "with", "pragma", "explain"})
_WRITE_OK = frozenset({"insert", "update", "replace", "create"})
_WRITE_BLOCK = frozenset(
    {
        "drop",
        "delete",
        "truncate",
        "alter",
        "attach",
        "detach",
        "vacuum",
        "reindex",
    }
)


def default_warm_db_path(*, migrate: bool = True) -> Path:
    """
    Canonical L2 database path.

    Uses the existing AppData helpers. ``migrate=True`` copies a legacy
    DB into the canonical location only when the target is missing.
    """
    from paths import get_db_path

    return get_db_path(migrate=migrate)


@dataclass(frozen=True)
class WarmHealth:
    """Point-in-time L2 journal / reachability check."""

    db_path: str
    exists: bool
    size_bytes: int
    journal_mode: str
    wal_enabled: bool
    readable: bool
    table_count: int
    checked_at: str
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True when the file is readable and journal mode is WAL."""
        return self.readable and self.wal_enabled


class WarmStore:
    """
    Safe open-path for the AppData SQLite store (L2).

    Connections are short-lived: call ``open_connection()`` or use
    ``connection()`` as a context manager. WAL is requested on every
    new connection; the journal mode is stored in the database file.
    """

    def __init__(
        self,
        db_path: Optional[PathLike] = None,
        *,
        migrate: bool = True,
        busy_timeout: float = BUSY_TIMEOUT_SECONDS,
    ) -> None:
        self._path = Path(db_path).expanduser() if db_path is not None else default_warm_db_path(migrate=migrate)
        self._busy_timeout = float(busy_timeout)
        self._lock = threading.Lock()
        self._wal_logged = False

    @property
    def db_path(self) -> Path:
        """Resolved SQLite file path. The file is not created until first open."""
        return self._path

    def open_connection(self) -> sqlite3.Connection:
        """
        Open a WAL-mode connection.

        The caller must close it. Prefer ``connection()`` in new code.
        Does not DROP, DELETE, or otherwise mutate user tables.
        """
        path = self._prepare_path()
        try:
            conn = sqlite3.connect(
                str(path),
                timeout=self._busy_timeout,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise StorageError(
                f"could not open L2 database at {path}",
                details={"path": str(path), "error": str(exc)},
            ) from exc

        try:
            mode = _apply_runtime_pragmas(conn, busy_timeout_s=self._busy_timeout)
        except sqlite3.Error as exc:
            conn.close()
            raise StorageError(
                f"could not configure L2 connection at {path}",
                details={"path": str(path), "error": str(exc)},
            ) from exc

        self._log_wal_once(mode)
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager around ``open_connection()``; always closes."""
        conn = self.open_connection()
        try:
            yield conn
        finally:
            conn.close()

    def journal_mode(self) -> str:
        """Current SQLite journal mode (expected: ``wal``). Opens a connection."""
        with self.connection() as conn:
            return _read_journal_mode(conn)

    def ensure_wal(self) -> str:
        """
        Open once, request WAL, return the resulting journal mode.

        Safe to call on an existing user database: it only changes the
        journal mode, not table contents.
        """
        return self.journal_mode()

    def health(self) -> WarmHealth:
        """
        Journal and reachability check.

        If the file does not exist yet, it is **not** created. Existing
        files are opened (WAL requested) and probed with ``SELECT 1``.
        """
        checked_at = utc_now_iso_z()
        path = self._path
        exists = path.is_file()
        size_bytes = 0
        if exists:
            try:
                size_bytes = int(path.stat().st_size)
            except OSError:
                size_bytes = 0
        if not exists:
            return WarmHealth(
                db_path=str(path),
                exists=False,
                size_bytes=0,
                journal_mode="",
                wal_enabled=False,
                readable=False,
                table_count=0,
                checked_at=checked_at,
            )

        try:
            with self.connection() as conn:
                mode = _read_journal_mode(conn)
                conn.execute("SELECT 1").fetchone()
                table_count = _count_user_tables(conn)
            return WarmHealth(
                db_path=str(path),
                exists=True,
                size_bytes=size_bytes,
                journal_mode=mode,
                wal_enabled=mode == "wal",
                readable=True,
                table_count=table_count,
                checked_at=checked_at,
            )
        except (sqlite3.Error, StorageError, OSError) as exc:
            log.warning("L2 health check failed at %s: %s", path, exc)
            return WarmHealth(
                db_path=str(path),
                exists=True,
                size_bytes=size_bytes,
                journal_mode="",
                wal_enabled=False,
                readable=False,
                table_count=0,
                checked_at=checked_at,
                error=str(exc),
            )

    def query(
        self,
        sql: str,
        params: SqlParams = (),
    ) -> list[tuple[Any, ...]]:
        """
        Run a read-only statement on a short-lived connection.

        Allowed: SELECT, WITH, PRAGMA, EXPLAIN. Multi-statement SQL is
        refused. Returns rows as tuples.
        """
        _assert_read_sql(sql)
        log.debug("L2 query %s", _sql_preview(sql))
        try:
            with self.connection() as conn:
                cur = conn.execute(sql, params)
                return list(cur.fetchall())
        except sqlite3.Error as exc:
            raise StorageError(
                "L2 query failed",
                details={"sql": _sql_preview(sql), "error": str(exc)},
            ) from exc

    def execute(self, sql: str, params: SqlParams = ()) -> int:
        """
        Run a guarded write on a short-lived connection and commit.

        Allowed: INSERT, UPDATE, REPLACE, CREATE TABLE, CREATE INDEX.
        DROP / DELETE / TRUNCATE / ALTER and multi-statement SQL are
        refused. Returns ``cursor.rowcount`` (``-1`` when SQLite does
        not report a count, e.g. CREATE).
        """
        _assert_write_sql(sql)
        log.debug("L2 execute %s", _sql_preview(sql))
        try:
            with self.connection() as conn:
                cur = conn.execute(sql, params)
                conn.commit()
                return int(cur.rowcount)
        except sqlite3.Error as exc:
            raise StorageError(
                "L2 execute failed",
                details={"sql": _sql_preview(sql), "error": str(exc)},
            ) from exc

    def table_names(self) -> list[str]:
        """User table names (excludes ``sqlite_*``). Empty if the file is new."""
        if not self._path.is_file():
            return []
        rows = self.query(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        return [str(row[0]) for row in rows]

    def _prepare_path(self) -> Path:
        path = self._path
        if path.exists() and path.is_dir():
            raise StorageError(
                f"L2 database path is a directory: {path}",
                details={"path": str(path)},
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(
                f"could not create L2 parent directory {path.parent}",
                details={"path": str(path), "error": str(exc)},
            ) from exc
        return path

    def _log_wal_once(self, mode: str) -> None:
        with self._lock:
            if self._wal_logged:
                return
            self._wal_logged = True
        if mode == "wal":
            log.info("L2 Warm Store opened with WAL at %s", self._path)
        else:
            log.warning(
                "L2 Warm Store journal_mode=%s (wanted wal) at %s",
                mode or "unknown",
                self._path,
            )

    def __repr__(self) -> str:
        return f"WarmStore(db_path={str(self._path)!r})"


def _apply_runtime_pragmas(conn: sqlite3.Connection, *, busy_timeout_s: float) -> str:
    """
    Per-connection pragmas. WAL is the only durable file-format change.

    ``foreign_keys`` and ``busy_timeout`` are connection-local and match
    the existing news_engine open path. No schema statements here.
    """
    timeout_ms = max(0, int(busy_timeout_s * 1000))
    conn.execute(f"PRAGMA busy_timeout={timeout_ms};")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        row = conn.execute("PRAGMA journal_mode=WAL;").fetchone()
        mode = str(row[0]).lower() if row else ""
    except sqlite3.Error as exc:
        log.warning("PRAGMA journal_mode=WAL failed: %s", exc)
        mode = _read_journal_mode(conn)
    return mode


def _read_journal_mode(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA journal_mode;").fetchone()
    except sqlite3.Error:
        return ""
    if not row:
        return ""
    return str(row[0]).lower()


def _count_user_tables(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()
    except sqlite3.Error:
        return 0
    if not row or row[0] is None:
        return 0
    return int(row[0])


def _assert_read_sql(sql: str) -> None:
    _assert_single_statement(sql)
    keyword = _leading_keyword(sql)
    if keyword not in _READ_OK:
        raise StorageError(
            "L2 query only allows SELECT / WITH / PRAGMA / EXPLAIN",
            details={"keyword": keyword or "", "sql": _sql_preview(sql)},
        )


def _assert_write_sql(sql: str) -> None:
    _assert_single_statement(sql)
    tokens = _leading_tokens(sql, 3)
    keyword = tokens[0] if tokens else ""
    if keyword in _WRITE_BLOCK:
        raise StorageError(
            f"L2 execute refuses {keyword.upper()} statements",
            details={"keyword": keyword, "sql": _sql_preview(sql)},
        )
    if keyword not in _WRITE_OK:
        raise StorageError(
            "L2 execute only allows INSERT / UPDATE / REPLACE / CREATE TABLE|INDEX",
            details={"keyword": keyword or "", "sql": _sql_preview(sql)},
        )
    if keyword == "create" and not _create_is_table_or_index(tokens):
        raise StorageError(
            "L2 execute only allows CREATE TABLE or CREATE INDEX",
            details={"sql": _sql_preview(sql)},
        )


def _create_is_table_or_index(tokens: list[str]) -> bool:
    if len(tokens) < 2:
        return False
    second = tokens[1]
    if second in {"table", "index"}:
        return True
    return second == "unique" and len(tokens) >= 3 and tokens[2] == "index"


def _assert_single_statement(sql: str) -> None:
    if not isinstance(sql, str) or not sql.strip():
        raise StorageError("SQL statement is empty")
    body = sql.strip()
    if body.endswith(";"):
        body = body[:-1]
    if ";" in body:
        raise StorageError(
            "L2 helpers refuse multi-statement SQL",
            details={"sql": _sql_preview(sql)},
        )


def _leading_keyword(sql: str) -> str:
    tokens = _leading_tokens(sql, 1)
    return tokens[0] if tokens else ""


def _leading_tokens(sql: str, count: int) -> list[str]:
    s = _lstrip_sql_comments(sql)
    tokens: list[str] = []
    i = 0
    n = len(s)
    while i < n and len(tokens) < count:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        if not (s[i].isalnum() or s[i] == "_"):
            break
        start = i
        i += 1
        while i < n and (s[i].isalnum() or s[i] == "_"):
            i += 1
        tokens.append(s[start:i].lower())
    return tokens


def _lstrip_sql_comments(sql: str) -> str:
    s = sql.strip()
    while s:
        if s.startswith("--"):
            nl = s.find("\n")
            if nl < 0:
                return ""
            s = s[nl + 1 :].lstrip()
            continue
        if s.startswith("/*"):
            end = s.find("*/")
            if end < 0:
                return ""
            s = s[end + 2 :].lstrip()
            continue
        break
    return s


def _sql_preview(sql: str, limit: int = 120) -> str:
    text = " ".join(sql.split())
    if len(text) <= limit:
        return text
    return text[: limit] + "..."
