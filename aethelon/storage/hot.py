# -*- coding: utf-8 -*-
"""
L1 Hot Cache — in-memory RAM store for live UI snapshots.

Responsibilities
----------------
Hold the latest dashboard-facing payloads in process memory so a live
UI can read them without hitting SQLite. Data is volatile: it is gone
when the process exits.

This phase does **not** implement eviction, TTL, or size limits.
Those belong to a later Stage D prompt.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from aethelon.core.logger import get_logger
from aethelon.storage.timeutil import utc_now_iso_z

__all__ = ["HotCache"]

log = get_logger(__name__)


class HotCache:
    """
    Thread-safe in-memory key/value store (L1).

    Keys are non-empty strings. Values are stored as given; callers
    should keep them JSON-serializable if they will later cross IPC.
    ``updated_at`` is ISO 8601 UTC Z and changes on every mutation.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, Any] = {}
        self._updated_at: Optional[str] = None

    def put(self, key: str, value: Any) -> None:
        """Insert or replace ``key``. Empty keys are rejected."""
        _require_key(key)
        with self._lock:
            self._items[key] = value
            self._updated_at = utc_now_iso_z()
        log.debug("L1 put key=%s", key)

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for ``key``, or ``default`` if missing."""
        _require_key(key)
        with self._lock:
            return self._items.get(key, default)

    def delete(self, key: str) -> bool:
        """Remove ``key``. Returns True if it was present."""
        _require_key(key)
        with self._lock:
            existed = key in self._items
            if existed:
                del self._items[key]
                self._updated_at = utc_now_iso_z()
        if existed:
            log.debug("L1 delete key=%s", key)
        return existed

    def snapshot(self) -> dict[str, Any]:
        """Shallow copy of the current map. Safe for the caller to mutate."""
        with self._lock:
            return dict(self._items)

    def clear(self) -> None:
        """Drop every in-memory entry. Does not touch L2 or L3."""
        with self._lock:
            self._items.clear()
            self._updated_at = utc_now_iso_z()
        log.debug("L1 cleared")

    def keys(self) -> list[str]:
        """Current keys, in insertion order."""
        with self._lock:
            return list(self._items.keys())

    def updated_at(self) -> Optional[str]:
        """ISO 8601 UTC Z of the last mutation, or None if never written."""
        with self._lock:
            return self._updated_at

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __repr__(self) -> str:
        return f"HotCache(size={len(self)}, updated_at={self.updated_at()!r})"


def _require_key(key: str) -> None:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("hot cache key must be a non-empty string")
