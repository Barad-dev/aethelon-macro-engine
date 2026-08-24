# -*- coding: utf-8 -*-
"""
L2 Warm Store — local SQLite in WAL mode for recent transactional history.

Responsibilities
----------------
Open the AppData SQLite database safely and enable Write-Ahead Logging
so later readers (GUI, analysis, future compiled engines) can share the
file without blocking each other.

This phase does **not** create new analytical tables, migrate schema,
or rewrite ``news_engine.py``. Existing user rows are never deleted.

Default path (via ``paths.get_db_path``):
  Windows : %APPDATA%\\Aethelon\\data\\news_engine_store.db
  Other   : ~/.aethelon/data/news_engine_store.db
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

from aethelon.core.logger import get_logger
from aethelon.storage.exceptions import StorageError

__all__ = [
    "WarmStore",
    "BUSY_TIMEOUT_SECONDS",
    "default_warm_db_path",
]

log = get_logger(__name__)

PathLike = Union[str, Path]

# sqlite3.connect timeout is seconds; also applied as PRAGMA busy_timeout (ms).
BUSY_TIMEOUT_SECONDS: float = 5.0


def default_warm_db_path(*, migrate: bool = True) -> Path:
    """
    Canonical L2 database path.

    Uses the existing AppData helpers. ``migrate=True`` copies a legacy
    DB into the canonical location only when the target is missing.
    """
    from paths import get_db_path

    return get_db_path(migrate=migrate)


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
