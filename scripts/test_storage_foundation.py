# -*- coding: utf-8 -*-
"""
scripts/test_storage_foundation.py — Stage D storage foundation check
====================================================================
Offline checks for L1 HotCache, L2 WarmStore (WAL open), and L3 ColdVault
stub. Uses a temporary SQLite file. Does not open the live AppData DB,
does not import news_engine, and does not touch the GUI.

Usage
-----
    python scripts/test_storage_foundation.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aethelon.core.logger import get_logger  # noqa: E402
from aethelon.storage import (  # noqa: E402
    ColdVault,
    HotCache,
    StorageFoundation,
    WarmStore,
    default_storage,
    default_warm_db_path,
    parse_iso_utc,
    utc_now_iso_z,
)

log = get_logger(__name__)


@dataclass(frozen=True)
class CheckResult:
    """One scenario outcome for the stdout summary."""

    name: str
    ok: bool
    detail: str


def _as_of_ok(value: object) -> bool:
    if not value or not isinstance(value, str):
        return False
    if "T" not in value or not value.endswith("Z"):
        return False
    try:
        parse_iso_utc(value)
    except ValueError:
        return False
    return True


def _run_case(name: str, fn: Callable[[], CheckResult]) -> CheckResult:
    try:
        return fn()
    except Exception as exc:
        log.warning("scenario %s raised: %s", name, exc)
        return CheckResult(name=name, ok=False, detail=f"raised {type(exc).__name__}: {exc}")


def _l1_roundtrip() -> CheckResult:
    cache = HotCache()
    if len(cache) != 0 or cache.updated_at() is not None:
        return CheckResult("l1_roundtrip", False, "fresh cache should be empty")
    cache.put("regime", {"name": "GOLDILOCKS"})
    cache.put("status", "CALM")
    if cache.get("status") != "CALM":
        return CheckResult("l1_roundtrip", False, "get missed put")
    snap = cache.snapshot()
    snap["status"] = "mutated"
    if cache.get("status") != "CALM":
        return CheckResult("l1_roundtrip", False, "snapshot leaked mutation into cache")
    if not _as_of_ok(cache.updated_at()):
        return CheckResult("l1_roundtrip", False, f"updated_at not UTC Z: {cache.updated_at()!r}")
    cache.delete("status")
    if "status" in cache.keys() or len(cache) != 1:
        return CheckResult("l1_roundtrip", False, "delete did not remove key")
    cache.clear()
    if len(cache) != 0:
        return CheckResult("l1_roundtrip", False, "clear left items")
    return CheckResult("l1_roundtrip", True, "put/get/snapshot/delete/clear + UTC Z")


def _l1_rejects_empty_key() -> CheckResult:
    cache = HotCache()
    try:
        cache.put("  ", "x")
    except ValueError:
        return CheckResult("l1_rejects_empty_key", True, "empty key rejected")
    return CheckResult("l1_rejects_empty_key", False, "empty key was accepted")


def _l2_wal_on_temp_db() -> CheckResult:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "warm.db"
        store = WarmStore(db_path=db, migrate=False)
        mode = store.ensure_wal()
        if mode != "wal":
            return CheckResult("l2_wal_on_temp_db", False, f"journal_mode={mode!r}")
        if not db.is_file():
            return CheckResult("l2_wal_on_temp_db", False, "db file was not created")
        return CheckResult("l2_wal_on_temp_db", True, f"WAL at {db.name}")


def _l2_does_not_wipe_rows() -> CheckResult:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "existing.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, note TEXT)")
            conn.execute("INSERT INTO probe (note) VALUES ('keep-me')")
            conn.commit()
        finally:
            conn.close()

        store = WarmStore(db_path=db, migrate=False)
        with store.connection() as warm:
            if _journal_mode(warm) != "wal":
                return CheckResult("l2_does_not_wipe_rows", False, "WAL not enabled on existing file")
            row = warm.execute("SELECT note FROM probe WHERE id = 1").fetchone()
            tables = {
                r[0]
                for r in warm.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        if row is None or row[0] != "keep-me":
            return CheckResult("l2_does_not_wipe_rows", False, "existing row missing after WAL open")
        if "probe" not in tables:
            return CheckResult("l2_does_not_wipe_rows", False, "existing table dropped")
        return CheckResult("l2_does_not_wipe_rows", True, "existing row intact after WAL open")


def _l2_default_path_matches_helpers() -> CheckResult:
    from paths import get_db_path

    expected = get_db_path(migrate=True)
    got = default_warm_db_path(migrate=True)
    if got != expected:
        return CheckResult(
            "l2_default_path_matches_helpers",
            False,
            f"warm={got} paths={expected}",
        )
    # Resolve only — do not open the live user database in this check.
    store = WarmStore(migrate=True)
    if store.db_path != expected:
        return CheckResult(
            "l2_default_path_matches_helpers",
            False,
            f"WarmStore.db_path={store.db_path} expected={expected}",
        )
    return CheckResult("l2_default_path_matches_helpers", True, str(got))


def _l3_stub_is_inert() -> CheckResult:
    vault = ColdVault(vault_dir=Path("unused-vault"))
    if vault.is_ready():
        return CheckResult("l3_stub_is_inert", False, "stub reported ready")
    vault.archive({"kind": "test"})
    found = vault.retrieve({"kind": "test"})
    if found:
        return CheckResult("l3_stub_is_inert", False, f"stub returned {found!r}")
    return CheckResult("l3_stub_is_inert", True, "archive/retrieve no-ops, is_ready=False")


def _foundation_assembles() -> CheckResult:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "stack.db"
        vault = Path(tmp) / "cold"
        stack = default_storage(db_path=db, vault_dir=vault, migrate=False)
        if not isinstance(stack, StorageFoundation):
            return CheckResult("foundation_assembles", False, type(stack).__name__)
        if not _as_of_ok(stack.assembled_at):
            return CheckResult(
                "foundation_assembles",
                False,
                f"assembled_at not UTC Z: {stack.assembled_at!r}",
            )
        if stack.warm.ensure_wal() != "wal":
            return CheckResult("foundation_assembles", False, "stack L2 not WAL")
        if stack.cold.is_ready():
            return CheckResult("foundation_assembles", False, "stack L3 should be stub")
        stack.hot.put("ping", 1)
        if stack.hot.get("ping") != 1:
            return CheckResult("foundation_assembles", False, "stack L1 missed put")
        return CheckResult("foundation_assembles", True, f"assembled_at={stack.assembled_at}")


def _utc_helper() -> CheckResult:
    stamp = utc_now_iso_z()
    if not _as_of_ok(stamp):
        return CheckResult("utc_helper", False, f"bad stamp {stamp!r}")
    return CheckResult("utc_helper", True, stamp)


def _journal_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA journal_mode;").fetchone()
    return str(row[0]).lower() if row else ""


def main() -> int:
    cases: list[Callable[[], CheckResult]] = [
        _l1_roundtrip,
        _l1_rejects_empty_key,
        _l2_wal_on_temp_db,
        _l2_does_not_wipe_rows,
        _l2_default_path_matches_helpers,
        _l3_stub_is_inert,
        _foundation_assembles,
        _utc_helper,
    ]
    results = [_run_case(fn.__name__, fn) for fn in cases]
    ok_n = sum(1 for r in results if r.ok)
    print("Aethelon storage foundation check")
    print("=" * 56)
    for r in results:
        mark = "OK  " if r.ok else "FAIL"
        print(f"  [{mark}] {r.name}: {r.detail}")
    print("-" * 56)
    print(f"  {ok_n}/{len(results)} passed")
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
