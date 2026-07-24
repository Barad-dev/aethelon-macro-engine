# -*- coding: utf-8 -*-
"""
research_desk_data.py — Research Desk Data Aggregator (Step 5.1)
================================================================
Single clean dict/JSON payload for the future Research Desk UI.

Does NOT touch GUI layout. Pulls from existing engines / SQLite:

  1. Current MacroState (regime, confidence, lesson, dials)
  2. Active InstrumentThesis for XAUUSD / EURUSD / GBPUSD / USDCHF
  3. Historical regime distribution (macro_state ledger)
  4. Recent economic surprises + optional event-study sample query

CLI / GUI can both call:

    from research_desk_data import build_research_desk

    desk = build_research_desk()          # read-only from DB + light engines
    desk = build_research_desk(ctx=ctx)   # enrich from an existing get_news_context()

Safe: every section isolated; missing pieces return empty/default blocks.
"""

from __future__ import annotations

import json
import os
import traceback
from datetime import datetime
from typing import Any, Optional


try:
    from paths import get_db_path_str as _resolve_db
    def _default_db() -> str:
        return _resolve_db(migrate=True)
except Exception:  # pragma: no cover
    def _default_db() -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_engine_store.db")

TRACKED_SYMBOLS = ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF")
DESK_SCHEMA_VERSION = "research_desk_v1"


# =============================================================================
# SECTION BUILDERS
# =============================================================================

def _section_error(name: str, exc: BaseException) -> dict:
    return {
        "ok": False,
        "section": name,
        "error": f"{type(exc).__name__}: {exc}",
        "data": None,
    }


def _build_macro_state(db_path: str, ctx: Optional[dict] = None) -> dict:
    """1) Current macro regime + lesson."""
    try:
        macro = None
        if ctx and isinstance(ctx.get("macro_state"), dict) and ctx["macro_state"].get("regime"):
            macro = dict(ctx["macro_state"])
        else:
            from macro_state_analyzer import MacroStateAnalyzer
            analyzer = MacroStateAnalyzer(db_path=db_path, auto_save=False)
            macro = analyzer.latest_state()

        if not macro:
            return {
                "ok": True,
                "available": False,
                "message": "No MacroState in database yet. Run the engine or backfill_macro_history.py.",
                "regime": None,
                "confidence": None,
                "lesson": None,
                "dials": {},
                "summary_line": None,
                "as_of": None,
                "raw": None,
            }

        dials = {
            "growth": macro.get("growth"),
            "inflation": macro.get("inflation"),
            "policy": macro.get("policy"),
            "liquidity": macro.get("liquidity"),
            "risk": macro.get("risk"),
        }
        return {
            "ok": True,
            "available": True,
            "regime": macro.get("regime"),
            "confidence": macro.get("confidence"),
            "lesson": macro.get("lesson"),
            "dials": dials,
            "scores": {
                "growth": macro.get("growth_score"),
                "inflation": macro.get("inflation_score"),
                "policy": macro.get("policy_score"),
                "liquidity": macro.get("liquidity_score"),
                "risk": macro.get("risk_score"),
            },
            "summary_line": macro.get("summary_line"),
            "as_of": macro.get("as_of"),
            "rules_version": macro.get("rules_version"),
            "raw": {
                k: macro.get(k)
                for k in (
                    "regime", "growth", "inflation", "policy", "liquidity", "risk",
                    "confidence", "as_of", "lesson", "summary_line",
                )
            },
        }
    except Exception as exc:
        return _section_error("macro_state", exc)


def _build_instrument_theses(db_path: str, ctx: Optional[dict] = None) -> dict:
    """2) Active theses for all 4 symbols."""
    try:
        theses: list[dict] = []
        if ctx and isinstance(ctx.get("instrument_theses"), list) and ctx["instrument_theses"]:
            theses = list(ctx["instrument_theses"])
        else:
            from instrument_thesis import InstrumentThesisEngine, TRACKED_SYMBOLS as TH_SYMS
            engine = InstrumentThesisEngine(db_path=db_path)
            theses = engine.get_all()
            # If empty but we have macro, generate (no force side-effects beyond upsert)
            if not theses:
                from macro_state_analyzer import MacroStateAnalyzer
                macro = MacroStateAnalyzer(db_path=db_path, auto_save=False).latest_state()
                if macro and macro.get("regime"):
                    theses = engine.update_from_macro_state(macro, write_history=False)

        by_symbol: dict[str, dict] = {}
        for t in theses:
            sym = (t.get("symbol") or "").upper()
            if not sym:
                continue
            by_symbol[sym] = {
                "symbol": sym,
                "current_bias": t.get("current_bias"),
                "active_thesis": t.get("active_thesis"),
                "invalidation_triggers": t.get("invalidation_triggers"),
                "regime": t.get("regime"),
                "macro_as_of": t.get("macro_as_of"),
                "last_updated": t.get("last_updated"),
                "playbook_version": t.get("playbook_version"),
                # short fields for UI cards
                "reason_short": _truncate(t.get("active_thesis"), 220),
                "invalidation_short": _truncate(t.get("invalidation_triggers"), 160),
            }

        ordered = []
        for sym in TRACKED_SYMBOLS:
            ordered.append(by_symbol.get(sym) or {
                "symbol": sym,
                "current_bias": None,
                "active_thesis": None,
                "invalidation_triggers": None,
                "available": False,
            })
            if sym in by_symbol:
                ordered[-1]["available"] = True

        return {
            "ok": True,
            "available": any(x.get("available") for x in ordered),
            "symbols": list(TRACKED_SYMBOLS),
            "theses": ordered,
            "by_symbol": by_symbol,
            "count": sum(1 for x in ordered if x.get("available")),
        }
    except Exception as exc:
        return _section_error("instrument_theses", exc)


def _build_regime_history(db_path: str, limit: int = 120) -> dict:
    """3) Historical regime distribution from macro_state ledger."""
    try:
        from macro_state_analyzer import MacroStateAnalyzer
        analyzer = MacroStateAnalyzer(db_path=db_path, auto_save=False)
        summary = analyzer.summarize_history()
        history_rows = analyzer.history(limit=limit)
        # history() is newest-first; normalize for timeline display
        timeline = []
        for row in reversed(history_rows):
            timeline.append({
                "as_of": row.get("as_of"),
                "regime": row.get("regime"),
                "growth": row.get("growth"),
                "inflation": row.get("inflation"),
                "policy": row.get("policy"),
                "liquidity": row.get("liquidity"),
                "risk": row.get("risk"),
                "confidence": row.get("confidence"),
            })

        regimes = summary.get("regimes") or {}
        n = int(summary.get("n") or 0) or 1
        distribution = [
            {
                "regime": name,
                "count": cnt,
                "pct": round(100.0 * cnt / n, 1),
            }
            for name, cnt in regimes.items()
        ]

        return {
            "ok": True,
            "available": bool(summary.get("n")),
            "from": summary.get("from"),
            "to": summary.get("to"),
            "n_snapshots": summary.get("n", 0),
            "unit": summary.get("unit"),
            "distribution": distribution,
            "regimes": regimes,
            "growth": summary.get("growth") or {},
            "inflation": summary.get("inflation") or {},
            "policy": summary.get("policy") or {},
            "liquidity": summary.get("liquidity") or {},
            "risk": summary.get("risk") or {},
            "timeline": timeline,
            "summary_text": analyzer.format_history_summary(summary) if summary.get("n") else "",
        }
    except Exception as exc:
        return _section_error("regime_history", exc)


def _build_event_study(
    db_path: str,
    recent_limit: int = 15,
    sample_family: str = "CORE_CPI",
    sample_min_surprise: float = 0.1,
    sample_regime: Optional[str] = None,
) -> dict:
    """4) Recent surprises + a sample event-study reaction block."""
    try:
        from event_study_engine import EventStudyEngine
        engine = EventStudyEngine(db_path=db_path)

        recent = engine.query_events(limit=recent_limit)
        recent_fmt = []
        for e in recent:
            recent_fmt.append({
                "event_key": e.get("event_key"),
                "event_family": e.get("event_family"),
                "title": e.get("title"),
                "currency": e.get("currency"),
                "event_time": e.get("event_time"),
                "actual_raw": e.get("actual_raw"),
                "forecast_raw": e.get("forecast_raw"),
                "surprise_raw": e.get("surprise_raw"),
                "surprise_pct": e.get("surprise_pct"),
                "surprise_direction": e.get("surprise_direction"),
                "beat_miss": e.get("beat_miss"),
                "regime": e.get("regime"),
                "impact": e.get("impact"),
                "instrument_signals": e.get("instrument_signals") or {},
            })

        # Prefer sample study in current regime if known
        regime_for_study = sample_regime
        if not regime_for_study:
            try:
                from macro_state_analyzer import MacroStateAnalyzer
                ms = MacroStateAnalyzer(db_path=db_path, auto_save=False).latest_state()
                if ms:
                    regime_for_study = ms.get("regime")
            except Exception:
                pass

        study = engine.study_reaction(
            event_family=sample_family,
            min_surprise=sample_min_surprise,
            regime=regime_for_study,
            surprise_side="positive",
            horizon="immediate",
        )
        # Fallbacks so the desk always shows *something* useful if data is thin
        if study.get("n_events", 0) == 0:
            study = engine.study_reaction(
                event_family=sample_family,
                min_surprise=0.0,
                regime=None,
                surprise_side="either",
                horizon="immediate",
            )
        if study.get("n_events", 0) == 0 and recent_fmt:
            fam = recent_fmt[0].get("event_family") or "CPI"
            study = engine.study_reaction(
                event_family=fam,
                min_surprise=0.0,
                regime=None,
                surprise_side="either",
                horizon="immediate",
            )

        return {
            "ok": True,
            "available": engine.count() > 0,
            "ledger_size": engine.count(),
            "recent_surprises": recent_fmt,
            "recent_count": len(recent_fmt),
            "sample_study": {
                "query": study.get("query"),
                "n_events": study.get("n_events"),
                "symbol_reactions": study.get("symbol_reactions"),
                "events_preview": study.get("events_preview"),
                "note": study.get("note"),
                "report_text": EventStudyEngine.format_study_report(study),
            },
            "horizons_supported": ["immediate", "24h", "72h"],
        }
    except Exception as exc:
        return _section_error("event_study", exc)


# =============================================================================
# PUBLIC API
# =============================================================================

def build_research_desk(
    db_path: Optional[str] = None,
    ctx: Optional[dict] = None,
    recent_surprises: int = 15,
    history_limit: int = 120,
    sample_event_family: str = "CORE_CPI",
    sample_min_surprise: float = 0.1,
) -> dict:
    """
    Aggregate all Research Desk sections into one display-ready dict.

    Parameters
    ----------
    db_path : optional path to news_engine_store.db
              (default: %%APPDATA%%\\Quantamental\\data\\news_engine_store.db)
    ctx : optional get_news_context() result to prefer live fields
    """
    path = db_path or _default_db()
    generated_at = datetime.now().isoformat(timespec="seconds")

    macro = _build_macro_state(path, ctx=ctx)
    theses = _build_instrument_theses(path, ctx=ctx)
    history = _build_regime_history(path, limit=history_limit)

    sample_regime = None
    if isinstance(macro, dict) and macro.get("ok") and macro.get("regime"):
        sample_regime = macro.get("regime")

    events = _build_event_study(
        path,
        recent_limit=recent_surprises,
        sample_family=sample_event_family,
        sample_min_surprise=sample_min_surprise,
        sample_regime=sample_regime,
    )

    sections_ok = {
        "macro_state": bool(isinstance(macro, dict) and macro.get("ok")),
        "instrument_theses": bool(isinstance(theses, dict) and theses.get("ok")),
        "regime_history": bool(isinstance(history, dict) and history.get("ok")),
        "event_study": bool(isinstance(events, dict) and events.get("ok")),
    }

    desk = {
        "schema_version": DESK_SCHEMA_VERSION,
        "generated_at": generated_at,
        "db_path": path,
        "sections_ok": sections_ok,
        "all_ok": all(sections_ok.values()),
        # The four Research Desk blocks
        "macro_state": macro,
        "instrument_theses": theses,
        "regime_history": history,
        "event_study": events,
        # Convenience header for status bars
        "header": {
            "regime": macro.get("regime") if isinstance(macro, dict) else None,
            "confidence": macro.get("confidence") if isinstance(macro, dict) else None,
            "as_of": macro.get("as_of") if isinstance(macro, dict) else None,
            "thesis_count": theses.get("count") if isinstance(theses, dict) else 0,
            "history_snapshots": history.get("n_snapshots") if isinstance(history, dict) else 0,
            "surprise_ledger": events.get("ledger_size") if isinstance(events, dict) else 0,
        },
    }

    # Stage A: validate against Pydantic v2 contracts (non-fatal — preserve dict API)
    try:
        from models.desk_schemas import parse_research_desk
        validated = parse_research_desk(desk)
        # Round-trip keeps aliases (`from`/`to`) and coerces types for consumers
        desk = validated.to_desk_dict()
    except Exception as _val_exc:
        # Never break the live desk if schema drifts; callers still get raw dict
        desk["_schema_validation_warning"] = f"{type(_val_exc).__name__}: {_val_exc}"

    return desk


def build_research_desk_model(
    db_path: Optional[str] = None,
    ctx: Optional[dict] = None,
    **kwargs,
):
    """
    Typed Research Desk entrypoint (Pydantic v2 ResearchDeskPayload).

    Prefer this when callers want strict models; GUI may keep using
    build_research_desk() → dict for drop-in compatibility.
    """
    from models.desk_schemas import ResearchDeskPayload, parse_research_desk
    raw = build_research_desk(db_path=db_path, ctx=ctx, **kwargs)
    # Strip non-schema diagnostic key if present
    raw = {k: v for k, v in raw.items() if k != "_schema_validation_warning"}
    return parse_research_desk(raw)


def research_desk_to_json(desk: Optional[dict] = None, **kwargs) -> str:
    """Serialize desk payload as pretty JSON (datetime-safe)."""
    payload = desk if desk is not None else build_research_desk(**kwargs)
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False)


def format_research_desk_preview(desk: dict) -> str:
    """Human-readable smoke-test printout (not the full GUI)."""
    lines = [
        "=" * 72,
        "  RESEARCH DESK DATA AGGREGATOR  —  preview",
        "=" * 72,
        f"  generated_at: {desk.get('generated_at')}",
        f"  schema:       {desk.get('schema_version')}",
        f"  all_ok:       {desk.get('all_ok')}  sections={desk.get('sections_ok')}",
        "",
    ]

    # 1 Macro
    m = desk.get("macro_state") or {}
    lines.append("  [1] MACRO STATE")
    if m.get("ok") and m.get("available"):
        lines.append(f"      Regime:     {m.get('regime')}  (confidence {m.get('confidence')})")
        lines.append(f"      As of:      {m.get('as_of')}")
        dials = m.get("dials") or {}
        lines.append(
            f"      Dials:      growth={dials.get('growth')} inflation={dials.get('inflation')} "
            f"policy={dials.get('policy')} liquidity={dials.get('liquidity')} risk={dials.get('risk')}"
        )
        lesson = (m.get("lesson") or "").splitlines()
        if lesson:
            lines.append(f"      Lesson:     {lesson[0][:100]}")
            if len(lesson) > 1:
                lines.append(f"                 … (+{len(lesson)-1} more lines in full payload)")
    else:
        lines.append(f"      (unavailable) {m.get('message') or m.get('error') or m}")
    lines.append("")

    # 2 Theses
    t = desk.get("instrument_theses") or {}
    lines.append("  [2] INSTRUMENT THESES")
    if t.get("ok"):
        for item in t.get("theses") or []:
            bias = item.get("current_bias") or "—"
            lines.append(f"      {item.get('symbol'):<8}  {bias}")
            if item.get("reason_short"):
                lines.append(f"               {item['reason_short'][:100]}…")
            if item.get("invalidation_short"):
                lines.append(f"               Invalidation: {item['invalidation_short'][:90]}")
    else:
        lines.append(f"      (error) {t.get('error')}")
    lines.append("")

    # 3 History
    h = desk.get("regime_history") or {}
    lines.append("  [3] REGIME HISTORY")
    if h.get("ok") and h.get("available"):
        lines.append(f"      Range: {h.get('from')} → {h.get('to')}  ({h.get('n_snapshots')} snapshots)")
        for row in (h.get("distribution") or [])[:8]:
            lines.append(f"      • {row['regime']:<22} {row['count']:>4}  ({row['pct']}%)")
    else:
        lines.append(f"      (unavailable) {h.get('error') or h.get('message') or 'no history'}")
    lines.append("")

    # 4 Event study
    e = desk.get("event_study") or {}
    lines.append("  [4] EVENT STUDY / SURPRISES")
    if e.get("ok"):
        lines.append(f"      Ledger size: {e.get('ledger_size')}  |  recent shown: {e.get('recent_count')}")
        for s in (e.get("recent_surprises") or [])[:5]:
            lines.append(
                f"      [{s.get('event_time')}] {s.get('event_family')}: "
                f"surprise={s.get('surprise_raw')}  {str(s.get('title') or '')[:40]}  "
                f"regime={s.get('regime')}"
            )
        study = e.get("sample_study") or {}
        lines.append(
            f"      Sample study: family={((study.get('query') or {}).get('event_family'))}  "
            f"n={study.get('n_events')}  regime={((study.get('query') or {}).get('regime'))}"
        )
        for sym, st in (study.get("symbol_reactions") or {}).items():
            lines.append(
                f"         {sym}: dominant={st.get('dominant_direction')}  "
                f"n={st.get('n')}  UP={st.get('pct_up')}% DOWN={st.get('pct_down')}%"
            )
    else:
        lines.append(f"      (error) {e.get('error')}")

    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


def _truncate(text: Optional[str], n: int) -> Optional[str]:
    if text is None:
        return None
    s = str(text).strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


# =============================================================================
# LIGHT WIRING HELPER (for news_engine / future GUI)
# =============================================================================

def get_research_desk_from_context(ctx: Optional[dict] = None, **kwargs) -> dict:
    """
    Preferred entry when you already have get_news_context() output.
    Falls back to DB-only aggregation if ctx is None.
    """
    return build_research_desk(ctx=ctx, **kwargs)


# =============================================================================
# SELF-TEST
# =============================================================================

if __name__ == "__main__":
    print("Building Research Desk payload (DB-only, no GUI)…\n")
    try:
        desk = build_research_desk()
        print(format_research_desk_preview(desk))
        # Compact JSON stats for machine verification
        print("\nJSON keys:", list(desk.keys()))
        print("Header:", json.dumps(desk.get("header"), default=str))
        print("Section availability:")
        for name in ("macro_state", "instrument_theses", "regime_history", "event_study"):
            block = desk.get(name) or {}
            print(f"  {name}: ok={block.get('ok')} available={block.get('available', 'n/a')}")
        # Prove serializable
        blob = research_desk_to_json(desk)
        print(f"\nJSON serialization OK ({len(blob)} chars)")
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
