# -*- coding: utf-8 -*-
"""
event_study_engine.py — Economic Surprise & Event-Study Ledger
==============================================================
Captures high-impact calendar releases (CPI, NFP, GDP, …), measures how
much they *surprised* the market, tags the macro regime at release time,
and supports historical questions like:

  "When Core CPI surprised by more than +0.1 during REFLATION,
   what did XAUUSD / EURUSD / GBPUSD / USDCHF tend to do afterward?"

Surprise logic (teaching version of street convention)
-----------------------------------------------------
  surprise_raw  = Actual − Forecast
  surprise_pct  = (Actual − Forecast) / |Forecast| × 100   (if Forecast ≠ 0)

  Positive surprise on growth/inflation prints usually means "hotter than
  expected"; interpretation for FX/gold depends on the event family and
  the regime (wired via instrument signals + thesis history).

Does NOT require numpy. Safe to call from the live engine (all failures
are isolated).

Tables
------
  event_study           — one row per released event (upsert by event_key)
  event_study_windows   — per-symbol reaction windows (signals / thesis shifts)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional


# =============================================================================
# CONFIG
# =============================================================================

TRACKED_SYMBOLS = ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF")
EVENT_STUDY_VERSION = "surprise_v1"
MIN_IMPACT_TO_LOG = 2  # medium+ high-impact calendar events

try:
    from paths import get_db_path_str as _resolve_db
    def _default_db() -> str:
        return _resolve_db(migrate=True)
except Exception:  # pragma: no cover
    def _default_db() -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_engine_store.db")

_DB_LOCK = threading.RLock()

EVENT_STUDY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS event_study (
    event_key TEXT PRIMARY KEY,
    event_family TEXT,
    title TEXT NOT NULL,
    currency TEXT,
    impact INTEGER,
    event_time TEXT,
    actual_raw TEXT,
    forecast_raw TEXT,
    previous_raw TEXT,
    actual_value REAL,
    forecast_value REAL,
    previous_value REAL,
    surprise_raw REAL,
    surprise_pct REAL,
    surprise_direction TEXT,
    beat_miss TEXT,
    regime TEXT,
    growth TEXT,
    inflation TEXT,
    policy TEXT,
    liquidity TEXT,
    risk TEXT,
    instrument_signals_json TEXT,
    thesis_bias_json TEXT,
    notes TEXT,
    rules_version TEXT,
    captured_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

EVENT_STUDY_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_event_study_family_time
ON event_study(event_family, event_time)
"""

EVENT_STUDY_REGIME_IDX_SQL = """
CREATE INDEX IF NOT EXISTS idx_event_study_regime
ON event_study(regime, event_family)
"""

EVENT_STUDY_WINDOWS_SQL = """
CREATE TABLE IF NOT EXISTS event_study_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    horizon TEXT NOT NULL,
    bias_at_event TEXT,
    bias_later TEXT,
    signal_at_event TEXT,
    direction_label TEXT,
    source TEXT NOT NULL,
    detail TEXT,
    recorded_at TEXT NOT NULL,
    UNIQUE(event_key, symbol, horizon, source)
)
"""

# Event family matchers (order matters — more specific first)
_FAMILY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("CORE_CPI", ("core cpi", "core consumer price", "cpi core")),
    ("CPI", ("consumer price index", "cpi ", " cpi", "inflation rate", "hicp")),
    ("CORE_PCE", ("core pce", "pce core", "core personal consumption")),
    ("PCE", ("pce price", "personal consumption expenditure", "pce ")),
    ("NFP", ("nonfarm", "non-farm", "non farm", "nfp", "payrolls")),
    ("UNEMPLOYMENT", ("unemployment rate", "jobless rate", "u-rate")),
    ("GDP", ("gross domestic product", "gdp ")),
    ("FOMC", ("fomc", "fed interest rate decision", "federal funds rate", "fed rate decision")),
    ("RETAIL_SALES", ("retail sales",)),
    ("PMI", ("pmi", "purchasing managers")),
    ("ISM", ("ism manufacturing", "ism services", "ism ")),
    ("TRADE_BALANCE", ("trade balance", "trade deficit")),
    ("HOUSING", ("building permits", "housing starts", "existing home", "new home sales")),
    ("CONSUMER_CONFIDENCE", ("consumer confidence", "michigan sentiment", "consumer sentiment")),
]


# =============================================================================
# SURPRISE MATH
# =============================================================================

def parse_econ_number(raw: Any) -> Optional[float]:
    """
    Parse calendar strings like '3.5%', '256K', '1.2M', '-0.1' into floats.
    K/M scale to thousands/millions of units (absolute), not percentages.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in (".", "–", "—", "-", "n/a", "N/A", "NA"):
        return None
    mult = 1.0
    su = s.upper().replace(",", "")
    if su.endswith("%"):
        su = su[:-1]
    if su.endswith("K"):
        mult = 1_000.0
        su = su[:-1]
    elif su.endswith("M"):
        mult = 1_000_000.0
        su = su[:-1]
    elif su.endswith("B"):
        mult = 1_000_000_000.0
        su = su[:-1]
    # Keep digits, sign, decimal
    cleaned = re.sub(r"[^\d.\-]", "", su)
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        return float(cleaned) * mult
    except ValueError:
        return None


def compute_surprise(
    actual_raw: Any,
    forecast_raw: Any,
    previous_raw: Any = None,
) -> dict:
    """
    Core EconomicSurprise object (dict).

    surprise_raw = Actual − Forecast
      > 0  → hotter / stronger than expected (for most growth & inflation prints)
      < 0  → softer / weaker than expected
      = 0  → inline

    surprise_pct = percent deviation from forecast when forecast ≠ 0
    """
    actual = parse_econ_number(actual_raw)
    forecast = parse_econ_number(forecast_raw)
    previous = parse_econ_number(previous_raw)

    out: dict[str, Any] = {
        "actual_value": actual,
        "forecast_value": forecast,
        "previous_value": previous,
        "surprise_raw": None,
        "surprise_pct": None,
        "surprise_direction": None,
        "beat_miss": None,
        "computable": False,
    }
    if actual is None or forecast is None:
        return out

    surprise_raw = actual - forecast
    if abs(forecast) > 1e-12:
        surprise_pct = (surprise_raw / abs(forecast)) * 100.0
    else:
        # Forecast ~ 0: percent undefined; keep raw only
        surprise_pct = None

    if surprise_raw > 1e-12:
        direction, beat = "POSITIVE", "beat"
    elif surprise_raw < -1e-12:
        direction, beat = "NEGATIVE", "miss"
    else:
        direction, beat = "INLINE", "inline"

    out.update({
        "surprise_raw": round(surprise_raw, 6),
        "surprise_pct": round(surprise_pct, 4) if surprise_pct is not None else None,
        "surprise_direction": direction,
        "beat_miss": beat,
        "computable": True,
    })
    return out


def classify_event_family(title: str) -> str:
    t = (title or "").lower()
    for family, keys in _FAMILY_RULES:
        if any(k in t for k in keys):
            return family
    return "OTHER"


def make_event_key(currency: str, title: str, event_time: Any) -> str:
    if isinstance(event_time, datetime):
        ts = event_time.isoformat(timespec="seconds")
    else:
        ts = str(event_time or "")[:32]
    return f"{currency or ''}|{(title or '').strip()}|{ts}"


# =============================================================================
# ENGINE
# =============================================================================

class EventStudyEngine:
    """
    Logs economic surprises and answers historical event-study queries.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _default_db()
        self._ensure_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _ensure_tables(self) -> None:
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute(EVENT_STUDY_TABLE_SQL)
                conn.execute(EVENT_STUDY_INDEX_SQL)
                conn.execute(EVENT_STUDY_REGIME_IDX_SQL)
                conn.execute(EVENT_STUDY_WINDOWS_SQL)
                conn.commit()
            finally:
                conn.close()

    # ── Capture ───────────────────────────────────────────────────────────

    def capture_release(
        self,
        event: dict,
        macro_state: Optional[dict] = None,
        analyzed: Optional[dict] = None,
        thesis_list: Optional[list] = None,
        min_impact: int = MIN_IMPACT_TO_LOG,
    ) -> Optional[dict]:
        """
        If event has actual+forecast and enough impact, upsert into event_study
        and record immediate symbol windows from instrument signals / theses.
        """
        try:
            impact = int(event.get("impact") or 0)
        except Exception:
            impact = 0
        if impact < min_impact:
            return None

        actual_raw = event.get("actual", "")
        forecast_raw = event.get("forecast", "")
        previous_raw = event.get("previous", "")
        surprise = compute_surprise(actual_raw, forecast_raw, previous_raw)
        if not surprise.get("computable"):
            return None

        title = event.get("title") or ""
        currency = event.get("currency") or ""
        dt = event.get("datetime")
        if isinstance(dt, datetime):
            event_time = dt.isoformat(timespec="seconds")
        elif dt:
            event_time = str(dt)
        else:
            event_time = None

        event_key = make_event_key(currency, title, event_time or title)
        family = classify_event_family(title)
        macro = macro_state or {}

        signals = {}
        if analyzed and isinstance(analyzed.get("instrument_signals"), dict):
            signals = analyzed["instrument_signals"]
        thesis_bias = {}
        if thesis_list:
            for t in thesis_list:
                sym = t.get("symbol")
                if sym:
                    thesis_bias[sym] = t.get("current_bias")

        notes = (
            f"{family}: surprise_raw={surprise['surprise_raw']} "
            f"({surprise['surprise_direction']}); regime={macro.get('regime')}"
        )
        now = datetime.now().isoformat(timespec="seconds")
        row = {
            "event_key": event_key,
            "event_family": family,
            "title": title,
            "currency": currency,
            "impact": impact,
            "event_time": event_time,
            "actual_raw": str(actual_raw) if actual_raw is not None else None,
            "forecast_raw": str(forecast_raw) if forecast_raw is not None else None,
            "previous_raw": str(previous_raw) if previous_raw is not None else None,
            "actual_value": surprise["actual_value"],
            "forecast_value": surprise["forecast_value"],
            "previous_value": surprise["previous_value"],
            "surprise_raw": surprise["surprise_raw"],
            "surprise_pct": surprise["surprise_pct"],
            "surprise_direction": surprise["surprise_direction"],
            "beat_miss": surprise["beat_miss"] or (analyzed or {}).get("beat_miss"),
            "regime": macro.get("regime"),
            "growth": macro.get("growth"),
            "inflation": macro.get("inflation"),
            "policy": macro.get("policy"),
            "liquidity": macro.get("liquidity"),
            "risk": macro.get("risk"),
            "instrument_signals_json": json.dumps(signals, default=str),
            "thesis_bias_json": json.dumps(thesis_bias, default=str),
            "notes": notes,
            "rules_version": EVENT_STUDY_VERSION,
            "captured_at": now,
            "updated_at": now,
        }
        self._upsert_event(row)
        self._write_immediate_windows(event_key, signals, thesis_bias)
        self._attach_thesis_shift_windows(event_key, event_time, thesis_bias)
        return row

    def capture_from_ff_batch(
        self,
        events: list[dict],
        macro_state: Optional[dict] = None,
        thesis_list: Optional[list] = None,
        analyze_fn=None,
        min_impact: int = MIN_IMPACT_TO_LOG,
    ) -> int:
        """Capture many FF-style events. analyze_fn(event)->dict optional."""
        n = 0
        for ev in events or []:
            try:
                analyzed = analyze_fn(ev) if analyze_fn else None
                if self.capture_release(
                    ev,
                    macro_state=macro_state,
                    analyzed=analyzed,
                    thesis_list=thesis_list,
                    min_impact=min_impact,
                ):
                    n += 1
            except Exception:
                continue
        return n

    def _upsert_event(self, row: dict) -> None:
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO event_study ("
                    "event_key, event_family, title, currency, impact, event_time, "
                    "actual_raw, forecast_raw, previous_raw, actual_value, forecast_value, "
                    "previous_value, surprise_raw, surprise_pct, surprise_direction, beat_miss, "
                    "regime, growth, inflation, policy, liquidity, risk, "
                    "instrument_signals_json, thesis_bias_json, notes, rules_version, "
                    "captured_at, updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(event_key) DO UPDATE SET "
                    "actual_raw=excluded.actual_raw, forecast_raw=excluded.forecast_raw, "
                    "previous_raw=excluded.previous_raw, actual_value=excluded.actual_value, "
                    "forecast_value=excluded.forecast_value, previous_value=excluded.previous_value, "
                    "surprise_raw=excluded.surprise_raw, surprise_pct=excluded.surprise_pct, "
                    "surprise_direction=excluded.surprise_direction, beat_miss=excluded.beat_miss, "
                    "regime=excluded.regime, growth=excluded.growth, inflation=excluded.inflation, "
                    "policy=excluded.policy, liquidity=excluded.liquidity, risk=excluded.risk, "
                    "instrument_signals_json=excluded.instrument_signals_json, "
                    "thesis_bias_json=excluded.thesis_bias_json, notes=excluded.notes, "
                    "rules_version=excluded.rules_version, updated_at=excluded.updated_at",
                    (
                        row["event_key"], row["event_family"], row["title"], row["currency"],
                        row["impact"], row["event_time"], row["actual_raw"], row["forecast_raw"],
                        row["previous_raw"], row["actual_value"], row["forecast_value"],
                        row["previous_value"], row["surprise_raw"], row["surprise_pct"],
                        row["surprise_direction"], row["beat_miss"], row["regime"],
                        row["growth"], row["inflation"], row["policy"], row["liquidity"],
                        row["risk"], row["instrument_signals_json"], row["thesis_bias_json"],
                        row["notes"], row["rules_version"], row["captured_at"], row["updated_at"],
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def _write_immediate_windows(
        self,
        event_key: str,
        signals: dict,
        thesis_bias: dict,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with _DB_LOCK:
            conn = self._connect()
            try:
                for sym in TRACKED_SYMBOLS:
                    sig = (signals or {}).get(sym)
                    direction = None
                    if sig in ("BULL", "BULLISH"):
                        direction = "UP"
                    elif sig in ("BEAR", "BEARISH"):
                        direction = "DOWN"
                    elif sig:
                        direction = "FLAT"
                    conn.execute(
                        "INSERT INTO event_study_windows ("
                        "event_key, symbol, horizon, bias_at_event, bias_later, "
                        "signal_at_event, direction_label, source, detail, recorded_at"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(event_key, symbol, horizon, source) DO UPDATE SET "
                        "signal_at_event=excluded.signal_at_event, "
                        "direction_label=excluded.direction_label, "
                        "bias_at_event=excluded.bias_at_event, "
                        "detail=excluded.detail, recorded_at=excluded.recorded_at",
                        (
                            event_key, sym, "immediate",
                            (thesis_bias or {}).get(sym), None,
                            sig, direction, "event_signal",
                            "Direction from release-time instrument_signals (news engine).",
                            now,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

    def _attach_thesis_shift_windows(
        self,
        event_key: str,
        event_time: Optional[str],
        thesis_bias_at_event: dict,
        horizons_hours: tuple[int, ...] = (24, 72),
    ) -> None:
        """
        If instrument_thesis_history exists, compare bias at event vs later.
        Without price data this is our best 'what happened after' proxy.
        """
        if not event_time:
            return
        try:
            et = datetime.fromisoformat(str(event_time).replace("Z", "+00:00").split("+")[0])
        except Exception:
            return

        now = datetime.now().isoformat(timespec="seconds")
        with _DB_LOCK:
            conn = self._connect()
            try:
                # Table may not exist yet
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='instrument_thesis_history'"
                )
                if not cur.fetchone():
                    return

                for hours in horizons_hours:
                    horizon_end = et + timedelta(hours=hours)
                    for sym in TRACKED_SYMBOLS:
                        # Latest thesis at or before event
                        pre = conn.execute(
                            "SELECT current_bias, recorded_at FROM instrument_thesis_history "
                            "WHERE symbol=? AND recorded_at <= ? ORDER BY recorded_at DESC LIMIT 1",
                            (sym, et.isoformat(timespec="seconds")),
                        ).fetchone()
                        # First thesis after event within horizon (or latest in window)
                        post = conn.execute(
                            "SELECT current_bias, recorded_at FROM instrument_thesis_history "
                            "WHERE symbol=? AND recorded_at > ? AND recorded_at <= ? "
                            "ORDER BY recorded_at DESC LIMIT 1",
                            (sym, et.isoformat(timespec="seconds"), horizon_end.isoformat(timespec="seconds")),
                        ).fetchone()
                        bias_pre = (pre[0] if pre else None) or (thesis_bias_at_event or {}).get(sym)
                        bias_post = post[0] if post else None
                        direction = self._bias_shift_direction(bias_pre, bias_post)
                        if bias_post is None and bias_pre is None:
                            continue
                        conn.execute(
                            "INSERT INTO event_study_windows ("
                            "event_key, symbol, horizon, bias_at_event, bias_later, "
                            "signal_at_event, direction_label, source, detail, recorded_at"
                            ") VALUES (?,?,?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(event_key, symbol, horizon, source) DO UPDATE SET "
                            "bias_at_event=excluded.bias_at_event, bias_later=excluded.bias_later, "
                            "direction_label=excluded.direction_label, detail=excluded.detail, "
                            "recorded_at=excluded.recorded_at",
                            (
                                event_key, sym, f"{hours}h",
                                bias_pre, bias_post, None, direction, "thesis_history",
                                f"Thesis bias shift within {hours}h after release "
                                f"(proxy until price feed is added).",
                                now,
                            ),
                        )
                conn.commit()
            except Exception:
                pass
            finally:
                conn.close()

    @staticmethod
    def _bias_shift_direction(pre: Optional[str], post: Optional[str]) -> str:
        if not post:
            return "UNKNOWN"
        if not pre or pre == post:
            return {"BULLISH": "UP", "BEARISH": "DOWN", "NEUTRAL": "FLAT"}.get(post, "FLAT")
        order = {"BEARISH": -1, "NEUTRAL": 0, "BULLISH": 1}
        a, b = order.get(pre, 0), order.get(post, 0)
        if b > a:
            return "UP"
        if b < a:
            return "DOWN"
        return "FLAT"

    # ── Backfill from stored FF events ────────────────────────────────────

    def backfill_from_ff_store(
        self,
        macro_state: Optional[dict] = None,
        analyze_fn=None,
        min_impact: int = MIN_IMPACT_TO_LOG,
    ) -> int:
        """Scan ff_events table for released prints and log surprises."""
        with _DB_LOCK:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT source, title, currency, impact, forecast, previous, actual, datetime "
                    "FROM ff_events WHERE actual IS NOT NULL AND actual != '' AND actual != '.'"
                )
                rows = cur.fetchall()
            finally:
                conn.close()

        n = 0
        for source, title, currency, impact, forecast, previous, actual, dt_text in rows:
            ev = {
                "source": source,
                "title": title,
                "currency": currency,
                "impact": impact or 0,
                "forecast": forecast,
                "previous": previous,
                "actual": actual,
                "datetime": None,
            }
            if dt_text:
                try:
                    ev["datetime"] = datetime.fromisoformat(dt_text)
                except Exception:
                    ev["datetime"] = dt_text
            analyzed = analyze_fn(ev) if analyze_fn else None
            if self.capture_release(ev, macro_state=macro_state, analyzed=analyzed, min_impact=min_impact):
                n += 1
        return n

    # ── Queries ───────────────────────────────────────────────────────────

    def query_events(
        self,
        event_family: Optional[str] = None,
        regime: Optional[str] = None,
        min_surprise: Optional[float] = None,
        max_surprise: Optional[float] = None,
        surprise_direction: Optional[str] = None,
        title_contains: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        """Flexible filter over the event_study ledger."""
        clauses = ["1=1"]
        params: list[Any] = []
        if event_family:
            clauses.append("UPPER(event_family) = UPPER(?)")
            params.append(event_family)
        if regime:
            clauses.append("UPPER(regime) = UPPER(?)")
            params.append(regime)
        if min_surprise is not None:
            clauses.append("surprise_raw >= ?")
            params.append(min_surprise)
        if max_surprise is not None:
            clauses.append("surprise_raw <= ?")
            params.append(max_surprise)
        if surprise_direction:
            clauses.append("UPPER(surprise_direction) = UPPER(?)")
            params.append(surprise_direction)
        if title_contains:
            clauses.append("LOWER(title) LIKE ?")
            params.append(f"%{title_contains.lower()}%")
        sql = (
            "SELECT event_key, event_family, title, currency, impact, event_time, "
            "actual_raw, forecast_raw, previous_raw, actual_value, forecast_value, "
            "surprise_raw, surprise_pct, surprise_direction, beat_miss, regime, "
            "growth, inflation, policy, risk, instrument_signals_json, thesis_bias_json, notes "
            f"FROM event_study WHERE {' AND '.join(clauses)} "
            "ORDER BY event_time DESC LIMIT ?"
        )
        params.append(limit)
        with _DB_LOCK:
            conn = self._connect()
            try:
                cur = conn.execute(sql, params)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            finally:
                conn.close()
        for r in rows:
            try:
                r["instrument_signals"] = json.loads(r.pop("instrument_signals_json") or "{}")
            except Exception:
                r["instrument_signals"] = {}
            try:
                r["thesis_bias"] = json.loads(r.pop("thesis_bias_json") or "{}")
            except Exception:
                r["thesis_bias"] = {}
        return rows

    def study_reaction(
        self,
        event_family: str,
        min_surprise: float = 0.0,
        max_surprise: Optional[float] = None,
        regime: Optional[str] = None,
        surprise_side: str = "positive",
        horizon: str = "immediate",
        title_contains: Optional[str] = None,
    ) -> dict:
        """
        Answer: historically, given family + surprise filter + regime,
        how did the 4 symbols tend to react?

        surprise_side: 'positive' (surprise_raw >= min), 'negative' (<= -min), 'either'
        horizon: 'immediate' | '24h' | '72h'
        """
        if surprise_side.lower() == "positive":
            events = self.query_events(
                event_family=event_family,
                regime=regime,
                min_surprise=min_surprise,
                max_surprise=max_surprise,
                title_contains=title_contains,
                limit=500,
            )
        elif surprise_side.lower() == "negative":
            # more negative than -min_surprise
            events = self.query_events(
                event_family=event_family,
                regime=regime,
                max_surprise=-abs(min_surprise) if min_surprise else 0.0,
                title_contains=title_contains,
                limit=500,
            )
        else:
            events = self.query_events(
                event_family=event_family,
                regime=regime,
                title_contains=title_contains,
                limit=500,
            )
            if min_surprise:
                events = [
                    e for e in events
                    if e.get("surprise_raw") is not None
                    and abs(float(e["surprise_raw"])) >= abs(min_surprise)
                ]

        keys = [e["event_key"] for e in events]
        symbol_stats: dict[str, Counter] = {s: Counter() for s in TRACKED_SYMBOLS}
        samples: dict[str, list] = defaultdict(list)

        if keys:
            windows = self._fetch_windows(keys, horizon=horizon)
            for w in windows:
                sym = w.get("symbol")
                if sym not in symbol_stats:
                    continue
                lab = w.get("direction_label") or "UNKNOWN"
                symbol_stats[sym][lab] += 1
                if len(samples[sym]) < 5:
                    samples[sym].append(w)
            # Fallback: use instrument_signals on the event if no windows
            if not windows:
                for e in events:
                    sigs = e.get("instrument_signals") or {}
                    for sym in TRACKED_SYMBOLS:
                        sig = sigs.get(sym)
                        if sig in ("BULL", "BULLISH"):
                            symbol_stats[sym]["UP"] += 1
                        elif sig in ("BEAR", "BEARISH"):
                            symbol_stats[sym]["DOWN"] += 1
                        elif sig:
                            symbol_stats[sym]["FLAT"] += 1

        summary_by_symbol = {}
        for sym in TRACKED_SYMBOLS:
            c = symbol_stats[sym]
            total = sum(c.values()) or 0
            dominant = c.most_common(1)[0][0] if c else "NO_DATA"
            summary_by_symbol[sym] = {
                "counts": dict(c),
                "n": total,
                "dominant_direction": dominant,
                "pct_up": round(100 * c.get("UP", 0) / total, 1) if total else None,
                "pct_down": round(100 * c.get("DOWN", 0) / total, 1) if total else None,
            }

        return {
            "query": {
                "event_family": event_family,
                "min_surprise": min_surprise,
                "max_surprise": max_surprise,
                "regime": regime,
                "surprise_side": surprise_side,
                "horizon": horizon,
                "title_contains": title_contains,
            },
            "n_events": len(events),
            "events_preview": [
                {
                    "time": e.get("event_time"),
                    "title": e.get("title"),
                    "surprise_raw": e.get("surprise_raw"),
                    "regime": e.get("regime"),
                }
                for e in events[:8]
            ],
            "symbol_reactions": summary_by_symbol,
            "note": (
                "Immediate horizon uses release-time instrument signals. "
                "24h/72h horizons use instrument_thesis_history bias shifts when available "
                "(price returns can be added later without changing this schema)."
            ),
        }

    def _fetch_windows(self, event_keys: list[str], horizon: str = "immediate") -> list[dict]:
        if not event_keys:
            return []
        placeholders = ",".join("?" * len(event_keys))
        with _DB_LOCK:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"SELECT event_key, symbol, horizon, bias_at_event, bias_later, "
                    f"signal_at_event, direction_label, source, detail "
                    f"FROM event_study_windows "
                    f"WHERE event_key IN ({placeholders}) AND horizon = ?",
                    (*event_keys, horizon),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
            finally:
                conn.close()

    def count(self) -> int:
        with _DB_LOCK:
            conn = self._connect()
            try:
                return int(conn.execute("SELECT COUNT(*) FROM event_study").fetchone()[0])
            finally:
                conn.close()

    @staticmethod
    def format_study_report(result: dict) -> str:
        q = result.get("query") or {}
        lines = [
            "=" * 72,
            "  EVENT STUDY REPORT",
            "=" * 72,
            f"  Family: {q.get('event_family')}  |  regime: {q.get('regime') or 'ANY'}  |  "
            f"side: {q.get('surprise_side')}  |  min_surprise: {q.get('min_surprise')}  |  "
            f"horizon: {q.get('horizon')}",
            f"  Matching releases: {result.get('n_events', 0)}",
            "",
            "  Symbol reaction frequencies:",
        ]
        for sym, st in (result.get("symbol_reactions") or {}).items():
            lines.append(
                f"    {sym:<8}  n={st.get('n', 0):<4}  dominant={st.get('dominant_direction')}  "
                f"UP={st.get('pct_up')}%  DOWN={st.get('pct_down')}%  counts={st.get('counts')}"
            )
        preview = result.get("events_preview") or []
        if preview:
            lines.append("")
            lines.append("  Sample events:")
            for e in preview[:5]:
                lines.append(
                    f"    [{e.get('time')}] surprise={e.get('surprise_raw')}  "
                    f"regime={e.get('regime')}  {str(e.get('title') or '')[:50]}"
                )
        if result.get("note"):
            lines.append("")
            lines.append(f"  Note: {result['note']}")
        lines.append("=" * 72)
        return "\n".join(lines)

    @staticmethod
    def explain_surprise_logic() -> str:
        return """
Economic Surprise logic (event_study_engine)
--------------------------------------------
1) Parse Actual and Forecast from the calendar (handles %, K, M).
2) surprise_raw  = Actual − Forecast
3) surprise_pct  = (Actual − Forecast) / |Forecast| × 100  (if Forecast ≠ 0)
4) Direction: POSITIVE (beat) / NEGATIVE (miss) / INLINE
5) Event family is classified from the title (CORE_CPI, NFP, GDP, …)
6) Each logged row stores the MacroState regime dials at capture time
7) event_study_windows records per-symbol reactions:
     • immediate  → instrument_signals at release
     • 24h / 72h  → thesis bias shifts from instrument_thesis_history
   (Price returns can fill the same window table later.)

Example question this engine answers:
  study_reaction(event_family='CORE_CPI', min_surprise=0.1,
                 regime='REFLATION', surprise_side='positive')
""".strip()


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    print(EventStudyEngine.explain_surprise_logic())
    print()

    engine = EventStudyEngine()
    macro = None
    thesis = None
    analyze_fn = None
    try:
        from macro_state_analyzer import MacroStateAnalyzer
        macro = MacroStateAnalyzer().latest_state()
    except Exception:
        pass
    try:
        from instrument_thesis import InstrumentThesisEngine
        thesis = InstrumentThesisEngine().get_all()
    except Exception:
        pass
    try:
        from news_engine import analyze_ff_event
        analyze_fn = analyze_ff_event
    except Exception:
        pass

    n = engine.backfill_from_ff_store(macro_state=macro, analyze_fn=analyze_fn)
    print(f"Backfilled/updated {n} released events into event_study.")
    print(f"Ledger size: {engine.count()} rows.\n")

    # Demo query — Core CPI-like positive surprises in REFLATION (or any if none)
    report = engine.study_reaction(
        event_family="CORE_CPI",
        min_surprise=0.1,
        regime="REFLATION",
        surprise_side="positive",
        horizon="immediate",
    )
    if report["n_events"] == 0:
        report = engine.study_reaction(
            event_family="CPI",
            min_surprise=0.0,
            regime=None,
            surprise_side="positive",
            horizon="immediate",
        )
    if report["n_events"] == 0:
        # Any family with data
        report = engine.study_reaction(
            event_family="NFP",
            min_surprise=0.0,
            regime=None,
            surprise_side="either",
            horizon="immediate",
        )
    print(EventStudyEngine.format_study_report(report))
