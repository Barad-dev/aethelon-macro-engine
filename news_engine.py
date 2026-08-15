# -*- coding: utf-8 -*-
"""
news_engine.py — News & Macro Analytical Engine v5.0
=====================================================
Context-Aware News & Macro Engine with NLP Sentiment Analysis

Features:
  - Multi-source data aggregation (Forex Factory, RSS feeds, FRED)
  - NLP sentiment analysis (VADER + financial lexicon)
  - Context memory with pattern detection and macro regime classification
  - Aggregate pressure scores per instrument (XAUUSD, EURUSD, GBPUSD, USDCHF)
  - Signal convergence and multi-source correlation detection
  - Forward calendar with pre-positioning notes (next 72h)
  - Pressure trend analysis (slope, acceleration, volatility)
  - Live listener mode with background polling
  - Terminal live dashboard (in-place redraw, no flicker, no scrolling issues)
  - GUI dashboard with 8 tabs (Overview, Regime, Patterns, Signals,
    Calendar, News, FRED, Pressure) — scrollable, color-coded,
    with per-tab summaries and vivid dark-navy theme
  - One-shot, live terminal, and GUI modes

Usage:
    python run_engine.py              One-shot analysis
    python run_engine.py --live       Live terminal dashboard (60s)
    python run_engine.py --live 30    Live terminal dashboard (30s)
    python run_engine.py --gui        GUI dashboard with tabs (60s)
    python run_engine.py --gui 30    GUI dashboard with tabs (30s)
    python run_engine.py --test       Run built-in test suite
    python run_engine.py --news-only  Force refresh + print dashboard
    python run_engine.py --status     Quick listener status
    python run_engine.py --help       Show this help

Files:
    run_engine.py         Main entry point
    gui_qt_dashboard.py   PySide6 main window (Research Desk + legacy tabs)
    research_desk_view.py Research Desk 4-section view
    research_desk_data.py Research Desk aggregator (macro/thesis/history/surprises)
    news_engine.py        Core engine — sources, scoring, analysis
    sentiment_analyzer.py NLP sentiment (VADER + financial lexicon)
    pattern_engine.py     Context memory, patterns, regime, calendar
    macro_state_analyzer.py  Textbook FRED → MacroState (growth/inflation/policy)
    instrument_thesis.py     Regime playbook → XAUUSD/EURUSD/GBPUSD/USDCHF theses
    event_study_engine.py    Economic surprise ledger + historical reaction queries

Install:
    pip install requests feedparser
    pip install nltk   (optional — enables VADER sentiment)

Author: AI Vibe Coding
Version: 5.0
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
import random
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Optional

try:
    import requests
except ImportError:
    raise SystemExit("Install 'requests':  pip install requests")

try:
    import feedparser
except ImportError:
    raise SystemExit("Install 'feedparser':  pip install feedparser")

# Import our enhanced modules
from sentiment_analyzer import FinancialSentimentAnalyzer
from pattern_engine import build_context_report, record_pressure, remember_event
from macro_state_analyzer import (
    MacroStateAnalyzer,
    MACRO_STATE_TABLE_SQL,
    MACRO_STATE_INDEX_SQL,
    MACRO_STATE_UNIQUE_SQL,
)
from instrument_thesis import (
    InstrumentThesisEngine,
    INSTRUMENT_THESIS_TABLE_SQL,
    INSTRUMENT_THESIS_HISTORY_SQL,
    TRACKED_SYMBOLS,
)
from event_study_engine import (
    EventStudyEngine,
    EVENT_STUDY_TABLE_SQL,
    EVENT_STUDY_INDEX_SQL,
    EVENT_STUDY_REGIME_IDX_SQL,
    EVENT_STUDY_WINDOWS_SQL,
)
from research_desk_data import build_research_desk, get_research_desk_from_context
from utils.logger import get_logger

log = get_logger(__name__)

# Stage B3.4 — soft-wire async ingestion plane (optional; legacy fetch remains fallback)
try:
    from aethelon.ingestion import (
        DEFAULT_FRED_SERIES,
        DEFAULT_RSS_FEEDS,
        IngestionConfig,
        IngestionOrchestrator,
        default_ingestion_config,
    )

    _HAS_INGESTION_ORCHESTRATOR = True
except ImportError:  # pragma: no cover — package missing in odd run layouts
    DEFAULT_FRED_SERIES = ()  # type: ignore[misc, assignment]
    DEFAULT_RSS_FEEDS = {}  # type: ignore[misc, assignment]
    IngestionConfig = Any  # type: ignore[misc, assignment]
    IngestionOrchestrator = Any  # type: ignore[misc, assignment]
    default_ingestion_config = None  # type: ignore[assignment]
    _HAS_INGESTION_ORCHESTRATOR = False

# =============================================================================
# CONFIGURATION
# =============================================================================

# FRED key must come from the environment — never hardcode secrets in source.
# Set FRED_API_KEY in the process environment (or a local untracked .env loader).
FRED_API_KEY: str = (os.environ.get("FRED_API_KEY") or "").strip()
_FRED_KEY_WARNED: bool = False

NEWS_LOOKBACK_HOURS = 24
_STORE_RETENTION_HOURS = 72
_STORE_MAX_ITEMS_PER_SOURCE = 1500

FF_CALENDAR_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
    "https://nfs.faireconomy.media/ff_calendar_today.json",
]

# Additional economic calendar APIs (free, no key)
EXTRA_CALENDAR_URLS = [
    # Investing.com-style RSS economic calendar
    "https://www.investing.com/rss/news_1.rss", # Major economic news
    "https://www.investing.com/rss/news_25.rss",  # Forex news
]

FRED_SERIES = {
    "FEDFUNDS":          ("Fed Funds Rate",                 "USD", "rate"),
    "CPIAUCSL":          ("CPI All Items",                  "USD", "inflation"),
    "UNRATE":            ("US Unemployment Rate",            "USD", "employment"),
    "PAYEMS":            ("Nonfarm Payrolls (000s)",         "USD", "employment"),
    "GDP":               ("US Real GDP Growth",              "USD", "gdp"),
    "DGS10":             ("10-Yr Treasury Yield",            "USD", "yield"),
    "DGS2":              ("2-Yr Treasury Yield",             "USD", "yield"),
    "DTWEXBGS":          ("USD Broad Trade-Weighted Index",  "USD", "dollar_index"),
    "GOLDAMGBD228NLBM":  ("Gold Fix AM Price",               "XAU", "commodity"),
    "EUROUSDM":          ("EUR/USD Monthly",                 "EUR", "fx"),
    "GBPUSDM":          ("GBP/USD Monthly",                 "GBP", "fx"),
    "PCEPI":             ("PCE Price Index",                 "USD", "inflation"),
    "COREPCE":           ("Core PCE Price Index",             "USD", "inflation"),
    "T10YIE":            ("10-Yr Breakeven Inflation Rate",   "USD", "inflation"),
    "M2SL":              ("M2 Money Supply", "USD", "monetary"),
    "WALCL":             ("Fed Total Assets (Balance Sheet)", "USD", "monetary"),
    "VIXCLS":            ("VIX Volatility Index",             "USD", "volatility"),
    "DEXUSEU":           ("USD/EUR Exchange Rate",           "EUR", "fx"),
    "DEXUSUK":           ("USD/GBP Exchange Rate",           "GBP", "fx"),
    "DEXUSCH":           ("USD/CHF Exchange Rate",            "CHF", "fx"),
    "DEXJPUS":           ("JPY/USD Exchange Rate",            "JPY", "fx"),
}

# Working RSS feeds only — blocked/404/non-RSS sources removed
# Removed (403/404/unreachable/not RSS): Reuters, WSJ World, Forex Factory RSS,
# Myfxbook, Kitco, Gold Price, Mining.com, OilPrice, ZeroHedge,
# Economic Times Forex, huggingnews.com
RSS_FEEDS = {
    # Major financial news
    "Yahoo Finance":          "https://finance.yahoo.com/news/rssindex",
    "Nasdaq Markets":         "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
    "MarketWatch":            "https://feeds.marketwatch.com/marketwatch/topstories/",
    "MarketWatch Forex":      "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "CNBC Economy":           "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "CNBC Top News":          "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "WSJ Markets":            "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "Bloomberg Markets":      "https://feeds.bloomberg.com/markets/news.rss",
    # Forex-specific
    "FXStreet":               "https://www.fxstreet.com/rss/news",
    "ForexLive":              "https://www.forexlive.com/feed/news",
    "ForexLive Technicals":   "https://www.forexlive.com/feed/technical-analysis",
    # Economic analysis
    "Investing.com News":     "https://www.investing.com/rss/news.rss",
    "Investing.com Forex":    "https://www.investing.com/rss/news_25.rss",
    "Investing.com Economy":  "https://www.investing.com/rss/news_1.rss",
    # Alternative / aggregate
    "Google News Forex":      "https://news.google.com/rss/search?q=forex+economy&hl=en-US&gl=US&ceid=US:en",
    "Google News Gold":       "https://news.google.com/rss/search?q=gold+price+market&hl=en-US&gl=US&ceid=US:en",
    "Google News Fed":        "https://news.google.com/rss/search?q=federal+reserve+rate&hl=en-US&gl=US&ceid=US:en",
    "Google News Inflation":  "https://news.google.com/rss/search?q=inflation+cpi+economy&hl=en-US&gl=US&ceid=US:en",
    "Google News Recession":  "https://news.google.com/rss/search?q=recession+economy+gdp&hl=en-US&gl=US&ceid=US:en",
    "Google News Tariff":     "https://news.google.com/rss/search?q=tariff+trade+war&hl=en-US&gl=US&ceid=US:en",
    # Central bank feeds
    "Fed Reserve News":       "https://www.federalreserve.gov/feeds/press_all.xml",
    # Crypto/alt (sometimes leads risk sentiment)
    "CoinDesk Bitcoin":       "https://www.coindesk.com/arc/outboundfeeds/rss/",
}

# ── HTTP headers (browser-like to avoid 403 blocking) ──
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

FF_HEADERS = {
    **HEADERS,
    "Referer": "https://www.forexfactory.com/",
}

# Durable store under %APPDATA%\Aethelon\data\ (see paths.py)
from paths import DB_FILENAME, get_db_path_str, ensure_app_dirs, describe_paths  # noqa: E402

DB_PATH = get_db_path_str(migrate=True)  # AppData path; migrates legacy DB once
_DB_RETENTION_DAYS = 30
_DB_LOCK = threading.RLock()
ensure_app_dirs()

# Textbook macro regime analyzer (FRED → MacroState → SQLite)
_macro_state_analyzer = MacroStateAnalyzer(db_path=DB_PATH, auto_save=True)
# Living instrument theses (MacroState → XAUUSD/EURUSD/GBPUSD/USDCHF)
_instrument_thesis_engine = InstrumentThesisEngine(db_path=DB_PATH)
# Economic surprise / event-study ledger (Actual − Forecast + regime)
_event_study_engine = EventStudyEngine(db_path=DB_PATH)

IMPACT_TIERS = {3: "🔴 HIGH", 2: "🟡 MEDIUM", 1: "⚪ LOW", 0: "—"}

FRED_PROJECTION: dict[str, dict[str, tuple[str, str]]] = {
    "rate":         {"up":   ("USD BULL | Gold BEAR", "USD BULL | Gold BEAR"),
                     "down": ("USD BEAR | Gold BULL", "USD BEAR | Gold BULL")},
    "inflation":    {"up":   ("Gold BULL | USD mixed", "USD BULL if Fed hawkish | Gold BEAR"),
                     "down": ("USD BEAR | Gold BULL",  "USD BEAR | Gold BULL")},
    "employment":   {"up":   ("USD BULL | Risk-ON",        "USD BULL"),
                     "down": ("USD BEAR | Safe-Haven BULL", "USD BEAR")},
    "gdp":          {"up":   ("USD BULL", "USD BULL"),
                     "down": ("USD BEAR | Gold BULL", "USD BEAR | Gold BULL")},
    "yield":        {"up":   ("USD BULL | Gold BEAR", "USD BULL | Gold BEAR"),
                     "down": ("Gold BULL | USD BEAR", "Gold BULL | USD BEAR")},
    "dollar_index": {"up":   ("EURUSD BEAR | GBPUSD BEAR | Gold BEAR", "Same"),
                     "down": ("EURUSD BULL | GBPUSD BULL | Gold BULL", "Same")},
    "commodity":    {"up":   ("Gold BULL", "Gold BULL"),
                     "down": ("Gold BEAR", "Gold BEAR")},
    "fx":           {"up":   ("Pair BULL", "Pair BULL"),
                     "down": ("Pair BEAR", "Pair BEAR")},
    "monetary":     {"up":   ("USD BULL | Gold BEAR", "USD BULL | Gold BEAR"),
                     "down": ("USD BEAR | Gold BULL", "USD BEAR | Gold BULL")},
    "volatility":   {"up":   ("Risk-OFF | Gold BULL", "Risk-OFF | Gold BULL"),
                     "down": ("Risk-ON | Gold BEAR", "Risk-ON | Gold BEAR")},
}

CAUSALITY_CHAINS: dict[str, dict[str, list[str]]] = {
    "inflation": {
        "up":   ["Keeps the case for higher-for-longer Fed policy alive, since easing usually waits for clear disinflation.",
                 "Real yields tend to firm on stickier inflation prints, a headwind for non-yielding gold unless growth fears offset it.",
                 "If inflation accelerates while growth slows, stagflation risk rises — historically the strongest tailwind for gold."],
        "down": ["Strengthens the disinflation narrative and opens room for the Fed to consider cuts sooner.",
                 "Falling real yields have historically been supportive for gold and risk assets.",
                 "Persistent disinflation may eventually shift the regime from hawkish to dovish, a structural gold positive."],
    },
    "employment": {
        "up":   ["A resilient labor market gives the Fed cover to keep policy restrictive without triggering a hard landing.",
                 "Strong payrolls typically firm the dollar and cap gold's upside in the near term.",
                 "However, if strong employment coexists with falling inflation, the Fed may still cut — watch for divergence."],
        "down": ["A weakening labor market raises recession odds and pulls forward the expected timeline for cuts.",
                 "Softening employment usually weighs on the dollar and supports safe-haven demand for gold.",
                 "Rapid employment deterioration can trigger a risk-off rotation benefiting gold and the Swiss franc."],
    },
    "rate": {
        "up":   ["Higher policy rates widen rate-differentials in the dollar's favor against low-yield peers.",
                 "Tighter policy raises the opportunity cost of holding non-yielding assets like gold.",
                 "If the market perceives hikes as excessive, recession fears may eventually dominate and flip gold bullish."],
        "down": ["Rate cuts narrow yield differentials and typically weigh on the currency.",
                 "Lower policy rates reduce the opportunity cost of gold, a historically supportive backdrop.",
                 "Aggressive cutting cycles often signal economic distress, amplifying safe-haven demand for gold."],
    },
    "gdp": {
        "up":   ["Stronger growth supports a hawkish policy stance and typically firms the currency.",
                 "Robust growth can dampen safe-haven demand for gold unless inflation also accelerates alongside it.",
                 "Above-trend growth with stable inflation is the most dollar-positive scenario."],
        "down": ["Slowing growth raises the odds of policy easing and typically weighs on the currency.",
                 "Growth scares tend to support gold as a hedge against a broader slowdown.",
                 "If growth slows while inflation stays sticky, stagflation dynamics emerge — gold's best case."],
    },
    "yield": {
        "up":   ["Rising yields raise the opportunity cost of gold and often coincide with dollar strength.",
                 "Higher long-end yields can also signal the market pricing in stickier inflation or larger deficits.",
                 "If yields rise due to supply concerns rather than growth, gold may eventually decouple and rally."],
        "down": ["Falling yields lower the opportunity cost of holding gold, a supportive dynamic.",
                 "Declining yields often reflect growth concerns or expectations of easier policy ahead.",
                 "Persistent yield decline is one of the most reliable structural tailwinds for gold."],
    },
    "dollar_index": {
        "up":   ["A firmer dollar mechanically pressures dollar-priced commodities like gold and most major pairs.",
                 "Broad dollar strength often reflects a relative US growth or rate advantage over peers.",
                 "If dollar strength is driven by safe-haven flows rather than growth, gold may also rise alongside it."],
        "down": ["A softer dollar mechanically supports gold and most major currency pairs against it.",
                 "Broad dollar weakness often reflects a narrowing rate advantage or rising risk appetite elsewhere.",
                 "Sustained dollar weakness is typically the strongest combined tailwind for both gold and EUR/GBP pairs."],
    },
    "commodity": {
        "up":   ["Rising gold prices often reflect falling real yields, safe-haven demand, or both at once.",
                 "Sustained commodity strength can eventually feed back into headline inflation prints.",
                 "If gold rises while the dollar also strengthens, it signals safe-haven demand overriding the inverse correlation."],
        "down": ["Falling gold prices typically track firming real yields or fading safe-haven demand.",
                 "Softer commodity prices can help ease future inflation readings.",
                 "Gold weakness alongside dollar weakness is unusual and may signal liquidity-driven selling."],
    },
    "fx": {
        "up":   ["Strength in the pair often reflects a relative rate or growth advantage for that currency.",
                 "Momentum here can persist until the underlying rate-differential narrative changes.",
                 "Watch for central bank divergence as the primary driver of sustained FX trends."],
        "down": ["Weakness in the pair often reflects a relative rate or growth disadvantage for that currency.",
                 "Momentum here can persist until the underlying rate-differential narrative changes.",
                 "Pair weakness driven by safe-haven flows may reverse quickly when risk sentiment shifts."],
    },
    "monetary": {
        "up":   ["Expanding central bank balance sheets signal accommodative policy — gold supportive.",
                 "Money supply growth above GDP growth is historically inflationary and gold-positive long-term."],
        "down": ["Contracting balance sheets signal tightening — near-term gold headwind.",
                 "QT-driven liquidity reduction can pressure all assets including gold until the cycle completes."],
    },
    "volatility": {
        "up":   ["Rising VIX signals risk aversion — typically supportive for gold and safe-haven currencies.",
                 "Volatility spikes often precede regime shifts; watch for follow-through in subsequent sessions."],
        "down": ["Falling VIX signals risk appetite — typically pressures gold and safe-haven currencies.",
                 "Low volatility regimes favor carry trades and growth-sensitive currencies."],
    },
}

# =============================================================================
# SENTIMENT ANALYZER INSTANCE
# =============================================================================

_sentiment_analyzer = FinancialSentimentAnalyzer()

# =============================================================================
# DATETIME NORMALISATION
# =============================================================================

def _to_naive(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return datetime.fromtimestamp(dt.timestamp())
    return dt


# =============================================================================
# STAGE B3.4 — IngestionOrchestrator adapter (sync bridge)
# =============================================================================
# Listener cycles stay synchronous. These helpers run the async orchestrator
# in a short-lived worker thread, adapt NormalizedItem rows into the shapes
# expected by _merge_*, and fall back to the legacy requests/feedparser path
# on any failure. NLP / storage / GUI code is untouched.

# Set AETHELON_USE_ORCHESTRATOR=0 to force the pre-B3.4 fetch path.
_ORCH_RSS_TIMEOUT_S = 50.0
_ORCH_FF_TIMEOUT_S = 25.0
_ORCH_FRED_TIMEOUT_S = 45.0


def _orchestrator_enabled() -> bool:
    """Return True when the B3 ingestion orchestrator should be preferred."""
    if not _HAS_INGESTION_ORCHESTRATOR:
        return False
    flag = (os.environ.get("AETHELON_USE_ORCHESTRATOR") or "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


def _run_coro_sync(coro: Any, *, timeout: float, label: str) -> Any:
    """
    Run an async coroutine from synchronous listener code.

    Always uses a dedicated thread + ``asyncio.run`` so we never collide with
    a caller that already has an event loop (e.g. future GUI async work).
    """
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 — surface to caller
            box["error"] = exc

    thread = threading.Thread(
        target=_target,
        daemon=True,
        name=f"aethelon-orch-{label}",
    )
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise TimeoutError(f"IngestionOrchestrator {label} timed out after {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _parse_item_datetime(value: Any) -> Optional[datetime]:
    """Parse orchestrator ISO-Z / datetime fields into naive local wall time."""
    if isinstance(value, datetime):
        return _to_naive(value)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return _to_naive(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _news_engine_ingestion_config() -> Any:
    """
    Build an ``IngestionConfig`` for live fetches.

    Prefers ``aethelon.ingestion.config`` defaults; falls back to this module's
    ``RSS_FEEDS`` / ``FRED_SERIES`` keys when the package defaults are empty.
    """
    assert default_ingestion_config is not None
    base = default_ingestion_config()
    rss = dict(base.rss_feeds) if base.rss_feeds else dict(RSS_FEEDS)
    if not rss:
        rss = dict(RSS_FEEDS)
    series = list(base.fred_series) if base.fred_series else list(FRED_SERIES.keys())
    if not series:
        series = list(FRED_SERIES.keys())
    # Live path historically pulled a short recent window; keep that for cold start.
    return IngestionConfig(
        rss_feeds=rss,
        fred_series=series,
        fred_series_meta=dict(base.fred_series_meta),
        forex_factory_url=base.forex_factory_url,
        fred_observations_url=base.fred_observations_url,
        fred_recent_limit=5,
        run_forex_factory=True,
        fail_soft=True,
    )


def _adapt_rss_normalized(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Map an RSS/Atom ``NormalizedItem`` to the legacy store row shape."""
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    dt = _parse_item_datetime(item.get("datetime"))
    if dt is None:
        return None
    cutoff = datetime.now() - timedelta(hours=_STORE_RETENTION_HOURS)
    if dt < cutoff:
        return None
    return {
        "source": str(item.get("source") or "RSS"),
        "title": title,
        "summary": str(item.get("summary") or ""),
        "link": str(item.get("link") or ""),
        "datetime": dt,
        "impact": int(item.get("impact") or 1),
    }


def _adapt_ff_normalized(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Map a Forex Factory ``NormalizedItem`` to the legacy calendar row shape."""
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    dt = _parse_item_datetime(item.get("datetime"))
    impact_raw = item.get("impact", 1)
    try:
        impact = int(impact_raw) if impact_raw is not None else 1
    except (TypeError, ValueError):
        impact = 1
    raw = item.get("raw")
    if not isinstance(raw, dict):
        raw = dict(item)
    return {
        "source": str(item.get("source") or "ForexFactory"),
        "title": title,
        "currency": str(item.get("currency") or ""),
        "impact": impact,
        "forecast": item.get("forecast") if item.get("forecast") is not None else "",
        "previous": item.get("previous") if item.get("previous") is not None else "",
        "actual": item.get("actual") if item.get("actual") is not None else "",
        "datetime": dt,
        "raw": raw,
    }


def _adapt_fred_normalized_groups(
    items: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """
    Group FRED ``NormalizedItem`` rows into ``{series_id: [obs, ...]}``.

    Observation dicts keep the minimal ``date`` / ``value`` keys that
    ``_merge_fred_series`` already understands.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("series_id") or "").strip().upper()
        date_s = str(item.get("date") or "")[:10]
        value = item.get("value")
        if not sid or not date_s or value in (None, ".", ""):
            continue
        grouped.setdefault(sid, []).append({"date": date_s, "value": value})
    return grouped


async def _orchestrator_collect(
    *,
    want_rss: bool,
    want_ff: bool,
    want_fred: bool,
) -> list[dict[str, Any]]:
    """
    Run ``IngestionOrchestrator`` for the requested source families only.

    Returns raw ``NormalizedItem`` dicts (no store writes).
    """
    cfg = _news_engine_ingestion_config()
    async with IngestionOrchestrator(config=cfg) as orch:
        items = await orch.run(
            rss_feeds=dict(cfg.rss_feeds) if want_rss else {},
            run_forex_factory=want_ff,
            fred_series=list(cfg.fred_series) if want_fred else [],
            fred_recent_limit=int(cfg.fred_recent_limit),
            fail_soft=True,
        )
    return list(items or [])


# =============================================================================
# PERSISTENT SESSION STORE
# =============================================================================

_STORE_LOCK = threading.RLock()

_STORE: dict = {
    "ff_events":   {},
    "rss_items":   {},
    "fred_series": {},
    "extra_cal":   {},
}

_SOURCE_STATE: dict = {
    "ff":   {"last_attempt": datetime.min, "last_success": None, "last_note": "not yet checked"},
    "fred": {"last_attempt": datetime.min, "last_success": None, "last_note": "not yet checked"},
    "rss":  {"last_attempt": datetime.min, "last_success": None, "last_note": "not yet checked"},
    "extra": {"last_attempt": datetime.min, "last_success": None, "last_note": "not yet checked"},
}

_DB_SCHEMA_INITIALIZED = False
_PERSISTENT_STORE_LOADED = False

# =============================================================================
# SQLITE PERSISTENCE
# =============================================================================

def _get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def _row_to_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return _to_naive(datetime.fromisoformat(value))
    except Exception:
        try:
            return _to_naive(datetime.strptime(value, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return None

def _ensure_db_schema():
    global _DB_SCHEMA_INITIALIZED
    if _DB_SCHEMA_INITIALIZED:
        return
    # AppData (or override) parent — never assume install-dir colocated DB
    _db_parent = os.path.dirname(DB_PATH)
    if _db_parent:
        os.makedirs(_db_parent, exist_ok=True)
    with _DB_LOCK:
        conn = _get_db_connection()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ff_events ("
                "key TEXT PRIMARY KEY, source TEXT, title TEXT, currency TEXT, impact INTEGER, "
                "forecast TEXT, previous TEXT, actual TEXT, datetime TEXT, raw_json TEXT, inserted_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS rss_items ("
                "key TEXT PRIMARY KEY, source TEXT, title TEXT, summary TEXT, link TEXT, "
                "datetime TEXT, impact INTEGER, inserted_at TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS fred_series ("
                "series_id TEXT PRIMARY KEY, observations_json TEXT, updated_at TEXT)"
            )
            # Textbook macro regime snapshots (growth/inflation/policy/liquidity/risk)
            conn.execute(MACRO_STATE_TABLE_SQL)
            conn.execute(MACRO_STATE_INDEX_SQL)
            conn.execute(MACRO_STATE_UNIQUE_SQL)
            # Living instrument theses (one row per tracked symbol)
            conn.execute(INSTRUMENT_THESIS_TABLE_SQL)
            conn.execute(INSTRUMENT_THESIS_HISTORY_SQL)
            # Economic surprise / event-study ledger
            conn.execute(EVENT_STUDY_TABLE_SQL)
            conn.execute(EVENT_STUDY_INDEX_SQL)
            conn.execute(EVENT_STUDY_REGIME_IDX_SQL)
            conn.execute(EVENT_STUDY_WINDOWS_SQL)
            conn.commit()
            _DB_SCHEMA_INITIALIZED = True
        finally:
            conn.close()

def _prune_db_history() -> None:
    cutoff = datetime.now() - timedelta(days=_DB_RETENTION_DAYS)
    cutoff_iso = cutoff.isoformat()
    with _DB_LOCK:
        conn = _get_db_connection()
        try:
            conn.execute("DELETE FROM ff_events WHERE inserted_at < ?", (cutoff_iso,))
            conn.execute("DELETE FROM rss_items WHERE inserted_at < ?", (cutoff_iso,))
            conn.commit()
        finally:
            conn.close()

def _ensure_persistent_store_loaded() -> None:
    global _PERSISTENT_STORE_LOADED
    if _PERSISTENT_STORE_LOADED:
        return
    _ensure_db_schema()
    with _DB_LOCK:
        conn = _get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT key, source, title, currency, impact, forecast, previous, actual, datetime, raw_json FROM ff_events")
            for key, source, title, currency, impact, forecast, previous, actual, dt_text, raw_json in cur.fetchall():
                _STORE["ff_events"][key] = {
                    "source": source, "title": title, "currency": currency,
                    "impact": impact, "forecast": forecast, "previous": previous,
                    "actual": actual, "datetime": _row_to_datetime(dt_text),
                    "raw": json.loads(raw_json) if raw_json else {},
                }
            cur.execute("SELECT key, source, title, summary, link, datetime, impact FROM rss_items")
            for key, source, title, summary, link, dt_text, impact in cur.fetchall():
                _STORE["rss_items"][key] = {
                    "source": source, "title": title, "summary": summary,
                    "link": link, "datetime": _row_to_datetime(dt_text), "impact": impact,
                }
            cur.execute("SELECT series_id, observations_json FROM fred_series")
            for series_id, observations_json in cur.fetchall():
                try:
                    _STORE["fred_series"][series_id] = json.loads(observations_json)
                except Exception:
                    _STORE["fred_series"][series_id] = []
            _PERSISTENT_STORE_LOADED = True
        finally:
            conn.close()

_last_db_prune_at: float = 0.0
_DB_PRUNE_INTERVAL_SECONDS = 300  # prune at most once per 5 minutes

def _maybe_prune_db_history() -> None:
    global _last_db_prune_at
    now = time.time()
    if now - _last_db_prune_at < _DB_PRUNE_INTERVAL_SECONDS:
        return
    _last_db_prune_at = now
    try:
        _prune_db_history()
    except Exception:
        pass

def _persist_ff_event(ev: dict) -> None:
    _ensure_db_schema()
    self_key = f"{ev.get('currency','')}|{ev.get('title','')}|{ev.get('datetime')}"
    inserted_at = datetime.now().isoformat()
    raw_json = json.dumps(ev.get("raw", {}), default=str)
    dt_text = ev.get("datetime").isoformat() if isinstance(ev.get("datetime"), datetime) else None
    with _DB_LOCK:
        conn = _get_db_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO ff_events (key, source, title, currency, impact, forecast, previous, actual, datetime, raw_json, inserted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self_key, ev.get("source"), ev.get("title"), ev.get("currency"), ev.get("impact"),
                 ev.get("forecast"), ev.get("previous"), ev.get("actual"), dt_text, raw_json, inserted_at)
            )
            conn.commit()
        finally:
            conn.close()
    _maybe_prune_db_history()

def _persist_rss_item(item: dict) -> None:
    _ensure_db_schema()
    self_key = re.sub(r"\W+", "", item["title"].lower())[:60]
    inserted_at = datetime.now().isoformat()
    dt_text = item.get("datetime").isoformat() if isinstance(item.get("datetime"), datetime) else None
    with _DB_LOCK:
        conn = _get_db_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO rss_items (key, source, title, summary, link, datetime, impact, inserted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self_key, item.get("source"), item.get("title"), item.get("summary"), item.get("link"), dt_text,
                 item.get("impact"), inserted_at)
            )
            conn.commit()
        finally:
            conn.close()
    _maybe_prune_db_history()

def _persist_fred_series(series_id: str, observations: list[dict]) -> None:
    _ensure_db_schema()
    with _DB_LOCK:
        conn = _get_db_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO fred_series (series_id, observations_json, updated_at) VALUES (?, ?, ?)",
                (series_id, json.dumps(observations, default=str), datetime.now().isoformat())
            )
            conn.commit()
        finally:
            conn.close()

# =============================================================================
# LISTENER TIMING
# =============================================================================

_SOURCE_INTERVAL_SECONDS = {
    "ff":    30 * 60,
    "fred":  60 * 60,
    "rss":   3  * 60,
    "extra": 10 * 60,
}

_listener_thread: Optional[threading.Thread] = None
_listener_stop = threading.Event()
_listener_started_at: Optional[datetime] = None

# =============================================================================
# STORE PRUNING & MERGING
# =============================================================================

def _prune_by_age_and_size(store_key: str) -> None:
    cutoff = datetime.now() - timedelta(hours=_STORE_RETENTION_HOURS)
    bucket = _STORE[store_key]
    stale_keys = [k for k, v in bucket.items()
                  if isinstance(v.get("datetime"), datetime) and v["datetime"] < cutoff]
    for k in stale_keys:
        del bucket[k]
    if len(bucket) > _STORE_MAX_ITEMS_PER_SOURCE:
        ordered = sorted(bucket.items(), key=lambda kv: kv[1].get("datetime") or datetime.min)
        for k, _ in ordered[: len(ordered) - _STORE_MAX_ITEMS_PER_SOURCE]:
            del bucket[k]

def _merge_ff_events(new_events: list[dict]) -> int:
    added = 0
    with _STORE_LOCK:
        for ev in new_events:
            key = f"{ev.get('currency','')}|{ev.get('title','')}|{ev.get('datetime')}"
            if key not in _STORE["ff_events"]:
                added += 1
            _STORE["ff_events"][key] = ev
            try:
                _persist_ff_event(ev)
            except Exception:
                pass
        _prune_by_age_and_size("ff_events")
    return added

def _merge_rss_items(new_items: list[dict]) -> int:
    added = 0
    with _STORE_LOCK:
        for item in new_items:
            key = re.sub(r"\W+", "", item["title"].lower())[:60]
            if not key:
                continue
            if key not in _STORE["rss_items"]:
                added += 1
            _STORE["rss_items"][key] = item
            try:
                _persist_rss_item(item)
            except Exception:
                pass
        _prune_by_age_and_size("rss_items")
    return added

def _merge_fred_series(sid: str, obs_list: list[dict]) -> None:
    """
    Merge new FRED observations into the stored series by date.

    Important: live fetches only pull a few latest points. Never *replace*
    multi-year history with a short list — that would wipe the research ledger.
    """
    _FRED_MAX_OBS = 2000  # cap per series (daily VIX/yields need headroom)
    with _STORE_LOCK:
        existing = list(_STORE["fred_series"].get(sid) or [])
        by_date: dict[str, dict] = {}
        for row in existing + list(obs_list or []):
            if not isinstance(row, dict):
                continue
            d = str(row.get("date") or "")[:10]
            if not d or row.get("value") in (None, ".", ""):
                continue
            by_date[d] = {"date": d, "value": row.get("value")}
        merged = sorted(by_date.values(), key=lambda r: r["date"], reverse=True)
        if len(merged) > _FRED_MAX_OBS:
            merged = merged[:_FRED_MAX_OBS]
        _STORE["fred_series"][sid] = merged
        try:
            _persist_fred_series(sid, merged)
        except Exception:
            pass

# =============================================================================
# FOREX FACTORY CALENDAR
# =============================================================================

_FF_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%m/%d/%Y %I:%M%p",
    "%m/%d/%Y",
]

def _parse_ff_datetime(date_str: str, time_str: str) -> Optional[datetime]:
    if not date_str:
        return None
    # Prefer ISO timestamps from modern FF mirrors (e.g. 2026-07-12T18:30:00-04:00)
    iso_candidate = (date_str or "").strip()
    if "T" in iso_candidate:
        try:
            return _to_naive(datetime.fromisoformat(iso_candidate.replace("Z", "+00:00")))
        except Exception:
            pass
    combined = f"{date_str} {time_str}".strip() if time_str else date_str
    for fmt in _FF_DATE_FORMATS:
        try:
            dt = datetime.strptime(combined, fmt)
            return _to_naive(dt)
        except ValueError:
            continue
    return None

def _fetch_ff_calendar() -> list[dict]:
    """
    Fetch Forex Factory calendar events for the live store.

    Preferred path (B3.4): ``IngestionOrchestrator`` + config FF URL.
    On failure or empty result, falls back to the legacy multi-URL path
    (and Trading Economics if those also fail).
    """
    if _orchestrator_enabled():
        try:
            raw_items = _run_coro_sync(
                _orchestrator_collect(want_rss=False, want_ff=True, want_fred=False),
                timeout=_ORCH_FF_TIMEOUT_S,
                label="ff",
            )
            events = [
                adapted
                for item in (raw_items or [])
                if isinstance(item, dict)
                for adapted in (_adapt_ff_normalized(item),)
                if adapted is not None
            ]
            if events:
                log.info("FF fetch via orchestrator: %s events", len(events))
                return events
            log.warning("FF orchestrator returned no events — trying legacy URLs")
        except Exception as exc:
            log.warning("FF orchestrator path failed (%s) — legacy fallback", exc)
    return _fetch_ff_calendar_legacy()


def _fetch_ff_calendar_legacy() -> list[dict]:
    """Legacy Forex Factory multi-URL fetch with 429-aware retry + TE fallback."""
    events: list[dict] = []

    for url in FF_CALENDAR_URLS:
        for attempt in range(2):  # was 3 — keep first-pass snappy
            try:
                r = requests.get(url, headers=FF_HEADERS, timeout=12)

                # ── 404 = URL doesn't exist, skip ──
                if r.status_code == 404:
                    break

                # ── 429 = Rate limited — backoff ──
                if r.status_code == 429:
                    retry_after = r.headers.get("Retry-After")
                    wait = int(retry_after) if retry_after and str(retry_after).isdigit() else 8 * (attempt + 1)
                    wait = min(wait, 20)
                    print(f"   [FF] 429 rate-limited (attempt {attempt+1}/2) "
                          f"— waiting {wait}s...")
                    time.sleep(wait)
                    continue

                # ── 403 = Blocked — short backoff ──
                if r.status_code == 403:
                    print(f"   [FF] 403 blocked (attempt {attempt+1}/2) — retrying...")
                    time.sleep(3 * (attempt + 1))
                    continue

                r.raise_for_status()

                for ev in r.json():
                    date_str = ev.get("date", "")
                    time_str = ev.get("time", "")
                    dt = _parse_ff_datetime(date_str, time_str)
                    impact_raw = ev.get("impact", "Low")
                    if isinstance(impact_raw, str):
                        impact = {"High": 3, "Medium": 2, "Low": 1, "Holiday": 0}.get(impact_raw, 1)
                    else:
                        impact = int(impact_raw) if impact_raw else 1
                    # FF JSON uses "country" (e.g. USD); some mirrors use "currency"
                    currency = (
                        ev.get("currency")
                        or ev.get("country")
                        or ""
                    )
                    events.append({
                        "source":   "ForexFactory",
                        "title":    ev.get("title", ""),
                        "currency": currency,
                        "impact":   impact,
                        "forecast": ev.get("forecast", ""),
                        "previous": ev.get("previous", ""),
                        "actual":   ev.get("actual", ""),
                        "datetime": dt,
                        "raw":      ev,
                    })
                break  # success

            except requests.exceptions.Timeout:
                print(f"   [FF] Timeout (attempt {attempt+1}/2) — retrying...")
                time.sleep(2 * (attempt + 1))
            except Exception as e:
                print(f"   [FF] Error (attempt {attempt+1}/2): {e}")
                break

    # ── Fallback: if FF completely failed, try Trading Economics ──
    if not events:
        log.warning("FF legacy URLs failed — trying Trading Economics fallback")
        events = _fetch_te_fallback()

    if not events:
        log.warning("FF cycle: no calendar data from any source")

    return events


def _fetch_te_fallback() -> list[dict]:
    """Fallback calendar source — Trading Economics (free guest access)."""
    events: list[dict] = []
    try:
        url = "https://api.tradingeconomics.com/calendar?c=guest:guest"
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            for ev in r.json():
                try:
                    dt = _to_naive(datetime.fromisoformat(
                        ev.get("Date", "").replace("Z", "+00:00")
                    ))
                except Exception:
                    dt = None

                impact_str = str(ev.get("Importance", "")).lower()
                impact = 3 if "high" in impact_str else (2 if "medium" in impact_str else 1)

                events.append({
                    "source":   "TradingEconomics",
                    "title":    ev.get("Event", ""),
                    "currency": ev.get("Country", ""),
                    "impact":   impact,
                    "forecast": str(ev.get("Forecast", "")),
                    "previous": str(ev.get("Previous", "")),
                    "actual":   str(ev.get("Actual", "")),
                    "datetime": dt,
                    "raw":      ev,
                })
            print(f"   [TE] Fallback OK — {len(events)} events")
        else:
            print(f"   [TE] Fallback failed — status {r.status_code}")
    except Exception as e:
        print(f"   [TE] Fallback error: {e}")
    return events

def get_ff_calendar(force_refresh: bool = False) -> list[dict]:
    _ensure_persistent_store_loaded()
    if force_refresh:
        _listener_cycle_ff()
    _ensure_listener_running()
    with _STORE_LOCK:
        return list(_STORE["ff_events"].values())

# =============================================================================
# FRED API
# =============================================================================

def _fred_api_key() -> str:
    """
    Resolve FRED API key from the environment (refreshed each call).

    Never falls back to a hardcoded secret. Returns ``\"\"`` when unset.
    """
    global FRED_API_KEY, _FRED_KEY_WARNED
    key = (os.environ.get("FRED_API_KEY") or "").strip()
    FRED_API_KEY = key
    if not key and not _FRED_KEY_WARNED:
        _FRED_KEY_WARNED = True
        log.warning(
            "FRED_API_KEY is not set in the environment. "
            "FRED fetches will be skipped until the key is provided."
        )
    return key


def _fetch_fred_series(series_id: str, limit: int = 5) -> list[dict]:
    """
    Fetch recent observations for one FRED series (legacy single-series helper).

    Returns an empty list when the API key is missing or the request fails
    (never raises into the listener loop). Prefer ``_fetch_all_fred`` for
    bulk live refreshes (orchestrator path).
    """
    api_key = _fred_api_key()
    if not api_key:
        return []
    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "limit": limit,
                "sort_order": "desc",
            },
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("observations", [])
    except Exception:
        return []


def _fetch_all_fred() -> dict:
    """
    Fetch configured FRED series for the live store.

    Preferred path (B3.4): ``IngestionOrchestrator`` using config series ids.
    ``FRED_API_KEY`` still comes only from the environment; missing key skips
    FRED entirely. On orchestrator failure, falls back to the legacy
    threaded ``requests`` path.
    """
    if not _fred_api_key():
        return {}

    if _orchestrator_enabled():
        try:
            raw_items = _run_coro_sync(
                _orchestrator_collect(want_rss=False, want_ff=False, want_fred=True),
                timeout=_ORCH_FRED_TIMEOUT_S,
                label="fred",
            )
            grouped = _adapt_fred_normalized_groups(
                [item for item in (raw_items or []) if isinstance(item, dict)]
            )
            # Empty can be legitimate (no new points past watermark) — do not
            # fall back in that case; only fall back when the call itself fails.
            log.info(
                "FRED fetch via orchestrator: %s series",
                len(grouped),
            )
            return grouped
        except Exception as exc:
            log.warning("FRED orchestrator path failed (%s) — legacy fallback", exc)

    return _fetch_all_fred_legacy()


def _fetch_all_fred_legacy() -> dict:
    """Legacy parallel FRED fetch via ``requests`` (pre-B3.4 path)."""
    if not _fred_api_key():
        return {}

    result: dict = {}
    result_lock = threading.Lock()

    def _one(sid: str) -> None:
        obs = _fetch_fred_series(sid, limit=5)
        if obs:
            with result_lock:
                result[sid] = obs

    series_ids = list(FRED_SERIES.keys())
    if _HAS_INGESTION_ORCHESTRATOR and DEFAULT_FRED_SERIES:
        # Prefer config order when available; keep engine meta keys as superset.
        series_ids = list(DEFAULT_FRED_SERIES)
    threads = [
        threading.Thread(target=_one, args=(sid,), daemon=True) for sid in series_ids
    ]
    for t in threads:
        t.start()
    deadline = time.time() + 25
    for t in threads:
        remaining = max(0.1, deadline - time.time())
        t.join(timeout=remaining)
    return result

def get_fred_data(force_refresh: bool = False) -> dict:
    _ensure_persistent_store_loaded()
    if force_refresh:
        _listener_cycle_fred()
    _ensure_listener_running()
    with _STORE_LOCK:
        return dict(_STORE["fred_series"])

# =============================================================================
# RSS FEEDS
# =============================================================================

def _fetch_rss_feeds() -> list[dict]:
    """
    Fetch RSS/Atom headlines for the live store.

    Preferred path (B3.4): ``IngestionOrchestrator`` + config feed list.
    On failure, falls back to the legacy threaded feedparser path.
    """
    if _orchestrator_enabled():
        try:
            raw_items = _run_coro_sync(
                _orchestrator_collect(want_rss=True, want_ff=False, want_fred=False),
                timeout=_ORCH_RSS_TIMEOUT_S,
                label="rss",
            )
            adapted: list[dict] = []
            for item in raw_items or []:
                if not isinstance(item, dict):
                    continue
                row = _adapt_rss_normalized(item)
                if row is not None:
                    adapted.append(row)
            # Deduplicate by title key (same as legacy path)
            seen: set[str] = set()
            unique: list[dict] = []
            for row in adapted:
                key = re.sub(r"\W+", "", str(row.get("title", "")).lower())[:60]
                if key and key not in seen:
                    seen.add(key)
                    unique.append(row)
            unique_sorted = sorted(
                unique,
                key=lambda x: x.get("datetime") or datetime.min,
                reverse=True,
            )
            log.info("RSS fetch via orchestrator: %s items", len(unique_sorted))
            return unique_sorted
        except Exception as exc:
            log.warning("RSS orchestrator path failed (%s) — legacy fallback", exc)
    return _fetch_rss_feeds_legacy()


def _fetch_rss_feeds_legacy() -> list[dict]:
    """Legacy parallel RSS fetch via ``requests`` + ``feedparser``."""
    items: list[dict] = []
    items_lock = threading.Lock()
    cutoff = datetime.now() - timedelta(hours=_STORE_RETENTION_HOURS)
    rss_headers = {
        **HEADERS,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    feeds = dict(RSS_FEEDS)
    if _HAS_INGESTION_ORCHESTRATOR and DEFAULT_RSS_FEEDS:
        feeds = dict(DEFAULT_RSS_FEEDS)

    def _pull(name: str, url: str) -> None:
        try:
            # Use requests with timeout + browser headers; feedparser alone
            # can hang indefinitely on blocked/slow hosts.
            r = requests.get(url, headers=rss_headers, timeout=10)
            if r.status_code != 200:
                return
            feed = feedparser.parse(r.content)
            local: list[dict] = []
            for entry in feed.entries:
                pub: Optional[datetime] = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        pub = datetime(*entry.published_parsed[:6])
                    except Exception:
                        pass
                if pub is None and hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    try:
                        pub = datetime(*entry.updated_parsed[:6])
                    except Exception:
                        pass
                pub = pub or datetime.now()
                pub = _to_naive(pub) or datetime.now()
                if pub < cutoff:
                    continue
                local.append({
                    "source":   name,
                    "title":    getattr(entry, "title", ""),
                    "summary":  getattr(entry, "summary", ""),
                    "link":     getattr(entry, "link", ""),
                    "datetime": pub,
                    "impact":   1,
                })
            if local:
                with items_lock:
                    items.extend(local)
        except Exception:
            pass

    threads = [threading.Thread(target=_pull, args=(n, u), daemon=True)
               for n, u in feeds.items()]
    for t in threads:
        t.start()
    # Global wait cap (not 14s per thread — that made refresh feel frozen)
    deadline = time.time() + 18
    for t in threads:
        remaining = max(0.1, deadline - time.time())
        t.join(timeout=remaining)

    seen: set[str] = set()
    unique: list[dict] = []
    for item in items:
        key = re.sub(r"\W+", "", item["title"].lower())[:60]
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
    return sorted(unique, key=lambda x: x["datetime"], reverse=True)

def get_rss_news(force_refresh: bool = False) -> list[dict]:
    _ensure_persistent_store_loaded()
    if force_refresh:
        _listener_cycle_rss()
    _ensure_listener_running()
    with _STORE_LOCK:
        return list(_STORE["rss_items"].values())

# =============================================================================
# EXTRA CALENDAR SOURCES
# =============================================================================

def _fetch_extra_calendars() -> list[dict]:
    """Fetch additional economic calendar RSS feeds."""
    items: list[dict] = []
    for url in EXTRA_CALENDAR_URLS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                pub = datetime.now()
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        pub = _to_naive(datetime(*entry.published_parsed[:6])) or datetime.now()
                    except Exception:
                        pass
                items.append({
                    "source":   "ExtraCalendar",
                    "title":    getattr(entry, "title", ""),
                    "summary":  getattr(entry, "summary", ""),
                    "link":     getattr(entry, "link", ""),
                    "datetime": pub,
                    "impact":   2,  # Default to medium for calendar items
                })
        except Exception:
            pass
    return items

# =============================================================================
# ALWAYS-ON LISTENER
# =============================================================================

def _listener_cycle_ff() -> None:
    state = _SOURCE_STATE["ff"]
    state["last_attempt"] = datetime.now()
    try:
        events = _fetch_ff_calendar()
        added = _merge_ff_events(events)
        # Capture high-impact released prints into event_study (safe / non-fatal)
        try:
            macro = _macro_state_analyzer.latest_state() or {}
            theses = _instrument_thesis_engine.get_all()
            n_surp = _event_study_engine.capture_from_ff_batch(
                events,
                macro_state=macro,
                thesis_list=theses,
                analyze_fn=analyze_ff_event,
            )
            note = f"+{added} new" if added else "no change"
            if n_surp:
                note += f" · surprises={n_surp}"
            state["last_note"] = note
        except Exception as es_exc:
            state["last_note"] = (f"+{added} new" if added else "no change") + f" · event_study skip"
            print(f"   [EVENT_STUDY] capture skipped: {es_exc}")
        state["last_success"] = datetime.now()
        log.info("FF cycle ok: %s", state.get("last_note"))
    except Exception as exc:
        state["last_note"] = f"skipped ({exc})"
        log.warning("FF cycle failed: %s", exc)

def _listener_cycle_rss() -> None:
    state = _SOURCE_STATE["rss"]
    state["last_attempt"] = datetime.now()
    try:
        items = _fetch_rss_feeds()
        added = _merge_rss_items(items)
        state["last_success"] = datetime.now()
        state["last_note"] = f"+{added} new" if added else "no change"
        log.info("RSS cycle ok: %s", state["last_note"])
    except Exception as exc:
        state["last_note"] = f"skipped ({exc})"
        log.warning("RSS cycle failed: %s", exc)

def _listener_cycle_fred() -> None:
    state = _SOURCE_STATE["fred"]
    state["last_attempt"] = datetime.now()
    try:
        if not _fred_api_key():
            state["last_note"] = "skipped (FRED_API_KEY not set)"
            log.info("FRED cycle skipped: FRED_API_KEY not set")
            return
        data = _fetch_all_fred()
        for sid, obs in data.items():
            _merge_fred_series(sid, obs)
        # Refresh textbook MacroState + instrument theses whenever FRED updates
        if data:
            try:
                ms = _macro_state_analyzer.analyze_and_save(data)
                regime = ms.get("regime", "?")
                try:
                    _instrument_thesis_engine.update_from_macro_state(ms)
                except Exception as th_exc:
                    print(f"   [THESIS] update skipped: {th_exc}")
                state["last_success"] = datetime.now()
                state["last_note"] = f"{len(data)} series · macro={regime}"
            except Exception as macro_exc:
                state["last_success"] = datetime.now()
                state["last_note"] = f"{len(data)} series · macro err ({macro_exc})"
        else:
            state["last_success"] = datetime.now()
            state["last_note"] = "no change"
        log.info("FRED cycle ok: %s", state.get("last_note"))
    except Exception as exc:
        state["last_note"] = f"skipped ({exc})"
        log.warning("FRED cycle failed: %s", exc)

def _listener_cycle_extra() -> None:
    state = _SOURCE_STATE["extra"]
    state["last_attempt"] = datetime.now()
    try:
        items = _fetch_extra_calendars()
        added = _merge_rss_items(items)  # reuse RSS merge logic
        state["last_success"] = datetime.now()
        state["last_note"] = f"+{added} new" if added else "no change"
    except Exception as exc:
        state["last_note"] = f"skipped ({exc})"

def _listener_loop(poll_tick_seconds: int = 5) -> None:
    global _listener_started_at
    _listener_started_at = datetime.now()
    log.info("News listener started (v5.0 context-aware)")

    # Skip redundant first-pass if force_refresh already populated sources
    # (last_success within the last 90 seconds).
    def _needs_bootstrap(name: str) -> bool:
        last = _SOURCE_STATE[name].get("last_success")
        if not isinstance(last, datetime):
            return True
        return (datetime.now() - last).total_seconds() > 90

    if _needs_bootstrap("ff"):
        _listener_cycle_ff()
    if _needs_bootstrap("rss"):
        _listener_cycle_rss()
    if _needs_bootstrap("fred"):
        _listener_cycle_fred()
    if _needs_bootstrap("extra"):
        _listener_cycle_extra()

    while not _listener_stop.is_set():
        now = datetime.now()
        for name, cycle_fn in (("ff", _listener_cycle_ff),
                                ("rss", _listener_cycle_rss),
                                ("fred", _listener_cycle_fred),
                                ("extra", _listener_cycle_extra)):
            state = _SOURCE_STATE[name]
            base = _SOURCE_INTERVAL_SECONDS[name]
            jitter = base * random.uniform(-0.15, 0.15)
            due_at = state["last_attempt"] + timedelta(seconds=base + jitter)
            if now >= due_at:
                try:
                    cycle_fn()
                except Exception as exc:
                    log.exception("Listener %s cycle crashed: %s", name, exc)
                    state["last_note"] = f"crashed ({exc})"
        _listener_stop.wait(poll_tick_seconds)

    log.info("News listener stopped")

def start_news_listener() -> None:
    global _listener_thread
    with _STORE_LOCK:
        if _listener_thread is not None and _listener_thread.is_alive():
            return
        _listener_stop.clear()
        _listener_thread = threading.Thread(target=_listener_loop, daemon=True, name="NewsListener")
        _listener_thread.start()

start_news_background_thread = start_news_listener

def stop_news_listener() -> None:
    _listener_stop.set()

def _ensure_listener_running() -> None:
    if not (_listener_thread is not None and _listener_thread.is_alive()):
        start_news_listener()

def listener_status() -> dict:
    with _STORE_LOCK:
        return {
            "alive": bool(_listener_thread and _listener_thread.is_alive()),
            "started_at": _listener_started_at,
            "sources": {k: dict(v) for k, v in _SOURCE_STATE.items()},
            "store_totals": {
                "ff_events":   len(_STORE["ff_events"]),
                "rss_items":   len(_STORE["rss_items"]),
                "fred_series": len(_STORE["fred_series"]),
            },
        }

# =============================================================================
# ANALYSIS ENGINE (enhanced with sentiment_analyzer)
# =============================================================================

def _compare_ff_beat_miss(event: dict) -> tuple[Optional[str], Optional[float]]:
    try:
        def _clean(s: str) -> str:
            return (s.replace("%", "").replace("K", "000").replace("M", "000000").strip())
        actual_str   = _clean(str(event.get("actual",   "") or ""))
        forecast_str = _clean(str(event.get("forecast", "") or ""))
        if not actual_str or actual_str in (".", "–", "-", ""):
            return None, None
        if not forecast_str or forecast_str in (".", "–", "-", ""):
            return None, None
        actual   = float(re.sub(r"[^\d.\-]", "", actual_str))
        forecast = float(re.sub(r"[^\d.\-]", "", forecast_str))
        surprise_pct = ((actual - forecast) / abs(forecast) * 100) if forecast else None
        if actual > forecast:   return "beat", surprise_pct
        if actual < forecast:   return "miss", surprise_pct
        return "inline", 0.0
    except Exception:
        return None, None

def _guess_category(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["cpi", "inflation", "pce", "price"]):         return "inflation"
    if any(w in t for w in ["payroll", "nfp", "unemployment", "jobs"]):   return "employment"
    if any(w in t for w in ["gdp", "growth", "output"]):                  return "gdp"
    if any(w in t for w in ["rate", "fomc", "federal", "ecb", "boe"]):    return "rate"
    if any(w in t for w in ["yield", "treasury", "bond"]):                return "yield"
    if any(w in t for w in ["dollar", "dxy", "index"]):                   return "dollar_index"
    if any(w in t for w in ["gold", "xau", "silver"]):                    return "commodity"
    if any(w in t for w in ["vix", "volatility"]):                         return "volatility"
    if any(w in t for w in ["balance sheet", "m2", "money supply", "qt", "qe"]): return "monetary"
    if any(w in t for w in ["eur", "gbp", "usd", "chf"]):                 return "fx"
    return "general"

def _project_impact(category: str, direction: str) -> tuple[str, str]:
    proj = FRED_PROJECTION.get(category, {})
    return proj.get(direction, ("Unknown", "Unknown"))

def _causality_narrative(category: str, direction: str) -> list[str]:
    if direction not in ("up", "down"):
        return []
    return CAUSALITY_CHAINS.get(category, {}).get(direction, [])

def analyze_ff_event(event: dict) -> dict:
    title     = event.get("title", "")
    currency  = event.get("currency", "USD")
    impact    = event.get("impact", 1)
    actual    = event.get("actual", "")
    forecast  = event.get("forecast", "")
    previous  = event.get("previous", "")
    beat_miss, surprise_pct = _compare_ff_beat_miss(event)

    # Use enhanced sentiment analyzer
    sentiment = _sentiment_analyzer.analyze(title)
    instrument_labels = sentiment["instrument_labels"]
    instrument_weights = sentiment["instrument_weights"]
    general_tone = sentiment["general_tone"]
    entities = sentiment["entities"]

    if beat_miss == "miss":
        instrument_labels = {k: ("BULL" if v == "BEAR" else "BEAR" if v == "BULL" else v)
                             for k, v in instrument_labels.items()}
        instrument_weights = {k: -v for k, v in instrument_weights.items()}

    cat        = _guess_category(title)
    direction  = "up" if beat_miss == "beat" else ("down" if beat_miss == "miss" else "neutral")
    short_t, long_t = (_project_impact(cat, direction) if direction != "neutral"
                       else ("Await actual", "Await actual"))
    reasoning = _causality_narrative(cat, direction)

    return {
        "source":                "ForexFactory",
        "title":                 title,
        "currency":              currency,
        "impact":                impact,
        "actual":                actual,
        "forecast":              forecast,
        "previous":              previous,
        "beat_miss":             beat_miss,
        "surprise_magnitude_pct": surprise_pct,
        "instrument_signals":    instrument_labels,
        "instrument_weights":     instrument_weights,
        "general_tone":          general_tone,
        "entities":              entities,
        "short_term_impact":     short_t,
        "long_term_impact":      long_t,
        "macro_reasoning":       reasoning,
        "datetime":              event.get("datetime"),
    }

def analyze_rss_item(item: dict) -> dict:
    title    = item.get("title",   "")
    summary  = item.get("summary", "")
    combined = f"{title} {summary}"

    sentiment = _sentiment_analyzer.analyze(combined)
    instrument_labels = sentiment["instrument_labels"]
    instrument_weights = sentiment["instrument_weights"]
    general_tone = sentiment["general_tone"]
    entities = sentiment["entities"]

    n_kw = len(entities.get("indicators", [])) + len(entities.get("central_banks", []))
    impact = 3 if n_kw >= 3 else (2 if n_kw >= 1 else 1)

    # Also check keyword density for impact
    lower = combined.lower()
    high_impact_kws = {"cpi", "fomc", "nfp", "rate hike", "rate cut", "gdp",
                       "inflation", "powell", "lagarde", "ecb", "boe",
                       "recession", "war", "tariff"}
    if any(kw in lower for kw in high_impact_kws):
        impact = max(impact, 2)

    cat        = _guess_category(combined)
    direction  = "up" if general_tone > 0.2 else ("down" if general_tone < -0.2 else "neutral")
    short_t, long_t = (_project_impact(cat, direction) if direction != "neutral"
                       else ("Unclear", "Unclear"))
    reasoning = _causality_narrative(cat, direction)

    return {
        "source":             item.get("source", "RSS"),
        "title":              title[:120],
        "summary":            summary[:200] if summary else "",
        "impact":             impact,
        "instrument_signals": instrument_labels,
        "instrument_weights": instrument_weights,
        "general_tone":       general_tone,
        "entities":           entities,
        "short_term_impact":  short_t,
        "long_term_impact":   long_t,
        "macro_reasoning":    reasoning,
        "datetime":           item.get("datetime"),
        "link":               item.get("link", ""),
    }

# =============================================================================
# FRED NARRATIVE BUILDER
# =============================================================================

def build_fred_narrative(fred_data: dict) -> list[dict]:
    narratives = []
    for sid, obs_list in fred_data.items():
        if len(obs_list) < 2:
            continue
        label, _, cat = FRED_SERIES.get(sid, (sid, "USD", "general"))
        try:
            latest_val = float(obs_list[0]["value"])
            prev_val   = float(obs_list[1]["value"])
        except (ValueError, KeyError, IndexError):
            continue

        change_pct = ((latest_val - prev_val) / abs(prev_val) * 100) if prev_val else 0
        direction  = "up" if latest_val > prev_val else "down"
        short_t, long_t = _project_impact(cat, direction)
        reasoning = _causality_narrative(cat, direction)

        narratives.append({
            "series_id":         sid,
            "label":             label,
            "latest_value":      latest_val,
            "previous_value":    prev_val,
            "change_pct":        change_pct,
            "direction":         direction,
            "short_term_impact": short_t,
            "long_term_impact":  long_t,
            "macro_reasoning":   reasoning,
            "latest_date":       obs_list[0].get("date", ""),
            "previous_date":     obs_list[1].get("date", ""),
        })
    return narratives

# =============================================================================
# AGGREGATE PRESSURE SCORES
# =============================================================================

def compute_pressure_scores(analyzed_events: list[dict]) -> dict[str, float]:
    scores: dict[str, float] = {"XAUUSD": 0.0, "EURUSD": 0.0, "GBPUSD": 0.0, "USDCHF": 0.0}
    for ev in analyzed_events:
        weight = ev.get("impact", 1)
        weighted_signals = ev.get("instrument_weights")
        if weighted_signals:
            for inst, w in weighted_signals.items():
                if inst in scores:
                    scores[inst] += w * weight
        else:
            for inst, sig in ev.get("instrument_signals", {}).items():
                if inst not in scores:
                    continue
                if sig == "BULL":   scores[inst] += weight
                elif sig == "BEAR":  scores[inst] -= weight
    # Record to pattern engine
    for inst, val in scores.items():
        record_pressure(inst, val)
    return scores

# =============================================================================
# MASTER ENTRY POINT
# =============================================================================

def get_news_context(force_refresh: bool = False,
                     high_impact_only: bool = False,
                     lookback_hours: int = NEWS_LOOKBACK_HOURS) -> dict:
    """
    Returns a complete analytical snapshot with context intelligence.
    """
    try:
        cutoff = datetime.now() - timedelta(hours=lookback_hours)

        # On force refresh, pull all sources in parallel once (faster first paint)
        if force_refresh:
            _ensure_persistent_store_loaded()
            threads = [
                threading.Thread(target=_listener_cycle_ff, daemon=True),
                threading.Thread(target=_listener_cycle_rss, daemon=True),
                threading.Thread(target=_listener_cycle_fred, daemon=True),
                threading.Thread(target=_listener_cycle_extra, daemon=True),
            ]
            for t in threads:
                t.start()
            deadline = time.time() + 40
            for t in threads:
                remaining = max(0.1, deadline - time.time())
                t.join(timeout=remaining)

        ff_raw = get_ff_calendar(force_refresh=False)
        ff_recent = [e for e in ff_raw if e.get("datetime") is not None and e["datetime"] >= cutoff]
        ff_analyzed = [
            analyze_ff_event(e) for e in ff_recent
            if e["impact"] >= (2 if high_impact_only else 1)
        ]

        # Event-study capture is done in the FF listener cycle; also do a light
        # pass here so one-shot runs without a prior listener still log surprises.
        try:
            _ms_preview = _macro_state_analyzer.latest_state() or {}
            _th_preview = _instrument_thesis_engine.get_all()
            # Prefer pairing analyzed signals when available
            _by_title = {(a.get("title"), str(a.get("datetime"))): a for a in ff_analyzed}
            for _ev in ff_raw:
                if int(_ev.get("impact") or 0) < 2:
                    continue
                _key = (_ev.get("title"), str(_ev.get("datetime")))
                _event_study_engine.capture_release(
                    _ev,
                    macro_state=_ms_preview,
                    analyzed=_by_title.get(_key),
                    thesis_list=_th_preview,
                )
        except Exception as _es_exc:
            print(f"   [EVENT_STUDY] context capture skipped: {_es_exc}")

        rss_raw = get_rss_news(force_refresh=False)
        rss_recent = [i for i in rss_raw
                     if isinstance(i.get("datetime"), datetime) and i["datetime"] >= cutoff]
        high_impact_kws = {"cpi", "fomc", "nfp", "rate", "gdp", "inflation",
                           "gold", "xauusd", "powell", "lagarde", "ecb", "boe",
                           "recession", "war", "tariff", "rate hike", "rate cut"}
        rss_analyzed = [
            analyze_rss_item(i) for i in rss_recent
            if not high_impact_only or
            any(kw in (i.get("title", "") + i.get("summary", "")).lower()
                for kw in high_impact_kws)
        ]

        fred_data       = get_fred_data(force_refresh=False)
        fred_narratives = build_fred_narrative(fred_data)

        # ── TEXTBOOK MACRO STATE (FRED → growth/inflation/policy/liquidity/risk) ──
        macro_state: dict = {}
        instrument_theses: list = []
        try:
            if fred_data:
                macro_state = _macro_state_analyzer.analyze_and_save(fred_data)
            else:
                macro_state = _macro_state_analyzer.latest_state() or {}
        except Exception as macro_exc:
            print(f"   [MACRO] MacroStateAnalyzer skipped: {macro_exc}")
            macro_state = {}

        # ── INSTRUMENT THESES (regime playbook → 4 symbols) ──
        try:
            if macro_state and macro_state.get("regime"):
                instrument_theses = _instrument_thesis_engine.update_from_macro_state(
                    macro_state
                )
            else:
                instrument_theses = _instrument_thesis_engine.get_all()
        except Exception as th_exc:
            print(f"   [THESIS] InstrumentThesisEngine skipped: {th_exc}")
            instrument_theses = []

        pressure = compute_pressure_scores(ff_analyzed + rss_analyzed)

        # ── CONTEXT INTELLIGENCE LAYER ──
        # This is the key v5.0 upgrade: pass everything to the pattern engine
        # for context-aware analysis (patterns, regime, convergence, forward calendar)
        all_analyzed = ff_analyzed + rss_analyzed
        context_report = build_context_report(
            analyzed_events=all_analyzed,
            ff_events_raw=ff_raw,
            pressure_scores=pressure,
            lookback_hours=lookback_hours,
        )
        # Attach data-driven macro state + theses alongside news-flow regime
        if context_report is not None:
            if macro_state:
                context_report["macro_state"] = macro_state
            if instrument_theses:
                context_report["instrument_theses"] = instrument_theses

        summary = _build_summary(
            ff_analyzed, rss_analyzed, fred_narratives,
            pressure, context_report,
            macro_state=macro_state,
            instrument_theses=instrument_theses,
        )

        status = listener_status()

        # Research Desk payload (Step 5.1) — display-ready aggregate; GUI unused yet
        research_desk: dict = {}
        try:
            research_desk = get_research_desk_from_context({
                "macro_state": macro_state,
                "instrument_theses": instrument_theses,
            })
        except Exception as desk_exc:
            print(f"   [RESEARCH_DESK] aggregate skipped: {desk_exc}")
            research_desk = {}

        return {
            "ff_analyzed":         ff_analyzed,
            "rss_analyzed":        rss_analyzed,
            "fred_narratives":     fred_narratives,
            "macro_state":         macro_state,
            "instrument_theses":   instrument_theses,
            "research_desk":       research_desk,
            "pressure_scores":     pressure,
            "context_report":      context_report,
            "store_totals":        status["store_totals"],
            "listener_alive":      status["alive"],
            "summary":             summary,
            "generated_at":        datetime.now(),
        }
    except Exception as exc:
        log.exception("get_news_context degraded this cycle: %s", exc)
        return {
            "ff_analyzed": [], "rss_analyzed": [], "fred_narratives": [],
            "macro_state": {}, "instrument_theses": [], "research_desk": {},
            "pressure_scores": {"XAUUSD": 0.0, "EURUSD": 0.0, "GBPUSD": 0.0, "USDCHF": 0.0},
            "context_report": {}, "store_totals": {}, "listener_alive": False,
            "summary": "News engine temporarily unavailable this cycle — using neutral defaults.",
            "generated_at": datetime.now(),
        }

# =============================================================================
# SUMMARY FORMATTER
# =============================================================================

def _build_summary(ff_analyzed: list[dict], rss_analyzed: list[dict],
                   fred_narratives: list[dict], pressure: dict[str, float],
                   context_report: dict, macro_state: Optional[dict] = None,
                   instrument_theses: Optional[list] = None) -> str:
    """
    Build the full text summary with improved spacing and readability.
    Used by non-dashboard modes (one-shot, news-only, __main__).
    """
    lines = []
    w = 110
    sep = "═" * w
    sub_sep = "─" * w
    dot_sep = "·" * w

    # ── Header ──
    lines.append("")
    lines.append(sep)
    lines.append("  📰  INSTITUTIONAL NEWS & MACRO ANALYTICAL ENGINE  ·  v5.0 Context-Aware")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(sep)
    lines.append("")

    status = listener_status()
    dot = "🟢 LIVE" if status["alive"] else "🔴 NOT RUNNING"
    totals = status["store_totals"]
    lines.append(f"  Listener: {dot}")
    lines.append(f"  Sentiment: {_sentiment_analyzer.method}")
    lines.append(f"  Store →  FF: {totals.get('ff_events', 0)}   "
                 f"RSS: {totals.get('rss_items', 0)}   "
                 f"FRED: {totals.get('fred_series', 0)} series")
    lines.append("")
    lines.append(sub_sep)
    lines.append("")

    # ── Textbook Macro State (FRED rules — learn from this block) ──
    if macro_state:
        lines.append("  ║                     📚  TEXTBOOK MACRO STATE (FRED rules)                              ║")
        lines.append(sub_sep)
        lines.append(f"  Regime:     {macro_state.get('regime', 'N/A')}  "
                     f"(confidence {macro_state.get('confidence', 0):.0%})")
        lines.append(f"  Growth:     {macro_state.get('growth', 'N/A')}")
        lines.append(f"  Inflation:  {macro_state.get('inflation', 'N/A')}")
        lines.append(f"  Policy:     {macro_state.get('policy', 'N/A')}")
        lines.append(f"  Liquidity:  {macro_state.get('liquidity', 'N/A')}")
        lines.append(f"  Risk:       {macro_state.get('risk', 'N/A')}")
        if macro_state.get("summary_line"):
            lines.append(f"  Snapshot:   {macro_state['summary_line']}")
        lines.append("")
        # Short lesson teaser (full lesson is in macro_state['lesson'] / DB)
        lesson = macro_state.get("lesson") or ""
        for ln in lesson.splitlines()[:12]:
            lines.append(f"  {ln}" if ln else "")
        if lesson.count("\n") > 12:
            lines.append("  … (full lesson stored in macro_state table)")
        lines.append("")
        lines.append(sub_sep)
        lines.append("")

    # ── Instrument theses (regime playbook → 4 symbols) ──
    if instrument_theses:
        lines.append("  ║                     🎯  INSTRUMENT THESES (regime playbook)                           ║")
        lines.append(sub_sep)
        by_sym = {t.get("symbol"): t for t in instrument_theses}
        for sym in TRACKED_SYMBOLS:
            t = by_sym.get(sym) or {}
            bias = t.get("current_bias", "N/A")
            lines.append(f"  {sym:<8}  {bias}")
            thesis = (t.get("active_thesis") or "")[:160]
            if thesis:
                lines.append(f"           {thesis}{'…' if len(t.get('active_thesis') or '') > 160 else ''}")
        lines.append("")
        lines.append(sub_sep)
        lines.append("")

    # ── Context Intelligence Report ──
    if context_report and context_report.get("narrative"):
        lines.append(context_report["narrative"])
        lines.append("")
        lines.append(sub_sep)
        lines.append("")

    # ── Forex Factory ──
    lines.append("  ╔════════════════════════════════════════════════════════════════════════════════════════╗")
    lines.append("  ║ 📅  FOREX FACTORY ECONOMIC CALENDAR                              ║")
    lines.append("  ╚════════════════════════════════════════════════════════════════════════════════════════╝")
    lines.append("")

    if not ff_analyzed:
        lines.append("  No events in current lookback window.")
        lines.append("")
    else:
        BM_LABEL = {"beat": "✅ BEAT", "miss": "❌ MISS",
                    "inline": "➡ INLINE", None: "⏳ PENDING"}
        for tier_label, min_impact in [("🔴 HIGH IMPACT", 3), ("🟡 MEDIUM IMPACT", 2), ("⚪ LOW IMPACT", 1)]:
            tier_events = [e for e in ff_analyzed if e["impact"] == min_impact]
            if not tier_events:
                continue
            lines.append(f"  {tier_label}")
            lines.append(f"  {'─' * 80}")
            lines.append("")
            for ev in tier_events:
                dt = ev.get("datetime")
                dt_str = dt.strftime("%m/%d %H:%M") if dt else "??"
                bm = BM_LABEL.get(ev.get("beat_miss"), "")
                lines.append(f"    [{dt_str}] {ev['currency']} — {ev['title']}")
                lines.append("")
                if ev.get("actual") or ev.get("forecast"):
                    surprise = ev.get("surprise_magnitude_pct")
                    surprise_str = f"  |  Surprise: {surprise:+.1f}%" if surprise is not None else ""
                    lines.append(f"      Actual: {ev.get('actual') or '?'} "
                                 f"Forecast: {ev.get('forecast') or '?'}    "
                                 f"Prev: {ev.get('previous') or '?'}    {bm}{surprise_str}")
                    lines.append("")
                if ev.get("instrument_signals"):
                    sigs = "   ".join(f"{k}: {v}" for k, v in ev["instrument_signals"].items())
                    lines.append(f"      Signals → {sigs}")
                    lines.append("")
                if ev.get("general_tone") is not None:
                    tone = ev["general_tone"]
                    tone_str = "BULLISH" if tone > 0.2 else ("BEARISH" if tone < -0.2 else "NEUTRAL")
                    lines.append(f"      Sentiment: {tone_str} ({tone:+.2f})")
                    lines.append("")
                lines.append(f"      📊 SHORT-TERM: {ev['short_term_impact']}")
                lines.append(f"      📈 LONG-TERM:  {ev['long_term_impact']}")
                lines.append("")
                for r in ev.get("macro_reasoning", []):
                    lines.append(f"      🧠 {r}")
                    lines.append("")
                lines.append(f"  {dot_sep}")
                lines.append("")

    # ── RSS headlines ──
    lines.append("  ╔════════════════════════════════════════════════════════════════════════════════════════╗")
    lines.append("  ║                        📰  LIVE NEWS HEADLINES — Top 15 Relevant                        ║")
    lines.append("  ╚════════════════════════════════════════════════════════════════════════════════════════╝")
    lines.append("")

    sorted_rss = sorted(
        rss_analyzed,
        key=lambda x: (-x["impact"],
                       -(x["datetime"].timestamp() if isinstance(x.get("datetime"), datetime) else 0))
    )
    shown = 0
    for item in sorted_rss:
        if not item.get("instrument_signals"):
            continue
        dt = item.get("datetime")
        dt_str = dt.strftime("%m/%d %H:%M") if isinstance(dt, datetime) else "??"
        impact_tier = {3: "🔴 HIGH", 2: "🟡 MEDIUM", 1: "⚪ LOW"}.get(item.get("impact", 1), "—")
        lines.append(f"    [{dt_str}] [{impact_tier}] ({item['source']})")
        lines.append(f"    {item['title']}")
        lines.append("")
        sigs = "   ".join(f"{k}: {v}" for k, v in item["instrument_signals"].items())
        lines.append(f"      Signals → {sigs}")
        if item.get("general_tone") is not None:
            tone = item["general_tone"]
            tone_str = "BULLISH" if tone > 0.2 else ("BEARISH" if tone < -0.2 else "NEUTRAL")
            lines.append(f"      Sentiment: {tone_str} ({tone:+.2f})")
        lines.append(f"      📊 SHORT: {item['short_term_impact']}")
        lines.append(f"      📈 LONG:  {item['long_term_impact']}")
        lines.append("")
        for r in item.get("macro_reasoning", []):
            lines.append(f"      🧠 {r}")
            lines.append("")
        if item.get("link"):
            lines.append(f"      🔗 {item['link'][:80]}")
        lines.append(f"  {dot_sep}")
        lines.append("")
        shown += 1
        if shown >= 15:
            break
    if shown == 0:
        lines.append("  No relevant headlines with instrument signals in current window.")
        lines.append("")

    # ── FRED ──
    lines.append("  ╔════════════════════════════════════════════════════════════════════════════════════════╗")
    lines.append("  ║                        🏛️  FRED MACRO INDICATORS                                       ║")
    lines.append("  ╚════════════════════════════════════════════════════════════════════════════════════════╝")
    lines.append("")

    if not fred_narratives:
        lines.append("  FRED data unavailable (check API key or network).")
        lines.append("")
    else:
        for n in fred_narratives:
            arrow = "↑" if n["direction"] == "up" else "↓"
            lines.append(f"    {n['label']} ({n['series_id']})")
            lines.append("")
            lines.append(f"      Latest: {n['latest_value']:.4f} ({n['latest_date']})    "
                         f"Prev: {n['previous_value']:.4f} ({n['previous_date']})    "
                         f"{arrow} {abs(n['change_pct']):.2f}%")
            lines.append("")
            lines.append(f"      📊 SHORT: {n['short_term_impact']}")
            lines.append(f"      📈 LONG:  {n['long_term_impact']}")
            lines.append("")
            for r in n.get("macro_reasoning", []):
                lines.append(f"      🧠 {r}")
                lines.append("")
            lines.append(f"  {dot_sep}")
            lines.append("")

    # ── Pressure scoreboard ──
    lines.append("  ╔════════════════════════════════════════════════════════════════════════════════════════╗")
    lines.append("  ║                        📊  AGGREGATE NEWS PRESSURE SCORES                              ║")
    lines.append("  ╚════════════════════════════════════════════════════════════════════════════════════════╝")
    lines.append("")
    lines.append("  (Positive = net bullish pressure  |  Negative = net bearish pressure)")
    lines.append("  (Weighted: conviction score × impact tier, HIGH×3  MEDIUM×2  LOW×1)")
    lines.append("")
    for inst, score in sorted(pressure.items(), key=lambda x: -abs(x[1])):
        bar_len = min(int(abs(score)), 30)
        bar = ("█" * bar_len) if score >= 0 else ("▓" * bar_len)
        label = "BULLISH ↑" if score > 1 else ("BEARISH ↓" if score < -1 else "NEUTRAL →")
        lines.append(f"  {inst:<10} {score:+6.1f}  {bar:<30}  {label}")
    lines.append("")

    lines.append(sep)
    return "\n".join(lines)

# =============================================================================
# STANDALONE RUN
# =============================================================================

if __name__ == "__main__":
    print("Starting v5.0 Context-Aware engine and doing an initial synchronous pass…\n")
    ctx = get_news_context(force_refresh=True)
    print(ctx["summary"])
    print(f"\nTotals → FF={len(ctx['ff_analyzed'])} events  "
          f"RSS={len(ctx['rss_analyzed'])} items  "
          f"FRED={len(ctx['fred_narratives'])} series  "
          f"| Listener alive: {ctx['listener_alive']}")