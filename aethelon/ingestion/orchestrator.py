# -*- coding: utf-8 -*-
"""
aethelon.ingestion.orchestrator — Stage B3.2/B3.3 IngestionOrchestrator
=======================================================================
Coordinates existing source drivers and returns a flat list of
``NormalizedItem`` rows. This layer:

  * owns (or accepts) ``AsyncHttpClient`` + ``WatermarkManager``
  * reads non-secret source lists from ``aethelon.ingestion.config``
  * calls ``RSSDriver``, ``ForexFactoryDriver``, and ``FREDDriver``
  * does **not** run NLP, sentiment, regime, thesis, or storage writes

Drivers remain responsible for watermark filtering and advancement.
API keys are resolved only from the environment (or an explicit override).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import (
    Any,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from aethelon.core.logger import get_logger
from aethelon.ingestion.client import AsyncHttpClient
from aethelon.ingestion.config import (
    FRED_API_KEY_ENV,
    IngestionConfig,
    RssFeedSpec,
    default_ingestion_config,
)
from aethelon.ingestion.drivers import (
    FREDDriver,
    ForexFactoryDriver,
    NormalizedItem,
    RSSDriver,
)
from aethelon.ingestion.watermark import WatermarkManager

__all__ = [
    "IngestionOrchestrator",
    "RssFeedSpec",
]

log = get_logger(__name__)

PathLike = Union[str, Path]

# Per-feed cap so one hung RSS host cannot consume the whole sequential pass.
# Healthy feeds usually return in 1–3s; on timeout the next feed still runs.
_RSS_FEED_TIMEOUT_S = 8.0

# Sentinel: distinguish "caller omitted" from "caller passed empty / False".
_UNSET: Any = object()


class IngestionOrchestrator:
    """
    Conservative multi-source ingestion coordinator (Stage B3.2 + B3.3).

    Responsibilities
    ----------------
    * Open / reuse an ``AsyncHttpClient``
    * Construct drivers with a shared ``WatermarkManager``
    * Load source lists / endpoints from ``IngestionConfig``
    * Invoke each configured source
    * Collect every accepted ``NormalizedItem`` into one list

    Non-responsibilities (intentionally out of scope)
    -------------------------------------------------
    * NLP / sentiment / regime / thesis
    * Analytical database writes
    * Changing driver filter or watermark semantics
    * Storing API keys in config files

    Parameters
    ----------
    http :
        Optional shared HTTP client. When omitted, a private client is
        created and closed by this orchestrator.
    watermarks :
        Optional watermark store. When omitted, the default AppData path
        is used (``%APPDATA%\\Aethelon\\state\\watermarks.json``).
    watermark_path :
        Optional path override used only when ``watermarks`` is not given.
    config :
        Optional ``IngestionConfig``. When omitted, module defaults from
        ``aethelon.ingestion.config`` are used.
    fred_api_key :
        Optional FRED key. When omitted, reads ``FRED_API_KEY`` from the
        environment. A missing key logs a WARNING and skips FRED only.
    """

    def __init__(
        self,
        http: Optional[AsyncHttpClient] = None,
        watermarks: Optional[WatermarkManager] = None,
        *,
        watermark_path: Optional[PathLike] = None,
        config: Optional[IngestionConfig] = None,
        fred_api_key: Optional[str] = None,
    ) -> None:
        self._owns_http = http is None
        self._http = http if http is not None else AsyncHttpClient()
        if watermarks is not None:
            self._watermarks = watermarks
        else:
            self._watermarks = WatermarkManager(path=watermark_path)
        self._config = config if config is not None else default_ingestion_config()
        # Capture constructor override; env is re-read at run time as fallback.
        self._fred_api_key_override = (
            fred_api_key.strip() if isinstance(fred_api_key, str) else fred_api_key
        )
        self._opened_by_us = False

    # ----- properties --------------------------------------------------------

    @property
    def http(self) -> AsyncHttpClient:
        """Shared HTTP transport used by all drivers."""
        return self._http

    @property
    def watermarks(self) -> WatermarkManager:
        """Shared high-water-mark store used by all drivers."""
        return self._watermarks

    @property
    def config(self) -> IngestionConfig:
        """Active non-secret ingestion configuration."""
        return self._config

    # ----- lifecycle ---------------------------------------------------------

    async def open(self) -> "IngestionOrchestrator":
        """Ensure the underlying HTTP client is open."""
        if not self._http.is_open:
            await self._http.open()
            self._opened_by_us = True
        return self

    async def aclose(self) -> None:
        """Close the HTTP client only when this orchestrator owns it."""
        if self._owns_http and self._http.is_open:
            await self._http.aclose()
        self._opened_by_us = False

    async def close(self) -> None:
        """Alias for :meth:`aclose`."""
        await self.aclose()

    async def __aenter__(self) -> "IngestionOrchestrator":
        return await self.open()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    # ----- public API --------------------------------------------------------

    async def run(
        self,
        *,
        rss_feeds: Any = _UNSET,
        run_forex_factory: Any = _UNSET,
        fred_series: Any = _UNSET,
        fred_api_key: Optional[str] = None,
        fred_recent_limit: Any = _UNSET,
        fail_soft: Any = _UNSET,
    ) -> list[NormalizedItem]:
        """
        Run configured drivers and return a single list of normalized items.

        When a parameter is omitted, the value comes from ``self.config``
        (module defaults unless a custom ``IngestionConfig`` was injected).

        Explicit overrides:

        * Pass a mapping / sequence for ``rss_feeds`` or ``fred_series`` to
          replace the config list for this call only.
        * Pass an **empty** mapping / sequence to skip that source family.
        * Pass ``run_forex_factory=False`` to skip the calendar fetch.

        Parameters
        ----------
        rss_feeds :
            Optional override. Omitted → ``config.rss_feeds``.
            Empty → skip RSS.
        run_forex_factory :
            Optional override. Omitted → ``config.run_forex_factory``.
        fred_series :
            Optional override. Omitted → ``config.fred_series``.
            Empty → skip FRED.
        fred_api_key :
            Per-call key override. Falls back to constructor value, then
            ``os.environ[FRED_API_KEY_ENV]``. Missing key → WARNING + skip FRED.
            Never read from config files.
        fred_recent_limit :
            Optional override. Omitted → ``config.fred_recent_limit``.
        fail_soft :
            Optional override. Omitted → ``config.fail_soft``.
            When True, a single source failure is logged and others continue.

        Returns
        -------
        list[NormalizedItem]
            Flat list of driver rows. Order is RSS feeds (config order),
            then Forex Factory, then FRED series (config order). No sorting
            or deduplication is applied (callers may do that later).
        """
        await self.open()
        cfg = self._config

        if rss_feeds is _UNSET:
            feeds = cfg.rss_feed_specs()
        else:
            feeds = self._normalize_rss_feeds(rss_feeds)

        if fred_series is _UNSET:
            series = cfg.fred_series_ids()
        else:
            series = self._normalize_fred_series(fred_series)

        do_ff: bool = (
            cfg.run_forex_factory if run_forex_factory is _UNSET else bool(run_forex_factory)
        )
        recent_limit: int = (
            int(cfg.fred_recent_limit)
            if fred_recent_limit is _UNSET
            else int(fred_recent_limit)
        )
        soft: bool = cfg.fail_soft if fail_soft is _UNSET else bool(fail_soft)

        collected: list[NormalizedItem] = []

        log.info(
            "IngestionOrchestrator start rss=%s ff=%s fred=%s fail_soft=%s",
            len(feeds),
            do_ff,
            len(series),
            soft,
        )

        if feeds:
            rss_items = await self._run_rss(feeds, fail_soft=soft)
            collected.extend(rss_items)

        if do_ff:
            ff_items = await self._run_forex_factory(fail_soft=soft)
            collected.extend(ff_items)

        if series:
            key = self._resolve_fred_api_key(fred_api_key)
            if not key:
                log.warning(
                    "%s is not set — skipping FRED section "
                    "(%s series requested). Set the environment variable "
                    "or pass fred_api_key to enable FRED fetches.",
                    FRED_API_KEY_ENV,
                    len(series),
                )
            else:
                fred_items = await self._run_fred(
                    series,
                    api_key=key,
                    recent_limit=recent_limit,
                    fail_soft=soft,
                )
                collected.extend(fred_items)

        log.info(
            "IngestionOrchestrator done total_items=%s",
            len(collected),
        )
        return collected

    # ----- internal source runners -------------------------------------------

    async def _run_rss(
        self,
        feeds: list[RssFeedSpec],
        *,
        fail_soft: bool,
    ) -> list[NormalizedItem]:
        """
        Fetch each configured RSS/Atom feed via ``RSSDriver``.

        Each feed is bounded by ``_RSS_FEED_TIMEOUT_S``. A timeout or
        fetch error is logged; when ``fail_soft`` is True the remaining
        feeds still run.
        """
        driver = RSSDriver(self._http, self._watermarks)
        out: list[NormalizedItem] = []
        for name, url in feeds:
            try:
                items = await asyncio.wait_for(
                    driver.fetch(url, source_name=name),
                    timeout=_RSS_FEED_TIMEOUT_S,
                )
                out.extend(items)
                log.debug(
                    "orchestrator RSS ok source=%s new=%s",
                    name,
                    len(items),
                )
            except TimeoutError:
                log.error(
                    "orchestrator RSS timeout source=%s url=%s timeout=%.0fs",
                    name,
                    url,
                    _RSS_FEED_TIMEOUT_S,
                )
                if not fail_soft:
                    raise
            except Exception as exc:
                log.error(
                    "orchestrator RSS failed source=%s url=%s err=%s",
                    name,
                    url,
                    exc,
                )
                if not fail_soft:
                    raise
        return out

    async def _run_forex_factory(self, *, fail_soft: bool) -> list[NormalizedItem]:
        """Fetch the weekly economic calendar via ``ForexFactoryDriver``."""
        driver = ForexFactoryDriver(
            self._http,
            self._watermarks,
            calendar_url=self._config.forex_factory_url,
        )
        try:
            items = await driver.fetch()
            log.debug("orchestrator ForexFactory ok new=%s", len(items))
            return items
        except Exception as exc:
            log.error("orchestrator ForexFactory failed err=%s", exc)
            if not fail_soft:
                raise
            return []

    async def _run_fred(
        self,
        series_ids: list[str],
        *,
        api_key: str,
        recent_limit: int,
        fail_soft: bool,
    ) -> list[NormalizedItem]:
        """
        Fetch each FRED series via ``FREDDriver``.

        Inserts a short pause *between* consecutive series requests
        (``IngestionConfig.fred_series_pacing_s``, default 0.75s) so bulk
        pulls stay well under typical FRED rate guidance (~120 req/min).
        The first series is fetched immediately; retries inside the HTTP
        client and per-series ``fail_soft`` handling are unchanged.
        ``FREDDriver`` itself is not modified.
        """
        driver = FREDDriver(
            self._http,
            self._watermarks,
            api_key=api_key,
            observations_url=self._config.fred_observations_url,
        )
        pacing_s = float(getattr(self._config, "fred_series_pacing_s", 0.75) or 0.0)
        out: list[NormalizedItem] = []
        for index, sid in enumerate(series_ids):
            if index > 0 and pacing_s > 0.0:
                log.debug(
                    "orchestrator FRED pacing sleep=%.2fs before series=%s",
                    pacing_s,
                    sid,
                )
                await asyncio.sleep(pacing_s)
            try:
                items = await driver.fetch(sid, recent_limit=recent_limit)
                out.extend(items)
                log.debug(
                    "orchestrator FRED ok series=%s new=%s",
                    sid,
                    len(items),
                )
            except Exception as exc:
                log.error(
                    "orchestrator FRED failed series=%s err=%s",
                    sid,
                    exc,
                )
                if not fail_soft:
                    raise
        return out

    # ----- config helpers ----------------------------------------------------

    def _resolve_fred_api_key(self, per_call: Optional[str]) -> str:
        """
        Resolve FRED key: per-call override → constructor → environment.

        Never embeds a hardcoded secret and never reads keys from config.
        """
        if isinstance(per_call, str) and per_call.strip():
            return per_call.strip()
        if isinstance(self._fred_api_key_override, str) and self._fred_api_key_override.strip():
            return self._fred_api_key_override.strip()
        return (os.environ.get(FRED_API_KEY_ENV) or "").strip()

    @staticmethod
    def _normalize_rss_feeds(
        feeds: Optional[
            Union[Mapping[str, str], Sequence[RssFeedSpec], Sequence[Mapping[str, str]]]
        ],
    ) -> list[RssFeedSpec]:
        """Normalize caller feed config into ``(name, url)`` pairs."""
        if not feeds:
            return []

        if isinstance(feeds, Mapping):
            result: list[RssFeedSpec] = []
            for name, url in feeds.items():
                n = str(name).strip()
                u = str(url).strip()
                if n and u:
                    result.append((n, u))
            return result

        result = []
        for entry in feeds:
            if isinstance(entry, Mapping):
                name = str(
                    entry.get("name")
                    or entry.get("source_name")
                    or entry.get("source")
                    or ""
                ).strip()
                url = str(
                    entry.get("url")
                    or entry.get("feed_url")
                    or entry.get("link")
                    or ""
                ).strip()
                if name and url:
                    result.append((name, url))
                continue
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                name = str(entry[0]).strip()
                url = str(entry[1]).strip()
                if name and url:
                    result.append((name, url))
                continue
            log.warning(
                "orchestrator skipping unrecognized rss feed entry type=%s",
                type(entry).__name__,
            )
        return result

    @staticmethod
    def _normalize_fred_series(
        series: Optional[Sequence[str]],
    ) -> list[str]:
        """Deduplicate FRED series ids while preserving order."""
        if not series:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for raw in series:
            sid = str(raw or "").strip().upper()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append(sid)
        return out
