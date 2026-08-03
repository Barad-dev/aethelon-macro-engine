# -*- coding: utf-8 -*-
"""
aethelon.ingestion.watermark — Offline catch-up watermark persistence (Stage B2)
================================================================================
Stores per-source high-water marks so ingestion drivers only fetch / emit
items newer than the last successful run.

Default store (JSON):
  Windows : %APPDATA%\\Aethelon\\state\\watermarks.json
  Other   : ~/.aethelon/state/watermarks.json

All timestamps are timezone-aware UTC and serialized as ISO 8601
(e.g. ``2026-07-26T13:30:00Z``).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from aethelon.core.logger import get_logger

__all__ = [
    "WatermarkManager",
    "default_watermark_path",
    "to_utc",
    "to_iso_z",
    "parse_iso_utc",
]

log = get_logger(__name__)

PathLike = Union[str, Path]

_APP_NAME = "Aethelon"
_STATE_SUBDIR = "state"
_DEFAULT_FILENAME = "watermarks.json"


# =============================================================================
# Time helpers (UTC-only contract)
# =============================================================================

def to_utc(dt: datetime) -> datetime:
    """Normalize any datetime to timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso_z(dt: datetime) -> str:
    """Format as ISO 8601 UTC with ``Z`` suffix (second precision)."""
    utc = to_utc(dt).replace(microsecond=0)
    return utc.isoformat().replace("+00:00", "Z")


def parse_iso_utc(value: str) -> datetime:
    """
    Parse an ISO 8601 string into timezone-aware UTC.

    Accepts ``...Z``, ``...+00:00``, and naive forms (treated as UTC).
    """
    s = (value or "").strip()
    if not s:
        raise ValueError("empty timestamp")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Common space separator from legacy stores
    if len(s) >= 19 and s[10] == " ":
        s = s[:10] + "T" + s[11:]
    dt = datetime.fromisoformat(s)
    return to_utc(dt)


def default_watermark_path() -> Path:
    """
    Resolve the default AppData watermark JSON path.

    Canonical: ``%APPDATA%\\Aethelon\\state\\watermarks.json`` (via ``paths``).
    """
    try:
        from paths import get_state_dir

        state = get_state_dir()
        state.mkdir(parents=True, exist_ok=True)
        return state / _DEFAULT_FILENAME
    except Exception:
        if sys.platform == "win32":
            base = os.environ.get("APPDATA") or str(
                Path.home() / "AppData" / "Roaming"
            )
            root = Path(base) / _APP_NAME
        else:
            root = Path.home() / f".{_APP_NAME.lower()}"
        state = root / _STATE_SUBDIR
        state.mkdir(parents=True, exist_ok=True)
        return state / _DEFAULT_FILENAME


# =============================================================================
# Watermark manager
# =============================================================================

class WatermarkManager:
    """
    Thread-safe JSON persistence for per-source catch-up watermarks.

    File schema::

        {
          "schema_version": 1,
          "updated_at": "2026-07-26T12:00:00Z",
          "sources": {
            "rss:Yahoo Finance": "2026-07-26T11:55:00Z",
            "ff:weekly": "2026-07-26T12:00:00Z",
            "fred:CPIAUCSL": "2026-06-01T00:00:00Z"
          }
        }

    Parameters
    ----------
    path :
        Optional override for the JSON file location. Defaults to AppData.
    autosave :
        When True (default), every ``update_watermark`` flushes to disk.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: Optional[PathLike] = None,
        *,
        autosave: bool = True,
    ) -> None:
        self._path = Path(path) if path is not None else default_watermark_path()
        self._autosave = autosave
        self._lock = threading.RLock()
        self._sources: dict[str, str] = {}
        self._updated_at: Optional[str] = None
        self._load()

    # ----- properties --------------------------------------------------------

    @property
    def path(self) -> Path:
        """Absolute path to the watermark JSON file."""
        return self._path

    @property
    def source_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._sources.keys())

    # ----- public API --------------------------------------------------------

    def get_watermark(self, source_id: str) -> Optional[datetime]:
        """
        Return the last successful watermark for ``source_id``, or ``None``.

        The returned datetime is always timezone-aware UTC.
        """
        key = (source_id or "").strip()
        if not key:
            return None
        with self._lock:
            raw = self._sources.get(key)
        if not raw:
            return None
        try:
            return parse_iso_utc(raw)
        except (TypeError, ValueError) as exc:
            log.warning(
                "invalid watermark for source=%s value=%r err=%s — treating as unset",
                key,
                raw,
                exc,
            )
            return None

    def update_watermark(self, source_id: str, dt: datetime) -> None:
        """
        Persist a new high-water mark for ``source_id``.

        Only advances the watermark (never moves it backwards). If ``dt``
        is not strictly newer than the stored value, this is a no-op.
        """
        key = (source_id or "").strip()
        if not key:
            raise ValueError("source_id must be a non-empty string")
        if not isinstance(dt, datetime):
            raise TypeError("dt must be a datetime instance")

        utc = to_utc(dt)
        iso = to_iso_z(utc)

        with self._lock:
            current = self._sources.get(key)
            if current:
                try:
                    cur_dt = parse_iso_utc(current)
                    if utc <= cur_dt:
                        log.debug(
                            "watermark unchanged source=%s current=%s candidate=%s",
                            key,
                            current,
                            iso,
                        )
                        return
                except (TypeError, ValueError):
                    pass  # replace corrupt value

            self._sources[key] = iso
            self._updated_at = to_iso_z(datetime.now(tz=timezone.utc))
            log.info("watermark advanced source=%s ts=%s", key, iso)
            if self._autosave:
                self._save_unlocked()

    def set_watermark(
        self,
        source_id: str,
        dt: datetime,
        *,
        force: bool = False,
    ) -> None:
        """
        Set watermark explicitly.

        When ``force=True``, allow moving the mark backwards (admin/repair).
        """
        if not force:
            self.update_watermark(source_id, dt)
            return
        key = (source_id or "").strip()
        if not key:
            raise ValueError("source_id must be a non-empty string")
        iso = to_iso_z(to_utc(dt))
        with self._lock:
            self._sources[key] = iso
            self._updated_at = to_iso_z(datetime.now(tz=timezone.utc))
            log.warning("watermark force-set source=%s ts=%s", key, iso)
            if self._autosave:
                self._save_unlocked()

    def clear(self, source_id: Optional[str] = None) -> None:
        """Clear one source or all watermarks."""
        with self._lock:
            if source_id is None:
                self._sources.clear()
                log.info("all watermarks cleared path=%s", self._path)
            else:
                key = source_id.strip()
                self._sources.pop(key, None)
                log.info("watermark cleared source=%s", key)
            self._updated_at = to_iso_z(datetime.now(tz=timezone.utc))
            if self._autosave:
                self._save_unlocked()

    def save(self) -> None:
        """Flush in-memory state to disk (safe under lock)."""
        with self._lock:
            self._save_unlocked()

    def reload(self) -> None:
        """Re-read watermarks from disk, discarding unsaved in-memory changes."""
        with self._lock:
            self._load_unlocked()

    def as_dict(self) -> dict[str, Any]:
        """Snapshot for diagnostics / IPC."""
        with self._lock:
            return {
                "schema_version": self.SCHEMA_VERSION,
                "path": str(self._path),
                "updated_at": self._updated_at,
                "sources": dict(self._sources),
            }

    # ----- internal I/O ------------------------------------------------------

    def _load(self) -> None:
        with self._lock:
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        path = self._path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("cannot create watermark dir %s: %s", path.parent, exc)
            self._sources = {}
            return

        if not path.is_file():
            log.debug("no watermark file yet at %s — starting empty", path)
            self._sources = {}
            self._updated_at = None
            return

        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            log.error(
                "failed reading watermarks %s: %s — starting empty (ingestion continues)",
                path,
                exc,
            )
            self._sources = {}
            self._updated_at = None
            return

        if not raw.strip():
            log.warning("empty watermark file %s — starting empty", path)
            self._sources = {}
            self._updated_at = None
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.error(
                "corrupt watermarks JSON %s: %s — starting empty (ingestion continues)",
                path,
                exc,
            )
            self._sources = {}
            self._updated_at = None
            return

        if data is None or data == [] or data == {}:
            log.warning("blank watermark payload %s — starting empty", path)
            self._sources = {}
            self._updated_at = None
            return

        sources = data.get("sources") if isinstance(data, dict) else None
        if not isinstance(sources, dict):
            if isinstance(data, dict) and data and all(
                isinstance(k, str) and isinstance(v, str) for k, v in data.items()
            ) and "schema_version" not in data:
                sources = data
            else:
                log.warning(
                    "unexpected watermark schema in %s type=%s — starting empty",
                    path,
                    type(data).__name__,
                )
                sources = {}

        cleaned: dict[str, str] = {}
        for k, v in sources.items():
            key = str(k).strip()
            if not key or not isinstance(v, str):
                continue
            try:
                cleaned[key] = to_iso_z(parse_iso_utc(v))
            except (TypeError, ValueError):
                log.warning("skipping bad watermark entry %s=%r", key, v)
        self._sources = cleaned
        self._updated_at = data.get("updated_at") if isinstance(data, dict) else None
        log.info(
            "watermarks loaded path=%s count=%s",
            path,
            len(self._sources),
        )

    def _save_unlocked(self) -> None:
        path = self._path
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "updated_at": self._updated_at
            or to_iso_z(datetime.now(tz=timezone.utc)),
            "sources": dict(sorted(self._sources.items())),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
            tmp.write_text(text, encoding="utf-8")
            # Atomic replace on Windows-safe path
            os.replace(tmp, path)
            log.debug("watermarks saved path=%s count=%s", path, len(self._sources))
        except OSError as exc:
            log.error("failed writing watermarks %s: %s", path, exc)
            try:
                if tmp.is_file():
                    tmp.unlink()
            except OSError:
                pass
