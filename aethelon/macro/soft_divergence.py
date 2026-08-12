# -*- coding: utf-8 -*-
"""
aethelon.macro.soft_divergence — Stage C4 soft-divergence detector
=================================================================
Pure, offline, rule-based detection of **mild / noisy** moves that sit
*below* Stage C3 hard-invalidation thresholds.

Returns ``SoftDivergenceSignal`` objects. Empty list when nothing
meaningful is present or history is incomplete.

Design
------
  * Soft band only: never emit when a C3 hard rule would already fire.
  * No spam on typical month-to-month noise (floors are still conservative).
  * No network, database, GUI, or ``news_engine`` imports.
  * Does not change C2 classification or C3 hard-invalidation behavior.
  * Incomplete history → no signal.
  * Timestamps are UTC ISO 8601 Z via the C1 schema validators.

``is_noise``
------------
  * ``True``  — isolated / reversing print in the lower soft band
  * ``False`` — two consecutive steps in the same direction (watch, not hard)
  * ``None``  — noticeable but unconfirmed

Same input shapes as C3: ``SeriesChange`` / dicts, and FRED-like
``{series_id: [obs, ...]}`` maps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from aethelon.core.logger import get_logger
from aethelon.macro.hard_invalidation import (
    CPI_MOM_PCT_HARD,
    CPI_YOY_MIN_POINTS,
    CPI_YOY_PP_SHIFT,
    FEDFUNDS_ABS_MOVE_PP,
    GDP_QOQ_PCT_HARD,
    PAYEMS_CRASH_THOUSANDS,
    SeriesChange,
    UNRATE_FROM_LOW_PP,
    UNRATE_LOOKBACK,
    UNRATE_STEP_RISE_PP,
)
from aethelon.macro.schemas import MacroRegime, SoftDivergenceSignal, _to_utc_z

log = get_logger(__name__)

# =============================================================================
# Soft floors (hard C3 constants are exclusive ceilings)
# =============================================================================

# Policy rate: 50bp is unusual; 25bp FOMC prints are ignored
FEDFUNDS_SOFT_MIN_PP = 0.50

# Unemployment
UNRATE_SOFT_STEP_MIN_PP = 0.20
UNRATE_SOFT_FROM_LOW_MIN_PP = 0.30

# Price index
CPI_MOM_SOFT_MIN_PCT = 0.60
CPI_YOY_SOFT_MIN_PP = 0.80

# Payrolls (thousands, negative = job loss). Typical +150–250k is ignored.
PAYEMS_SOFT_MIN_THOUSANDS = -200.0

# GDP QoQ %
GDP_SOFT_MAX_PCT = -0.80

# Series families (same coverage as C3 — VIX/risk left for C5)
_POLICY_RATE_IDS = frozenset({"FEDFUNDS", "DFEDTARU", "DFEDTARL", "IORB"})
_UNEMPLOYMENT_IDS = frozenset({"UNRATE", "U6RATE"})
_PRICE_INDEX_IDS = frozenset(
    {"CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE", "COREPCE"}
)
_PAYROLL_IDS = frozenset({"PAYEMS"})
_GDP_IDS = frozenset({"GDP", "GDPC1"})
_WATCHED_IDS = (
    _POLICY_RATE_IDS
    | _UNEMPLOYMENT_IDS
    | _PRICE_INDEX_IDS
    | _PAYROLL_IDS
    | _GDP_IDS
)


@dataclass(frozen=True)
class _Point:
    """Internal sorted observation (oldest → newest)."""

    date: str
    value: float


# =============================================================================
# Parsing helpers (local copies — do not reach into C3 privates)
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
    """Parse a FRED-like observation list into ascending ``_Point`` rows."""
    if not raw or not isinstance(raw, (list, tuple)):
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
        date_key = date_s[:10] if ("T" in date_s or len(date_s) >= 10) else date_s
        points.append(_Point(date=date_key, value=val))

    dedup: dict[str, _Point] = {}
    for p in points:
        dedup[p.date] = p
    return [dedup[k] for k in sorted(dedup.keys())]


def _pct_change(new: float, old: float) -> Optional[float]:
    if old == 0:
        return None
    return (new - old) / abs(old) * 100.0


def _yoy_pct(points: Sequence[_Point], index: int) -> Optional[float]:
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


def _band_strength(magnitude: float, floor: float, ceiling: float) -> float:
    """
    Map a value inside [floor, ceiling) to strength in ~[0.30, 0.70].

    Stays clearly below a 'hard' 0.85–0.95 feel.
    """
    span = ceiling - floor
    if span <= 0:
        return 0.40
    t = (magnitude - floor) / span
    if t < 0.0:
        t = 0.0
    if t > 0.999:
        t = 0.999
    return round(0.30 + 0.40 * t, 3)


def _noise_flag(
    points: Sequence[_Point],
    *,
    last_delta: float,
    in_lower_half: bool,
) -> Optional[bool]:
    """
    Conservative noise read from the last two steps when history allows.

    Two same-direction steps → likely real (False).
    Last step reverses the previous → likely noise (True).
    Otherwise lean noise if the print is in the lower half of the soft band.
    """
    if len(points) >= 3:
        d1 = points[-2].value - points[-3].value
        d2 = last_delta
        if d1 == 0 or d2 == 0:
            return True if in_lower_half else None
        same_dir = (d1 > 0 and d2 > 0) or (d1 < 0 and d2 < 0)
        if same_dir:
            return False
        return True
    if in_lower_half:
        return True
    return None


def _signal(
    *,
    signal_id: str,
    title: str,
    summary: str,
    strength: float,
    is_noise: Optional[bool],
    indicator_ids: list[str],
    as_of: Optional[str],
    detected_at: str,
    reasoning_chain: list[str],
    related_regime: Optional[MacroRegime],
    details: Optional[dict[str, Any]] = None,
) -> SoftDivergenceSignal:
    return SoftDivergenceSignal(
        signal_id=signal_id,
        title=title,
        summary=summary,
        strength=max(0.0, min(1.0, float(strength))),
        is_noise=is_noise,
        indicator_ids=indicator_ids,
        related_regime=related_regime,
        detected_at=detected_at,
        as_of=as_of,
        reasoning_chain=reasoning_chain,
        market_narrative=None,
        details=details,
    )


# =============================================================================
# Per-family evaluators (soft band strictly below C3)
# =============================================================================

def _eval_policy_rate(
    sid: str,
    points: Sequence[_Point],
    *,
    related_regime: Optional[MacroRegime],
    detected_at: str,
) -> Optional[SoftDivergenceSignal]:
    if len(points) < 2:
        return None
    prior, latest = points[-2], points[-1]
    delta = latest.value - prior.value
    abs_delta = abs(delta)

    if abs_delta >= FEDFUNDS_ABS_MOVE_PP:
        return None  # C3 owns this
    if abs_delta < FEDFUNDS_SOFT_MIN_PP:
        return None

    strength = _band_strength(abs_delta, FEDFUNDS_SOFT_MIN_PP, FEDFUNDS_ABS_MOVE_PP)
    mid = (FEDFUNDS_SOFT_MIN_PP + FEDFUNDS_ABS_MOVE_PP) / 2.0
    is_noise = _noise_flag(points, last_delta=delta, in_lower_half=abs_delta < mid)
    direction = "hike" if delta > 0 else "cut"
    chain = [
        f"series={sid} policy-rate family (soft band)",
        f"prior={prior.value:.3f} on {prior.date}",
        f"latest={latest.value:.3f} on {latest.date}",
        f"delta={delta:+.3f} pp; soft=[{FEDFUNDS_SOFT_MIN_PP:.2f}, {FEDFUNDS_ABS_MOVE_PP:.2f}) pp",
        f"is_noise={is_noise!s}",
    ]
    return _signal(
        signal_id=f"soft:{sid}:rate_move:{latest.date}",
        title=f"{sid} mild policy-rate {direction}",
        summary=(
            f"{sid} moved {delta:+.2f} pp from {prior.value:.2f} to "
            f"{latest.value:.2f} ({prior.date} → {latest.date}). "
            f"Soft band is [{FEDFUNDS_SOFT_MIN_PP:.2f}, {FEDFUNDS_ABS_MOVE_PP:.2f}) pp."
        ),
        strength=strength,
        is_noise=is_noise,
        indicator_ids=[sid],
        as_of=_to_utc_z(latest.date),
        detected_at=detected_at,
        reasoning_chain=chain,
        related_regime=related_regime,
        details={
            "prior": prior.value,
            "latest": latest.value,
            "delta_pp": round(delta, 4),
            "soft_min_pp": FEDFUNDS_SOFT_MIN_PP,
            "hard_min_pp": FEDFUNDS_ABS_MOVE_PP,
            "rule": "rate_move",
        },
    )


def _eval_unemployment(
    sid: str,
    points: Sequence[_Point],
    *,
    related_regime: Optional[MacroRegime],
    detected_at: str,
) -> Optional[SoftDivergenceSignal]:
    if len(points) < 2:
        return None
    prior, latest = points[-2], points[-1]
    step = latest.value - prior.value

    # Single-print rise in the soft step band
    if UNRATE_SOFT_STEP_MIN_PP <= step < UNRATE_STEP_RISE_PP:
        strength = _band_strength(step, UNRATE_SOFT_STEP_MIN_PP, UNRATE_STEP_RISE_PP)
        mid = (UNRATE_SOFT_STEP_MIN_PP + UNRATE_STEP_RISE_PP) / 2.0
        is_noise = _noise_flag(points, last_delta=step, in_lower_half=step < mid)
        chain = [
            f"series={sid} unemployment family (soft step)",
            f"prior={prior.value:.3f} on {prior.date}",
            f"latest={latest.value:.3f} on {latest.date}",
            f"step_delta={step:+.3f} pp; soft=[{UNRATE_SOFT_STEP_MIN_PP:.2f}, {UNRATE_STEP_RISE_PP:.2f}) pp",
            f"is_noise={is_noise!s}",
        ]
        return _signal(
            signal_id=f"soft:{sid}:step_rise:{latest.date}",
            title=f"{sid} mild unemployment uptick",
            summary=(
                f"{sid} rose {step:+.2f} pp in one step "
                f"({prior.value:.2f} → {latest.value:.2f}). "
                f"Soft band is [+{UNRATE_SOFT_STEP_MIN_PP:.2f}, +{UNRATE_STEP_RISE_PP:.2f}) pp."
            ),
            strength=strength,
            is_noise=is_noise,
            indicator_ids=[sid],
            as_of=_to_utc_z(latest.date),
            detected_at=detected_at,
            reasoning_chain=chain,
            related_regime=related_regime,
            details={
                "step_pp": round(step, 4),
                "soft_min_pp": UNRATE_SOFT_STEP_MIN_PP,
                "hard_min_pp": UNRATE_STEP_RISE_PP,
                "rule": "step_rise",
            },
        )

    if step >= UNRATE_STEP_RISE_PP:
        return None  # C3 owns hard step

    # From-low (only if not already a hard from-low)
    window = points[-(UNRATE_LOOKBACK + 1) :]
    if len(window) < 4:
        return None
    hist = window[:-1]
    low_pt = min(hist, key=lambda p: p.value)
    from_low = latest.value - low_pt.value
    if from_low >= UNRATE_FROM_LOW_PP:
        return None  # C3 owns this
    if from_low < UNRATE_SOFT_FROM_LOW_MIN_PP:
        return None

    strength = _band_strength(
        from_low, UNRATE_SOFT_FROM_LOW_MIN_PP, UNRATE_FROM_LOW_PP
    )
    mid = (UNRATE_SOFT_FROM_LOW_MIN_PP + UNRATE_FROM_LOW_PP) / 2.0
    is_noise = _noise_flag(points, last_delta=step, in_lower_half=from_low < mid)
    chain = [
        f"series={sid} unemployment family (soft from-low)",
        f"latest={latest.value:.3f} on {latest.date}",
        f"recent_low={low_pt.value:.3f} on {low_pt.date}",
        f"from_low={from_low:+.3f} pp; soft=[{UNRATE_SOFT_FROM_LOW_MIN_PP:.2f}, {UNRATE_FROM_LOW_PP:.2f}) pp",
        f"is_noise={is_noise!s}",
    ]
    return _signal(
        signal_id=f"soft:{sid}:from_low:{latest.date}",
        title=f"{sid} mild labor deterioration from recent low",
        summary=(
            f"{sid} is {from_low:+.2f} pp above its recent low "
            f"({low_pt.value:.2f} on {low_pt.date} → {latest.value:.2f} on "
            f"{latest.date}). Soft band is "
            f"[+{UNRATE_SOFT_FROM_LOW_MIN_PP:.2f}, +{UNRATE_FROM_LOW_PP:.2f}) pp."
        ),
        strength=strength,
        is_noise=is_noise,
        indicator_ids=[sid],
        as_of=_to_utc_z(latest.date),
        detected_at=detected_at,
        reasoning_chain=chain,
        related_regime=related_regime,
        details={
            "from_low_pp": round(from_low, 4),
            "recent_low": low_pt.value,
            "soft_min_pp": UNRATE_SOFT_FROM_LOW_MIN_PP,
            "hard_min_pp": UNRATE_FROM_LOW_PP,
            "rule": "from_low",
        },
    )


def _eval_price_index(
    sid: str,
    points: Sequence[_Point],
    *,
    related_regime: Optional[MacroRegime],
    detected_at: str,
) -> Optional[SoftDivergenceSignal]:
    if len(points) < 2:
        return None
    prior, latest = points[-2], points[-1]
    mom = _pct_change(latest.value, prior.value)

    if mom is not None and abs(mom) >= CPI_MOM_PCT_HARD:
        return None  # C3 owns hard MoM
    if mom is not None and abs(mom) >= CPI_MOM_SOFT_MIN_PCT:
        strength = _band_strength(abs(mom), CPI_MOM_SOFT_MIN_PCT, CPI_MOM_PCT_HARD)
        mid = (CPI_MOM_SOFT_MIN_PCT + CPI_MOM_PCT_HARD) / 2.0
        is_noise = _noise_flag(points, last_delta=mom, in_lower_half=abs(mom) < mid)
        direction = "upside" if mom > 0 else "downside"
        chain = [
            f"series={sid} price-index family (soft MoM)",
            f"prior_index={prior.value:.4f} on {prior.date}",
            f"latest_index={latest.value:.4f} on {latest.date}",
            f"MoM%={mom:+.3f}; soft=[{CPI_MOM_SOFT_MIN_PCT:.2f}, {CPI_MOM_PCT_HARD:.2f}) %",
            f"is_noise={is_noise!s}",
        ]
        return _signal(
            signal_id=f"soft:{sid}:mom:{latest.date}",
            title=f"{sid} mild monthly price-index {direction}",
            summary=(
                f"{sid} index MoM change {mom:+.2f}% "
                f"({prior.date} → {latest.date}). "
                f"Soft band is [{CPI_MOM_SOFT_MIN_PCT:.2f}, {CPI_MOM_PCT_HARD:.2f}) %."
            ),
            strength=strength,
            is_noise=is_noise,
            indicator_ids=[sid],
            as_of=_to_utc_z(latest.date),
            detected_at=detected_at,
            reasoning_chain=chain,
            related_regime=related_regime,
            details={
                "mom_pct": round(mom, 4),
                "soft_min_pct": CPI_MOM_SOFT_MIN_PCT,
                "hard_min_pct": CPI_MOM_PCT_HARD,
                "rule": "mom",
            },
        )

    if len(points) < CPI_YOY_MIN_POINTS:
        return None
    yoy_latest = _yoy_pct(points, len(points) - 1)
    yoy_prior = _yoy_pct(points, len(points) - 2)
    if yoy_latest is None or yoy_prior is None:
        return None
    yoy_shift = yoy_latest - yoy_prior
    if abs(yoy_shift) >= CPI_YOY_PP_SHIFT:
        return None  # C3 owns hard YoY
    if abs(yoy_shift) < CPI_YOY_SOFT_MIN_PP:
        return None

    strength = _band_strength(abs(yoy_shift), CPI_YOY_SOFT_MIN_PP, CPI_YOY_PP_SHIFT)
    mid = (CPI_YOY_SOFT_MIN_PP + CPI_YOY_PP_SHIFT) / 2.0
    is_noise = _noise_flag(
        points, last_delta=yoy_shift, in_lower_half=abs(yoy_shift) < mid
    )
    chain = [
        f"series={sid} price-index family (soft YoY shift)",
        f"YoY% prior={yoy_prior:.3f}, latest={yoy_latest:.3f}, shift={yoy_shift:+.3f} pp",
        f"soft=[{CPI_YOY_SOFT_MIN_PP:.2f}, {CPI_YOY_PP_SHIFT:.2f}) pp",
        f"is_noise={is_noise!s}",
    ]
    return _signal(
        signal_id=f"soft:{sid}:yoy_shift:{latest.date}",
        title=f"{sid} mild YoY inflation path shift",
        summary=(
            f"{sid} YoY inflation moved from {yoy_prior:.2f}% to "
            f"{yoy_latest:.2f}% (shift {yoy_shift:+.2f} pp). "
            f"Soft band is [{CPI_YOY_SOFT_MIN_PP:.2f}, {CPI_YOY_PP_SHIFT:.2f}) pp."
        ),
        strength=strength,
        is_noise=is_noise,
        indicator_ids=[sid],
        as_of=_to_utc_z(latest.date),
        detected_at=detected_at,
        reasoning_chain=chain,
        related_regime=related_regime,
        details={
            "yoy_shift_pp": round(yoy_shift, 4),
            "soft_min_pp": CPI_YOY_SOFT_MIN_PP,
            "hard_min_pp": CPI_YOY_PP_SHIFT,
            "rule": "yoy_shift",
        },
    )


def _eval_payrolls(
    sid: str,
    points: Sequence[_Point],
    *,
    related_regime: Optional[MacroRegime],
    detected_at: str,
) -> Optional[SoftDivergenceSignal]:
    if len(points) < 2:
        return None
    prior, latest = points[-2], points[-1]
    delta = latest.value - prior.value

    # Soft = job-loss print below hard crash, above typical noise
    # PAYEMS_CRASH_THOUSANDS is more negative (e.g. -400); soft starts at -200
    if delta <= PAYEMS_CRASH_THOUSANDS:
        return None  # C3 owns crash
    if delta > PAYEMS_SOFT_MIN_THOUSANDS:
        return None

    mag = abs(delta)
    floor = abs(PAYEMS_SOFT_MIN_THOUSANDS)
    ceiling = abs(PAYEMS_CRASH_THOUSANDS)
    strength = _band_strength(mag, floor, ceiling)
    mid = (floor + ceiling) / 2.0
    is_noise = _noise_flag(points, last_delta=delta, in_lower_half=mag < mid)
    chain = [
        f"series={sid} payrolls family (soft weakness)",
        f"prior={prior.value:.1f} on {prior.date}",
        f"latest={latest.value:.1f} on {latest.date}",
        f"MoM change={delta:+.1f}k; soft=({PAYEMS_CRASH_THOUSANDS:.0f}k, {PAYEMS_SOFT_MIN_THOUSANDS:.0f}k]",
        f"is_noise={is_noise!s}",
    ]
    return _signal(
        signal_id=f"soft:{sid}:weak:{latest.date}",
        title=f"{sid} mild payroll weakness",
        summary=(
            f"{sid} fell by {delta:.0f}k ({prior.date} → {latest.date}). "
            f"Soft band is ({PAYEMS_CRASH_THOUSANDS:.0f}k, "
            f"{PAYEMS_SOFT_MIN_THOUSANDS:.0f}k] MoM."
        ),
        strength=strength,
        is_noise=is_noise,
        indicator_ids=[sid],
        as_of=_to_utc_z(latest.date),
        detected_at=detected_at,
        reasoning_chain=chain,
        related_regime=related_regime,
        details={
            "mom_change_thousands": round(delta, 2),
            "soft_min_thousands": PAYEMS_SOFT_MIN_THOUSANDS,
            "hard_crash_thousands": PAYEMS_CRASH_THOUSANDS,
            "rule": "payroll_weak",
        },
    )


def _eval_gdp(
    sid: str,
    points: Sequence[_Point],
    *,
    related_regime: Optional[MacroRegime],
    detected_at: str,
) -> Optional[SoftDivergenceSignal]:
    if len(points) < 2:
        return None
    prior, latest = points[-2], points[-1]
    qoq = _pct_change(latest.value, prior.value)
    if qoq is None:
        return None
    if qoq <= GDP_QOQ_PCT_HARD:
        return None  # C3 owns hard contraction
    if qoq > GDP_SOFT_MAX_PCT:
        return None

    mag = abs(qoq)
    floor = abs(GDP_SOFT_MAX_PCT)
    ceiling = abs(GDP_QOQ_PCT_HARD)
    strength = _band_strength(mag, floor, ceiling)
    mid = (floor + ceiling) / 2.0
    is_noise = _noise_flag(points, last_delta=qoq, in_lower_half=mag < mid)
    chain = [
        f"series={sid} GDP family (soft contraction)",
        f"prior={prior.value:.3f} on {prior.date}",
        f"latest={latest.value:.3f} on {latest.date}",
        f"QoQ%={qoq:+.3f}; soft=({GDP_QOQ_PCT_HARD:.2f}, {GDP_SOFT_MAX_PCT:.2f}] %",
        f"is_noise={is_noise!s}",
    ]
    return _signal(
        signal_id=f"soft:{sid}:qoq:{latest.date}",
        title=f"{sid} mild GDP contraction",
        summary=(
            f"{sid} QoQ change {qoq:+.2f}% ({prior.date} → {latest.date}). "
            f"Soft band is ({GDP_QOQ_PCT_HARD:.2f}, {GDP_SOFT_MAX_PCT:.2f}] %."
        ),
        strength=strength,
        is_noise=is_noise,
        indicator_ids=[sid],
        as_of=_to_utc_z(latest.date),
        detected_at=detected_at,
        reasoning_chain=chain,
        related_regime=related_regime,
        details={
            "qoq_pct": round(qoq, 4),
            "soft_max_pct": GDP_SOFT_MAX_PCT,
            "hard_max_pct": GDP_QOQ_PCT_HARD,
            "rule": "gdp_soft",
        },
    )


def _points_from_change(change: SeriesChange) -> list[_Point]:
    sid_dates = list(change.history_dates)
    hist = list(change.history)
    points: list[_Point] = []

    if hist:
        for i, val in enumerate(hist):
            d = sid_dates[i] if i < len(sid_dates) else f"h{i:04d}"
            points.append(_Point(date=str(d)[:32], value=float(val)))
        points.sort(key=lambda p: p.date)

    as_of_s = _to_utc_z(change.as_of) or "latest"
    latest_date = as_of_s[:10] if as_of_s and as_of_s != "latest" else "latest"

    if change.prior is not None:
        if not points:
            points.append(_Point(date="prior", value=float(change.prior)))
            points.append(_Point(date=latest_date, value=float(change.latest)))
        elif abs(points[-1].value - float(change.latest)) > 1e-12:
            points.append(_Point(date=latest_date, value=float(change.latest)))
    else:
        if not points:
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
) -> Optional[SoftDivergenceSignal]:
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


def _coerce_change(
    raw: Union[SeriesChange, Mapping[str, Any], Any],
) -> Optional[SeriesChange]:
    if isinstance(raw, SeriesChange):
        return raw
    if not isinstance(raw, Mapping):
        log.debug("soft_divergence: ignore non-mapping change type=%s", type(raw))
        return None
    sid = _norm_series_id(raw.get("series_id") or raw.get("id") or raw.get("series"))
    latest = _as_float(raw.get("latest") if "latest" in raw else raw.get("value"))
    if not sid or latest is None:
        log.debug("soft_divergence: change missing series_id/latest")
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


# =============================================================================
# Public API
# =============================================================================

def detect_soft_divergences(
    changes: Optional[Iterable[Union[SeriesChange, Mapping[str, Any]]]] = None,
    *,
    fred_series: Optional[Mapping[str, Any]] = None,
    related_regime: Optional[Union[MacroRegime, str]] = None,
    detected_at: Optional[Union[str, datetime]] = None,
) -> list[SoftDivergenceSignal]:
    """
    Detect soft divergences (mild / noisy moves below C3 hard thresholds).

    Parameters
    ----------
    changes:
        Optional iterable of ``SeriesChange`` or dicts with ``series_id``,
        ``latest``, optional ``prior`` / ``as_of`` / ``history``.
    fred_series:
        Optional in-memory map ``{series_id: [obs, ...]}``.
    related_regime:
        Optional Stage C regime context attached to emitted signals.
    detected_at:
        Optional detection timestamp (UTC Z). Defaults to now (UTC).

    Returns
    -------
    list[SoftDivergenceSignal]
        Empty when nothing is in the soft band or inputs are incomplete.
        Ordered by descending strength, then ``signal_id``.
    """
    detected = _to_utc_z(detected_at) or _now_utc_z()
    regime = _regime_or_none(related_regime)
    signals: list[SoftDivergenceSignal] = []
    seen_ids: set[str] = set()

    for raw in changes or []:
        change = _coerce_change(raw)
        if change is None:
            continue
        sid = _norm_series_id(change.series_id)
        if sid not in _WATCHED_IDS:
            log.debug("soft_divergence: skip unsupported series_id=%s", sid)
            continue
        points = _points_from_change(change)
        if len(points) < 2:
            log.debug(
                "soft_divergence: incomplete history series=%s points=%s",
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

    if isinstance(fred_series, Mapping):
        for key, obs in fred_series.items():
            sid = _norm_series_id(key)
            if sid not in _WATCHED_IDS:
                log.debug("soft_divergence: skip unsupported fred series=%s", sid)
                continue
            points = _parse_obs_list(obs)
            if len(points) < 2:
                log.debug(
                    "soft_divergence: incomplete fred history series=%s points=%s",
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

    signals.sort(key=lambda s: (-float(s.strength), s.signal_id))

    if signals:
        log.info(
            "soft_divergence: emitted %s signal(s): %s",
            len(signals),
            ", ".join(s.signal_id for s in signals),
        )
    else:
        log.debug("soft_divergence: no soft divergences detected")

    return signals


def detect_soft_divergences_from_fred(
    fred_series: Mapping[str, Any],
    *,
    related_regime: Optional[Union[MacroRegime, str]] = None,
    detected_at: Optional[Union[str, datetime]] = None,
) -> list[SoftDivergenceSignal]:
    """Thin helper: run C4 on an in-memory FRED-like observation map only."""
    return detect_soft_divergences(
        fred_series=fred_series,
        related_regime=related_regime,
        detected_at=detected_at,
    )
