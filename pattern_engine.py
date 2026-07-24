# -*- coding: utf-8 -*-
"""
pattern_engine.py — Context Memory, Pattern Detection & Forward Calendar
========================================================================
The "brain" layer that sits on top of the news engine. It remembers past
news flow, detects recurring patterns, correlates events, and looks ahead
at the upcoming calendar to anticipate market-moving catalysts.

Key capabilities:
  • CONTEXT MEMORY — stores every analyzed event with timestamps so the
    engine can reason over the full session history, not just the latest
    snapshot.
  • PATTERN DETECTION — identifies recurring themes (e.g. "inflation
    surprise cluster", "hawkish shift", "safe-haven rotation") by
    correlating multiple events over time windows.
  • FORWARD CALENDAR AWARENESS — scans upcoming FF calendar events in
    the next 24–72h and flags high-impact catalysts that the market
    is pricing in *before* they happen.
  • CORRELATION ENGINE — detects when two independent sources report
    the same theme (confirmation), which raises conviction.
  • REGIME DETECTION — classifies the overall macro regime (risk-on,
    risk-off, hawkish, dovish, transition) based on the aggregate flow.
  • SIGNAL CONVERGENCE — when multiple independent signals point the
    same direction for an instrument, conviction rises. When they
    conflict, conviction is tempered and a "mixed signals" flag is set.
"""

from __future__ import annotations

import re
import math
import threading
from collections import deque, defaultdict
from datetime import datetime, timedelta
from typing import Optional# =============================================================================
# REGIME DEFINITIONS
# =============================================================================

REGIMES = {
    "RISK_OFF": {
        "description": "Safe-haven demand dominant; gold and CHF bid, risk currencies sold.",
        "gold_bias": "BULL", "dollar_bias": "MIXED",
        "typical_drivers": ["geopolitical tension", "recession fears", "crisis",
                           "market selloff", "flight to safety"],
    },
    "RISK_ON": {
        "description": "Risk appetite strong; gold sold, growth currencies bid.",
        "gold_bias": "BEAR", "dollar_bias": "MIXED",
        "typical_drivers": ["recovery", "expansion", "strong gdp", "robust data",
                           "rate hike", "hawkish"],
    },
    "HAWKISH": {
        "description": "Central bank tightening bias; USD firm, gold pressured.",
        "gold_bias": "BEAR", "dollar_bias": "BULL",
        "typical_drivers": ["rate hike", "hawkish", "inflation up", "strong payrolls",
                           "fomc hawkish", "taper"],
    },
    "DOVISH": {
        "description": "Central bank easing bias; USD soft, gold supported.",
        "gold_bias": "BULL", "dollar_bias": "BEAR",
        "typical_drivers": ["rate cut", "dovish", "inflation down", "weak payrolls",
                           "qe", "recession"],
    },
    "TRANSITION": {
        "description": "Mixed signals; regime unclear, market searching for direction.",
        "gold_bias": "NEUTRAL", "dollar_bias": "NEUTRAL",
        "typical_drivers": [],
    },
    "STAGFLATION": {
        "description": "Stagnant growth + rising inflation; gold strongly bid, USD mixed.",
        "gold_bias": "BULL", "dollar_bias": "MIXED",
        "typical_drivers": ["stagflation", "inflation up", "gdp down",
                           "recession", "supply shock"],
    },
}

# Pattern templates: each fires when N matching events occur within a time window
PATTERN_TEMPLATES: list[dict] = [
    {
        "id": "inflation_surprise_cluster",
        "name": "Inflation Surprise Cluster",
        "description": "Multiple inflation-related beats in a short window signal sticky inflation.",
        "keywords": ["cpi", "inflation", "pce", "core cpi", "core pce"],
        "min_matches": 2,
        "window_hours": 48,
        "regime_hint": "HAWKISH",
        "gold_implication": "BEAR short-term (real yields) / BULL if growth fears dominate",
    },
    {
        "id": "hawkish_shift",
        "name": "Hawkish Policy Shift",
        "description": "Central bank officials or data consistently signaling tighter policy.",
        "keywords": ["hawkish", "rate hike", "taper", "fomc", "powell", "fed"],
        "min_matches": 3,
        "window_hours": 72,
        "regime_hint": "HAWKISH",
        "gold_implication": "BEAR — higher real yields pressure non-yielding gold",
    },
    {
        "id": "dovish_shift",
        "name": "Dovish Policy Shift",
        "description": "Central bank officials or data consistently signaling easier policy.",
        "keywords": ["dovish", "rate cut", "qe", "recession", "weak payrolls"],
        "min_matches": 3,
        "window_hours": 72,
        "regime_hint": "DOVISH",
        "gold_implication": "BULL — lower real yields support gold",
    },
    {
        "id": "safe_haven_rotation",
        "name": "Safe-Haven Rotation",
        "description": "Geopolitical or crisis events driving flight to safety.",
        "keywords": ["war", "conflict", "sanction", "crisis", "geopolit", "safe haven",
                     "flight to safety", "risk off", "escalation"],
        "min_matches": 2,
        "window_hours": 24,
        "regime_hint": "RISK_OFF",
        "gold_implication": "BULL — safe-haven demand surges",
    },
    {
        "id": "recession_signals",
        "name": "Recession Signal Buildup",
        "description": "Multiple growth deterioration signals raising recession odds.",
        "keywords": ["recession", "contraction", "slowdown", "gdp down",
                     "weak payrolls", "hard landing", "selloff"],
        "min_matches": 2,
        "window_hours": 72,
        "regime_hint": "RISK_OFF",
        "gold_implication": "BULL — recession fears drive safe-haven bid",
    },
    {
        "id": "dollar_strength_narrative",
        "name": "Dollar Strength Narrative Building",
        "description": "Consistent USD-positive data and rhetoric.",
        "keywords": ["strong dollar", "dxy up", "rate hike", "hawkish", "strong payrolls",
                     "gdp up", "ism up"],
        "min_matches": 3,
        "window_hours": 48,
        "regime_hint": "HAWKISH",
        "gold_implication": "BEAR — strong dollar pressures gold",
    },
    {
        "id": "dollar_weakness_narrative",
        "name": "Dollar Weakness Narrative Building",
        "description": "Consistent USD-negative data and rhetoric.",
        "keywords": ["weak dollar", "rate cut", "dovish", "weak payrolls",
                     "gdp down", "recession"],
        "min_matches": 3,
        "window_hours": 48,
        "regime_hint": "DOVISH",
        "gold_implication": "BULL — weak dollar supports gold",
    },
    {
        "id": "stagflation_risk",
        "name": "Stagflation Risk Emerging",
        "description": "Rising inflation + weakening growth = stagflationary pressure.",
        "keywords": ["stagflation", "inflation up", "gdp down", "recession",
                     "slowdown", "supply shock"],
        "min_matches": 2,
        "window_hours": 96,
        "regime_hint": "STAGFLATION",
        "gold_implication": "BULL — gold is the classic stagflation hedge",
    },
    {
        "id": "trade_tension_escalation",
        "name": "Trade Tension Escalation",
        "description": "Tariff/trade war headlines escalating.",
        "keywords": ["tariff", "trade war", "sanction", "embargo"],
        "min_matches": 2,
        "window_hours": 48,
        "regime_hint": "RISK_OFF",
        "gold_implication": "BULL — trade tensions support safe-haven assets",
    },
]


# =============================================================================
# CONTEXT MEMORY STORE
# =============================================================================

_MEMORY_LOCK = threading.RLock()

# Stores all analyzed events for the session, keyed by a unique ID
_memory_store: dict[str, dict] = {}

# Per-instrument pressure timeline: {instrument: deque[(datetime, score)]}
_pressure_timeline: dict[str, deque] = {
    "XAUUSD": deque(maxlen=2000),
    "EURUSD": deque(maxlen=2000),
    "GBPUSD": deque(maxlen=2000),
    "USDCHF": deque(maxlen=2000),
}

# Per-instrument event log: {instrument: deque[(datetime, event_summary)]}
_event_log: dict[str, deque] = {
    "XAUUSD": deque(maxlen=500),
    "EURUSD": deque(maxlen=500),
    "GBPUSD": deque(maxlen=500),
    "USDCHF": deque(maxlen=500),
}

# Detected patterns history
_detected_patterns: deque = deque(maxlen=200)

# Regime history
_regime_history: deque = deque(maxlen=100)

# Current regime
_current_regime: str = "TRANSITION"
_regime_confidence: float = 0.0


def _make_event_id(event: dict) -> str:
    """Generate a unique ID for an analyzed event."""
    title = event.get("title", "")[:80]
    source = event.get("source", "")
    dt = event.get("datetime")
    dt_str = dt.isoformat() if isinstance(dt, datetime) else str(dt)
    return re.sub(r"\W+", "", f"{source}{title}{dt_str}")[:80]


def remember_event(analyzed_event: dict) -> None:
    """Store an analyzed event in context memory for pattern detection."""
    eid = _make_event_id(analyzed_event)
    with _MEMORY_LOCK:
        _memory_store[eid] = {
            **analyzed_event,
            "_remembered_at": datetime.now(),
        }

        # Update per-instrument event logs
        for inst in ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF"):
            weights = analyzed_event.get("instrument_weights", {})
            if inst in weights:
                _event_log[inst].append((
                    analyzed_event.get("datetime", datetime.now()),
                    {
                        "title": analyzed_event.get("title", ""),
                        "source": analyzed_event.get("source", ""),
                        "weight": weights[inst],
                        "label": analyzed_event.get("instrument_signals", {}).get(inst, "NEUTRAL"),
                    }
                ))


def record_pressure(instrument: str, score: float, timestamp: Optional[datetime] = None) -> None:
    """Record a pressure data point for an instrument."""
    ts = timestamp or datetime.now()
    with _MEMORY_LOCK:
        if instrument in _pressure_timeline:
            _pressure_timeline[instrument].append((ts, score))


def get_memory_snapshot() -> dict:
    """Return a snapshot of the memory store for diagnostics."""
    with _MEMORY_LOCK:
        return {
            "total_events_remembered": len(_memory_store),
            "pressure_points": {k: len(v) for k, v in _pressure_timeline.items()},
            "event_log_sizes": {k: len(v) for k, v in _event_log.items()},
            "detected_patterns": len(_detected_patterns),
            "current_regime": _current_regime,
            "regime_confidence": _regime_confidence,
        }


# =============================================================================
# PATTERN DETECTION
# =============================================================================

def detect_patterns(lookback_hours: int = 72) -> list[dict]:
    """
    Scan the memory store for recurring patterns within the lookback window.
    Returns a list of detected pattern dicts with conviction levels.
    """
    cutoff = datetime.now() - timedelta(hours=lookback_hours)
    detected: list[dict] = []

    with _MEMORY_LOCK:
        recent_events = [
            ev for ev in _memory_store.values()
            if ev.get("_remembered_at", datetime.min) >= cutoff
        ]

    for template in PATTERN_TEMPLATES:
        matches = []
        for ev in recent_events:
            text = (ev.get("title", "") + " " +
                    " ".join(ev.get("macro_reasoning", []))).lower()
            if any(kw in text for kw in template["keywords"]):
                matches.append(ev)

        if len(matches) >= template["min_matches"]:
            # Calculate conviction based on match count, recency, and source diversity
            base_conviction = min(len(matches) / (template["min_matches"] * 2), 1.0)
            sources = set(ev.get("source", "") for ev in matches)
            source_diversity = min(len(sources) / 3, 1.0)
            conviction = (base_conviction * 0.6 + source_diversity * 0.4)

            # Recency boost: more recent matches = higher conviction
            now = datetime.now()
            recent_matches = [
                m for m in matches
                if m.get("_remembered_at", datetime.min) >
 now - timedelta(hours=template["window_hours"] // 2)
            ]
            recency_boost = min(len(recent_matches) / len(matches), 1.0) * 0.2
            conviction = min(conviction + recency_boost, 1.0)

            pattern = {
                "pattern_id": template["id"],
                "pattern_name": template["name"],
                "description": template["description"],
                "match_count": len(matches),
                "min_required": template["min_matches"],
                "window_hours": template["window_hours"],
                "conviction": round(conviction, 2),
                "regime_hint": template["regime_hint"],
                "gold_implication": template["gold_implication"],
                "matching_events": [
                    {"title": m.get("title", ""), "source": m.get("source", ""),
                     "datetime": m.get("datetime")}
 for m in matches[:8]
                ],
                "detected_at": datetime.now(),
            }
            detected.append(pattern)

            with _MEMORY_LOCK:
                _detected_patterns.append(pattern)

    # Sort by conviction descending
    detected.sort(key=lambda x: -x["conviction"])
    return detected


# =============================================================================
# SIGNAL CONVERGENCE ANALYSIS
# =============================================================================

def analyze_signal_convergence(analyzed_events: list[dict]) -> dict[str, dict]:
    """
    For each instrument, check whether multiple independent sources agree
    (convergence = high conviction) or conflict (divergence = low conviction).
    """
    instrument_signals: dict[str, list[dict]] = defaultdict(list)

    for ev in analyzed_events:
        weights = ev.get("instrument_weights", {})
        for inst, w in weights.items():
            if inst in ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF"):
                instrument_signals[inst].append({
                    "source": ev.get("source", "unknown"),
                    "weight": w,
                    "title": ev.get("title", ""),
                    "datetime": ev.get("datetime"),
                })

    convergence: dict[str, dict] = {}
    for inst, signals in instrument_signals.items():
        if not signals:
            convergence[inst] = {
                "direction": "NEUTRAL", "conviction": 0.0,
                "agreement": 0.0, "source_count": 0,
                "conflict": False, "note": "No signals",
            }
            continue

        bullish = [s for s in signals if s["weight"] > 0]
        bearish = [s for s in signals if s["weight"] < 0]
        total = len(signals)
        dominant_side = "BULL" if len(bullish) >= len(bearish) else "BEAR"
        dominant_count = max(len(bullish), len(bearish))
        minority_count = min(len(bullish), len(bearish))
        agreement = dominant_count / total if total > 0 else 0.0
        conflict = minority_count > 0 and minority_count / total > 0.3

        avg_weight = sum(s["weight"] for s in signals) / total
        unique_sources = set(s["source"] for s in signals)
        source_diversity = min(len(unique_sources) / 3, 1.0)

        conviction = abs(avg_weight) * agreement * (0.5 + 0.5 * source_diversity)
        conviction = min(conviction, 1.0)

        direction = "BULL" if avg_weight > 0.15 else ("BEAR" if avg_weight < -0.15 else "NEUTRAL")

        note_parts = []
        if conflict:
            note_parts.append("MIXED SIGNALS — sources disagree")
        if len(unique_sources) >= 3:
            note_parts.append(f"Multi-source confirmation ({len(unique_sources)} sources)")
        if agreement >= 0.8:
            note_parts.append("Strong consensus")
        if not note_parts:
            note_parts.append("Moderate agreement")

        convergence[inst] = {
            "direction": direction,
            "conviction": round(conviction, 2),
            "agreement": round(agreement, 2),
            "source_count": len(unique_sources),
            "signal_count": total,
            "conflict": conflict,
            "avg_weight": round(avg_weight, 3),
            "note": " | ".join(note_parts),
        }

    return convergence


# =============================================================================
# REGIME DETECTION
# =============================================================================

def detect_regime(analyzed_events: list[dict],
                  detected_patterns: list[dict]) -> dict:
    """
    Classify the current macro regime based on aggregate news flow and
    detected patterns. Returns regime name, confidence, and supporting evidence.
    """
    regime_scores: dict[str, float] = {r: 0.0 for r in REGIMES}

    # Score from patterns
    for pat in detected_patterns:
        hint = pat.get("regime_hint", "TRANSITION")
        if hint in regime_scores:
            regime_scores[hint] += pat.get("conviction", 0) * 2.0

    # Score from individual event keywords
    for ev in analyzed_events:
        text = (ev.get("title", "") + " " +
                " ".join(ev.get("macro_reasoning", []))).lower()

        for regime_name, regime_info in REGIMES.items():
            for driver in regime_info.get("typical_drivers", []):
                if driver in text:
                    weight = ev.get("instrument_weights", {})
                    max_w = max((abs(v) for v in weight.values()), default=0.5)
                    regime_scores[regime_name] += 0.3 * max_w

    # Determine dominant regime
    best_regime = max(regime_scores, key=regime_scores.get)
    total_score = sum(regime_scores.values())
    confidence = regime_scores[best_regime] / total_score if total_score > 0 else 0.0

    global _current_regime, _regime_confidence
    with _MEMORY_LOCK:
        _current_regime = best_regime
        _regime_confidence = confidence
        _regime_history.append({
            "regime": best_regime,
            "confidence": confidence,
            "scores": dict(regime_scores),
            "timestamp": datetime.now(),
        })

    return {
        "regime": best_regime,
        "confidence": round(confidence, 2),
        "description": REGIMES[best_regime]["description"],
        "gold_bias": REGIMES[best_regime]["gold_bias"],
        "dollar_bias": REGIMES[best_regime]["dollar_bias"],
        "all_scores": {k: round(v, 2) for k, v in regime_scores.items()},
    }


# =============================================================================
# FORWARD CALENDAR AWARENESS
# =============================================================================

def analyze_forward_calendar(ff_events: list[dict],
 lookforward_hours: int = 72) -> list[dict]:
    """
    Scan upcoming FF calendar events and flag high-impact catalysts
    that the market is pricing in BEFORE they happen.
    """
    now = datetime.now()
    horizon = now + timedelta(hours=lookforward_hours)
    upcoming: list[dict] = []

    for ev in ff_events:
        ev_dt = ev.get("datetime")
        if not isinstance(ev_dt, datetime):
            continue
        if ev_dt < now or ev_dt > horizon:
            continue
        if ev.get("impact", 0) < 2:
            continue

        # Estimate market pre-positioning
        hours_until = (ev_dt - now).total_seconds() / 3600
        if hours_until < 1:
            urgency = "IMMINENT"
            pre_positioning = "Market should be fully positioned; expect volatility spike."
        elif hours_until < 6:
            urgency = "VERY SOON"
            pre_positioning = "Pre-positioning intensifying; expect tightening ranges then breakout."
        elif hours_until < 24:
            urgency = "WITHIN 24H"
            pre_positioning = "Pre-positioning building; watch for directional drift."
        else:
            urgency = "UPCOMING"
            pre_positioning = "On the radar; gradual positioning may begin."

        # Guess category and project impact
        title = ev.get("title", "")
        cat = _guess_category(title)
        short_t, long_t = _project_impact(cat, "neutral")

        # Check if there's an actual yet
        has_actual = bool(ev.get("actual"))
        if has_actual:
            status = "RELEASED"
        else:
            status = "SCHEDULED"

        upcoming.append({
            "title": title,
            "currency": ev.get("currency", ""),
            "impact": ev.get("impact", 1),
            "forecast": ev.get("forecast", ""),
            "previous": ev.get("previous", ""),
            "actual": ev.get("actual", ""),
            "datetime": ev_dt,
            "hours_until": round(hours_until, 1),
            "urgency": urgency,
            "status": status,
            "category": cat,
            "pre_positioning_note": pre_positioning,
            "estimated_impact": short_t,
        })

    upcoming.sort(key=lambda x: x["datetime"])
    return upcoming


def _guess_category(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["cpi", "inflation", "pce", "price"]):         return "inflation"
    if any(w in t for w in ["payroll", "nfp", "unemployment", "jobs"]):   return "employment"
    if any(w in t for w in ["gdp", "growth", "output"]):                  return "gdp"
    if any(w in t for w in ["rate", "fomc", "federal", "ecb", "boe"]):    return "rate"
    if any(w in t for w in ["yield", "treasury", "bond"]):                return "yield"
    if any(w in t for w in ["dollar", "dxy", "index"]):                   return "dollar_index"
    if any(w in t for w in ["gold", "xau", "silver"]):                    return "commodity"
    if any(w in t for w in ["eur", "gbp", "usd", "chf"]):                 return "fx"
    return "general"


def _project_impact(category: str, direction: str) -> tuple[str, str]:
    projections = {
        "inflation":      ("Gold: BULL if inflation hot | USD: BULL if Fed hikes",
                           "Gold: depends on growth context | USD: depends on Fed response"),
        "employment":     ("USD: BULL if strong | Gold: BEAR if strong",
                           "USD: BULL if strong | Gold: BEAR if strong"),
        "gdp":            ("USD: BULL if strong | Gold: BEAR if strong",
                           "USD: BULL if strong | Gold: BEAR if strong"),
        "rate":           ("USD: BULL if hike | Gold: BEAR if hike",
                           "USD: BULL if hike | Gold: BEAR if hike"),
        "yield":          ("Gold: BEAR if yields rise | USD: BULL if yields rise",
                           "Gold: BEAR if yields rise | USD: BULL if yields rise"),
        "dollar_index":   ("EURUSD/GBPUSD: BEAR if DXY up | Gold: BEAR if DXY up",
                           "EURUSD/GBPUSD: BEAR if DXY up | Gold: BEAR if DXY up"),
        "commodity":      ("Gold: direct impact",
                           "Gold: direct impact"),
        "fx":             ("Pair-specific",
                           "Pair-specific"),
 "general":        ("Unclear — monitor context",
                           "Unclear — monitor context"),
    }
    return projections.get(category, ("Unknown", "Unknown"))


# =============================================================================
# TREND ANALYSIS (per-instrument pressure over time)
# =============================================================================

def analyze_pressure_trend(instrument: str, window: int = 10) -> dict:
    """
    Analyze the pressure trend for an instrument over recent snapshots.
    Uses linear regression slope to determine direction and acceleration.
    """
    with _MEMORY_LOCK:
        timeline = list(_pressure_timeline.get(instrument, deque()))

    if len(timeline) < 3:
        return {
            "instrument": instrument,
            "trend": "INSUFFICIENT DATA",
            "slope": 0.0,
            "acceleration": 0.0,
            "current": 0.0,
            "average": 0.0,
            "volatility": 0.0,
            "data_points": len(timeline),
        }

    values = [v for _, v in timeline[-window:]]
    n = len(values)

    # Linear regression slope
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    slope = numerator / denominator if denominator != 0 else 0.0

    # Acceleration (second derivative)
    if n >= 4:
        half = n // 2
        slope_first = _linear_slope(values[:half])
        slope_second = _linear_slope(values[half:])
        acceleration = slope_second - slope_first
    else:
        acceleration = 0.0

    # Volatility (std dev)
    variance = sum((v - y_mean) ** 2 for v in values) / n
    volatility = math.sqrt(variance)

    current = values[-1]

    if abs(slope) < 0.1:
        trend = "STABLE"
    elif slope > 0:
        if acceleration > 0.1:
            trend = "ACCELERATING BULLISH"
        elif acceleration < -0.1:
            trend = "FADING BULLISH"
        else:
            trend = "STEADY BULLISH"
    else:
        if acceleration < -0.1:
            trend = "ACCELERATING BEARISH"
        elif acceleration > 0.1:
            trend = "FADING BEARISH"
        else:
            trend = "STEADY BEARISH"

    return {
        "instrument": instrument,
        "trend": trend,
        "slope": round(slope, 3),
        "acceleration": round(acceleration, 3),
        "current": round(current, 2),
        "average": round(y_mean, 2),
        "volatility": round(volatility, 2),
        "data_points": n,
    }


def _linear_slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    return numerator / denominator if denominator != 0 else 0.0


# =============================================================================
# CORRELATION ENGINE
# =============================================================================

def detect_correlations(analyzed_events: list[dict]) -> list[dict]:
    """
    Detect when multiple independent sources report the same theme.
    Confirmation from multiple sources raises conviction.
    """
    theme_events: dict[str, list[dict]] = defaultdict(list)

    for ev in analyzed_events:
        text = (ev.get("title", "") + " " + ev.get("summary", "")).lower()
        themes = []
        for kw in KEYWORD_INSTRUMENT_MAP_GLOBAL:
            if kw in text:
                themes.append(kw)
        for theme in themes:
            theme_events[theme].append(ev)

    correlations: list[dict] = []
    for theme, events in theme_events.items():
        if len(events) < 2:
            continue
        sources = set(ev.get("source", "") for ev in events)
        if len(sources) < 2:
            continue

        # Check if events are close in time (within 6 hours)
        dts = [ev.get("datetime") for ev in events if isinstance(ev.get("datetime"), datetime)]
        if len(dts) >= 2:
            time_span = (max(dts) - min(dts)).total_seconds() / 3600
        else:
            time_span = 0

        if time_span <= 24:
            correlations.append({
                "theme": theme,
                "source_count": len(sources),
                "event_count": len(events),
                "sources": list(sources),
                "time_span_hours": round(time_span, 1),
                "conviction_boost": min(len(sources) * 0.15, 0.5),
                "note": f"Multi-source confirmation on '{theme}' — conviction boosted by {min(len(sources) * 0.15, 0.5):.0%}",
            })

    correlations.sort(key=lambda x: -x["source_count"])
    return correlations


# A reduced keyword set for correlation detection (avoid noise from very common words)
KEYWORD_INSTRUMENT_MAP_GLOBAL = [
    "cpi", "inflation", "pce", "nfp", "non-farm payroll", "gdp",
    "fomc", "federal reserve", "rate hike", "rate cut", "hawkish", "dovish",
    "ecb", "boe", "powell", "lagarde", "bailey",
    "recession", "gold", "dollar", "yield", "treasury",
    "war", "conflict", "tariff", "trade war", "sanction",
    "qe", "taper", "stagflation", "safe haven", "risk off", "risk on",
]


# =============================================================================
# COMPREHENSIVE CONTEXT REPORT
# =============================================================================

def build_context_report(analyzed_events: list[dict],
                         ff_events_raw: list[dict],
                         pressure_scores: dict[str, float],
                         lookback_hours: int = 72) -> dict:
    """
    Build a comprehensive context report combining all pattern engine
    capabilities. This is the "intelligence layer" that makes the engine
    context-aware rather than reactive.
    """
    # Remember all events
    for ev in analyzed_events:
        remember_event(ev)

    # Record pressure
    for inst, score in pressure_scores.items():
        record_pressure(inst, score)

    # Detect patterns
    patterns = detect_patterns(lookback_hours)

    # Signal convergence
    convergence = analyze_signal_convergence(analyzed_events)

    # Regime detection
    regime = detect_regime(analyzed_events, patterns)

    # Forward calendar
    forward_calendar = analyze_forward_calendar(ff_events_raw)

    # Pressure trends
    trends = {}
    for inst in ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF"):
        trends[inst] = analyze_pressure_trend(inst)

    # Correlations
    correlations = detect_correlations(analyzed_events)

    # Build narrative synthesis
    narrative = _build_narrative(regime, patterns, convergence, trends, correlations, forward_calendar)

    return {
        "regime": regime,
        "patterns": patterns,
        "convergence": convergence,
        "forward_calendar": forward_calendar,
        "pressure_trends": trends,
        "correlations": correlations,
        "narrative": narrative,
        "memory_snapshot": get_memory_snapshot(),
    }


def _build_narrative(regime: dict, patterns: list[dict],
                     convergence: dict[str, dict], trends: dict[str, dict],
                     correlations: list[dict],
                     forward_calendar: list[dict]) -> str:
    """Synthesize a human-readable narrative from all context engine outputs."""
    lines = []
    sep = "═" * 90

    lines.append(sep)
    lines.append("  🧠  CONTEXT INTELLIGENCE REPORT  ·  Pattern Engine v1.0")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(sep)

    # Regime
    lines.append(f"\n  📊 MACRO REGIME: {regime['regime']}  (confidence: {regime['confidence']:.0%})")
    lines.append(f"     {regime['description']}")
    lines.append(f"     Gold bias: {regime['gold_bias']}  |  Dollar bias: {regime['dollar_bias']}")

    # Patterns
    if patterns:
        lines.append(f"\n  🔍 DETECTED PATTERNS ({len(patterns)}):")
        for p in patterns[:5]:
            lines.append(f"     • {p['pattern_name']}  (conviction: {p['conviction']:.0%})")
            lines.append(f"       {p['description']}")
            lines.append(f"       Matches: {p['match_count']}/{p['min_required']} required |  "
                         f"Gold implication: {p['gold_implication']}")
            for m in p["matching_events"][:3]:
                dt = m.get("datetime")
                dt_str = dt.strftime("%m/%d %H:%M") if isinstance(dt, datetime) else "??"
                lines.append(f" └─ [{dt_str}] ({m['source']}) {m['title'][:70]}")
    else:
        lines.append("\n  🔍 DETECTED PATTERNS: None — no recurring themes in current window.")

    # Signal convergence
    lines.append(f"\n  📡 SIGNAL CONVERGENCE:")
    for inst in ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF"):
        conv = convergence.get(inst, {})
        direction = conv.get("direction", "NEUTRAL")
        conviction = conv.get("conviction", 0.0)
        agreement = conv.get("agreement", 0.0)
        note = conv.get("note", "")
        conflict = "⚠️ " if conv.get("conflict") else "✅ "
        lines.append(f"     {conflict}{inst:<8} → {direction:<7}  conviction:{conviction:.0%}  "
                     f"agreement:{agreement:.0%}  sources:{conv.get('source_count', 0)}")
        lines.append(f"              {note}")

    # Pressure trends
    lines.append(f"\n  📈 PRESSURE TRENDS:")
    for inst in ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF"):
        t = trends.get(inst, {})
        lines.append(f"     {inst:<8} → {t.get('trend', 'N/A'):<22}  "
                     f"slope:{t.get('slope', 0):+.3f}  accel:{t.get('acceleration', 0):+.3f}  "
                     f"vol:{t.get('volatility', 0):.2f}")

    # Correlations
    if correlations:
        lines.append(f"\n  🔗 MULTI-SOURCE CORRELATIONS:")
        for c in correlations[:5]:
            lines.append(f"     • '{c['theme']}' — {c['source_count']} sources, "
                         f"{c['event_count']} events, span:{c['time_span_hours']}h")
            lines.append(f"       {c['note']}")

    # Forward calendar
    if forward_calendar:
        lines.append(f"\n  ⏭️  FORWARD CALENDAR (next 72h high-impact):")
        for ev in forward_calendar[:8]:
            dt = ev["datetime"]
            dt_str = dt.strftime("%m/%d %H:%M") if isinstance(dt, datetime) else "??"
            lines.append(f"     [{dt_str}] {ev['urgency']:<12} {ev['currency']} — {ev['title']}")
            lines.append(f" {ev['pre_positioning_note']}")
    else:
        lines.append(f"\n  ⏭️  FORWARD CALENDAR: No high-impact events in next 72h.")

    lines.append(f"\n{sep}")
    return "\n".join(lines)