# -*- coding: utf-8 -*-
"""
paths.py — Centralized path management (Stage A)
================================================
Resolves durable user data under:

  Windows:  %APPDATA%\\Quantamental\\
  Other:    ~/.quantamental/

The SQLite store (`news_engine_store.db`) lives in that directory.
On first use, any legacy DB next to the install/source tree is copied
into AppData so historical data is preserved across updates.

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
from typing import Optional, Union

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "Quantamental"
DB_FILENAME = "news_engine_store.db"

# Subfolders under the app data root (reserved for Stage B/C layout)
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
    User-writable application data root.

    Windows → %APPDATA%\\Quantamental
    macOS/Linux → ~/.quantamental
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if not base:
            # Defensive fallback if APPDATA is unset
            base = str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    # Cross-platform fallback (POSIX + others)
    return Path.home() / f".{APP_NAME.lower()}"


def ensure_app_dirs() -> Path:
    """
    Create the AppData layout if missing. Returns the app data root.

      %APPDATA%\\Quantamental\\
        data\\
        logs\\
        config\\
        state\\
    """
    root = get_app_data_dir()
    for sub in (DATA_SUBDIR, LOGS_SUBDIR, CONFIG_SUBDIR, STATE_SUBDIR):
        (root / sub).mkdir(parents=True, exist_ok=True)
    # Also ensure root itself exists (mkdir parents already did)
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_logs_dir() -> Path:
    ensure_app_dirs()
    return get_app_data_dir() / LOGS_SUBDIR


def get_config_dir() -> Path:
    ensure_app_dirs()
    return get_app_data_dir() / CONFIG_SUBDIR


def get_state_dir() -> Path:
    ensure_app_dirs()
    return get_app_data_dir() / STATE_SUBDIR


def get_data_dir() -> Path:
    ensure_app_dirs()
    return get_app_data_dir() / DATA_SUBDIR


def legacy_db_candidates() -> list[Path]:
    """
    Locations that may hold a pre-AppData database (migration sources).

    Order = preference when multiple exist (newest install-local first).
    """
    root = get_project_root()
    app = get_app_data_dir()
    return [
        root / DB_FILENAME,                       # classic colocated store
        root / DATA_SUBDIR / DB_FILENAME,         # if someone nested it
        app / DB_FILENAME,                        # older AppData root placement
    ]


def _preferred_db_path() -> Path:
    """Canonical DB location: %APPDATA%\\Quantamental\\data\\news_engine_store.db"""
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
    # Prefer the most recently modified legacy copy
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def migrate_legacy_db(target: Optional[Path] = None, *, force: bool = False) -> Optional[Path]:
    """
    Copy legacy install-local DB into AppData if the target is missing.

    Returns the source path that was migrated, or None if no migration ran.
    Safe to call repeatedly (no-op once target exists, unless force=True).
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
        # Leave dest missing; caller will create a fresh DB on first connect
        _migrated = True
        return None


def get_db_path(*, migrate: bool = True) -> Path:
    """
    Resolve the active SQLite database path (Path object).

    Ensures AppData dirs exist and, when migrate=True, copies any legacy
    project-local DB into AppData on first use.
    """
    global _cached_db
    with _lock:
        if _cached_db is not None and not migrate:
            return _cached_db

        ensure_app_dirs()
        target = _preferred_db_path()

        if migrate and not _migrated:
            migrate_legacy_db(target)

        # If still missing after migration, engines create schema on open.
        _cached_db = target
        return target


def get_db_path_str(*, migrate: bool = True) -> str:
    """String form of get_db_path() for sqlite3 / older call sites."""
    return str(get_db_path(migrate=migrate))


def set_db_path_override(path: Optional[PathLike]) -> Path:
    """
    Force a custom DB path (tests / CLI --db). Disables further auto-cache
    until process restart unless called again with None to clear.
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


def describe_paths() -> dict:
    """Diagnostic snapshot for --status / debugging."""
    db = get_db_path()
    legacy = [str(p) for p in legacy_db_candidates() if p.is_file()]
    return {
        "app_name": APP_NAME,
        "app_data_dir": str(get_app_data_dir()),
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


# Eager-friendly alias used by modules that expect a module-level constant
# evaluated at import time. Call get_db_path() for the live value after
# set_db_path_override().
def default_db_path() -> str:
    """Import-time friendly default DB string (runs migration once)."""
    return get_db_path_str(migrate=True)


if __name__ == "__main__":
    # Quick diagnostic: python paths.py
    migrate_legacy_db()
    info = describe_paths()
    print("Quantamental path layout")
    print("=" * 56)
    for k, v in info.items():
        print(f"  {k:18} {v}")
