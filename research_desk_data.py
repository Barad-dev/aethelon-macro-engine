# -*- coding: utf-8 -*-
"""
research_desk_data.py — Research Desk Data Aggregator (Stage A refined)
=======================================================================
Builds a typed Research Desk payload via Pydantic v2 models in
`models.desk_schemas`, then exposes:

  build_research_desk()        → dict  (model_dump JSON mode — UI / IPC)
  build_research_desk_model()  → ResearchDeskPayload
  research_desk_to_json()      → str   (strict JSON)

Sections:
  1. MacroState  (+ layman_meaning, market_impact)
  2. Instrument theses (+ layman_meaning, market_impact per symbol)
  3. Regime history distribution
  4. Event-study / surprise ledger

Safe: every section isolated; missing pieces return empty/default blocks.
"""

from __future__ import annotations

import json
import os
import traceback
from datetime import datetime, timezone
from typing import Any, Optional, Union

from models.desk_schemas import (
    DeskHeader,
    EventStudyItem,
    EventStudySection,
    InstrumentThesisCard,
    InstrumentThesesSection,
    MacroDials,
    MacroScores,
    MacroStateSection,
    MarketImpact,
    RegimeDistributionRow,
    RegimeHistorySection,
    RegimeTimelineRow,
    ResearchDeskPayload,
    SampleStudyBlock,
    SectionsOk,
    dumps_research_desk,
    parse_research_desk,
)

try:
    from paths import get_db_path_str as _resolve_db

    def _default_db() -> str:
        return _resolve_db(migrate=True)
except Exception:  # pragma: no cover
    def _default_db() -> str:
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "news_engine_store.db"
        )

TRACKED_SYMBOLS = ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF")
# Bumped with layman_meaning / market_impact / polyglot JSON contract
DESK_SCHEMA_VERSION = "research_desk_v2"

DeskResult = Union[ResearchDeskPayload, dict[str, Any]]


# =============================================================================
# PLAIN-LANGUAGE + MARKET IMPACT HELPERS
# =============================================================================

# Regime → plain English (teaching approximations, not Fed definitions)
_REGIME_LAYMAN: dict[str, str] = {
    "REFLATION": (
        "The economy is growing and prices are still rising. Policymakers often "
        "keep rates relatively firm, which can support the dollar and cap gold."
    ),
    "GOLDILOCKS": (
        "Growth is decent while inflation is calm — a 'just right' backdrop. "
        "Risk assets often do well; gold and defensive currencies get less of a bid."
    ),
    "STAGFLATION": (
        "Growth is weak but inflation stays sticky. Markets dislike this mix: "
        "gold can attract hedge demand while growth-linked currencies struggle."
    ),
    "DISINFLATION": (
        "Price pressures are cooling. If growth holds up, policy can ease later — "
        "often softer for the dollar and friendlier for gold over time."
    ),
    "RECESSION": (
        "Activity is contracting or jobs are cracking. Investors seek safety: "
        "gold and CHF tend to find demand; cyclical FX can sell off."
    ),
    "TIGHTENING": (
        "Policy is restrictive — higher real rates. That usually pressures gold "
        "and supports the currency of the hawkish central bank (often USD)."
    ),
    "EASING": (
        "Policy is accommodative or cutting. Lower real rates historically support "
        "gold and can weigh on the dollar if cuts are priced aggressively."
    ),
    "RISK_OFF": (
        "Fear is elevated. Money flows toward perceived safe havens (gold, CHF, USD) "
        "and away from risk-sensitive currencies."
    ),
    "RISK_ON": (
        "Risk appetite is strong. Investors prefer growth assets; gold and pure "
        "safe-haven bids often fade."
    ),
}

# Coarse macro → default directional framing for the USD complex / gold
_REGIME_MACRO_DIRECTION: dict[str, str] = {
    "REFLATION": "MIXED",
    "GOLDILOCKS": "NEUTRAL",
    "STAGFLATION": "BULLISH",   # risk-asset stress / gold-friendly tilt at macro level
    "DISINFLATION": "MIXED",
    "RECESSION": "BULLISH",
    "TIGHTENING": "BEARISH",    # for gold-centric macro read
    "EASING": "BULLISH",
    "RISK_OFF": "BULLISH",
    "RISK_ON": "BEARISH",
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _truncate(text: Optional[str], n: int) -> Optional[str]:
    if text is None:
        return None
    s = str(text).strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _section_error_model(name: str, exc: BaseException) -> dict[str, Any]:
    """Minimal error dict; validated into the section model with ok=False."""
    return {
        "ok": False,
        "section": name,
        "error": f"{type(exc).__name__}: {exc}",
        "available": False,
        "data": None,
    }


def _layman_for_regime(
    regime: Optional[str],
    dials: Optional[dict] = None,
    lesson: Optional[str] = None,
) -> str:
    reg = (regime or "").strip().upper() or "UNKNOWN"
    base = _REGIME_LAYMAN.get(reg)
    if not base:
        base = (
            f"Current textbook regime label is '{reg or 'unknown'}'. "
            "It summarizes growth, inflation, policy, liquidity, and risk dials "
            "into one market-cycle story."
        )
    parts = [base]
    if dials:
        bits = []
        for k in ("growth", "inflation", "policy", "liquidity", "risk"):
            if dials.get(k):
                bits.append(f"{k}={dials[k]}")
        if bits:
            parts.append("Dial snapshot: " + ", ".join(bits) + ".")
    if lesson:
        first = str(lesson).strip().splitlines()[0].strip()
        if first:
            parts.append("Engine lesson: " + _truncate(first, 180))
    return " ".join(p for p in parts if p)


def _macro_market_impact(
    regime: Optional[str],
    confidence: Optional[float],
    dials: Optional[dict] = None,
) -> MarketImpact:
    reg = (regime or "").strip().upper()
    direction = _REGIME_MACRO_DIRECTION.get(reg, "UNKNOWN")
    # Macro impact framed around gold / risk complex (desk default narrative)
    conf = confidence if confidence is not None else 0.5
    if conf > 1.0:
        conf = conf / 100.0
    conf = max(0.0, min(1.0, float(conf)))

    exec_note = (
        f"Treat {reg or 'the current regime'} as a multi-week backdrop, not a trade signal. "
        "Revisit after high-impact data (CPI, NFP, FOMC) or a dial flip."
    )
    if dials and str(dials.get("risk", "")).lower() in ("risk_off", "elevated", "high"):
        exec_note += " Risk dial is elevated — size down and prefer confirmation."

    return MarketImpact(
        direction=direction,  # type: ignore[arg-type]
        probability=round(conf, 4),
        horizon="macro",
        execution_note=exec_note,
        symbols_affected=list(TRACKED_SYMBOLS),
        invalidation=(
            "Regime label changes or confidence collapses after a major data surprise."
        ),
        not_advice=True,
    )


def _thesis_layman(
    symbol: str,
    bias: Optional[str],
    regime: Optional[str],
    thesis: Optional[str],
) -> str:
    b = (bias or "NEUTRAL").upper()
    reg = regime or "the current regime"
    verb = {
        "BULLISH": "favors upside",
        "BEARISH": "favors downside",
        "NEUTRAL": "does not show a strong directional edge",
    }.get(b, "is mixed")
    head = f"{symbol}: playbook bias is {b} under {reg} — the setup {verb}."
    body = _truncate(thesis, 200)
    if body:
        return f"{head} In plain terms: {body}"
    return head


def _thesis_market_impact(
    symbol: str,
    bias: Optional[str],
    confidence: Optional[float],
    invalidation: Optional[str],
) -> MarketImpact:
    b = (bias or "NEUTRAL").upper()
    if b in ("BULL", "LONG"):
        b = "BULLISH"
    elif b in ("BEAR", "SHORT"):
        b = "BEARISH"
    if b not in ("BULLISH", "BEARISH", "NEUTRAL", "MIXED", "UNKNOWN"):
        b = "UNKNOWN"

    conf = confidence if confidence is not None else 0.55
    if conf > 1.0:
        conf = conf / 100.0
    conf = max(0.0, min(1.0, float(conf)))
    # Slightly discount pair-level vs macro confidence
    pair_p = round(min(1.0, conf * 0.95), 4)

    notes = {
        "BULLISH": f"{symbol}: bias long-side on the quoted pair; wait for pullbacks into structure rather than chasing spikes.",
        "BEARISH": f"{symbol}: bias short-side on the quoted pair; fade strength into resistance only with risk defined.",
        "NEUTRAL": f"{symbol}: no edge — stand aside or trade mean-reversion only with tight risk.",
        "MIXED": f"{symbol}: conflicting signals — reduce size until bias clarifies.",
        "UNKNOWN": f"{symbol}: insufficient thesis data for directional framing.",
    }
    return MarketImpact(
        direction=b,  # type: ignore[arg-type]
        probability=pair_p,
        horizon="swing",
        execution_note=notes.get(b, notes["UNKNOWN"]),
        symbols_affected=[symbol],
        invalidation=_truncate(invalidation, 240) or "Playbook invalidation triggers fire.",
        not_advice=True,
    )


# =============================================================================
# SECTION BUILDERS → Pydantic models
# =============================================================================

def _build_macro_state(
    db_path: str, ctx: Optional[dict] = None
) -> MacroStateSection:
    """1) Current macro regime + lesson + layman/impact."""
    try:
        macro = None
        if ctx and isinstance(ctx.get("macro_state"), dict) and ctx["macro_state"].get(
            "regime"
        ):
            macro = dict(ctx["macro_state"])
        else:
            from macro_state_analyzer import MacroStateAnalyzer

            analyzer = MacroStateAnalyzer(db_path=db_path, auto_save=False)
            macro = analyzer.latest_state()

        if not macro:
            return MacroStateSection(
                ok=True,
                available=False,
                message=(
                    "No MacroState in database yet. "
                    "Run the engine or backfill_macro_history.py."
                ),
                layman_meaning=(
                    "There is not enough stored macro history yet to explain "
                    "the cycle in plain language."
                ),
                market_impact=MarketImpact(
                    direction="UNKNOWN",
                    probability=None,
                    horizon="macro",
                    execution_note="No regime — do not lean on macro bias until data is available.",
                    symbols_affected=list(TRACKED_SYMBOLS),
                    not_advice=True,
                ),
            )

        dials_dict = {
            "growth": macro.get("growth"),
            "inflation": macro.get("inflation"),
            "policy": macro.get("policy"),
            "liquidity": macro.get("liquidity"),
            "risk": macro.get("risk"),
        }
        lesson = macro.get("lesson")
        regime = macro.get("regime")
        confidence = macro.get("confidence")

        return MacroStateSection.model_validate(
            {
                "ok": True,
                "available": True,
                "regime": regime,
                "confidence": confidence,
                "lesson": lesson,
                "layman_meaning": _layman_for_regime(regime, dials_dict, lesson),
                "market_impact": _macro_market_impact(
                    regime, confidence, dials_dict
                ).model_dump(mode="json"),
                "dials": dials_dict,
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
                        "regime",
                        "growth",
                        "inflation",
                        "policy",
                        "liquidity",
                        "risk",
                        "confidence",
                        "as_of",
                        "lesson",
                        "summary_line",
                    )
                },
            }
        )
    except Exception as exc:
        return MacroStateSection.model_validate(_section_error_model("macro_state", exc))


def _build_instrument_theses(
    db_path: str,
    ctx: Optional[dict] = None,
    macro_confidence: Optional[float] = None,
) -> InstrumentThesesSection:
    """2) Active theses for all 4 symbols with layman/impact."""
    try:
        theses: list[dict] = []
        if ctx and isinstance(ctx.get("instrument_theses"), list) and ctx[
            "instrument_theses"
        ]:
            theses = list(ctx["instrument_theses"])
        else:
            from instrument_thesis import InstrumentThesisEngine

            engine = InstrumentThesisEngine(db_path=db_path)
            theses = engine.get_all()
            if not theses:
                from macro_state_analyzer import MacroStateAnalyzer

                macro = MacroStateAnalyzer(
                    db_path=db_path, auto_save=False
                ).latest_state()
                if macro and macro.get("regime"):
                    theses = engine.update_from_macro_state(
                        macro, write_history=False
                    )

        by_symbol: dict[str, dict] = {}
        for t in theses:
            sym = (t.get("symbol") or "").upper()
            if not sym:
                continue
            bias = t.get("current_bias")
            active = t.get("active_thesis")
            inv = t.get("invalidation_triggers")
            regime = t.get("regime")
            card = {
                "symbol": sym,
                "current_bias": bias,
                "active_thesis": active,
                "invalidation_triggers": inv,
                "regime": regime,
                "macro_as_of": t.get("macro_as_of"),
                "last_updated": t.get("last_updated"),
                "playbook_version": t.get("playbook_version"),
                "reason_short": _truncate(active, 220),
                "invalidation_short": _truncate(inv, 160),
                "available": True,
                "layman_meaning": _thesis_layman(sym, bias, regime, active),
                "market_impact": _thesis_market_impact(
                    sym, bias, macro_confidence, inv
                ).model_dump(mode="json"),
            }
            by_symbol[sym] = card

        ordered: list[dict] = []
        for sym in TRACKED_SYMBOLS:
            if sym in by_symbol:
                ordered.append(by_symbol[sym])
            else:
                ordered.append(
                    {
                        "symbol": sym,
                        "current_bias": None,
                        "active_thesis": None,
                        "invalidation_triggers": None,
                        "available": False,
                        "layman_meaning": (
                            f"{sym}: no active thesis yet — run the engine "
                            "after a MacroState snapshot exists."
                        ),
                        "market_impact": MarketImpact(
                            direction="UNKNOWN",
                            probability=None,
                            horizon="swing",
                            execution_note=f"{sym}: no playbook bias available.",
                            symbols_affected=[sym],
                            not_advice=True,
                        ).model_dump(mode="json"),
                    }
                )

        return InstrumentThesesSection.model_validate(
            {
                "ok": True,
                "available": any(x.get("available") for x in ordered),
                "symbols": list(TRACKED_SYMBOLS),
                "theses": ordered,
                "by_symbol": by_symbol,
                "count": sum(1 for x in ordered if x.get("available")),
            }
        )
    except Exception as exc:
        return InstrumentThesesSection.model_validate(
            _section_error_model("instrument_theses", exc)
        )


def _build_regime_history(
    db_path: str, limit: int = 120
) -> RegimeHistorySection:
    """3) Historical regime distribution from macro_state ledger."""
    try:
        from macro_state_analyzer import MacroStateAnalyzer

        analyzer = MacroStateAnalyzer(db_path=db_path, auto_save=False)
        summary = analyzer.summarize_history()
        history_rows = analyzer.history(limit=limit)

        timeline = []
        for row in reversed(history_rows):
            timeline.append(
                {
                    "as_of": row.get("as_of"),
                    "regime": row.get("regime"),
                    "growth": row.get("growth"),
                    "inflation": row.get("inflation"),
                    "policy": row.get("policy"),
                    "liquidity": row.get("liquidity"),
                    "risk": row.get("risk"),
                    "confidence": row.get("confidence"),
                }
            )

        regimes = summary.get("regimes") or {}
        n = int(summary.get("n") or 0) or 1
        distribution = [
            {
                "regime": name,
                "count": int(cnt),
                "pct": round(100.0 * float(cnt) / n, 1),
            }
            for name, cnt in regimes.items()
        ]

        return RegimeHistorySection.model_validate(
            {
                "ok": True,
                "available": bool(summary.get("n")),
                "from": summary.get("from"),
                "to": summary.get("to"),
                "n_snapshots": summary.get("n", 0),
                "unit": summary.get("unit"),
                "distribution": distribution,
                "regimes": {str(k): int(v) for k, v in regimes.items()},
                "growth": summary.get("growth") or {},
                "inflation": summary.get("inflation") or {},
                "policy": summary.get("policy") or {},
                "liquidity": summary.get("liquidity") or {},
                "risk": summary.get("risk") or {},
                "timeline": timeline,
                "summary_text": (
                    analyzer.format_history_summary(summary)
                    if summary.get("n")
                    else ""
                ),
            }
        )
    except Exception as exc:
        return RegimeHistorySection.model_validate(
            _section_error_model("regime_history", exc)
        )


def _build_event_study(
    db_path: str,
    recent_limit: int = 15,
    sample_family: str = "CORE_CPI",
    sample_min_surprise: float = 0.1,
    sample_regime: Optional[str] = None,
) -> EventStudySection:
    """4) Recent surprises + a sample event-study reaction block."""
    try:
        from event_study_engine import EventStudyEngine

        engine = EventStudyEngine(db_path=db_path)

        recent = engine.query_events(limit=recent_limit)
        recent_fmt = []
        for e in recent:
            recent_fmt.append(
                {
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
                }
            )

        regime_for_study = sample_regime
        if not regime_for_study:
            try:
                from macro_state_analyzer import MacroStateAnalyzer

                ms = MacroStateAnalyzer(
                    db_path=db_path, auto_save=False
                ).latest_state()
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

        ledger_size = int(engine.count())
        return EventStudySection.model_validate(
            {
                "ok": True,
                "available": ledger_size > 0,
                "ledger_size": ledger_size,
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
        )
    except Exception as exc:
        return EventStudySection.model_validate(
            _section_error_model("event_study", exc)
        )


# =============================================================================
# PUBLIC API
# =============================================================================

def build_research_desk_model(
    db_path: Optional[str] = None,
    ctx: Optional[dict] = None,
    recent_surprises: int = 15,
    history_limit: int = 120,
    sample_event_family: str = "CORE_CPI",
    sample_min_surprise: float = 0.1,
) -> ResearchDeskPayload:
    """
    Aggregate all Research Desk sections into a validated ResearchDeskPayload.

    This is the canonical typed entrypoint (parsing + validation).
    """
    path = db_path or _default_db()
    generated_at = _now_iso()

    macro = _build_macro_state(path, ctx=ctx)
    macro_conf = macro.confidence

    theses = _build_instrument_theses(
        path, ctx=ctx, macro_confidence=macro_conf
    )
    history = _build_regime_history(path, limit=history_limit)

    sample_regime = macro.regime if (macro.ok and macro.regime) else None
    events = _build_event_study(
        path,
        recent_limit=recent_surprises,
        sample_family=sample_event_family,
        sample_min_surprise=sample_min_surprise,
        sample_regime=sample_regime,
    )

    sections_ok = SectionsOk(
        macro_state=bool(macro.ok),
        instrument_theses=bool(theses.ok),
        regime_history=bool(history.ok),
        event_study=bool(events.ok),
    )

    header = DeskHeader(
        regime=macro.regime,
        confidence=macro.confidence,
        as_of=macro.as_of,
        thesis_count=int(theses.count or 0),
        history_snapshots=int(history.n_snapshots or 0),
        surprise_ledger=int(events.ledger_size or 0),
    )

    payload = ResearchDeskPayload(
        schema_version=DESK_SCHEMA_VERSION,
        generated_at=generated_at,
        db_path=path,
        sections_ok=sections_ok,
        all_ok=all(
            (
                sections_ok.macro_state,
                sections_ok.instrument_theses,
                sections_ok.regime_history,
                sections_ok.event_study,
            )
        ),
        macro_state=macro,
        instrument_theses=theses,
        regime_history=history,
        event_study=events,
        header=header,
    )
    # Final validation pass (ensures nested constraints + ISO coercion)
    return ResearchDeskPayload.model_validate(payload.model_dump(mode="python", by_alias=True))


def build_research_desk(
    db_path: Optional[str] = None,
    ctx: Optional[dict] = None,
    recent_surprises: int = 15,
    history_limit: int = 120,
    sample_event_family: str = "CORE_CPI",
    sample_min_surprise: float = 0.1,
) -> dict[str, Any]:
    """
    Aggregate Research Desk → JSON-safe dict (model_dump mode='json').

    Drop-in for GUI / news_engine; always polyglot-serializable.
    """
    try:
        model = build_research_desk_model(
            db_path=db_path,
            ctx=ctx,
            recent_surprises=recent_surprises,
            history_limit=history_limit,
            sample_event_family=sample_event_family,
            sample_min_surprise=sample_min_surprise,
        )
        return model.to_desk_dict()
    except Exception as exc:
        # Absolute last resort — never crash the live UI
        return {
            "schema_version": DESK_SCHEMA_VERSION,
            "generated_at": _now_iso(),
            "db_path": db_path or _default_db(),
            "sections_ok": {
                "macro_state": False,
                "instrument_theses": False,
                "regime_history": False,
                "event_study": False,
            },
            "all_ok": False,
            "macro_state": {"ok": False, "available": False, "error": str(exc)},
            "instrument_theses": {"ok": False, "available": False},
            "regime_history": {"ok": False, "available": False},
            "event_study": {"ok": False, "available": False},
            "header": {},
            "_schema_validation_warning": f"{type(exc).__name__}: {exc}",
        }


def research_desk_to_json(
    desk: Optional[DeskResult] = None, **kwargs
) -> str:
    """Serialize desk payload as pretty polyglot JSON."""
    if desk is None:
        model = build_research_desk_model(**kwargs)
        return dumps_research_desk(model, indent=2)
    if isinstance(desk, ResearchDeskPayload):
        return dumps_research_desk(desk, indent=2)
    # dict path — re-validate then dump
    try:
        return dumps_research_desk(parse_research_desk(desk), indent=2)
    except Exception:
        return json.dumps(desk, indent=2, default=str, ensure_ascii=False)


def format_research_desk_preview(desk: DeskResult) -> str:
    """Human-readable smoke-test printout (not the full GUI)."""
    if isinstance(desk, ResearchDeskPayload):
        d = desk.to_desk_dict()
    else:
        d = desk

    lines = [
        "=" * 72,
        "  RESEARCH DESK DATA AGGREGATOR  —  preview",
        "=" * 72,
        f"  generated_at: {d.get('generated_at')}",
        f"  schema:       {d.get('schema_version')}",
        f"  all_ok:       {d.get('all_ok')}  sections={d.get('sections_ok')}",
        "",
    ]

    m = d.get("macro_state") or {}
    lines.append("  [1] MACRO STATE")
    if m.get("ok") and m.get("available"):
        lines.append(
            f"      Regime:     {m.get('regime')}  (confidence {m.get('confidence')})"
        )
        lines.append(f"      As of:      {m.get('as_of')}")
        dials = m.get("dials") or {}
        lines.append(
            f"      Dials:      growth={dials.get('growth')} inflation={dials.get('inflation')} "
            f"policy={dials.get('policy')} liquidity={dials.get('liquidity')} risk={dials.get('risk')}"
        )
        if m.get("layman_meaning"):
            lines.append(f"      Layman:     {_truncate(m.get('layman_meaning'), 120)}")
        mi = m.get("market_impact") or {}
        if mi:
            lines.append(
                f"      Impact:     dir={mi.get('direction')}  p={mi.get('probability')}  "
                f"horizon={mi.get('horizon')}"
            )
        lesson = (m.get("lesson") or "").splitlines()
        if lesson:
            lines.append(f"      Lesson:     {lesson[0][:100]}")
    else:
        lines.append(f"      (unavailable) {m.get('message') or m.get('error') or m}")
    lines.append("")

    t = d.get("instrument_theses") or {}
    lines.append("  [2] INSTRUMENT THESES")
    if t.get("ok"):
        for item in t.get("theses") or []:
            bias = item.get("current_bias") or "—"
            lines.append(f"      {item.get('symbol'):<8}  {bias}")
            if item.get("layman_meaning"):
                lines.append(f"               {str(item['layman_meaning'])[:110]}")
            mi = item.get("market_impact") or {}
            if mi:
                lines.append(
                    f"               impact: {mi.get('direction')} p={mi.get('probability')}"
                )
            if item.get("invalidation_short"):
                lines.append(
                    f"               Invalidation: {item['invalidation_short'][:90]}"
                )
    else:
        lines.append(f"      (error) {t.get('error')}")
    lines.append("")

    h = d.get("regime_history") or {}
    lines.append("  [3] REGIME HISTORY")
    if h.get("ok") and h.get("available"):
        lines.append(
            f"      Range: {h.get('from')} → {h.get('to')}  ({h.get('n_snapshots')} snapshots)"
        )
        for row in (h.get("distribution") or [])[:8]:
            lines.append(
                f"      • {row['regime']:<22} {row['count']:>4}  ({row['pct']}%)"
            )
    else:
        lines.append(
            f"      (unavailable) {h.get('error') or h.get('message') or 'no history'}"
        )
    lines.append("")

    e = d.get("event_study") or {}
    lines.append("  [4] EVENT STUDY / SURPRISES")
    if e.get("ok"):
        lines.append(
            f"      Ledger size: {e.get('ledger_size')}  |  recent shown: {e.get('recent_count')}"
        )
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


def get_research_desk_from_context(
    ctx: Optional[dict] = None, **kwargs
) -> dict[str, Any]:
    """Preferred entry when you already have get_news_context() output."""
    return build_research_desk(ctx=ctx, **kwargs)


# =============================================================================
# SELF-TEST
# =============================================================================

if __name__ == "__main__":
    print("Building Research Desk payload (Pydantic v2, DB-only)…\n")
    try:
        model = build_research_desk_model()
        desk = model.to_desk_dict()
        print(format_research_desk_preview(model))
        print("\nJSON keys:", list(desk.keys()))
        print("Header:", json.dumps(desk.get("header"), ensure_ascii=False))
        print("Schema:", desk.get("schema_version"))
        ms = desk.get("macro_state") or {}
        print("Macro layman:", (ms.get("layman_meaning") or "")[:120])
        print("Macro impact:", ms.get("market_impact"))
        th = (desk.get("instrument_theses") or {}).get("theses") or []
        if th:
            print("First thesis layman:", (th[0].get("layman_meaning") or "")[:120])
            print("First thesis impact:", th[0].get("market_impact"))
        blob = research_desk_to_json(model)
        # Prove pure JSON types (no datetime leftovers)
        parsed = json.loads(blob)
        assert isinstance(parsed.get("generated_at"), (str, type(None)))
        print(f"\nJSON serialization OK ({len(blob)} chars) — polyglot-safe")
        print("all_ok:", model.all_ok)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
