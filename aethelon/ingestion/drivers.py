# -*- coding: utf-8 -*-
"""
aethelon.ingestion.drivers — Source-specific async data drivers (Stage B2)
==========================================================================
Each driver:

  1. Reads the per-source watermark
  2. Fetches via ``AsyncHttpClient``
  3. Filters items strictly newer than the watermark
  4. Advances the watermark to the newest accepted item timestamp

Drivers never write the analytical DB; they return normalized dict rows
for the analytical core / store layer to consume.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional, Sequence

from aethelon.core.logger import get_logger
from aethelon.ingestion.client import AsyncHttpClient
from aethelon.ingestion.exceptions import IngestionNetworkError
from aethelon.ingestion.watermark import (
    WatermarkManager,
    to_iso_z,
    to_utc,
)

__all__ = [
    "BaseDriver",
    "RSSDriver",
    "ForexFactoryDriver",
    "FREDDriver",
    "NormalizedItem",
]

log = get_logger(__name__)

# Type alias for driver output rows (JSON-serializable plain dicts)
NormalizedItem = dict[str, Any]

FF_WEEKLY_URL = "https://nodedata.forexfactory.com/calendar/weekly.json"
FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"

# RSS namespace map (Atom + media extensions commonly appear in finance feeds)
_RSS_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


# =============================================================================
# Base driver
# =============================================================================

class BaseDriver(ABC):
    """
    Abstract ingestion driver.

    Subclasses implement :meth:`fetch` to pull remote data, filter against
    the watermark, and return normalized items. Watermark updates are
    performed by the base helper :meth:`_commit_watermark` after a
    successful parse of *new* items.
    """

    #: Stable source key prefix used in watermark file (override in subclass)
    source_prefix: str = "driver"

    def __init__(
        self,
        http: AsyncHttpClient,
        watermarks: WatermarkManager,
    ) -> None:
        self.http = http
        self.watermarks = watermarks

    @abstractmethod
    async def fetch(self, *args: Any, **kwargs: Any) -> list[NormalizedItem]:
        """Fetch, filter by watermark, update watermark, return new items."""

    def source_id(self, *parts: str) -> str:
        """Build a deterministic watermark key: ``prefix:part:part``."""
        clean = [self.source_prefix] + [p.strip() for p in parts if p and str(p).strip()]
        return ":".join(clean)

    def _watermark_cutoff(self, source_id: str) -> Optional[datetime]:
        return self.watermarks.get_watermark(source_id)

    def _is_newer(self, item_ts: datetime, cutoff: Optional[datetime]) -> bool:
        if cutoff is None:
            return True
        return to_utc(item_ts) > to_utc(cutoff)

    def _commit_watermark(
        self,
        source_id: str,
        item_timestamps: Sequence[datetime],
    ) -> Optional[datetime]:
        """
        Advance watermark to max(item_timestamps) if any exist.

        Returns the committed timestamp, or ``None`` if nothing to advance.
        """
        if not item_timestamps:
            return None
        newest = max(to_utc(t) for t in item_timestamps)
        self.watermarks.update_watermark(source_id, newest)
        return newest


# =============================================================================
# RSS
# =============================================================================

class RSSDriver(BaseDriver):
    """
    Fetch and parse RSS 2.0 / Atom feeds.

    Only entries with a *parseable* ``pubDate`` / ``updated`` / ``published``
    strictly after the stored watermark are returned.

    Items lacking a publication timestamp are **dropped** (never assigned
    ``datetime.now()``). Using wall-clock time would advance the watermark
    into the future and permanently suppress older valid items on later runs.

    The watermark advances only to the newest *accepted* item timestamp.
    """

    source_prefix = "rss"

    async def fetch(
        self,
        feed_url: str,
        *,
        source_name: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> list[NormalizedItem]:
        """
        Pull one RSS/Atom feed and return items newer than the watermark.

        Parameters
        ----------
        feed_url :
            Absolute feed URL.
        source_name :
            Human label (e.g. ``\"Yahoo Finance\"``); used in output rows.
        source_id :
            Optional explicit watermark key. Default: ``rss:<source_name|url>``.
        """
        label = (source_name or "").strip() or feed_url
        sid = source_id or self.source_id(label)
        cutoff = self._watermark_cutoff(sid)

        log.info(
            "RSSDriver fetch source=%s cutoff=%s url=%s",
            sid,
            to_iso_z(cutoff) if cutoff else "(none)",
            feed_url,
        )

        response = await self.http.get(feed_url)
        if response.status_code >= 400:
            raise IngestionNetworkError(
                f"RSS fetch HTTP {response.status_code} for {feed_url}",
                url=feed_url,
                method="GET",
                status_code=response.status_code,
            )

        text = response.text
        try:
            raw_items = self._parse_feed_xml(text, feed_url=feed_url, source_name=label)
        except ET.ParseError as exc:
            log.error("RSS XML parse failed source=%s err=%s", sid, exc)
            raise IngestionNetworkError(
                f"RSS XML parse error for {feed_url}: {exc}",
                url=feed_url,
                method="GET",
                cause=exc,
            ) from exc

        accepted: list[NormalizedItem] = []
        accepted_ts: list[datetime] = []
        skipped_undated = 0
        for item in raw_items:
            ts = item.get("_ts")
            if not isinstance(ts, datetime):
                skipped_undated += 1
                continue
            if not self._is_newer(ts, cutoff):
                continue
            row = {k: v for k, v in item.items() if not str(k).startswith("_")}
            row["datetime"] = to_iso_z(ts)
            row["source_id"] = sid
            accepted.append(row)
            accepted_ts.append(ts)

        committed = self._commit_watermark(sid, accepted_ts)
        log.info(
            "RSSDriver done source=%s parsed=%s new=%s undated_skipped=%s watermark=%s",
            sid,
            len(raw_items),
            len(accepted),
            skipped_undated,
            to_iso_z(committed) if committed else "(unchanged)",
        )
        return accepted

    def _parse_feed_xml(
        self,
        xml_text: str,
        *,
        feed_url: str,
        source_name: str,
    ) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text)
        parsed: list[Optional[dict[str, Any]]] = []

        channel = root.find("channel")
        if channel is not None:
            parsed = [
                self._rss_item(el, source_name=source_name, feed_url=feed_url)
                for el in channel.findall("item")
            ]
            return [p for p in parsed if p is not None]

        if root.tag.endswith("rss") or root.tag == "rss":
            ch = root.find("channel") or root.find(
                "{http://purl.org/rss/1.0/}channel"
            )
            if ch is not None:
                items = ch.findall("item") or ch.findall(
                    "{http://purl.org/rss/1.0/}item"
                )
                parsed = [
                    self._rss_item(el, source_name=source_name, feed_url=feed_url)
                    for el in items
                ]
                return [p for p in parsed if p is not None]

        if root.tag.endswith("feed") or root.tag == "{http://www.w3.org/2005/Atom}feed":
            entries = root.findall("atom:entry", _RSS_NS) or root.findall(
                "{http://www.w3.org/2005/Atom}entry"
            )
            if not entries:
                entries = [e for e in root if e.tag.endswith("entry")]
            parsed = [
                self._atom_entry(el, source_name=source_name, feed_url=feed_url)
                for el in entries
            ]
            return [p for p in parsed if p is not None]

        items = root.findall(".//item")
        if items:
            parsed = [
                self._rss_item(el, source_name=source_name, feed_url=feed_url)
                for el in items
            ]
            return [p for p in parsed if p is not None]

        log.warning("unrecognized feed structure url=%s root=%s", feed_url, root.tag)
        return []

    def _rss_item(
        self,
        el: ET.Element,
        *,
        source_name: str,
        feed_url: str,
    ) -> Optional[dict[str, Any]]:
        """
        Normalize one RSS ``<item>``.

        Returns ``None`` when no parseable publication date exists so the
        caller never advances watermarks from synthetic timestamps.
        """
        title = _text(el, "title") or ""
        link = _text(el, "link") or ""
        summary = _text(el, "description") or _text(el, "content:encoded", _RSS_NS) or ""
        guid = _text(el, "guid") or link or title
        pub = (
            _text(el, "pubDate")
            or _text(el, "dc:date", _RSS_NS)
            or _text(el, "date")
        )
        ts = _parse_feed_datetime(pub)
        if ts is None:
            log.debug(
                "RSS item skipped (no pubDate) source=%s title=%s",
                source_name,
                (title or "")[:80],
            )
            return None
        return {
            "source": source_name,
            "title": title.strip(),
            "summary": _strip_html(summary)[:2000],
            "link": link.strip(),
            "guid": guid.strip(),
            "feed_url": feed_url,
            "kind": "rss",
            "_ts": to_utc(ts),
        }

    def _atom_entry(
        self,
        el: ET.Element,
        *,
        source_name: str,
        feed_url: str,
    ) -> Optional[dict[str, Any]]:
        """
        Normalize one Atom ``<entry>``.

        Returns ``None`` when ``updated`` / ``published`` cannot be parsed.
        """
        title = _text(el, "title") or _text(el, "atom:title", _RSS_NS) or ""
        summary = (
            _text(el, "summary")
            or _text(el, "atom:summary", _RSS_NS)
            or _text(el, "content")
            or _text(el, "atom:content", _RSS_NS)
            or ""
        )
        link = ""
        for link_el in list(el):
            if link_el.tag.endswith("link"):
                href = link_el.attrib.get("href") or ""
                rel = link_el.attrib.get("rel", "alternate")
                if href and rel in ("alternate", ""):
                    link = href
                    break
        entry_id = (
            _text(el, "id")
            or _text(el, "atom:id", _RSS_NS)
            or link
            or title
        )
        pub = (
            _text(el, "updated")
            or _text(el, "atom:updated", _RSS_NS)
            or _text(el, "published")
            or _text(el, "atom:published", _RSS_NS)
        )
        ts = _parse_feed_datetime(pub)
        if ts is None:
            log.debug(
                "Atom entry skipped (no updated/published) source=%s title=%s",
                source_name,
                (title or "")[:80],
            )
            return None
        return {
            "source": source_name,
            "title": title.strip(),
            "summary": _strip_html(summary)[:2000],
            "link": link.strip(),
            "guid": entry_id.strip(),
            "feed_url": feed_url,
            "kind": "atom",
            "_ts": to_utc(ts),
        }


# =============================================================================
# Forex Factory
# =============================================================================

class ForexFactoryDriver(BaseDriver):
    """
    Fetch weekly economic calendar events from NodeData / Forex Factory JSON.

    Default endpoint::

        https://nodedata.forexfactory.com/calendar/weekly.json

    Events with timestamps ≤ watermark are filtered out. Watermark advances
    to the newest *accepted* event datetime.
    """

    source_prefix = "ff"

    def __init__(
        self,
        http: AsyncHttpClient,
        watermarks: WatermarkManager,
        *,
        calendar_url: str = FF_WEEKLY_URL,
        source_key: str = "weekly",
    ) -> None:
        super().__init__(http, watermarks)
        self.calendar_url = calendar_url
        self.source_key = source_key

    async def fetch(
        self,
        *,
        url: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> list[NormalizedItem]:
        """
        Download calendar JSON and return events newer than the watermark.
        """
        endpoint = url or self.calendar_url
        sid = source_id or self.source_id(self.source_key)
        cutoff = self._watermark_cutoff(sid)

        log.info(
            "ForexFactoryDriver fetch source=%s cutoff=%s url=%s",
            sid,
            to_iso_z(cutoff) if cutoff else "(none)",
            endpoint,
        )

        response = await self.http.get(endpoint)
        if response.status_code >= 400:
            raise IngestionNetworkError(
                f"ForexFactory HTTP {response.status_code} for {endpoint}",
                url=endpoint,
                method="GET",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise IngestionNetworkError(
                f"ForexFactory JSON decode failed for {endpoint}: {exc}",
                url=endpoint,
                method="GET",
                cause=exc,
            ) from exc

        if not isinstance(payload, list):
            log.warning(
                "ForexFactory unexpected payload type=%s",
                type(payload).__name__,
            )
            payload = []

        accepted: list[NormalizedItem] = []
        accepted_ts: list[datetime] = []

        for raw in payload:
            if not isinstance(raw, dict):
                continue
            ts = _ff_event_datetime(raw)
            if ts is None:
                continue
            if not self._is_newer(ts, cutoff):
                continue
            item = _normalize_ff_event(raw, ts=ts, source_id=sid)
            accepted.append(item)
            accepted_ts.append(ts)

        committed = self._commit_watermark(sid, accepted_ts)
        log.info(
            "ForexFactoryDriver done source=%s fetched=%s new=%s watermark=%s",
            sid,
            len(payload),
            len(accepted),
            to_iso_z(committed) if committed else "(unchanged)",
        )
        return accepted


# =============================================================================
# FRED
# =============================================================================

class FREDDriver(BaseDriver):
    """
    Fetch FRED series observations via the St. Louis Fed API.

    When a watermark exists for ``fred:<series_id>``, ``observation_start``
    is set to that date (UTC calendar day). Otherwise the driver requests a
    limited recent window (``recent_limit`` most-recent points via
    ``limit`` + ``sort_order=desc``, then re-ordered ascending).

    Parameters
    ----------
    api_key :
        FRED API key (required for live queries).
    """

    source_prefix = "fred"

    def __init__(
        self,
        http: AsyncHttpClient,
        watermarks: WatermarkManager,
        *,
        api_key: str,
        observations_url: str = FRED_OBS_URL,
    ) -> None:
        super().__init__(http, watermarks)
        key = (api_key or "").strip()
        if not key:
            raise ValueError("FREDDriver requires a non-empty api_key")
        self.api_key = key
        self.observations_url = observations_url

    async def fetch(
        self,
        series_id: str,
        *,
        recent_limit: int = 20,
        source_id: Optional[str] = None,
    ) -> list[NormalizedItem]:
        """
        Query observations for one FRED series, filter by watermark, update it.

        Parameters
        ----------
        series_id :
            FRED series id (e.g. ``CPIAUCSL``, ``FEDFUNDS``).
        recent_limit :
            Max observations when no watermark exists yet.
        source_id :
            Optional watermark key override (default ``fred:<series_id>``).
        """
        sid_series = (series_id or "").strip().upper()
        if not sid_series:
            raise ValueError("series_id is required")

        sid = source_id or self.source_id(sid_series)
        cutoff = self._watermark_cutoff(sid)

        params: dict[str, str] = {
            "series_id": sid_series,
            "api_key": self.api_key,
            "file_type": "json",
        }

        if cutoff is not None:
            # Re-fetch from watermark day to pick up revisions; filter strictly >
            params["observation_start"] = to_utc(cutoff).date().isoformat()
            params["sort_order"] = "asc"
        else:
            params["sort_order"] = "desc"
            params["limit"] = str(max(1, int(recent_limit)))

        log.info(
            "FREDDriver fetch series=%s cutoff=%s observation_start=%s",
            sid_series,
            to_iso_z(cutoff) if cutoff else "(none)",
            params.get("observation_start", "(limit-mode)"),
        )

        response = await self.http.get(self.observations_url, params=params)
        if response.status_code >= 400:
            raise IngestionNetworkError(
                f"FRED HTTP {response.status_code} for series {sid_series}",
                url=str(response.url),
                method="GET",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise IngestionNetworkError(
                f"FRED JSON decode failed for {sid_series}: {exc}",
                url=self.observations_url,
                method="GET",
                cause=exc,
            ) from exc

        observations = payload.get("observations") if isinstance(payload, dict) else None
        if not isinstance(observations, list):
            log.warning("FRED empty/invalid observations series=%s", sid_series)
            observations = []

        # Normalize order ascending by date
        parsed_rows: list[tuple[datetime, dict[str, Any]]] = []
        for obs in observations:
            if not isinstance(obs, dict):
                continue
            date_s = (obs.get("date") or "").strip()
            value_s = obs.get("value")
            if not date_s or value_s in (None, "."):
                continue
            try:
                # FRED dates are calendar dates — store as UTC midnight
                day = datetime.strptime(date_s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            try:
                value = float(value_s)
            except (TypeError, ValueError):
                continue
            parsed_rows.append(
                (
                    day,
                    {
                        "series_id": sid_series,
                        "date": date_s,
                        "value": value,
                        "realtime_start": obs.get("realtime_start"),
                        "realtime_end": obs.get("realtime_end"),
                        "kind": "fred",
                        "source_id": sid,
                        "datetime": to_iso_z(day),
                    },
                )
            )

        parsed_rows.sort(key=lambda x: x[0])

        accepted: list[NormalizedItem] = []
        accepted_ts: list[datetime] = []
        for day, row in parsed_rows:
            if not self._is_newer(day, cutoff):
                continue
            accepted.append(row)
            accepted_ts.append(day)

        committed = self._commit_watermark(sid, accepted_ts)
        log.info(
            "FREDDriver done series=%s fetched=%s new=%s watermark=%s",
            sid_series,
            len(parsed_rows),
            len(accepted),
            to_iso_z(committed) if committed else "(unchanged)",
        )
        return accepted


# =============================================================================
# Shared parse helpers
# =============================================================================

def _text(
    el: ET.Element,
    path: str,
    ns: Optional[dict[str, str]] = None,
) -> Optional[str]:
    node = el.find(path, ns) if ns else el.find(path)
    if node is None:
        # bare local-name search
        local = path.split(":")[-1]
        for child in el:
            if child.tag == local or child.tag.endswith("}" + local):
                node = child
                break
    if node is None or node.text is None:
        return None
    return node.text.strip()


def _parse_feed_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    s = value.strip()
    # RFC 2822 (RSS pubDate)
    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return to_utc(dt)
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    # ISO 8601
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return to_utc(dt)
    except ValueError:
        return None


def _strip_html(text: str) -> str:
    if not text:
        return ""
    # Lightweight strip — avoid heavy HTML deps in the ingestion plane
    out: list[str] = []
    in_tag = False
    for ch in text:
        if ch == "<":
            in_tag = True
            continue
        if ch == ">":
            in_tag = False
            continue
        if not in_tag:
            out.append(ch)
    return "".join(out).strip()


def _ff_event_datetime(raw: dict[str, Any]) -> Optional[datetime]:
    """
    Resolve Forex Factory / NodeData event timestamp.

    Common fields: ``date``, ``datetime``, ``time``, combined date+time.
    """
    for key in ("datetime", "dateTime", "timestamp", "date_utc"):
        val = raw.get(key)
        if val:
            ts = _parse_feed_datetime(str(val))
            if ts:
                return ts
            # epoch?
            try:
                num = float(val)
                if num > 1e12:
                    num /= 1000.0
                return datetime.fromtimestamp(num, tz=timezone.utc)
            except (TypeError, ValueError, OSError, OverflowError):
                pass

    date_s = raw.get("date") or raw.get("day")
    time_s = raw.get("time") or raw.get("event_time") or ""
    if date_s:
        combo = f"{date_s} {time_s}".strip() if time_s and str(time_s).lower() not in (
            "all day",
            "tentative",
            "",
        ) else str(date_s)
        ts = _parse_feed_datetime(combo)
        if ts:
            return ts
        # date only YYYY-MM-DD
        try:
            return datetime.strptime(str(date_s)[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    return None


def _normalize_ff_event(
    raw: dict[str, Any],
    *,
    ts: datetime,
    source_id: str,
) -> NormalizedItem:
    title = (
        raw.get("title")
        or raw.get("name")
        or raw.get("event")
        or ""
    )
    impact_raw = raw.get("impact")
    impact = 1
    if isinstance(impact_raw, (int, float)):
        impact = int(impact_raw)
    elif isinstance(impact_raw, str):
        mapping = {
            "low": 1,
            "medium": 2,
            "med": 2,
            "high": 3,
            "holiday": 1,
            "red": 3,
            "orange": 2,
            "yellow": 1,
        }
        impact = mapping.get(impact_raw.strip().lower(), 1)

    return {
        "source": "ForexFactory",
        "source_id": source_id,
        "kind": "ff_calendar",
        "title": str(title).strip(),
        "currency": raw.get("country") or raw.get("currency") or raw.get("ccy"),
        "impact": impact,
        "forecast": raw.get("forecast"),
        "previous": raw.get("previous"),
        "actual": raw.get("actual"),
        "datetime": to_iso_z(ts),
        "raw": {
            k: raw.get(k)
            for k in (
                "title",
                "name",
                "event",
                "country",
                "currency",
                "impact",
                "forecast",
                "previous",
                "actual",
                "date",
                "time",
            )
            if k in raw
        },
    }
