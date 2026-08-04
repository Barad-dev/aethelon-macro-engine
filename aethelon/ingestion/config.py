# -*- coding: utf-8 -*-
"""
aethelon.ingestion.config — Ingestion source configuration (Stage B3.3)
======================================================================
Central place for non-secret ingestion settings:

  * RSS / Atom feed list (display name → URL)
  * FRED series ids (and optional display metadata)
  * Forex Factory calendar URL
  * FRED observations endpoint
  * Small orchestrator defaults (limits, fail-soft, etc.)

Secrets (e.g. ``FRED_API_KEY``) are **never** stored here. They are read
only from the process environment at runtime.

This module is intentionally plain Python (no YAML/JSON loader yet) so it
stays easy to import, type-check, and review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

__all__ = [
    "RssFeedSpec",
    "FredSeriesMeta",
    "IngestionConfig",
    "DEFAULT_RSS_FEEDS",
    "DEFAULT_FRED_SERIES",
    "DEFAULT_FRED_SERIES_META",
    "FOREX_FACTORY_WEEKLY_URL",
    "FRED_OBSERVATIONS_URL",
    "FRED_API_KEY_ENV",
    "DEFAULT_FRED_RECENT_LIMIT",
    "DEFAULT_RUN_FOREX_FACTORY",
    "DEFAULT_FAIL_SOFT",
    "default_ingestion_config",
]

# (source_name, feed_url)
RssFeedSpec = tuple[str, str]

# (label, currency_or_unit_hint, category)
FredSeriesMeta = tuple[str, str, str]


# =============================================================================
# Endpoints (public, non-secret)
# =============================================================================

FOREX_FACTORY_WEEKLY_URL: str = (
    "https://nodedata.forexfactory.com/calendar/weekly.json"
)

FRED_OBSERVATIONS_URL: str = (
    "https://api.stlouisfed.org/fred/series/observations"
)

# Environment variable name only — never a key value.
FRED_API_KEY_ENV: str = "FRED_API_KEY"


# =============================================================================
# Orchestrator defaults
# =============================================================================

DEFAULT_FRED_RECENT_LIMIT: int = 20
DEFAULT_RUN_FOREX_FACTORY: bool = True
DEFAULT_FAIL_SOFT: bool = True


# =============================================================================
# RSS feeds (name → url)
# Working set mirrored from the live engine's known-good list.
# Blocked / 404 / non-RSS sources intentionally omitted.
# =============================================================================

DEFAULT_RSS_FEEDS: dict[str, str] = {
    # Major financial news
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "Nasdaq Markets": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
    "MarketWatch": "https://feeds.marketwatch.com/marketwatch/topstories/",
    "MarketWatch Forex": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "CNBC Economy": (
        "https://search.cnbc.com/rs/search/combinedcms/view.xml"
        "?partnerId=wrss01&id=20910258"
    ),
    "CNBC Top News": (
        "https://search.cnbc.com/rs/search/combinedcms/view.xml"
        "?partnerId=wrss01&id=100003114"
    ),
    "WSJ Markets": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "Bloomberg Markets": "https://feeds.bloomberg.com/markets/news.rss",
    # Forex-specific
    "FXStreet": "https://www.fxstreet.com/rss/news",
    "ForexLive": "https://www.forexlive.com/feed/news",
    "ForexLive Technicals": "https://www.forexlive.com/feed/technical-analysis",
    # Economic analysis
    "Investing.com News": "https://www.investing.com/rss/news.rss",
    "Investing.com Forex": "https://www.investing.com/rss/news_25.rss",
    "Investing.com Economy": "https://www.investing.com/rss/news_1.rss",
    # Alternative / aggregate
    "Google News Forex": (
        "https://news.google.com/rss/search?q=forex+economy"
        "&hl=en-US&gl=US&ceid=US:en"
    ),
    "Google News Gold": (
        "https://news.google.com/rss/search?q=gold+price+market"
        "&hl=en-US&gl=US&ceid=US:en"
    ),
    "Google News Fed": (
        "https://news.google.com/rss/search?q=federal+reserve+rate"
        "&hl=en-US&gl=US&ceid=US:en"
    ),
    "Google News Inflation": (
        "https://news.google.com/rss/search?q=inflation+cpi+economy"
        "&hl=en-US&gl=US&ceid=US:en"
    ),
    "Google News Recession": (
        "https://news.google.com/rss/search?q=recession+economy+gdp"
        "&hl=en-US&gl=US&ceid=US:en"
    ),
    "Google News Tariff": (
        "https://news.google.com/rss/search?q=tariff+trade+war"
        "&hl=en-US&gl=US&ceid=US:en"
    ),
    # Central bank
    "Fed Reserve News": "https://www.federalreserve.gov/feeds/press_all.xml",
    # Crypto / risk sentiment
    "CoinDesk Bitcoin": "https://www.coindesk.com/arc/outboundfeeds/rss/",
}


# =============================================================================
# FRED series
# =============================================================================

DEFAULT_FRED_SERIES_META: dict[str, FredSeriesMeta] = {
    "FEDFUNDS": ("Fed Funds Rate", "USD", "rate"),
    "CPIAUCSL": ("CPI All Items", "USD", "inflation"),
    "UNRATE": ("US Unemployment Rate", "USD", "employment"),
    "PAYEMS": ("Nonfarm Payrolls (000s)", "USD", "employment"),
    "GDP": ("US Real GDP Growth", "USD", "gdp"),
    "DGS10": ("10-Yr Treasury Yield", "USD", "yield"),
    "DGS2": ("2-Yr Treasury Yield", "USD", "yield"),
    "DTWEXBGS": ("USD Broad Trade-Weighted Index", "USD", "dollar_index"),
    "GOLDAMGBD228NLBM": ("Gold Fix AM Price", "XAU", "commodity"),
    "EUROUSDM": ("EUR/USD Monthly", "EUR", "fx"),
    "GBPUSDM": ("GBP/USD Monthly", "GBP", "fx"),
    "PCEPI": ("PCE Price Index", "USD", "inflation"),
    "COREPCE": ("Core PCE Price Index", "USD", "inflation"),
    "T10YIE": ("10-Yr Breakeven Inflation Rate", "USD", "inflation"),
    "M2SL": ("M2 Money Supply", "USD", "monetary"),
    "WALCL": ("Fed Total Assets (Balance Sheet)", "USD", "monetary"),
    "VIXCLS": ("VIX Volatility Index", "USD", "volatility"),
    "DEXUSEU": ("USD/EUR Exchange Rate", "EUR", "fx"),
    "DEXUSUK": ("USD/GBP Exchange Rate", "GBP", "fx"),
    "DEXUSCH": ("USD/CHF Exchange Rate", "CHF", "fx"),
    "DEXJPUS": ("JPY/USD Exchange Rate", "JPY", "fx"),
}

# Stable ordered id list used by the orchestrator for fetches.
DEFAULT_FRED_SERIES: tuple[str, ...] = tuple(DEFAULT_FRED_SERIES_META.keys())


# =============================================================================
# Bundled config object
# =============================================================================

@dataclass(frozen=True)
class IngestionConfig:
    """
    Immutable bundle of non-secret ingestion settings.

    Pass an instance to ``IngestionOrchestrator`` to override module defaults
    without changing driver code. API keys are never part of this object.
    """

    rss_feeds: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_RSS_FEEDS))
    fred_series: Sequence[str] = field(default_factory=lambda: list(DEFAULT_FRED_SERIES))
    fred_series_meta: Mapping[str, FredSeriesMeta] = field(
        default_factory=lambda: dict(DEFAULT_FRED_SERIES_META)
    )
    forex_factory_url: str = FOREX_FACTORY_WEEKLY_URL
    fred_observations_url: str = FRED_OBSERVATIONS_URL
    fred_recent_limit: int = DEFAULT_FRED_RECENT_LIMIT
    run_forex_factory: bool = DEFAULT_RUN_FOREX_FACTORY
    fail_soft: bool = DEFAULT_FAIL_SOFT

    def rss_feed_specs(self) -> list[RssFeedSpec]:
        """Return RSS feeds as ``(name, url)`` pairs in mapping order."""
        out: list[RssFeedSpec] = []
        for name, url in self.rss_feeds.items():
            n = str(name).strip()
            u = str(url).strip()
            if n and u:
                out.append((n, u))
        return out

    def fred_series_ids(self) -> list[str]:
        """Return uppercased, de-duplicated FRED series ids (order preserved)."""
        seen: set[str] = set()
        out: list[str] = []
        for raw in self.fred_series:
            sid = str(raw or "").strip().upper()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append(sid)
        return out


def default_ingestion_config() -> IngestionConfig:
    """
    Build a fresh ``IngestionConfig`` from module-level defaults.

    Returns a new instance each call so callers can treat it as isolated.
    """
    return IngestionConfig(
        rss_feeds=dict(DEFAULT_RSS_FEEDS),
        fred_series=list(DEFAULT_FRED_SERIES),
        fred_series_meta=dict(DEFAULT_FRED_SERIES_META),
        forex_factory_url=FOREX_FACTORY_WEEKLY_URL,
        fred_observations_url=FRED_OBSERVATIONS_URL,
        fred_recent_limit=DEFAULT_FRED_RECENT_LIMIT,
        run_forex_factory=DEFAULT_RUN_FOREX_FACTORY,
        fail_soft=DEFAULT_FAIL_SOFT,
    )
