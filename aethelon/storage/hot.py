# -*- coding: utf-8 -*-
"""
L1 Hot Cache — in-memory RAM store for live UI snapshots.

Responsibilities
----------------
Hold the latest dashboard-facing payloads in process memory so a live
UI can read them without hitting SQLite. Data is volatile: it is gone
when the process exits.

Keys may be flat (``put`` / ``get``) or namespaced (``dashboard/live``).
``put_latest`` / ``get_latest`` wrap a payload with an ISO 8601 UTC Z
``as_of`` stamp for dashboard-style snapshots.

This phase does **not** implement eviction, TTL, or size limits.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Any, Optional

from aethelon.core.logger import get_logger
from aethelon.storage.timeutil import utc_now_iso_z

__all__ = [
    "DASHBOARD_NAME",
    "DASHBOARD_NAMESPACE",
    "HotCache",
    "LatestSnapshot",
    "cache_key",
]

log = get_logger(__name__)

DASHBOARD_NAMESPACE = "dashboard"
DASHBOARD_NAME = "live"
_SEP = "/"


@dataclass(frozen=True)
class LatestSnapshot:
    """One named L1 snapshot: payload plus UTC Z timestamp."""

    as_of: str
    name: str
    namespace: str
    payload: Any

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict. Payload is shallow-copied."""
        data = asdict(self)
        data["payload"] = _shallow_copy(self.payload)
        return data


def cache_key(namespace: str, name: str) -> str:
    """
    Build a namespaced key ``namespace/name``.

    Both parts must be non-empty and must not contain ``/``.
    """
    ns = namespace.strip() if isinstance(namespace, str) else ""
    nm = name.strip() if isinstance(name, str) else ""
    if not ns or not nm:
        raise ValueError("namespace and name must be non-empty strings")
    if _SEP in ns or _SEP in nm:
        raise ValueError("namespace and name must not contain '/'")
    return f"{ns}{_SEP}{nm}"


class HotCache:
    """
    Thread-safe in-memory key/value store (L1).

    Flat keys still work. Prefer ``put_latest`` / ``get_dashboard`` for
    live UI blobs so every snapshot carries an ``as_of`` stamp.
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

    def put_ns(self, namespace: str, name: str, value: Any) -> str:
        """Store ``value`` at ``namespace/name``. Returns the full key."""
        key = cache_key(namespace, name)
        self.put(key, value)
        return key

    def get_ns(self, namespace: str, name: str, default: Any = None) -> Any:
        """Read ``namespace/name``."""
        return self.get(cache_key(namespace, name), default)

    def delete_ns(self, namespace: str, name: str) -> bool:
        """Remove ``namespace/name``. Returns True if it was present."""
        return self.delete(cache_key(namespace, name))

    def keys_in(self, namespace: str) -> list[str]:
        """Keys that belong to ``namespace`` (prefix ``namespace/``)."""
        prefix = _namespace_prefix(namespace)
        with self._lock:
            return [k for k in self._items if k.startswith(prefix)]

    def clear_namespace(self, namespace: str) -> int:
        """
        Drop keys in one namespace. Other namespaces are left alone.

        Returns the number of keys removed. Does not touch L2 or L3.
        """
        prefix = _namespace_prefix(namespace)
        with self._lock:
            doomed = [k for k in self._items if k.startswith(prefix)]
            for key in doomed:
                del self._items[key]
            if doomed:
                self._updated_at = utc_now_iso_z()
        if doomed:
            log.debug("L1 cleared namespace=%s count=%s", namespace.strip(), len(doomed))
        return len(doomed)

    def put_latest(
        self,
        name: str,
        payload: Any,
        *,
        namespace: str = DASHBOARD_NAMESPACE,
    ) -> LatestSnapshot:
        """
        Store a named snapshot with an ``as_of`` UTC Z stamp.

        Dict and list payloads are shallow-copied so later caller
        mutation does not leak into the cache.
        """
        record = LatestSnapshot(
            as_of=utc_now_iso_z(),
            name=name.strip(),
            namespace=namespace.strip(),
            payload=_shallow_copy(payload),
        )
        # to_dict copies payload again so the returned object is not the stored one.
        self.put_ns(record.namespace, record.name, record.to_dict())
        return record

    def get_latest(
        self,
        name: str,
        *,
        namespace: str = DASHBOARD_NAMESPACE,
    ) -> Optional[LatestSnapshot]:
        """Return the named snapshot, or None if missing / wrong shape."""
        raw = self.get_ns(namespace, name)
        return _as_latest_snapshot(raw, name=name, namespace=namespace)

    def put_dashboard(self, payload: Any) -> LatestSnapshot:
        """Store the live UI dashboard blob under ``dashboard/live``."""
        return self.put_latest(DASHBOARD_NAME, payload, namespace=DASHBOARD_NAMESPACE)

    def get_dashboard(self) -> Optional[LatestSnapshot]:
        """Read the live UI dashboard blob, or None if none has been stored."""
        return self.get_latest(DASHBOARD_NAME, namespace=DASHBOARD_NAMESPACE)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __repr__(self) -> str:
        return f"HotCache(size={len(self)}, updated_at={self.updated_at()!r})"


def _require_key(key: str) -> None:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("hot cache key must be a non-empty string")


def _namespace_prefix(namespace: str) -> str:
    ns = namespace.strip() if isinstance(namespace, str) else ""
    if not ns or _SEP in ns:
        raise ValueError("namespace must be a non-empty string without '/'")
    return f"{ns}{_SEP}"


def _shallow_copy(payload: Any) -> Any:
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, list):
        return list(payload)
    return payload


def _as_latest_snapshot(
    raw: Any,
    *,
    name: str,
    namespace: str,
) -> Optional[LatestSnapshot]:
    if not isinstance(raw, dict):
        return None
    as_of = raw.get("as_of")
    if not isinstance(as_of, str) or not as_of.endswith("Z"):
        return None
    return LatestSnapshot(
        as_of=as_of,
        name=str(raw.get("name") or name),
        namespace=str(raw.get("namespace") or namespace),
        payload=_shallow_copy(raw.get("payload")),
    )
