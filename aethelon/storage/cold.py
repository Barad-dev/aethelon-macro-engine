# -*- coding: utf-8 -*-
"""
L3 Cold Vault — long-term local archive for backfill / multi-year use.

Responsibilities
----------------
Store JSON payloads on disk under the AppData data root, gzip-compressed,
so later backfill and regime backtesting can read them without touching
the L2 SQLite store.

Layout (created on first archive / ``ensure``)::

    {data}/cold_vault/records/{kind}/{id}.json.gz

This phase is a durable start, not a multi-year compression policy.
There is no delete API and no L2 table access.
"""

from __future__ import annotations

import gzip
import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Union
from uuid import uuid4

from aethelon.core.logger import get_logger
from aethelon.storage.exceptions import StorageError
from aethelon.storage.timeutil import utc_now_iso_z

__all__ = [
    "ColdVault",
    "VaultRecord",
    "default_cold_vault_dir",
]

log = get_logger(__name__)

PathLike = Union[str, Path]

_VAULT_SUBDIR = "cold_vault"
_RECORDS_SUBDIR = "records"
_SUFFIX = ".json.gz"
_DEFAULT_KIND = "record"
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500
_SAFE_PART = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class VaultRecord:
    """One L3 archive record (JSON-friendly)."""

    id: str
    kind: str
    archived_at: str
    payload: Any
    path: str

    def to_dict(self) -> dict[str, Any]:
        """Plain dict for tests and later IPC."""
        return asdict(self)


def default_cold_vault_dir() -> Path:
    """
    Vault directory under the AppData data root.

    Windows : %APPDATA%\\Aethelon\\data\\cold_vault
    Other   : ~/.aethelon/data/cold_vault
    """
    from paths import get_data_dir

    return get_data_dir() / _VAULT_SUBDIR


class ColdVault:
    """
    Minimal gzip JSON archive (L3).

    ``archive`` writes one record; ``retrieve`` filters by ``kind``,
    ``id``, and optional ``limit`` (recent first). Same ``kind``+``id``
    overwrites that file only. Never opens the L2 database.
    """

    def __init__(self, vault_dir: Optional[PathLike] = None) -> None:
        self._vault_dir = (
            Path(vault_dir).expanduser() if vault_dir is not None else default_cold_vault_dir()
        )
        self._lock = threading.RLock()

    @property
    def vault_dir(self) -> Path:
        """Archive root. Created on ``ensure`` / first ``archive``."""
        return self._vault_dir

    def ensure(self) -> Path:
        """Create the vault and records directories. Returns the vault root."""
        try:
            self._vault_dir.mkdir(parents=True, exist_ok=True)
            (self._vault_dir / _RECORDS_SUBDIR).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(
                f"could not create L3 vault at {self._vault_dir}",
                details={"path": str(self._vault_dir), "error": str(exc)},
            ) from exc
        if not self._vault_dir.is_dir():
            raise StorageError(
                f"L3 vault path is not a directory: {self._vault_dir}",
                details={"path": str(self._vault_dir)},
            )
        return self._vault_dir

    def is_ready(self) -> bool:
        """True when the vault directory exists and can hold records."""
        records = self._vault_dir / _RECORDS_SUBDIR
        return self._vault_dir.is_dir() and records.is_dir()

    def readiness(self) -> dict[str, Any]:
        """
        Status blob. Tries ``ensure`` so a check can prepare an empty vault.

        ``checked_at`` is ISO 8601 UTC Z.
        """
        checked_at = utc_now_iso_z()
        error: Optional[str] = None
        try:
            self.ensure()
        except StorageError as exc:
            error = str(exc)
        ready = self.is_ready()
        count = 0
        if ready:
            try:
                count = self._record_count()
            except OSError as exc:
                error = str(exc)
                ready = False
        return {
            "ready": ready,
            "reason": "ok" if ready else (error or "unavailable"),
            "vault_dir": str(self._vault_dir),
            "record_count": count,
            "compressed": True,
            "checked_at": checked_at,
        }

    def archive(self, payload: Mapping[str, Any]) -> VaultRecord:
        """
        Persist one JSON payload as a gzip file.

        Top-level keys:
          • ``kind`` — folder name (default ``record``)
          • ``id`` — file stem (generated if omitted)
          • ``payload`` — body; if omitted, the remaining mapping is the body

        Returns the stored envelope. Does not touch L2.
        """
        if not isinstance(payload, Mapping):
            raise StorageError("L3 archive payload must be a mapping")
        kind = _safe_part(str(payload.get("kind") or _DEFAULT_KIND), fallback=_DEFAULT_KIND)
        record_id = _safe_part(str(payload.get("id") or _new_id()), fallback=_new_id())
        if "payload" in payload:
            body: Any = payload.get("payload")
        else:
            body = {
                key: value
                for key, value in payload.items()
                if key not in {"kind", "id"}
            }
        archived_at = utc_now_iso_z()
        record = VaultRecord(
            id=record_id,
            kind=kind,
            archived_at=archived_at,
            payload=body,
            path="",
        )
        path = self._record_path(kind, record_id)
        envelope = {
            "id": record.id,
            "kind": record.kind,
            "archived_at": record.archived_at,
            "payload": record.payload,
        }
        try:
            raw = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise StorageError(
                "L3 archive payload is not JSON-serializable",
                details={"kind": kind, "id": record_id, "error": str(exc)},
            ) from exc

        with self._lock:
            self.ensure()
            _atomic_gzip_write(path, raw)

        stored = VaultRecord(
            id=record.id,
            kind=record.kind,
            archived_at=record.archived_at,
            payload=record.payload,
            path=str(path),
        )
        log.info("L3 archived kind=%s id=%s", stored.kind, stored.id)
        return stored

    def retrieve(self, query: Optional[Mapping[str, Any]] = None) -> list[VaultRecord]:
        """
        Load records matching ``query``.

        Keys:
          • ``id`` — exact file stem
          • ``kind`` — folder
          • ``limit`` — max rows, newest ``archived_at`` first (default 50)

        Missing vault → empty list. Never opens L2.
        """
        q = dict(query or {})
        kind_raw = q.get("kind")
        id_raw = q.get("id")
        kind = _safe_part(str(kind_raw), fallback=_DEFAULT_KIND) if kind_raw else None
        record_id = _safe_part(str(id_raw), fallback="") if id_raw else None
        if record_id == "":
            record_id = None
        limit = _clamp_limit(q.get("limit"))

        if not (self._vault_dir / _RECORDS_SUBDIR).is_dir():
            return []

        with self._lock:
            if kind and record_id:
                rec = self._read_file(self._record_path(kind, record_id))
                return [rec] if rec is not None else []
            records = self._read_many(kind=kind, record_id=record_id)

        records.sort(key=lambda r: (r.archived_at, r.id), reverse=True)
        if record_id is None:
            records = records[:limit]
        return records

    def _records_root(self) -> Path:
        return self._vault_dir / _RECORDS_SUBDIR

    def _record_path(self, kind: str, record_id: str) -> Path:
        return self._records_root() / kind / f"{record_id}{_SUFFIX}"

    def _read_many(
        self,
        *,
        kind: Optional[str],
        record_id: Optional[str],
    ) -> list[VaultRecord]:
        found: list[VaultRecord] = []
        for path in self._iter_files(kind=kind):
            if record_id is not None and _id_from_filename(path) != record_id:
                continue
            rec = self._read_file(path)
            if rec is not None:
                found.append(rec)
        return found

    def _iter_files(self, *, kind: Optional[str]) -> list[Path]:
        root = self._records_root()
        folders: list[Path]
        if kind is not None:
            folders = [root / kind]
        elif root.is_dir():
            folders = sorted(p for p in root.iterdir() if p.is_dir())
        else:
            folders = []
        files: list[Path] = []
        for folder in folders:
            if not folder.is_dir():
                continue
            files.extend(sorted(folder.glob(f"*{_SUFFIX}")))
        return files

    def _read_file(self, path: Path) -> Optional[VaultRecord]:
        if not path.is_file():
            return None
        try:
            with gzip.open(path, "rb") as fh:
                data = json.loads(fh.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, gzip.BadGzipFile) as exc:
            log.warning("L3 skip unreadable record %s: %s", path, exc)
            return None
        if not isinstance(data, dict):
            log.warning("L3 skip non-object record %s", path)
            return None
        as_of = data.get("archived_at")
        if not isinstance(as_of, str) or not as_of.endswith("Z"):
            log.warning("L3 skip record without UTC Z archived_at %s", path)
            return None
        return VaultRecord(
            id=str(data.get("id") or _id_from_filename(path)),
            kind=str(data.get("kind") or path.parent.name),
            archived_at=as_of,
            payload=data.get("payload"),
            path=str(path),
        )

    def _record_count(self) -> int:
        return len(self._iter_files(kind=None))

    def __repr__(self) -> str:
        return f"ColdVault(vault_dir={str(self._vault_dir)!r}, ready={self.is_ready()})"


def _id_from_filename(path: Path) -> str:
    """Stem of ``{id}.json.gz`` (pathlib ``.stem`` would leave ``.json``)."""
    name = path.name
    if name.endswith(_SUFFIX):
        return name[: -len(_SUFFIX)]
    return path.stem


def _new_id() -> str:
    return uuid4().hex[:12]


def _safe_part(value: str, *, fallback: str) -> str:
    text = _SAFE_PART.sub("_", (value or "").strip())
    text = text.strip("._")[:80]
    if not text or text in {".", ".."}:
        return fallback
    return text


def _clamp_limit(raw: Any) -> int:
    if raw is None:
        return _DEFAULT_LIMIT
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    if n < 1:
        return 1
    if n > _MAX_LIMIT:
        return _MAX_LIMIT
    return n


def _atomic_gzip_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with gzip.open(tmp, "wb", compresslevel=6) as fh:
            fh.write(payload)
        tmp.replace(path)
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise StorageError(
            f"could not write L3 record {path}",
            details={"path": str(path), "error": str(exc)},
        ) from exc
