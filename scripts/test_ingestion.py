# -*- coding: utf-8 -*-
"""
scripts/test_ingestion.py — Stage B3.5 ingestion path verification
==================================================================
Simple, readable smoke test for the async ingestion plane.

What it does
------------
1. Runs ``IngestionOrchestrator`` with default (or quick) config
2. Prints counts per source family + a few sample rows
3. Reports whether FRED was skipped because ``FRED_API_KEY`` is missing
4. Optionally exercises the B3.4 soft-wire helpers in ``news_engine``
   (fetch-only; no store merge / no analytical DB writes)

What it does *not* do
---------------------
* NLP / sentiment / regime / thesis
* GUI
* Analytical database writes (``news_engine_store.db``)
* Changes to driver internals

Usage
-----
    python scripts/test_ingestion.py
    python scripts/test_ingestion.py --quick
    python scripts/test_ingestion.py --soft-wire
    python scripts/test_ingestion.py --use-app-watermarks

Environment
-----------
    FRED_API_KEY              Optional; FRED section skipped when unset
    AETHELON_USE_ORCHESTRATOR Soft-wire kill switch (default on)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

# ---------------------------------------------------------------------------
# Repo root on sys.path (allow ``python scripts/test_ingestion.py`` from anywhere)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aethelon.ingestion import (  # noqa: E402
    DEFAULT_FRED_SERIES,
    DEFAULT_RSS_FEEDS,
    FOREX_FACTORY_WEEKLY_URL,
    IngestionConfig,
    IngestionOrchestrator,
    default_ingestion_config,
)
from aethelon.ingestion.client import AsyncHttpClient, RetryPolicy  # noqa: E402
from aethelon.ingestion.config import (  # noqa: E402
    DEFAULT_FRED_RECENT_LIMIT,
    FRED_API_KEY_ENV,
)
from aethelon.ingestion.watermark import WatermarkManager  # noqa: E402


# =============================================================================
# Helpers
# =============================================================================

def _utc_now_iso() -> str:
    """Current UTC time as ISO 8601 with ``Z`` suffix."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _fred_key_present() -> bool:
    """True when ``FRED_API_KEY`` is set in the environment (non-empty)."""
    return bool((os.environ.get(FRED_API_KEY_ENV) or "").strip())


def _classify_item(item: dict[str, Any]) -> str:
    """
    Bucket a ``NormalizedItem`` into rss / ff / fred / other.

    Uses ``kind`` when present; falls back to well-known field heuristics.
    """
    kind = str(item.get("kind") or "").strip().lower()
    if kind in ("rss", "atom"):
        return "rss"
    if kind in ("ff_calendar", "ff", "forex_factory"):
        return "ff"
    if kind in ("fred",):
        return "fred"
    if item.get("series_id") and item.get("date") is not None:
        return "fred"
    if item.get("source") == "ForexFactory" or item.get("currency") is not None and item.get("forecast") is not None:
        src = str(item.get("source") or "")
        if src in ("ForexFactory", "TradingEconomics") or kind.startswith("ff"):
            return "ff"
    if item.get("feed_url") or item.get("link") is not None and item.get("title"):
        if str(item.get("source") or "") not in ("ForexFactory",):
            return "rss"
    return "other"


def _item_preview(item: dict[str, Any], *, width: int = 72) -> str:
    """One-line human preview of a normalized item."""
    kind = _classify_item(item)
    when = str(item.get("datetime") or item.get("date") or "?")
    if kind == "fred":
        sid = item.get("series_id") or "?"
        val = item.get("value")
        body = f"{sid} = {val}"
    elif kind == "ff":
        ccy = item.get("currency") or "?"
        title = str(item.get("title") or "")[:50]
        body = f"{ccy} | {title}"
    else:
        src = item.get("source") or "?"
        title = str(item.get("title") or "")[:50]
        body = f"{src} | {title}"
    line = f"[{kind:4}] {when}  {body}"
    if len(line) > width:
        return line[: width - 3] + "..."
    return line


def _print_section(title: str) -> None:
    """Print a simple section banner."""
    bar = "=" * 72
    print()
    print(bar)
    print(f"  {title}")
    print(bar)


_INVALID_FRED_IDS: frozenset[str] = frozenset(
    {"GOLDAMGBD228NLBM", "EUROUSDM", "GBPUSDM", "COREPCE", "DEXUSCH"}
)


def _assert_source_hygiene() -> None:
    """Fail fast if known-bad FRED ids or the stalling Nasdaq feed return."""
    leftover = _INVALID_FRED_IDS.intersection(DEFAULT_FRED_SERIES)
    if leftover:
        raise AssertionError(f"invalid FRED ids still in defaults: {sorted(leftover)}")
    if "PCEPILFE" not in DEFAULT_FRED_SERIES:
        raise AssertionError("PCEPILFE (Core PCE) missing from default FRED series")
    if "DEXSZUS" not in DEFAULT_FRED_SERIES:
        raise AssertionError("DEXSZUS (CHF/USD) missing from default FRED series")
    if "Nasdaq Markets" in DEFAULT_RSS_FEEDS:
        raise AssertionError("Nasdaq Markets must stay off the default RSS list")


def _check_rss_timeout_isolation() -> None:
    """Fail if one hung RSS fetch can block the rest of the sequential pass."""
    from unittest.mock import patch

    from aethelon.ingestion.orchestrator import IngestionOrchestrator

    class _FakeHttp:
        is_open = True

    async def _fetch(
        self: Any,
        feed_url: str,
        *,
        source_name: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if "slow" in feed_url:
            await asyncio.sleep(30.0)
            return []
        return [{"kind": "rss", "title": "ok", "source": source_name or ""}]

    async def _run() -> None:
        with tempfile.TemporaryDirectory(prefix="aethelon-rss-to-") as td:
            orch = IngestionOrchestrator(
                http=_FakeHttp(),  # type: ignore[arg-type]
                watermark_path=Path(td) / "wm.json",
            )
            with patch("aethelon.ingestion.orchestrator.RSSDriver.fetch", new=_fetch):
                t0 = time.perf_counter()
                items = await orch._run_rss(
                    [
                        ("Slow", "http://slow.example/rss"),
                        ("Fast", "http://fast.example/rss"),
                    ],
                    fail_soft=True,
                )
                elapsed = time.perf_counter() - t0
        if elapsed >= 15.0:
            raise AssertionError(
                f"slow RSS feed still blocked the pass ({elapsed:.1f}s)"
            )
        sources = {str(item.get("source") or "") for item in items}
        if "Fast" not in sources:
            raise AssertionError(f"fast feed did not run after slow timeout: {items!r}")

    asyncio.run(_run())


def _build_config(*, quick: bool) -> IngestionConfig:
    """
    Build the config used for this test run.

    ``quick`` trims RSS/FRED to a small subset so the script finishes faster
    on flaky networks while still exercising all three driver families.
    """
    base = default_ingestion_config()
    if not quick:
        return base

    # A few resilient public feeds + core macro series
    quick_rss: dict[str, str] = {}
    for name in (
        "Yahoo Finance",
        "Fed Reserve News",
        "Google News Fed",
    ):
        url = DEFAULT_RSS_FEEDS.get(name)
        if url:
            quick_rss[name] = url
    if not quick_rss and DEFAULT_RSS_FEEDS:
        # Fallback: first two configured feeds
        for i, (n, u) in enumerate(DEFAULT_RSS_FEEDS.items()):
            if i >= 2:
                break
            quick_rss[n] = u

    configured = set(DEFAULT_FRED_SERIES)
    quick_fred: list[str] = [
        sid for sid in ("FEDFUNDS", "CPIAUCSL", "UNRATE", "DGS10") if sid in configured
    ]
    if not quick_fred:
        quick_fred = list(DEFAULT_FRED_SERIES)[:3]

    return IngestionConfig(
        rss_feeds=quick_rss,
        fred_series=quick_fred,
        fred_series_meta=dict(base.fred_series_meta),
        forex_factory_url=base.forex_factory_url,
        fred_observations_url=base.fred_observations_url,
        fred_recent_limit=min(5, int(base.fred_recent_limit or DEFAULT_FRED_RECENT_LIMIT)),
        run_forex_factory=True,
        fail_soft=True,
    )


def _summarize(items: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Partition items by family for reporting."""
    buckets: dict[str, list[dict[str, Any]]] = {
        "rss": [],
        "ff": [],
        "fred": [],
        "other": [],
    }
    for raw in items:
        if not isinstance(raw, dict):
            continue
        buckets[_classify_item(raw)].append(raw)
    return buckets


def _print_family(
    label: str,
    rows: Sequence[dict[str, Any]],
    *,
    samples: int,
    extra: str = "",
) -> None:
    """Print count + sample lines for one source family."""
    suffix = f"  ({extra})" if extra else ""
    print(f"\n  {label}: {len(rows)} item(s){suffix}")
    if not rows:
        print("    (none)")
        return
    # Light per-source breakdown for RSS
    if label.upper().startswith("RSS"):
        by_src = Counter(str(r.get("source") or "?") for r in rows)
        top = by_src.most_common(8)
        if top:
            joined = ", ".join(f"{n}={c}" for n, c in top)
            print(f"    by source: {joined}")
    if label.upper().startswith("FRED"):
        by_sid = Counter(str(r.get("series_id") or "?") for r in rows)
        top = by_sid.most_common(12)
        if top:
            joined = ", ".join(f"{n}={c}" for n, c in top)
            print(f"    by series: {joined}")
    show = list(rows)[: max(0, samples)]
    for row in show:
        print(f"    • {_item_preview(row)}")
    if len(rows) > samples:
        print(f"    … {len(rows) - samples} more")


# =============================================================================
# Core orchestrator run
# =============================================================================

async def run_orchestrator_test(
    *,
    config: IngestionConfig,
    watermark_path: Path,
    samples: int,
    quick: bool = False,
) -> dict[str, Any]:
    """
    Execute one full orchestrator pass and return a result dict for reporting.

    Does not open or write the analytical database. Watermarks are written only
    to ``watermark_path`` (temp by default).

    When ``quick`` is True, uses a shorter HTTP timeout and fewer retries so
    offline / firewalled environments fail fast instead of multi-minute waits.
    """
    key_ok = _fred_key_present()
    watermarks = WatermarkManager(path=watermark_path, autosave=True)

    # Conservative defaults for a smoke script; production client stays unchanged.
    if quick:
        http = AsyncHttpClient(
            timeout=8.0,
            retry=RetryPolicy(max_retries=1, initial_delay=0.2, max_delay=1.0),
        )
    else:
        http = AsyncHttpClient()

    t0 = time.perf_counter()
    async with IngestionOrchestrator(
        http=http,
        watermarks=watermarks,
        config=config,
    ) as orch:
        items = await orch.run()
    elapsed = time.perf_counter() - t0

    buckets = _summarize(list(items))
    return {
        "items": list(items),
        "buckets": buckets,
        "elapsed_s": elapsed,
        "fred_key_present": key_ok,
        "fred_requested": len(list(config.fred_series)),
        "rss_configured": len(dict(config.rss_feeds)),
        "ff_enabled": bool(config.run_forex_factory),
        "ff_url": config.forex_factory_url,
        "watermark_path": str(watermark_path),
        "samples": samples,
        "quick": quick,
    }


def print_orchestrator_report(result: dict[str, Any]) -> None:
    """Pretty-print the orchestrator test result."""
    _print_section("IngestionOrchestrator result")
    print(f"  Finished at : {_utc_now_iso()}")
    print(f"  Elapsed     : {result['elapsed_s']:.1f}s")
    print(f"  Watermarks  : {result['watermark_path']}")
    print(f"  Config RSS  : {result['rss_configured']} feed(s)")
    print(f"  Config FF   : {'yes' if result['ff_enabled'] else 'no'}  ({result['ff_url']})")
    print(f"  Config FRED : {result['fred_requested']} series requested")

    if result["fred_key_present"]:
        print(f"  FRED key    : present ({FRED_API_KEY_ENV} is set)")
    else:
        print(
            f"  FRED key    : MISSING — FRED section skipped "
            f"(set {FRED_API_KEY_ENV} to enable)"
        )

    buckets: dict[str, list[dict[str, Any]]] = result["buckets"]
    samples = int(result["samples"])
    total = sum(len(v) for v in buckets.values())
    print(f"\n  Total NormalizedItem rows: {total}")

    _print_family("RSS / Atom", buckets["rss"], samples=samples)
    _print_family("Forex Factory", buckets["ff"], samples=samples)
    fred_extra = (
        "fetched"
        if result["fred_key_present"]
        else f"skipped — no {FRED_API_KEY_ENV}"
    )
    _print_family("FRED", buckets["fred"], samples=samples, extra=fred_extra)
    if buckets["other"]:
        _print_family("Other", buckets["other"], samples=samples)


# =============================================================================
# Optional soft-wire check (news_engine adapters, fetch-only)
# =============================================================================

def run_soft_wire_check(*, samples: int = 3) -> int:
    """
    Exercise B3.4 soft-wire surfaces without merging into the live store.

    Imports ``news_engine`` adapter helpers and runs a **tiny** orchestrator
    collect through the same classification/adapt path used by the live
    fetch wrappers. Does not call ``_merge_*`` or open the analytical DB
    deliberately from this script.

    Returns
    -------
    int
        0 on success, 1 on failure.
    """
    _print_section("Soft-wire check (news_engine adapters)")
    try:
        import news_engine as ne
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: could not import news_engine: {exc}")
        return 1

    enabled = bool(ne._orchestrator_enabled())
    has_pkg = bool(getattr(ne, "_HAS_INGESTION_ORCHESTRATOR", False))
    print(f"  _HAS_INGESTION_ORCHESTRATOR : {has_pkg}")
    print(f"  _orchestrator_enabled()    : {enabled}")
    print(
        f"  AETHELON_USE_ORCHESTRATOR  : "
        f"{os.environ.get('AETHELON_USE_ORCHESTRATOR', '(unset → default on)')}"
    )

    if not has_pkg:
        print("  FAIL: ingestion package not visible to news_engine")
        return 1

    # Synthetic NormalizedItem → legacy row (no network)
    rss_raw: dict[str, Any] = {
        "source": "Test Feed",
        "title": "B3.5 soft-wire sample headline",
        "summary": "adapter only",
        "link": "https://example.com/x",
        "datetime": _utc_now_iso(),
        "kind": "rss",
    }
    ff_raw: dict[str, Any] = {
        "source": "ForexFactory",
        "title": "Test CPI",
        "currency": "USD",
        "impact": 3,
        "forecast": "1.0",
        "previous": "0.9",
        "actual": "",
        "datetime": _utc_now_iso(),
        "kind": "ff_calendar",
        "raw": {"title": "Test CPI"},
    }
    fred_raw: list[dict[str, Any]] = [
        {
            "series_id": "CPIAUCSL",
            "date": "2026-07-01",
            "value": 3.14,
            "kind": "fred",
            "datetime": "2026-07-01T00:00:00Z",
        }
    ]

    rss_row = ne._adapt_rss_normalized(rss_raw)
    ff_row = ne._adapt_ff_normalized(ff_raw)
    fred_groups = ne._adapt_fred_normalized_groups(fred_raw)

    ok = True
    if not rss_row or "title" not in rss_row or "datetime" not in rss_row:
        print("  FAIL: RSS adapter did not produce a legacy row")
        ok = False
    else:
        print(f"  RSS adapter  OK → {rss_row.get('source')!r} | {rss_row.get('title')!r}")

    if not ff_row or ff_row.get("impact") != 3:
        print("  FAIL: FF adapter did not produce a legacy row")
        ok = False
    else:
        print(
            f"  FF  adapter  OK → {ff_row.get('currency')!r} | "
            f"{ff_row.get('title')!r} impact={ff_row.get('impact')}"
        )

    if "CPIAUCSL" not in fred_groups or not fred_groups["CPIAUCSL"]:
        print("  FAIL: FRED adapter grouping failed")
        ok = False
    else:
        print(
            f"  FRED adapter OK → CPIAUCSL "
            f"({len(fred_groups['CPIAUCSL'])} obs)"
        )

    # Optional live soft-wire fetch (network) — keep small via env already set
    # by caller; we only probe that the public fetch functions are callable
    # and return list/dict types without raising into the test harness.
    print("\n  Probing fetch entry points (may use network; fail-soft)…")
    try:
        # Prefer a very small orchestrator-backed path: call helpers that the
        # live listener uses. These adapt + return data; they do not merge.
        # To avoid a multi-minute full RSS sweep here, we only hit FF + FRED
        # when the orchestrator is enabled; RSS full list is covered above
        # by the direct orchestrator test.
        ff_items = ne._fetch_ff_calendar()
        print(f"  _fetch_ff_calendar() → {len(ff_items)} event(s)")
        for row in list(ff_items)[:samples]:
            title = str(row.get("title") or "")[:48]
            print(f"    • FF  {row.get('datetime')}  {row.get('currency')} | {title}")
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN: _fetch_ff_calendar raised: {exc}")
        ok = False

    if _fred_key_present():
        try:
            fred_map = ne._fetch_all_fred()
            n_series = len(fred_map) if isinstance(fred_map, dict) else 0
            n_obs = sum(len(v) for v in fred_map.values()) if isinstance(fred_map, dict) else 0
            print(f"  _fetch_all_fred()    → {n_series} series / {n_obs} obs")
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN: _fetch_all_fred raised: {exc}")
            ok = False
    else:
        print(f"  _fetch_all_fred()    → skipped (no {FRED_API_KEY_ENV})")

    if ok:
        print("\n  Soft-wire check: PASS")
        return 0
    print("\n  Soft-wire check: FAIL")
    return 1


# =============================================================================
# CLI
# =============================================================================

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(
        description=(
            "B3.5 — Verify IngestionOrchestrator (and optional news_engine soft-wire). "
            "Does not write the analytical database."
        )
    )
    p.add_argument(
        "--quick",
        action="store_true",
        help="Use a small RSS/FRED subset for a faster smoke run",
    )
    p.add_argument(
        "--soft-wire",
        action="store_true",
        help="Also exercise news_engine B3.4 adapters / fetch entry points",
    )
    p.add_argument(
        "--use-app-watermarks",
        action="store_true",
        help=(
            "Use the real AppData watermarks.json "
            "(default: isolated temp file so production catch-up is untouched)"
        ),
    )
    p.add_argument(
        "--samples",
        type=int,
        default=3,
        help="How many sample rows to print per family (default: 3)",
    )
    p.add_argument(
        "--skip-orchestrator",
        action="store_true",
        help="Skip the direct orchestrator run (soft-wire only)",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    CLI entry point.

    Returns process exit code (0 = success).
    """
    args = parse_args(argv)
    samples = max(0, int(args.samples))

    _print_section("Aethelon B3.5 — Ingestion path test")
    _assert_source_hygiene()
    _check_rss_timeout_isolation()
    print("  Hygiene     : FRED ids + Nasdaq list + RSS per-feed timeout OK")
    print(f"  Repo        : {_REPO_ROOT}")
    print(f"  Time (UTC)  : {_utc_now_iso()}")
    print(f"  Mode        : {'quick' if args.quick else 'full default config'}")
    print(f"  Soft-wire   : {'yes' if args.soft_wire else 'no'}")
    print(f"  FRED key    : {'set' if _fred_key_present() else 'NOT set'}")
    print(f"  Default RSS : {len(DEFAULT_RSS_FEEDS)} | FRED series: {len(DEFAULT_FRED_SERIES)}")
    print(f"  FF URL      : {FOREX_FACTORY_WEEKLY_URL}")
    print()
    print("  Note: This script does NOT write news_engine_store.db")
    print("        (analytical store / NLP / GUI are out of scope).")

    exit_code = 0
    tmp_dir: Optional[tempfile.TemporaryDirectory[str]] = None
    try:
        if not args.skip_orchestrator:
            config = _build_config(quick=bool(args.quick))
            if args.use_app_watermarks:
                # Real AppData path via WatermarkManager default
                wm_path = WatermarkManager().path
                print(f"\n  Using AppData watermarks: {wm_path}")
            else:
                tmp_dir = tempfile.TemporaryDirectory(prefix="aethelon-b35-")
                wm_path = Path(tmp_dir.name) / "watermarks.json"
                print(f"\n  Using temp watermarks: {wm_path}")

            try:
                result = asyncio.run(
                    run_orchestrator_test(
                        config=config,
                        watermark_path=wm_path,
                        samples=samples,
                        quick=bool(args.quick),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                print(f"\n  FAIL: orchestrator run raised: {exc}")
                return 1

            print_orchestrator_report(result)

            # Soft success criteria: no crash; empty families are OK (network /
            # watermarks / missing key). Warn if *everything* is empty while
            # FF was enabled — likely total network failure.
            buckets = result["buckets"]
            total = sum(len(v) for v in buckets.values())
            if total == 0 and result["ff_enabled"]:
                print(
                    "\n  WARN: zero items from all sources. "
                    "Check network, feed availability, or watermarks."
                )
            else:
                print("\n  Orchestrator run: PASS (completed without exception)")

        if args.soft_wire:
            sw = run_soft_wire_check(samples=samples)
            if sw != 0:
                exit_code = sw

    finally:
        if tmp_dir is not None:
            tmp_dir.cleanup()

    _print_section("Done")
    print(f"  Exit code: {exit_code}")
    print()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
