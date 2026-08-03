# -*- coding: utf-8 -*-
"""
backfill_macro_history.py — Build a chronological MacroState ledger
===================================================================
Walks historical FRED data month-by-month, applies textbook 5-dial rules,
and upserts each snapshot into the `macro_state` table.

Safe to re-run: each (as_of date, rules_version) is replaced, never duplicated.

Usage:
    python backfill_macro_history.py
    python backfill_macro_history.py --start 2022-01-01 --end 2026-07-01
    python backfill_macro_history.py --db-only          # no API; use stored fred_series only
    python backfill_macro_history.py --no-persist-fred  # don't write long history into fred_series

Notes:
  • The live engine only keeps ~5 FRED points per series. By default this
    script downloads multi-year history from the FRED API (same key as the engine),
    merges it with anything already in SQLite, then builds the timeline.
  • Frequency default is monthly (month-end snapshots) — ideal for macro regimes.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

# Ensure project root is on path when run as a script
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from macro_state_analyzer import MacroStateAnalyzer, RULES_VERSION, BACKFILL_SERIES
from paths import get_db_path_str, set_db_path_override, describe_paths


def _default_api_key() -> str:
    # Prefer env; fall back to news_engine constant if present
    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        return key
    try:
        from news_engine import FRED_API_KEY as _K
        return str(_K or "").strip()
    except Exception:
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill MacroState historical ledger from FRED.")
    parser.add_argument("--start", default="2022-01-01", help="Timeline start YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="Timeline end YYYY-MM-DD (default: today)")
    parser.add_argument("--freq", default="M", choices=["M", "Q", "D"],
                        help="Snapshot frequency: M=monthly, Q=quarterly, D=daily")
    parser.add_argument("--db-only", action="store_true",
                        help="Use only fred_series already in SQLite (no FRED API download)")
    parser.add_argument("--no-persist-fred", action="store_true",
                        help="Do not write extended FRED history back into fred_series")
    parser.add_argument(
        "--db",
        default=None,
        help="Path to news_engine_store.db (default: %%APPDATA%%\\Aethelon\\data\\)",
    )
    parser.add_argument("--api-key", default=None, help="FRED API key (or set FRED_API_KEY)")
    args = parser.parse_args(argv)

    end = args.end or date.today().isoformat()
    if args.db:
        db_path = str(set_db_path_override(args.db))
    else:
        db_path = get_db_path_str(migrate=True)

    print("=" * 64)
    print("  MacroState historical backfill")
    print("=" * 64)
    print(f"  DB:            {db_path}")
    print(f"  Range:         {args.start} → {end}")
    print(f"  Frequency:     {args.freq}")
    print(f"  Rules version: {RULES_VERSION}")
    print(f"  Mode:          {'DB only' if args.db_only else 'FRED API + DB merge'}")
    print()

    analyzer = MacroStateAnalyzer(db_path=db_path, auto_save=True)

    # 1) Load whatever is already stored
    stored = analyzer.load_fred_from_db()
    print(f"  Stored FRED series in DB: {len(stored)}")
    for sid in BACKFILL_SERIES:
        n = len(stored.get(sid) or [])
        if n:
            print(f"    - {sid}: {n} points")

    # 2) Optionally fetch deep history (needed for multi-year ledger)
    fetched: dict = {}
    if not args.db_only:
        api_key = (args.api_key or _default_api_key()).strip()
        if not api_key:
            print("\n  ERROR: No FRED API key. Set FRED_API_KEY or pass --api-key,")
            print("         or re-run with --db-only (limited history).")
            return 1
        print("\n  Downloading multi-year FRED history…")
        fetched = analyzer.fetch_fred_history(
            api_key=api_key,
            series_ids=list(BACKFILL_SERIES),
            start=args.start,
            end=end,
        )
        print(f"  Downloaded {len(fetched)} series.")

    # 3) Merge sources
    history = analyzer.merge_fred_histories(stored, fetched)
    if not history:
        print("\n  ERROR: No FRED observations available to backfill.")
        return 1

    print(f"\n  Merged history: {len(history)} series")
    for sid in sorted(history.keys()):
        obs = history[sid]
        print(f"    - {sid}: {len(obs)} points  ({obs[0].get('date')} → {obs[-1].get('date')})")

    # 4) Optionally persist long history into fred_series for future research
    if fetched and not args.no_persist_fred:
        n = analyzer.persist_fred_series_to_db(history)
        print(f"\n  Persisted extended FRED history for {n} series into fred_series.")

    # 5) Backfill macro_state timeline (upsert — safe to re-run)
    print("\n  Computing MacroState timeline…")
    result = analyzer.backfill_history(
        fred_history=history,
        start=args.start,
        end=end,
        frequency=args.freq,
        save=True,
    )

    if not result.get("ok"):
        print(f"\n  BACKFILL FAILED: {result.get('error')}")
        return 1

    print(f"\n  Computed: {result['computed']}  |  Saved/updated: {result['saved']}  "
          f"|  Skipped: {result['skipped']}")

    # 6) Human-readable summary
    print("\n" + "=" * 64)
    print("  HISTORICAL REGIME SUMMARY")
    print("=" * 64)
    text = analyzer.format_history_summary(result.get("summary") or {})
    print(text)

    # Sample trail: first / mid / last
    states = result.get("states") or []
    if states:
        print("\n  Sample path (first → middle → last):")
        for s in (states[0], states[len(states) // 2], states[-1]):
            print(f"    {s.get('summary_line')}")

    # Prove idempotency hint
    print("\n  Re-run safety: rows key on (as_of, rules_version) — second run updates, does not duplicate.")
    print("  Done.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
