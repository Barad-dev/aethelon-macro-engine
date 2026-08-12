# -*- coding: utf-8 -*-
"""
aethelon.macro.hard_invalidation — Stage C3 hard-invalidation detector
=====================================================================
Pure, offline, rule-based detection of **structural** breaks in core
macro / FRED-style series. Returns ``HardInvalidationSignal`` objects.

Design
------
  * Precision over recall: borderline moves are ignored on purpose.
  * No network, database, GUI, or ``news_engine`` imports.
  * Does not change Stage C2 regime classification.
  * Incomplete history → no signal (never invent a break).
  * Timestamps are UTC ISO 8601 Z via the C1 schema validators.

Supported input shapes
----------------------
  1. Explicit change summaries (``SeriesChange`` / plain dicts)
  2. In-memory FRED-like maps ``{series_id: [obs, ...]}`` where each obs
     is a dict with ``date`` / ``value`` (and optional ``datetime``)

Core series watched (when present)
----------------------------------
  * FEDFUNDS — policy rate level (percentage points)
  * UNRATE   — unemployment rate (percentage points)
  * CPIAUCSL / PCEPI / PCEPILFE — price *index* levels (MoM / YoY %)
  * PAYEMS   — nonfarm payrolls (thousands, MoM change)
  * GDP      — GDP level (QoQ % when two points exist)

Rules are documented next to their thresholds below. Optional dials such
as VIX are intentionally out of scope here (closer to shock / risk).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from aethelon.core.logger import get_logger
from aethelon.macro.schemas import HardInvalidationSignal, MacroRegime, _to_utc_z

log = get_logger(__name__)

# =============================================================================
# Thresholds (deliberately high — precision over recall)
# =============================================================================

# Policy rate (FEDFUNDS and close cousins): consecutive Δ in percentage points
FEDFUNDS_ABS_MOVE_PP = 0.75
# Stronger single-print emergency-style move
FEDFUNDS_EMERGENCY_PP = 1.00

# Unemployment (UNRATE): consecutive monthly rise in pp
UNRATE_STEP_RISE_PP = 0.40
# Rise vs recent local low over a short window (Sahm-inspired, simpler)
UNRATE_FROM_LOW_PP = 0.50
UNRATE_LOOKBACK = 12  # max prior points to scan for local low

# CPI / PCE *index*: MoM % and YoY pp shift
CPI_MOM_PCT_HARD = 1.20          # single-month % change on the index
CPI_YOY_PP_SHIFT = 2.00          # |latest YoY% − prior YoY%| in percentage points
CPI_YOY_MIN_POINTS = 13          # need ~12 lags for a YoY pair comparison

# Payrolls (PAYEMS): MoM change in thousands
PAYEMS_CRASH_THOUSANDS = -400.0

# GDP level: QoQ % decline
GDP_QOQ_PCT_HARD = -2.0

# Series id families (uppercase)
_POLICY_RATE_IDS = frozenset({"FEDFUNDS", "DFEDTARU", "DFEDTARL", "IORB"})
_UNEMPLOYMENT_IDS = frozenset({"UNRATE", "U6RATE"})
_PRICE_INDEX_IDS = frozenset(
    {"CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE", "COREPCE"}
)
_PAYROLL_IDS = frozenset({"PAYEMS"})
_GDP_IDS = frozenset({"GDP", "GDPC1"})


# =============================================================================
# Input containers
# =============================================================================

@dataclass(frozen=True)
class SeriesChange:
    """
    Explicit structured change for one series (no observation list needed).

    Prefer this when a caller already computed prior/latest values.
    Absolute levels are preferred; ``delta`` is optional and recomputed
    when both levels exist.
    """

    series_id: str
    latest: float
    prior: Optional[float] = None
    as_of: Optional[Union[str, datetime]] = None
    # Optional extra history, newest-last or newest-first — detector sorts.
    history: tuple[float, ...] = ()
    history_dates: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Point:
    """Internal sorted observation (oldest → newest)."""

    date: str
    value: float


# =============================================================================
# Parsing helpers
# =============================================================================

def _norm_series_id(value: Any) -> str:
    return str(value or "").strip().upper()


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "" or value == ".":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _now_utc_z() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_obs_list(raw: Any) -> list[_Point]:
    """
    Parse a FRED-like observation list into ascending ``_Point`` rows.

    Accepts dict rows with ``date`` + ``value``, or bare ``(date, value)``
    pairs. Invalid rows are skipped — never invented.
    """
    if not raw:
        return []
    if not isinstance(raw, (list, tuple)):
        return []

    points: list[_Point] = []
    for item in raw:
        date_s: Optional[str] = None
        val: Optional[float] = None
        if isinstance(item, Mapping):
            date_s = str(
                item.get("date")
                or item.get("datetime")
                or item.get("as_of")
                or ""
            ).strip()
            val = _as_float(item.get("value"))
            if val is None:
                val = _as_float(item.get("v"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            date_s = str(item[0]).strip()
            val = _as_float(item[1])
        else:
            continue
        if not date_s or val is None:
            continue
        # Normalize date-only to a stable sort key; keep original string
        if "T" in date_s:
            date_key = date_s[:10]
        else:
            date_key = date_s[:10] if len(date_s) >= 10 else date_s
        points.append(_Point(date=date_key, value=val))

    points.sort(key=lambda p: p.date)
    # Drop exact duplicate dates keeping the last value (revision-friendly)
    dedup: dict[str, _Point] = {}
    for p in points:
        dedup[p.date] = p
    return [dedup[k] for k in sorted(dedup.keys())]


def _pct_change(new: float, old: float) -> Optional[float]:
    if old == 0:
        return None
    return (new - old) / abs(old) * 100.0


def _yoy_pct(points: Sequence[_Point], index: int) -> Optional[float]:
    """YoY % at ``index`` if a point ~12 steps earlier exists."""
    if index < 12 or index >= len(points):
        return None
    return _pct_change(points[index].value, points[index - 12].value)


def _regime_or_none(value: Any) -> Optional[MacroRegime]:
    if value is None or value == "":
        return None
    if isinstance(value, MacroRegime):
        return value
    s = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    try:
        return MacroRegime(s)
    except ValueError:
        return None


def _signal(
    *,
    signal_id: str,
    title: str,
    summary: str,
    severity: float,
    indicator_ids: list[str],
    as_of: Optional[str],
    detected_at: str,
    reasoning_chain: list[str],
    related_regime: Optional[MacroRegime],
    details: Optional[dict[str, Any]] = None,
) -> HardInvalidationSignal:
    return HardInvalidationSignal(
        signal_id=signal_id,
        title=title,
        summary=summary,
        severity=max(0.0, min(1.0, float(severity))),
        active=True,
        indicator_ids=indicator_ids,
        related_regime=related_regime,
        detected_at=detected_at,
        as_of=as_of,
        reasoning_chain=reasoning_chain,
        market_narrative=None,
        details=details,
    )


# =============================================================================
# Per-family rule evaluators
# =============================================================================

def _eval_policy_rate(
    sid: str,
    points: Sequence[_Point],
    *,
    related_regime: Optional[MacroRegime],
    detected_at: str,
) -> Optional[HardInvalidationSignal]:
    if len(points) < 2:
        return None
    prior, latest = points[-2], points[-1]
    delta = latest.value - prior.value
    abs_delta = abs(delta)

    if abs_delta < FEDFUNDS_ABS_MOVE_PP:
        return None

    if abs_delta >= FEDFUNDS_EMERGENCY_PP:
        severity = 0.95
        grade = "emergency-scale"
    else:
        severity = 0.85
        grade = "large"

    direction = "hike" if delta > 0 else "cut"
    chain = [
        f"series={sid} policy-rate family",
        f"prior={prior.value:.3f} on {prior.date}",
        f"latest={latest.value:.3f} on {latest.date}",
        f"delta={delta:+.3f} pp (threshold ±{FEDFUNDS_ABS_MOVE_PP:.2f} pp)",
        f"classified as {grade} {direction}",
    ]
    return _signal(
        signal_id=f"hard:{sid}:rate_move:{latest.date}",
        title=f"{sid} {grade} policy-rate {direction}",
        summary=(
            f"{sid} moved {delta:+.2f} pp from {prior.value:.2f} to "
            f"{latest.value:.2f} ({prior.date} → {latest.date}). "
            f"Structural policy shift threshold is ±{FEDFUNDS_ABS_MOVE_PP:.2f} pp."
        ),
        severity=severity,
        indicator_ids=[sid],
        as_of=_to_utc_z(latest.date),
        detected_at=detected_at,
        reasoning_chain=chain,
        related_regime=related_regime,
        details={
            "prior": prior.value,
            "latest": latest.value,
            "delta_pp": round(delta, 4),
            "threshold_pp": FEDFUNDS_ABS_MOVE_PP,
        },
    )


def _eval_unemployment(
    sid: str,
    points: Sequence[_Point],
    *,
    related_regime: Optional[MacroRegime],
    detected_at: str,
) -> Optional[HardInvalidationSignal]:
    if len(points) < 2:
        return None

    prior, latest = points[-2], points[-1]
    step = latest.value - prior.value
    chain_base = [
        f"series={sid} unemployment family",
        f"prior={prior.value:.3f} on {prior.date}",
        f"latest={latest.value:.3f} on {latest.date}",
        f"step_delta={step:+.3f} pp",
    ]

    # Rule A: single-print jump (rare; high precision)
    if step >= UNRATE_STEP_RISE_PP:
        chain = chain_base + [
            f"single-print rise ≥ {UNRATE_STEP_RISE_PP:.2f} pp → hard invalidation"
        ]
        return _signal(
            signal_id=f"hard:{sid}:step_rise:{latest.date}",
            title=f"{sid} single-print unemployment spike",
            summary=(
                f"{sid} rose {step:+.2f} pp in one step "
                f"({prior.value:.2f} → {latest.value:.2f}). "
                f"Hard threshold is +{UNRATE_STEP_RISE_PP:.2f} pp."
            ),
            severity=0.90,
            indicator_ids=[sid],
            as_of=_to_utc_z(latest.date),
            detected_at=detected_at,
            reasoning_chain=chain,
            related_regime=related_regime,
            details={
                "prior": prior.value,
                "latest": latest.value,
                "step_pp": round(step, 4),
                "threshold_pp": UNRATE_STEP_RISE_PP,
                "rule": "step_rise",
            },
        )

    # Rule B: rise from recent local low (needs more history)
    window = points[-(UNRATE_LOOKBACK + 1) :]
    if len(window) < 4:
        return None
    # Local low among points before the latest
    hist = window[:-1]
    low_pt = min(hist, key=lambda p: p.value)
    from_low = latest.value - low_pt.value
    chain_base.append(
        f"recent_low={low_pt.value:.3f} on {low_pt.date}; "
        f"from_low={from_low:+.3f} pp"
    )
    if from_low < UNRATE_FROM_LOW_PP:
        return None

    chain = chain_base + [
        f"rise from recent low ≥ {UNRATE_FROM_LOW_PP:.2f} pp → hard invalidation"
    ]
    return _signal(
        signal_id=f"hard:{sid}:from_low:{latest.date}",
        title=f"{sid} labor-market deterioration from recent low",
        summary=(
            f"{sid} is {from_low:+.2f} pp above its recent low "
            f"({low_pt.value:.2f} on {low_pt.date} → {latest.value:.2f} on "
            f"{latest.date}). Hard threshold is +{UNRATE_FROM_LOW_PP:.2f} pp."
        ),
        severity=0.88,
        indicator_ids=[sid],
        as_of=_to_utc_z(latest.date),
        detected_at=detected_at,
        reasoning_chain=chain,
        related_regime=related_regime,
        details={
            "latest": latest.value,
            "recent_low": low_pt.value,
            "from_low_pp": round(from_low, 4),
            "threshold_pp": UNRATE_FROM_LOW_PP,
            "rule": "from_low",
        },
    )


def _eval_price_index(
    sid: str,
    points: Sequence[_Point],
    *,
    related_regime: Optional[MacroRegime],
    detected_at: str,
) -> Optional[HardInvalidationSignal]:
    if len(points) < 2:
        return None

    prior, latest = points[-2], points[-1]
    mom = _pct_change(latest.value, prior.value)
    chain = [
        f"series={sid} price-index family",
        f"prior_index={prior.value:.4f} on {prior.date}",
        f"latest_index={latest.value:.4f} on {latest.date}",
    ]
    if mom is not None:
        chain.append(f"MoM%={mom:+.3f} (hard MoM threshold ±{CPI_MOM_PCT_HARD:.2f}%)")

    # Rule A: extreme single-month index jump
    if mom is not None and abs(mom) >= CPI_MOM_PCT_HARD:
        direction = "surge" if mom > 0 else "collapse"
        chain.append(f"MoM move is structural ({direction})")
        return _signal(
            signal_id=f"hard:{sid}:mom:{latest.date}",
            title=f"{sid} extreme monthly price-index {direction}",
            summary=(
                f"{sid} index MoM change {mom:+.2f}% "
                f"({prior.date} → {latest.date}). "
                f"Hard threshold is ±{CPI_MOM_PCT_HARD:.2f}%."
            ),
            severity=0.92,
            indicator_ids=[sid],
            as_of=_to_utc_z(latest.date),
            detected_at=detected_at,
            reasoning_chain=chain,
            related_regime=related_regime,
            details={
                "mom_pct": round(mom, 4),
                "threshold_mom_pct": CPI_MOM_PCT_HARD,
                "rule": "mom",
            },
        )

    # Rule B: large shift in YoY inflation rate (needs long history)
    if len(points) < CPI_YOY_MIN_POINTS:
        return None
    yoy_latest = _yoy_pct(points, len(points) - 1)
    yoy_prior = _yoy_pct(points, len(points) - 2)
    if yoy_latest is None or yoy_prior is None:
        return None
    yoy_shift = yoy_latest - yoy_prior
    chain.append(
        f"YoY% prior={yoy_prior:.3f}, latest={yoy_latest:.3f}, "
        f"shift={yoy_shift:+.3f} pp (threshold ±{CPI_YOY_PP_SHIFT:.2f})"
    )
    if abs(yoy_shift) < CPI_YOY_PP_SHIFT:
        return None

    chain.append("YoY inflation path shift is structural")
    return _signal(
        signal_id=f"hard:{sid}:yoy_shift:{latest.date}",
        title=f"{sid} structural YoY inflation shift",
        summary=(
            f"{sid} YoY inflation moved from {yoy_prior:.2f}% to "
            f"{yoy_latest:.2f}% (shift {yoy_shift:+.2f} pp). "
            f"Hard threshold is ±{CPI_YOY_PP_SHIFT:.2f} pp."
        ),
        severity=0.90,
        indicator_ids=[sid],
        as_of=_to_utc_z(latest.date),
        detected_at=detected_at,
        reasoning_chain=chain,
        related_regime=related_regime,
        details={
            "yoy_prior_pct": round(yoy_prior, 4),
            "yoy_latest_pct": round(yoy_latest, 4),
            "yoy_shift_pp": round(yoy_shift, 4),
            "threshold_yoy_shift_pp": CPI_YOY_PP_SHIFT,
            "rule": "yoy_shift",
        },
    )


def _eval_payrolls(
    sid: str,
    points: Sequence[_Point],
    *,
    related_regime: Optional[MacroRegime],
    detected_at: str,
) -> Optional[HardInvalidationSignal]:
    """
    PAYEMS is a *level* (thousands). Hard rule uses MoM first difference.
    """
    if len(points) < 2:
        return None
    prior, latest = points[-2], points[-1]
    delta = latest.value - prior.value
    chain = [
        f"series={sid} payrolls family",
        f"prior={prior.value:.1f} on {prior.date}",
        f"latest={latest.value:.1f} on {latest.date}",
        f"MoM change={delta:+.1f}k (hard crash threshold {PAYEMS_CRASH_THOUSANDS:.0f}k)",
    ]
    if delta > PAYEMS_CRASH_THOUSANDS:
        return None

    chain.append("payroll collapse exceeds hard threshold")
    return _signal(
        signal_id=f"hard:{sid}:crash:{latest.date}",
        title=f"{sid} payroll collapse",
        summary=(
            f"{sid} fell by {delta:.0f}k "
            f"({prior.date} → {latest.date}). "
            f"Hard threshold is {PAYEMS_CRASH_THOUSANDS:.0f}k MoM."
        ),
        severity=0.93,
        indicator_ids=[sid],
        as_of=_to_utc_z(latest.date),
        detected_at=detected_at,
        reasoning_chain=chain,
        related_regime=related_regime,
        details={
            "mom_change_thousands": round(delta, 2),
            "threshold_thousands": PAYEMS_CRASH_THOUSANDS,
            "rule": "payroll_crash",
        },
    )


def _eval_gdp(
    sid: str,
    points: Sequence[_Point],
    *,
    related_regime: Optional[MacroRegime],
    detected_at: str,
) -> Optional[HardInvalidationSignal]:
    if len(points) < 2:
        return None
    prior, latest = points[-2], points[-1]
    qoq = _pct_change(latest.value, prior.value)
    if qoq is None:
        return None
    chain = [
        f"series={sid} GDP family",
        f"prior={prior.value:.3f} on {prior.date}",
        f"latest={latest.value:.3f} on {latest.date}",
        f"QoQ%={qoq:+.3f} (hard threshold {GDP_QOQ_PCT_HARD:.2f}%)",
    ]
    if qoq > GDP_QOQ_PCT_HARD:
        return None

    chain.append("GDP contraction exceeds hard threshold")
    return _signal(
        signal_id=f"hard:{sid}:qoq:{latest.date}",
        title=f"{sid} hard GDP contraction",
        summary=(
            f"{sid} QoQ change {qoq:+.2f}% "
            f"({prior.date} → {latest.date}). "
            f"Hard threshold is {GDP_QOQ_PCT_HARD:.2f}%."
        ),
        severity=0.91,
        indicator_ids=[sid],
        as_of=_to_utc_z(latest.date),
        detected_at=detected_at,
        reasoning_chain=chain,
        related_regime=related_regime,
        details={
            "qoq_pct": round(qoq, 4),
            "threshold_qoq_pct": GDP_QOQ_PCT_HARD,
            "rule": "gdp_contraction",
        },
    )


def _points_from_change(change: SeriesChange) -> list[_Point]:
    """Build a minimal point list from an explicit ``SeriesChange``."""
    sid_dates = list(change.history_dates)
    hist = list(change.history)
    points: list[_Point] = []

    if hist:
        for i, val in enumerate(hist):
            d = sid_dates[i] if i < len(sid_dates) else f"h{i:04d}"
            points.append(_Point(date=str(d)[:32], value=float(val)))
        points.sort(key=lambda p: p.date)

    as_of_s = _to_utc_z(change.as_of) or "latest"
    # Prefer date-only key when possible
    latest_date = as_of_s[:10] if as_of_s and as_of_s != "latest" else "latest"

    if change.prior is not None:
        # Avoid duplicating if history already ends with prior/latest
        if not points:
            points.append(_Point(date="prior", value=float(change.prior)))
            points.append(_Point(date=latest_date, value=float(change.latest)))
        else:
            # Ensure latest is present
            if abs(points[-1].value - float(change.latest)) > 1e-12:
                points.append(_Point(date=latest_date, value=float(change.latest)))
    else:
        if not points:
            # Single point only — rules will no-op (need ≥2)
            points.append(_Point(date=latest_date, value=float(change.latest)))
        elif abs(points[-1].value - float(change.latest)) > 1e-12:
            points.append(_Point(date=latest_date, value=float(change.latest)))

    return points


def _eval_series_points(
    sid: str,
    points: Sequence[_Point],
    *,
    related_regime: Optional[MacroRegime],
    detected_at: str,
) -> Optional[HardInvalidationSignal]:
    """Dispatch one series to the matching family rule set."""
    if sid in _POLICY_RATE_IDS:
        return _eval_policy_rate(
            sid, points, related_regime=related_regime, detected_at=detected_at
        )
    if sid in _UNEMPLOYMENT_IDS:
        return _eval_unemployment(
            sid, points, related_regime=related_regime, detected_at=detected_at
        )
    if sid in _PRICE_INDEX_IDS:
        return _eval_price_index(
            sid, points, related_regime=related_regime, detected_at=detected_at
        )
    if sid in _PAYROLL_IDS:
        return _eval_payrolls(
            sid, points, related_regime=related_regime, detected_at=detected_at
        )
    if sid in _GDP_IDS:
        return _eval_gdp(
            sid, points, related_regime=related_regime, detected_at=detected_at
        )
    return None


# =============================================================================
# Public API
# =============================================================================

def detect_hard_invalidations(
    changes: Optional[Iterable[Union[SeriesChange, Mapping[str, Any]]]] = None,
    *,
    fred_series: Optional[Mapping[str, Any]] = None,
    related_regime: Optional[Union[MacroRegime, str]] = None,
    detected_at: Optional[Union[str, datetime]] = None,
) -> list[HardInvalidationSignal]:
    """
    Detect structural hard-invalidation signals from structured inputs.

    Parameters
    ----------
    changes:
        Optional iterable of ``SeriesChange`` (or dicts with keys
        ``series_id``, ``latest``, optional ``prior`` / ``as_of`` /
        ``history``).
    fred_series:
        Optional in-memory map ``{series_id: [obs, ...]}``. Each obs should
        expose ``date`` and ``value`` (FRED-like). Unknown series ids are
        ignored rather than guessed.
    related_regime:
        Optional Stage C regime context attached to emitted signals.
    detected_at:
        Optional detection timestamp (UTC Z). Defaults to now (UTC).

    Returns
    -------
    list[HardInvalidationSignal]
        Empty when nothing clear is detected or inputs are incomplete.
        Ordered by descending severity, then ``signal_id``.
    """
    detected = _to_utc_z(detected_at) or _now_utc_z()
    regime = _regime_or_none(related_regime)
    signals: list[HardInvalidationSignal] = []
    seen_ids: set[str] = set()

    # --- Explicit change summaries ---
    for raw in changes or []:
        change = _coerce_change(raw)
        if change is None:
            continue
        sid = _norm_series_id(change.series_id)
        if not sid:
            continue
        if sid not in (
            _POLICY_RATE_IDS
            | _UNEMPLOYMENT_IDS
            | _PRICE_INDEX_IDS
            | _PAYROLL_IDS
            | _GDP_IDS
        ):
            log.debug("hard_invalidation: skip unsupported series_id=%s", sid)
            continue
        points = _points_from_change(change)
        if len(points) < 2:
            log.debug(
                "hard_invalidation: incomplete history series=%s points=%s",
                sid,
                len(points),
            )
            continue
        sig = _eval_series_points(
            sid, points, related_regime=regime, detected_at=detected
        )
        if sig is not None and sig.signal_id not in seen_ids:
            seen_ids.add(sig.signal_id)
            signals.append(sig)

    # --- FRED-like observation map ---
    if isinstance(fred_series, Mapping):
        for key, obs in fred_series.items():
            sid = _norm_series_id(key)
            if not sid:
                continue
            if sid not in (
                _POLICY_RATE_IDS
                | _UNEMPLOYMENT_IDS
                | _PRICE_INDEX_IDS
                | _PAYROLL_IDS
                | _GDP_IDS
            ):
                log.debug("hard_invalidation: skip unsupported fred series=%s", sid)
                continue
            points = _parse_obs_list(obs)
            if len(points) < 2:
                log.debug(
                    "hard_invalidation: incomplete fred history series=%s points=%s",
                    sid,
                    len(points),
                )
                continue
            sig = _eval_series_points(
                sid, points, related_regime=regime, detected_at=detected
            )
            if sig is not None and sig.signal_id not in seen_ids:
                seen_ids.add(sig.signal_id)
                signals.append(sig)

    signals.sort(key=lambda s: (-float(s.severity), s.signal_id))

    if signals:
        log.info(
            "hard_invalidation: emitted %s signal(s): %s",
            len(signals),
            ", ".join(s.signal_id for s in signals),
        )
    else:
        log.debug("hard_invalidation: no structural breaks detected")

    return signals


def detect_hard_invalidations_from_fred(
    fred_series: Mapping[str, Any],
    *,
    related_regime: Optional[Union[MacroRegime, str]] = None,
    detected_at: Optional[Union[str, datetime]] = None,
) -> list[HardInvalidationSignal]:
    """
    Thin helper: run detection on an in-memory FRED-like observation map only.

    Equivalent to ``detect_hard_invalidations(fred_series=...)``.
    """
    return detect_hard_invalidations(
        fred_series=fred_series,
        related_regime=related_regime,
        detected_at=detected_at,
    )


def _coerce_change(
    raw: Union[SeriesChange, Mapping[str, Any], Any],
) -> Optional[SeriesChange]:
    if isinstance(raw, SeriesChange):
        return raw
    if not isinstance(raw, Mapping):
        log.debug("hard_invalidation: ignore non-mapping change entry type=%s", type(raw))
        return None
    sid = _norm_series_id(raw.get("series_id") or raw.get("id") or raw.get("series"))
    latest = _as_float(raw.get("latest") if "latest" in raw else raw.get("value"))
    if not sid or latest is None:
        log.debug("hard_invalidation: change missing series_id/latest keys=%s", list(raw))
        return None
    prior = _as_float(raw.get("prior") if "prior" in raw else raw.get("previous"))
    hist_raw = raw.get("history") or ()
    hist: list[float] = []
    if isinstance(hist_raw, (list, tuple)):
        for x in hist_raw:
            f = _as_float(x)
            if f is not None:
                hist.append(f)
    dates_raw = raw.get("history_dates") or ()
    dates: list[str] = []
    if isinstance(dates_raw, (list, tuple)):
        dates = [str(d) for d in dates_raw]
    return SeriesChange(
        series_id=sid,
        latest=latest,
        prior=prior,
        as_of=raw.get("as_of") or raw.get("date") or raw.get("datetime"),  # type: ignore[arg-type]
        history=tuple(hist),
        history_dates=tuple(dates),
    )
