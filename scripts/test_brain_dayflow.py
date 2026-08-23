# -*- coding: utf-8 -*-
"""
scripts/test_brain_dayflow.py — long offline brain stress
=========================================================
Feeds messy, realistic day-trader fundamental snapshots into
``assemble_analysis_package`` for about 30 minutes of wall-clock time.

No network, no SQLite, no GUI, no ``news_engine`` import.
Does not change Stage C/D logic.

Usage
-----
    python scripts/test_brain_dayflow.py
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aethelon.core.logger import get_logger  # noqa: E402
from aethelon.macro import AnalysisPackage, assemble_analysis_package  # noqa: E402

log = get_logger(__name__)

DURATION_SEC = 30 * 60
HEARTBEAT_SEC = 60
_STATUS_OK = frozenset({"CALM", "WATCH", "ALERT", "SHOCK"})


@dataclass(frozen=True)
class Scenario:
    """One in-memory desk snapshot plus optional status invariants."""

    name: str
    kwargs: dict[str, Any]
    expect_status: Optional[frozenset[str]] = None
    forbid_status: Optional[frozenset[str]] = None


def _as_of_ok(value: Optional[str]) -> bool:
    if not value or not isinstance(value, str):
        return False
    return "T" in value and value.endswith("Z")


def _package_shape_ok(pkg: Any) -> Optional[str]:
    if not isinstance(pkg, AnalysisPackage):
        return f"type={type(pkg).__name__}"
    if not _as_of_ok(pkg.as_of):
        return f"bad as_of={pkg.as_of!r}"
    if pkg.schema_version != "analysis_package_v1":
        return f"schema={pkg.schema_version!r}"
    if pkg.status not in _STATUS_OK:
        return f"status={pkg.status!r}"
    if not str(pkg.status_note or "").strip():
        return "empty status_note"
    if not isinstance(pkg.hard_invalidations, list):
        return "hard_invalidations not a list"
    if not isinstance(pkg.soft_divergences, list):
        return "soft_divergences not a list"
    if not isinstance(pkg.shocks, list):
        return "shocks not a list"
    if not isinstance(pkg.errors, list):
        return "errors not a list"
    return None


def _catalog() -> list[Scenario]:
    """Rotating desk-day snapshots. Messy on purpose; no live I/O."""
    growth = ("STRONG", "TREND", "WEAK", "CONTRACTING", "SOFT", "???", "")
    inflation = ("HIGH", "ELEVATED", "TARGET", "LOW", "HOT", "", None)
    dials: list[Scenario] = []
    for g in growth:
        for inf in inflation:
            state: dict[str, Any] = {"as_of": "2026-08-21T12:00:00Z"}
            if g != "":
                state["growth"] = g
            if inf is not None:
                state["inflation"] = inf
            dials.append(
                Scenario(
                    name=f"dials:{g or 'missing'}x{inf if inf is not None else 'omit'}",
                    kwargs={"macro_state": state},
                    expect_status=frozenset({"CALM"}),
                )
            )

    return [
        Scenario("empty", kwargs={}, expect_status=frozenset({"CALM"})),
        Scenario(
            "morning_partial_context",
            kwargs={
                "context": {
                    "macro_state": {"growth": None, "inflation": "", "policy": " "},
                    "ff_analyzed": [],
                    "rss_analyzed": None,
                    "pressure_scores": {},
                }
            },
            expect_status=frozenset({"CALM"}),
        ),
        Scenario(
            "morning_dials_only",
            kwargs={
                "macro_state": {
                    "growth": "STRONG",
                    "inflation": "TARGET",
                    "policy": "RESTRICTIVE",
                    "liquidity": "TIGHT",
                    "risk": "RISK_ON",
                    "as_of": "2026-08-21T13:05:00Z",
                }
            },
            expect_status=frozenset({"CALM"}),
        ),
        *dials,
        Scenario(
            "mild_unrate",
            kwargs={
                "macro_state": {"growth": "STRONG", "inflation": "TARGET"},
                "fred_series": {
                    "UNRATE": [
                        {"date": "2026-01-01", "value": 4.10},
                        {"date": "2026-02-01", "value": 4.20},
                    ]
                },
            },
            forbid_status=frozenset({"ALERT", "SHOCK"}),
        ),
        Scenario(
            "mild_fedfunds",
            kwargs={
                "macro_state": {"growth": "STRONG", "inflation": "HIGH"},
                "fred_series": {
                    "FEDFUNDS": [
                        {"date": "2026-01-01", "value": 5.25},
                        {"date": "2026-02-01", "value": 5.50},
                    ]
                },
            },
            forbid_status=frozenset({"ALERT", "SHOCK"}),
        ),
        Scenario(
            "hard_unrate_spike",
            kwargs={
                "macro_state": {"growth": "WEAK", "inflation": "TARGET"},
                "fred_series": {
                    "UNRATE": [
                        {"date": "2026-01-01", "value": 4.0},
                        {"date": "2026-02-01", "value": 4.8},
                    ]
                },
            },
            expect_status=frozenset({"ALERT"}),
        ),
        Scenario(
            "hard_payems_crash",
            kwargs={
                "macro_state": {"growth": "WEAK", "inflation": "LOW"},
                "fred_series": {
                    "PAYEMS": [
                        {"date": "2026-01-01", "value": 158000.0},
                        {"date": "2026-02-01", "value": 157500.0},
                    ]
                },
            },
            expect_status=frozenset({"ALERT"}),
        ),
        Scenario(
            "hard_fedfunds_emergency",
            kwargs={
                "macro_state": {"growth": "STRONG", "inflation": "HIGH"},
                "fred_series": {
                    "FEDFUNDS": [
                        {"date": "2026-01-01", "value": 5.25},
                        {"date": "2026-02-01", "value": 6.50},
                    ]
                },
            },
            expect_status=frozenset({"ALERT"}),
        ),
        Scenario(
            "softish_unrate",
            kwargs={
                "macro_state": {"growth": "TREND", "inflation": "TARGET"},
                "fred_series": {
                    "UNRATE": [
                        {"date": "2026-01-01", "value": 4.00},
                        {"date": "2026-02-01", "value": 4.25},
                    ]
                },
            },
            expect_status=frozenset({"WATCH", "CALM"}),
            forbid_status=frozenset({"SHOCK"}),
        ),
        Scenario(
            "shock_strait",
            kwargs={
                "shock_events": [
                    {
                        "title": "Strait closure",
                        "kind": "STRAIT_CLOSURE",
                        "geopolitical": True,
                    }
                ]
            },
            expect_status=frozenset({"SHOCK"}),
        ),
        Scenario(
            "shock_emergency_cut",
            kwargs={
                "macro_state": {"growth": "WEAK", "inflation": "HIGH"},
                "shock_events": [
                    {
                        "title": "Emergency rate cut",
                        "kind": "EMERGENCY_RATE_CUT",
                        "emergency_cb": True,
                    }
                ],
            },
            expect_status=frozenset({"SHOCK"}),
        ),
        Scenario(
            "shock_plus_hard",
            kwargs={
                "macro_state": {"growth": "WEAK", "inflation": "HIGH"},
                "fred_series": {
                    "UNRATE": [
                        {"date": "2026-01-01", "value": 4.0},
                        {"date": "2026-02-01", "value": 4.9},
                    ]
                },
                "shock_events": [
                    {"title": "Invasion", "kind": "INVASION", "geopolitical": True}
                ],
            },
            expect_status=frozenset({"SHOCK"}),
        ),
        Scenario(
            "headline_only_war",
            kwargs={
                "shock_events": [
                    {
                        "title": "War breaks out as markets panic",
                        "summary": "breaking headline, no structured kind",
                    }
                ]
            },
            forbid_status=frozenset({"SHOCK"}),
        ),
        Scenario(
            "headline_fomc_rejected",
            kwargs={
                "shock_events": [
                    {"title": "FOMC hikes 25bp as expected", "kind": "FOMC"}
                ]
            },
            forbid_status=frozenset({"SHOCK"}),
        ),
        Scenario(
            "headline_cpi_print",
            kwargs={
                "shock_events": [
                    {"title": "CPI hotter than expected", "kind": "CPI"}
                ]
            },
            forbid_status=frozenset({"SHOCK"}),
        ),
        Scenario(
            "single_point_unrate",
            kwargs={
                "fred_series": {
                    "UNRATE": [{"date": "2026-02-01", "value": 4.2}],
                }
            },
            expect_status=frozenset({"CALM"}),
        ),
        Scenario(
            "missing_fred_values",
            kwargs={
                "fred_series": {
                    "UNRATE": [
                        {"date": "2026-01-01", "value": "."},
                        {"date": "2026-02-01", "value": None},
                        {"date": "2026-03-01"},
                    ]
                }
            },
        ),
        Scenario(
            "bad_dates",
            kwargs={
                "fred_series": {
                    "CPIAUCSL": [
                        {"date": "not-a-date", "value": 300.1},
                        {"date": "2026-13-40", "value": 301.0},
                        {"date": "", "value": 302.0},
                    ]
                }
            },
        ),
        Scenario(
            "item_list_fred",
            kwargs={
                "fred_series": [
                    {"series_id": "UNRATE", "date": "2026-01-01", "value": 4.0},
                    {"series_id": "UNRATE", "date": "2026-02-01", "value": 4.1},
                ]
            },
            forbid_status=frozenset({"ALERT", "SHOCK"}),
        ),
        Scenario(
            "context_with_fred_wrapper",
            kwargs={
                "context": {
                    "macro_state": {"growth": "STRONG", "inflation": "HIGH"},
                    "fred_series": {
                        "GDP": [
                            {"date": "2025-10-01", "value": 100.0},
                            {"date": "2026-01-01", "value": 99.5},
                        ]
                    },
                }
            },
        ),
        Scenario(
            "detected_at_z",
            kwargs={
                "macro_state": {"growth": "WEAK", "inflation": "LOW"},
                "detected_at": "2026-08-21T14:30:00Z",
            },
            expect_status=frozenset({"CALM"}),
        ),
        Scenario(
            "detected_at_junk",
            kwargs={"detected_at": "yesterday morning"},
        ),
        Scenario(
            "inactive_shock_only",
            kwargs={
                "shock_events": [
                    {
                        "title": "Old strait scare",
                        "kind": "STRAIT_CLOSURE",
                        "geopolitical": True,
                        "active": False,
                    }
                ]
            },
            forbid_status=frozenset({"SHOCK"}),
        ),
    ]


def _check_invariants(scn: Scenario, pkg: AnalysisPackage) -> Optional[str]:
    shape = _package_shape_ok(pkg)
    if shape:
        return shape
    if scn.expect_status is not None and pkg.status not in scn.expect_status:
        return f"status={pkg.status} expected {sorted(scn.expect_status)}"
    if scn.forbid_status is not None and pkg.status in scn.forbid_status:
        return f"status={pkg.status} forbidden"
    return None


def _run_one(scn: Scenario) -> tuple[str, str]:
    """
    Returns (kind, detail) where kind is pass | fail | crash.
    """
    try:
        pkg = assemble_analysis_package(**scn.kwargs)
    except Exception as exc:
        return "crash", f"{type(exc).__name__}: {exc}"
    err = _check_invariants(scn, pkg)
    if err:
        return "fail", err
    return "pass", pkg.status


def _quiet_macro_logs() -> None:
    """Keep AppData logs from drowning in per-call INFO during the long run."""
    logging.getLogger("aethelon.macro").setLevel(logging.WARNING)


def _print(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    """Rotate the catalog until wall-clock duration elapses."""
    _quiet_macro_logs()
    catalog = _catalog()
    if not catalog:
        _print("FAIL: empty scenario catalog")
        return 1

    n_pass = 0
    n_fail = 0
    n_crash = 0
    n_run = 0
    cycles = 0
    first_problems: list[str] = []
    fail_names: dict[str, int] = {}
    crash_names: dict[str, int] = {}

    started = time.monotonic()
    deadline = started + DURATION_SEC
    last_beat = started

    log.info(
        "test_brain_dayflow: start duration_sec=%s scenarios=%s",
        DURATION_SEC,
        len(catalog),
    )
    _print(
        f"Brain day-flow stress starting: {len(catalog)} scenarios, "
        f"{DURATION_SEC // 60} minutes"
    )

    try:
        while time.monotonic() < deadline:
            cycles += 1
            for scn in catalog:
                if time.monotonic() >= deadline:
                    break
                n_run += 1
                kind, detail = _run_one(scn)
                if kind == "pass":
                    n_pass += 1
                elif kind == "fail":
                    n_fail += 1
                    fail_names[scn.name] = fail_names.get(scn.name, 0) + 1
                    if len(first_problems) < 12:
                        first_problems.append(f"FAIL {scn.name}: {detail}")
                else:
                    n_crash += 1
                    crash_names[scn.name] = crash_names.get(scn.name, 0) + 1
                    if len(first_problems) < 12:
                        first_problems.append(f"CRASH {scn.name}: {detail}")

            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_SEC:
                elapsed = now - started
                _print(
                    f"  {elapsed:7.0f}s  run={n_run} pass={n_pass} "
                    f"fail={n_fail} crash={n_crash} cycles={cycles}"
                )
                log.info(
                    "test_brain_dayflow: heartbeat elapsed=%.0fs run=%s "
                    "pass=%s fail=%s crash=%s",
                    elapsed,
                    n_run,
                    n_pass,
                    n_fail,
                    n_crash,
                )
                last_beat = now
    except KeyboardInterrupt:
        _print("Interrupted — writing summary from partial run")

    elapsed = time.monotonic() - started
    stable = n_fail == 0 and n_crash == 0 and n_run > 0

    _print("")
    _print("Brain day-flow stress — final summary")
    _print("-------------------------------------")
    _print(f"  duration_s     {elapsed:.1f}")
    _print(f"  catalog_size   {len(catalog)}")
    _print(f"  cycles         {cycles}")
    _print(f"  scenarios_run  {n_run}")
    _print(f"  PASS           {n_pass}")
    _print(f"  FAIL           {n_fail}")
    _print(f"  CRASH          {n_crash}")
    _print(f"  STABLE         {'yes' if stable else 'no'}")
    if fail_names:
        _print("  fail_by_name:")
        for name, count in sorted(fail_names.items(), key=lambda x: (-x[1], x[0])):
            _print(f"    {name}: {count}")
    if crash_names:
        _print("  crash_by_name:")
        for name, count in sorted(crash_names.items(), key=lambda x: (-x[1], x[0])):
            _print(f"    {name}: {count}")
    if first_problems:
        _print("  first_problems:")
        for line in first_problems:
            _print(f"    {line}")
    _print("-------------------------------------")

    log.info(
        "test_brain_dayflow: done elapsed=%.1fs run=%s pass=%s fail=%s "
        "crash=%s stable=%s",
        elapsed,
        n_run,
        n_pass,
        n_fail,
        n_crash,
        stable,
    )
    return 0 if stable else 1


if __name__ == "__main__":
    raise SystemExit(main())
