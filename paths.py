# -*- coding: utf-8 -*-
"""
paths.py — Centralized path management (Stage B3.1)
===================================================
Resolves durable user data under the canonical Aethelon AppData root:

  Windows:  %APPDATA%\\Aethelon\\
  Other:    ~/.aethelon/

Subfolders: data/, logs/, config/, state/

The SQLite store (``news_engine_store.db``) lives under ``data/``.
On first use, legacy DBs are copied from:
  • project-local install tree
  • older %APPDATA%\\Quantamental\\ layout (Stage A name)

Usage:
    from paths import get_db_path, get_app_data_dir, ensure_app_dirs

    db = get_db_path()          # pathlib.Path
    db_str = str(get_db_path()) # for older sqlite call sites
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Optional, Union

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "Aethelon"
LEGACY_APP_NAME = "Quantamental"  # Stage A brand — still migrated if present
DB_FILENAME = "news_engine_store.db"

DATA_SUBDIR = "data"
LOGS_SUBDIR = "logs"
CONFIG_SUBDIR = "config"
STATE_SUBDIR = "state"

PathLike = Union[str, Path]

_lock = threading.RLock()
_migrated = False
_cached_db: Optional[Path] = None


# ---------------------------------------------------------------------------
# Core resolvers
# ---------------------------------------------------------------------------

def get_project_root() -> Path:
    """Directory containing this package / install tree (source checkout)."""
    return Path(__file__).resolve().parent


def get_app_data_dir() -> Path:
    """
    User-writable application data root (canonical).

    Windows → %APPDATA%\\Aethelon
    macOS/Linux → ~/.aethelon
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if not base:
            base = str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def get_legacy_app_data_dir() -> Path:
    """
    Previous Stage A AppData root (Quantamental).

    Used only as a migration *source* — never as the live write target.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if not base:
            base = str(Path.home() / "AppData" / "Roaming")
        return Path(base) / LEGACY_APP_NAME
    return Path.home() / f".{LEGACY_APP_NAME.lower()}"


def ensure_app_dirs() -> Path:
    """
    Create the AppData layout if missing. Returns the app data root.

      %APPDATA%\\Aethelon\\
        data\\
        logs\\
        config\\
        state\\
    """
    root = get_app_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    for sub in (DATA_SUBDIR, LOGS_SUBDIR, CONFIG_SUBDIR, STATE_SUBDIR):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def get_logs_dir() -> Path:
    """Return ``…/Aethelon/logs`` (created if needed)."""
    ensure_app_dirs()
    return get_app_data_dir() / LOGS_SUBDIR


def get_config_dir() -> Path:
    """Return ``…/Aethelon/config`` (created if needed)."""
    ensure_app_dirs()
    return get_app_data_dir() / CONFIG_SUBDIR


def get_state_dir() -> Path:
    """Return ``…/Aethelon/state`` (created if needed)."""
    ensure_app_dirs()
    return get_app_data_dir() / STATE_SUBDIR


def get_data_dir() -> Path:
    """Return ``…/Aethelon/data`` (created if needed)."""
    ensure_app_dirs()
    return get_app_data_dir() / DATA_SUBDIR


def legacy_db_candidates() -> list[Path]:
    """
    Locations that may hold a pre-canonical database (migration sources).

    Includes install-local copies and the legacy Quantamental AppData tree.
    """
    root = get_project_root()
    app = get_app_data_dir()
    legacy = get_legacy_app_data_dir()
    return [
        root / DB_FILENAME,
        root / DATA_SUBDIR / DB_FILENAME,
        app / DB_FILENAME,
        legacy / DATA_SUBDIR / DB_FILENAME,
        legacy / DB_FILENAME,
    ]


def _preferred_db_path() -> Path:
    """Canonical DB: %APPDATA%\\Aethelon\\data\\news_engine_store.db"""
    return get_data_dir() / DB_FILENAME


def _pick_migration_source(target: Path) -> Optional[Path]:
    """Return the best existing legacy DB that is not already the target."""
    candidates: list[tuple[float, Path]] = []
    for path in legacy_db_candidates():
        try:
            if not path.is_file():
                continue
            if path.resolve() == target.resolve():
                continue
            mtime = path.stat().st_mtime
            candidates.append((mtime, path))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def migrate_legacy_db(
    target: Optional[Path] = None,
    *,
    force: bool = False,
) -> Optional[Path]:
    """
    Copy legacy DB into the canonical Aethelon data path if the target is missing.

    Sources (newest mtime wins): install-local DB, Quantamental AppData DB, etc.

    Returns the source path that was migrated, or None if no migration ran.
    """
    global _migrated
    ensure_app_dirs()
    dest = (target or _preferred_db_path()).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.is_file() and not force:
        _migrated = True
        return None

    source = _pick_migration_source(dest)
    if source is None:
        _migrated = True
        return None

    try:
        shutil.copy2(source, dest)
        _migrated = True
        return source
    except OSError:
        _migrated = True
        return None


def get_db_path(*, migrate: bool = True) -> Path:
    """
    Resolve the active SQLite database path (Path object).

    Ensures AppData dirs exist and, when migrate=True, copies any legacy
    DB into ``%APPDATA%\\Aethelon\\data\\`` on first use.
    """
    global _cached_db
    with _lock:
        if _cached_db is not None and not migrate:
            return _cached_db

        ensure_app_dirs()
        target = _preferred_db_path()

        if migrate and not _migrated:
            migrate_legacy_db(target)

        _cached_db = target
        return target


def get_db_path_str(*, migrate: bool = True) -> str:
    """String form of get_db_path() for sqlite3 / older call sites."""
    return str(get_db_path(migrate=migrate))


def set_db_path_override(path: Optional[PathLike]) -> Path:
    """
    Force a custom DB path (tests / CLI --db). Clears cache when path is None.
    """
    global _cached_db, _migrated
    with _lock:
        if path is None:
            _cached_db = None
            _migrated = False
            return get_db_path()
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        _cached_db = p
        _migrated = True
        return p


def describe_paths() -> dict[str, Any]:
    """Diagnostic snapshot for --status / debugging."""
    db = get_db_path()
    legacy = [str(p) for p in legacy_db_candidates() if p.is_file()]
    return {
        "app_name": APP_NAME,
        "legacy_app_name": LEGACY_APP_NAME,
        "app_data_dir": str(get_app_data_dir()),
        "legacy_app_data_dir": str(get_legacy_app_data_dir()),
        "data_dir": str(get_data_dir()),
        "logs_dir": str(get_logs_dir()),
        "config_dir": str(get_config_dir()),
        "state_dir": str(get_state_dir()),
        "db_path": str(db),
        "db_exists": db.is_file(),
        "db_size_bytes": db.stat().st_size if db.is_file() else 0,
        "project_root": str(get_project_root()),
        "legacy_db_files": legacy,
        "platform": sys.platform,
    }


def default_db_path() -> str:
    """Import-time friendly default DB string (runs migration once)."""
    return get_db_path_str(migrate=True)


if __name__ == "__main__":
    migrate_legacy_db()
    info = describe_paths()
    print("Aethelon path layout")
    print("=" * 56)
    for k, v in info.items():
        print(f"  {k:22} {v}")
