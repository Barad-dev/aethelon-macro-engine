# -*- coding: utf-8 -*-
"""
scripts/test_analysis_package.py — Stage D offline package check
================================================================
In-memory scenarios for ``assemble_analysis_package``. No network,
no SQLite, no GUI, no ``news_engine`` import.

Usage
-----
    python scripts/test_analysis_package.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aethelon.core.logger import get_logger  # noqa: E402
from aethelon.macro import AnalysisPackage, assemble_analysis_package  # noqa: E402

log = get_logger(__name__)


@dataclass(frozen=True)
class CheckResult:
    """One scenario outcome for the stdout summary."""

    name: str
    ok: bool
    detail: str


def _as_of_ok(value: Optional[str]) -> bool:
    """True when ``as_of`` looks like UTC ISO 8601 Z."""
    if not value or not isinstance(value, str):
        return False
    return "T" in value and value.endswith("Z")


def _package_ok(pkg: Any) -> tuple[bool, str]:
    """Shared shape checks that every scenario must pass."""
    if not isinstance(pkg, AnalysisPackage):
        return False, f"expected AnalysisPackage, got {type(pkg).__name__}"
    if not _as_of_ok(pkg.as_of):
        return False, f"as_of is not UTC Z: {pkg.as_of!r}"
    if pkg.schema_version != "analysis_package_v1":
        return False, f"unexpected schema_version={pkg.schema_version!r}"
    if pkg.status not in {"CALM", "WATCH", "ALERT", "SHOCK"}:
        return False, f"status not in closed set: {pkg.status!r}"
    if not str(pkg.status_note or "").strip():
        return False, "status_note is empty"
    return True, ""


def _run_case(name: str, fn: Callable[[], CheckResult]) -> CheckResult:
    try:
        return fn()
    except Exception as exc:
        log.warning("scenario %s raised: %s", name, exc)
        return CheckResult(name=name, ok=False, detail=f"raised {type(exc).__name__}: {exc}")


def _empty_input() -> CheckResult:
    """Minimal call: valid package, no crash, CALM (no C3–C5 signals)."""
    pkg = assemble_analysis_package()
    ok, detail = _package_ok(pkg)
    if not ok:
        return CheckResult("empty_input", False, detail)
    if pkg.status != "CALM":
        return CheckResult(
            "empty_input",
            False,
            f"expected CALM, got {pkg.status} ({pkg.status_note})",
        )
    return CheckResult(
        "empty_input",
        True,
        f"status={pkg.status} as_of={pkg.as_of} regime="
        f"{None if pkg.regime is None else pkg.regime.regime}",
    )


def _macro_state_only() -> CheckResult:
    """Dial snapshot only: a regime plus CALM (no FRED / shocks)."""
    pkg = assemble_analysis_package(
        macro_state={
            "growth": "STRONG",
            "inflation": "HIGH",
            "as_of": "2026-08-01T00:00:00Z",
        }
    )
    ok, detail = _package_ok(pkg)
    if not ok:
        return CheckResult("macro_state_only", False, detail)
    if pkg.regime is None:
        return CheckResult("macro_state_only", False, "regime is None")
    if pkg.status != "CALM":
        return CheckResult(
            "macro_state_only",
            False,
            f"expected CALM, got {pkg.status} "
            f"(hard={len(pkg.hard_invalidations)} "
            f"soft={len(pkg.soft_divergences)} shocks={len(pkg.shocks)})",
        )
    if pkg.hard_invalidations or pkg.soft_divergences or pkg.shocks:
        return CheckResult(
            "macro_state_only",
            False,
            "expected empty C3–C5 lists without FRED/shocks",
        )
    return CheckResult(
        "macro_state_only",
        True,
        f"status={pkg.status} regime={pkg.regime.regime} "
        f"conf={pkg.regime.confidence:.2f}",
    )


def _fred_hard_unrate() -> CheckResult:
    """Two-point UNRATE jump above the C3 step-rise floor → ALERT."""
    pkg = assemble_analysis_package(
        macro_state={"growth": "WEAK", "inflation": "TARGET"},
        fred_series={
            "UNRATE": [
                {"date": "2026-01-01", "value": 4.0},
                {"date": "2026-02-01", "value": 4.8},
            ]
        },
    )
    ok, detail = _package_ok(pkg)
    if not ok:
        return CheckResult("fred_hard_unrate", False, detail)
    if pkg.status != "ALERT":
        return CheckResult(
            "fred_hard_unrate",
            False,
            f"expected ALERT, got {pkg.status} "
            f"(hard={len(pkg.hard_invalidations)} note={pkg.status_note})",
        )
    if not pkg.hard_invalidations:
        return CheckResult("fred_hard_unrate", False, "ALERT but hard list empty")
    return CheckResult(
        "fred_hard_unrate",
        True,
        f"status={pkg.status} hard={len(pkg.hard_invalidations)} "
        f"note={pkg.status_note}",
    )


def _structured_shock() -> CheckResult:
    """Structured C5 event → SHOCK. Title alone is not enough; kind is."""
    pkg = assemble_analysis_package(
        shock_events=[
            {
                "title": "Strait closure",
                "kind": "STRAIT_CLOSURE",
                "geopolitical": True,
            }
        ]
    )
    ok, detail = _package_ok(pkg)
    if not ok:
        return CheckResult("structured_shock", False, detail)
    if pkg.status != "SHOCK":
        return CheckResult(
            "structured_shock",
            False,
            f"expected SHOCK, got {pkg.status} "
            f"(shocks={len(pkg.shocks)} note={pkg.status_note})",
        )
    if not pkg.shocks:
        return CheckResult("structured_shock", False, "SHOCK but shock list empty")
    return CheckResult(
        "structured_shock",
        True,
        f"status={pkg.status} shocks={len(pkg.shocks)} note={pkg.status_note}",
    )


def main() -> int:
    """Run the four in-memory scenarios and print a PASS/FAIL summary."""
    log.info("test_analysis_package: starting offline scenarios")
    results = [
        _run_case("empty_input", _empty_input),
        _run_case("macro_state_only", _macro_state_only),
        _run_case("fred_hard_unrate", _fred_hard_unrate),
        _run_case("structured_shock", _structured_shock),
    ]

    print()
    print("AnalysisPackage offline test")
    print("----------------------------")
    n_pass = 0
    n_fail = 0
    for row in results:
        tag = "PASS" if row.ok else "FAIL"
        if row.ok:
            n_pass += 1
        else:
            n_fail += 1
        print(f"  {tag}  {row.name:<22} {row.detail}")

    print("----------------------------")
    print(f"  {n_pass} passed, {n_fail} failed")
    print()
    log.info("test_analysis_package: %s passed, %s failed", n_pass, n_fail)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
