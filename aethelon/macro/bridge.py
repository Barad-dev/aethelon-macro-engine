# -*- coding: utf-8 -*-
"""
aethelon.macro.bridge — Stage C6.1 read-only data bridge
========================================================
Build in-memory Stage C **inputs** from existing project data shapes.

This module does **not**:
  * run C2–C5 classifiers / detectors (that is C6.2)
  * parse headlines or isolate shocks
  * import ``news_engine``, GUI, or write to SQLite
  * fetch from the network

Accepted shapes (all optional, all in-memory)
---------------------------------------------
C2 — macro-state-like mapping (legacy analyzer / context payload)::

    {growth, inflation, policy, liquidity, risk,
     growth_score, inflation_score, as_of, regime}

    or a wrapper ``{"macro_state": {...}}``
    or Research Desk-style ``{"dials": {"growth": ...}, ...}``

C3/C4 — FRED observations::

    {series_id: [{"date": "YYYY-MM-DD", "value": ...}, ...]}
    or a flat list of NormalizedItem-like rows with series_id/date/value
    or a wrapper ``{"fred_series": {...}}`` / ``{"fred": {...}}``

Missing or unparseable fields become empty / None. Nothing is invented.
Timestamps on emitted ``SeriesChange.as_of`` are UTC ISO 8601 Z.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Union

from aethelon.core.logger import get_logger
from aethelon.macro.hard_invalidation import SeriesChange
from aethelon.macro.regime import RegimeInputs
from aethelon.macro.schemas import _to_utc_z

log = get_logger(__name__)

# Core series C3/C4 actually watch — others are still passed through in the
# FRED map (callers may want them) but SeriesChange lists prefer this set.
_CORE_SERIES: frozenset[str] = frozenset(
    {
        "FEDFUNDS",
        "DFEDTARU",
        "DFEDTARL",
        "IORB",
        "UNRATE",
        "U6RATE",
        "CPIAUCSL",
        "CPILFESL",
        "PCEPI",
        "PCEPILFE",
        "COREPCE",
        "PAYEMS",
        "GDP",
        "GDPC1",
    }
)


@dataclass(frozen=True)
class StageCInputs:
    """
    Bundled in-memory inputs for later Stage C wiring (C6.2+).

    Empty/partial fields are valid — detectors already treat incomplete
    data conservatively.
    """

    regime: RegimeInputs
    fred_series: dict[str, list[dict[str, Any]]]
    series_changes: tuple[SeriesChange, ...]


# =============================================================================
# Scalar helpers
# =============================================================================

def _as_str(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list, tuple)):
        return None
    s = str(value).strip()
    return s or None


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "" or value == ".":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unwrap_mapping(raw: Any, *keys: str) -> Any:
    """If ``raw`` is a wrapper dict, return the first present nested key."""
    if not isinstance(raw, Mapping):
        return raw
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return raw


# =============================================================================
# C2 — RegimeInputs
# =============================================================================

def regime_inputs_from_macro_state(state: Optional[Mapping[str, Any]]) -> RegimeInputs:
    """
    Build ``RegimeInputs`` from a macro-state-like dict.

    Accepts the legacy analyzer snapshot, a ``get_news_context`` wrapper
    with a ``macro_state`` key, or a Research Desk section that nests
    labels under ``dials``. Unknown / missing fields stay ``None``.
    """
    if not isinstance(state, Mapping) or not state:
        log.debug("bridge: no macro_state mapping — empty RegimeInputs")
        return RegimeInputs()

    inner = state
    nested = state.get("macro_state")
    if isinstance(nested, Mapping) and (
        "growth" in nested
        or "inflation" in nested
        or "regime" in nested
        or "dials" in nested
    ):
        inner = nested

    dials = inner.get("dials") if isinstance(inner.get("dials"), Mapping) else {}

    def _label(*names: str) -> Optional[str]:
        for name in names:
            raw = inner.get(name)
            if raw is None and dials:
                raw = dials.get(name)
            got = _as_str(raw)
            if got is not None:
                return got
        return None

    growth = _label("growth")
    inflation = _label("inflation")
    as_of_raw = inner.get("as_of") or inner.get("generated_at")
    as_of = _to_utc_z(as_of_raw) or _as_str(as_of_raw)

    inputs = RegimeInputs(
        growth=growth,
        inflation=inflation,
        policy=_label("policy"),
        liquidity=_label("liquidity"),
        risk=_label("risk"),
        growth_score=_as_float(inner.get("growth_score")),
        inflation_score=_as_float(inner.get("inflation_score")),
        as_of=as_of,
        legacy_regime=_label("regime"),
    )
    log.debug(
        "bridge: RegimeInputs growth=%s inflation=%s as_of=%s",
        inputs.growth,
        inputs.inflation,
        inputs.as_of,
    )
    return inputs


# =============================================================================
# C3/C4 — FRED map + SeriesChange
# =============================================================================

def _obs_row(raw: Any) -> Optional[dict[str, Any]]:
    """Normalize one observation to ``{date, value}`` (optional datetime)."""
    if not isinstance(raw, Mapping):
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            date_s = str(raw[0]).strip()
            val = _as_float(raw[1])
            if not date_s or val is None:
                return None
            date_key = date_s[:10]
            return {"date": date_key, "value": val}
        return None

    date_s = str(
        raw.get("date") or raw.get("datetime") or raw.get("as_of") or ""
    ).strip()
    if not date_s:
        return None
    date_key = date_s[:10] if ("T" in date_s or len(date_s) >= 10) else date_s
    val = _as_float(raw.get("value"))
    if val is None:
        val = _as_float(raw.get("v"))
    if val is None:
        return None
    row: dict[str, Any] = {"date": date_key, "value": val}
    dt_z = _to_utc_z(raw.get("datetime") or date_s)
    if dt_z:
        row["datetime"] = dt_z
    return row


def _dedup_sort_obs(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep last value per date; sort oldest → newest."""
    by_date: dict[str, dict[str, Any]] = {}
    for row in rows:
        d = str(row.get("date") or "")[:10]
        if d:
            by_date[d] = row
    return [by_date[k] for k in sorted(by_date.keys())]


def fred_map_from_observations(
    raw: Optional[Union[Mapping[str, Any], Sequence[Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """
    Build a C3/C4 FRED-like map ``{SERIES_ID: [{date, value}, ...]}``.

    Accepts:
      * already-grouped store maps
      * wrappers with ``fred_series`` / ``fred``
      * a flat sequence of NormalizedItem-like dicts
    """
    if raw is None:
        return {}

    # Wrapper from a live context payload
    if isinstance(raw, Mapping) and not _looks_like_series_map(raw):
        wrapped = _unwrap_mapping(raw, "fred_series", "fred", "observations")
        if wrapped is not raw:
            return fred_map_from_observations(wrapped)

    out: dict[str, list[dict[str, Any]]] = {}

    if isinstance(raw, Mapping):
        # Either {sid: [obs...]} or a single NormalizedItem
        if _is_single_item(raw):
            _absorb_item(out, raw)
        else:
            for key, val in raw.items():
                sid = str(key or "").strip().upper()
                if not sid or sid in ("FRED", "FRED_SERIES", "OBSERVATIONS"):
                    continue
                if isinstance(val, (list, tuple)):
                    rows = [r for r in (_obs_row(x) for x in val) if r is not None]
                    if rows:
                        out[sid] = _dedup_sort_obs(rows)
                elif isinstance(val, Mapping):
                    row = _obs_row(val)
                    if row is not None:
                        out.setdefault(sid, []).append(row)
                        out[sid] = _dedup_sort_obs(out[sid])
        log.debug("bridge: fred map from mapping — %s series", len(out))
        return out

    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, Mapping):
                _absorb_item(out, item)
        for sid in list(out):
            out[sid] = _dedup_sort_obs(out[sid])
        log.debug("bridge: fred map from item list — %s series", len(out))
        return out

    log.debug("bridge: unrecognized fred payload type=%s", type(raw))
    return {}


def _looks_like_series_map(raw: Mapping[str, Any]) -> bool:
    """True when keys look like FRED series ids with obs lists."""
    if not raw:
        return False
    # A context wrapper has these high-level keys
    context_keys = {"macro_state", "ff_analyzed", "rss_analyzed", "pressure_scores"}
    if context_keys.intersection(raw.keys()):
        return False
    sample_keys = list(raw.keys())[:8]
    obsish = 0
    for k in sample_keys:
        v = raw[k]
        if isinstance(v, (list, tuple)):
            obsish += 1
        elif isinstance(v, Mapping) and (
            "date" in v or "value" in v or "observations" in v
        ):
            obsish += 1
    return obsish >= max(1, len(sample_keys) // 2)


def _is_single_item(raw: Mapping[str, Any]) -> bool:
    return bool(raw.get("series_id") and (raw.get("date") or raw.get("value") is not None))


def _absorb_item(out: dict[str, list[dict[str, Any]]], item: Mapping[str, Any]) -> None:
    sid = str(item.get("series_id") or item.get("id") or "").strip().upper()
    if not sid:
        return
    nested = item.get("observations") or item.get("obs")
    if isinstance(nested, (list, tuple)):
        for x in nested:
            row = _obs_row(x)
            if row is not None:
                out.setdefault(sid, []).append(row)
        return
    row = _obs_row(item)
    if row is not None:
        out.setdefault(sid, []).append(row)


def series_changes_from_fred_map(
    fred_map: Optional[Mapping[str, Sequence[Mapping[str, Any]]]],
    *,
    core_only: bool = True,
) -> list[SeriesChange]:
    """
    Derive ``SeriesChange`` rows from a FRED-like observation map.

    A series with fewer than two parseable points is omitted (C3/C4 need
    a prior). ``core_only=True`` (default) keeps the C3/C4 watched set.
    """
    if not isinstance(fred_map, Mapping) or not fred_map:
        return []

    changes: list[SeriesChange] = []
    for key, obs in fred_map.items():
        sid = str(key or "").strip().upper()
        if not sid:
            continue
        if core_only and sid not in _CORE_SERIES:
            continue
        if not isinstance(obs, (list, tuple)):
            continue
        rows = _dedup_sort_obs([r for r in (_obs_row(x) for x in obs) if r is not None])
        if len(rows) < 2:
            log.debug("bridge: skip %s — need ≥2 points, got %s", sid, len(rows))
            continue
        latest_row, prior_row = rows[-1], rows[-2]
        latest = _as_float(latest_row.get("value"))
        prior = _as_float(prior_row.get("value"))
        if latest is None:
            continue
        history = tuple(float(r["value"]) for r in rows)
        history_dates = tuple(str(r["date"]) for r in rows)
        as_of = _to_utc_z(latest_row.get("datetime") or latest_row.get("date"))
        changes.append(
            SeriesChange(
                series_id=sid,
                latest=latest,
                prior=prior,
                as_of=as_of,
                history=history,
                history_dates=history_dates,
            )
        )

    changes.sort(key=lambda c: c.series_id)
    log.debug("bridge: built %s SeriesChange row(s)", len(changes))
    return changes


# =============================================================================
# Bundle
# =============================================================================

def build_stage_c_inputs(
    *,
    macro_state: Optional[Mapping[str, Any]] = None,
    fred_series: Optional[Union[Mapping[str, Any], Sequence[Any]]] = None,
    context: Optional[Mapping[str, Any]] = None,
) -> StageCInputs:
    """
    Assemble C2 + C3/C4 inputs from optional in-memory payloads.

    Parameters
    ----------
    macro_state:
        Analyzer-style dial dict (or wrapper with ``macro_state``).
    fred_series:
        Store map, NormalizedItem list, or wrapper with ``fred_series``.
    context:
        Optional live-context-like dict. Used only when the dedicated
        arguments are omitted. Never fetched; never written.

    Returns
    -------
    StageCInputs
        Always a concrete object. Empty pieces stay empty.
    """
    state_src: Optional[Mapping[str, Any]] = None
    if isinstance(macro_state, Mapping):
        state_src = macro_state
    elif isinstance(context, Mapping):
        maybe = context.get("macro_state")
        state_src = maybe if isinstance(maybe, Mapping) else context

    fred_src: Optional[Union[Mapping[str, Any], Sequence[Any]]] = fred_series
    if fred_src is None and isinstance(context, Mapping):
        if "fred_series" in context or "fred" in context:
            fred_src = context

    regime = regime_inputs_from_macro_state(state_src)
    fred_map = fred_map_from_observations(fred_src)
    changes = tuple(series_changes_from_fred_map(fred_map))

    log.info(
        "bridge: StageCInputs regime_growth=%s regime_inflation=%s "
        "fred_series=%s changes=%s",
        regime.growth,
        regime.inflation,
        len(fred_map),
        len(changes),
    )
    return StageCInputs(
        regime=regime,
        fred_series=fred_map,
        series_changes=changes,
    )
