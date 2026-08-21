# -*- coding: utf-8 -*-
"""
aethelon.macro.package — Stage D analysis package
=================================================
Stable envelope for later product layers (GUI, alerts, local AI audit).

This module does **not**:
  * change C2–C5 detector logic
  * import ``news_engine`` or GUI
  * write to SQLite
  * parse headlines
  * invent new detectors

Assembly is a thin wrap around existing Stage C helpers
(:func:`aethelon.macro.connect.stage_c_from_context`). Consumer status
(``CALM`` / ``WATCH`` / ``ALERT`` / ``SHOCK``) is filled from the
resulting signal lists. Incomplete inputs yield a valid empty or
partial package; assembly never raises to callers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Mapping, Optional, Sequence, Union

from pydantic import Field, field_validator

from aethelon.core.logger import get_logger
from aethelon.macro.connect import stage_c_from_context
from aethelon.macro.runner import StageCResult
from aethelon.macro.schemas import (
    ExogenousShockSignal,
    HardInvalidationSignal,
    RegimeResult,
    SoftDivergenceSignal,
    _MacroBase,
    _str_list,
    _to_utc_z,
)
from aethelon.macro.shock import ShockEvent

log = get_logger(__name__)

ConsumerStatus = Literal["CALM", "WATCH", "ALERT", "SHOCK"]

_STATUS_CALM: ConsumerStatus = "CALM"
_STATUS_WATCH: ConsumerStatus = "WATCH"
_STATUS_ALERT: ConsumerStatus = "ALERT"
_STATUS_SHOCK: ConsumerStatus = "SHOCK"


def _now_utc_z() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class AnalysisPackage(_MacroBase):
    """
    Reusable analysis snapshot assembled from Stage C outputs.

    ``status`` is one of ``CALM`` / ``WATCH`` / ``ALERT`` / ``SHOCK``.
    ``status_note`` is one short sentence explaining that label.
    """

    regime: Optional[RegimeResult] = Field(
        default=None,
        description="C2 regime result, or None if classification did not run",
    )
    hard_invalidations: list[HardInvalidationSignal] = Field(
        default_factory=list,
        description="C3 structural-break signals (empty when none / skipped)",
    )
    soft_divergences: list[SoftDivergenceSignal] = Field(
        default_factory=list,
        description="C4 soft-divergence signals (empty when none / skipped)",
    )
    shocks: list[ExogenousShockSignal] = Field(
        default_factory=list,
        description="C5 exogenous-shock signals (empty when none / skipped)",
    )
    as_of: Optional[str] = Field(
        default=None,
        description="Package time as UTC ISO 8601 Z",
    )
    status: str = Field(
        default="",
        description="Consumer label: CALM | WATCH | ALERT | SHOCK",
    )
    status_note: str = Field(
        default="",
        description="One short sentence explaining status",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Non-fatal assembly or Stage C step errors",
    )
    schema_version: str = Field(
        default="analysis_package_v1",
        description="Contract version for forward-compatible consumers",
    )

    @field_validator("as_of", mode="before")
    @classmethod
    def _as_of_z(cls, v: Any) -> Optional[str]:
        return _to_utc_z(v)

    @field_validator("status", "status_note", mode="before")
    @classmethod
    def _status_str(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v).strip()

    @field_validator("errors", mode="before")
    @classmethod
    def _errors(cls, v: Any) -> list[str]:
        return _str_list(v)


def _is_active(signal: Any) -> bool:
    """Missing ``active`` counts as True (C3/C5 default)."""
    flag = getattr(signal, "active", True)
    return bool(flag)


def _lead_title(signals: Sequence[Any], *, score_attr: str) -> str:
    """Title of the highest-scoring signal; fallback if title is blank."""

    def _score(item: Any) -> float:
        raw = getattr(item, score_attr, None)
        try:
            return float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    lead = max(signals, key=_score)
    title = str(getattr(lead, "title", "") or "").strip()
    return title or "unspecified"


def _count_note(kind_one: str, kind_many: str, items: Sequence[Any], *, score_attr: str) -> str:
    title = _lead_title(items, score_attr=score_attr)
    if len(items) == 1:
        return f"{kind_one}: {title}."
    return f"{len(items)} {kind_many}; leading: {title}."


def _status_for(
    shocks: Sequence[ExogenousShockSignal],
    hard: Sequence[HardInvalidationSignal],
    soft: Sequence[SoftDivergenceSignal],
) -> tuple[ConsumerStatus, str]:
    """
    Closed-set consumer label from existing signal lists.

    Priority (first match wins):
      1. any active shock → SHOCK
      2. any active hard invalidation → ALERT
      3. any soft divergence → WATCH
      4. otherwise CALM
    """
    active_shocks = [s for s in shocks if _is_active(s)]
    if active_shocks:
        return (
            _STATUS_SHOCK,
            _count_note(
                "Active exogenous shock",
                "active exogenous shocks",
                active_shocks,
                score_attr="severity",
            ),
        )

    active_hard = [s for s in hard if _is_active(s)]
    if active_hard:
        return (
            _STATUS_ALERT,
            _count_note(
                "Hard invalidation",
                "hard invalidations",
                active_hard,
                score_attr="severity",
            ),
        )

    if soft:
        return (
            _STATUS_WATCH,
            _count_note(
                "Soft divergence",
                "soft divergences",
                soft,
                score_attr="strength",
            ),
        )

    return (
        _STATUS_CALM,
        "No shocks, hard breaks, or soft divergences.",
    )


def _from_stage_c_result(result: StageCResult) -> AnalysisPackage:
    """Map a Stage C snapshot onto the package contract and fill status."""
    as_of = _to_utc_z(result.as_of) or _now_utc_z()
    hard = list(result.hard_invalidations)
    soft = list(result.soft_divergences)
    shocks = list(result.shocks)
    status, status_note = _status_for(shocks, hard, soft)
    return AnalysisPackage(
        regime=result.regime,
        hard_invalidations=hard,
        soft_divergences=soft,
        shocks=shocks,
        as_of=as_of,
        status=status,
        status_note=status_note,
        errors=list(result.errors),
    )


def _empty_package(*, as_of: str, errors: list[str]) -> AnalysisPackage:
    status, status_note = _status_for((), (), ())
    return AnalysisPackage(
        regime=None,
        hard_invalidations=[],
        soft_divergences=[],
        shocks=[],
        as_of=as_of,
        status=status,
        status_note=status_note,
        errors=errors,
    )


def assemble_analysis_package(
    context: Optional[Mapping[str, Any]] = None,
    *,
    macro_state: Optional[Mapping[str, Any]] = None,
    fred_series: Optional[Union[Mapping[str, Any], Iterable[Any]]] = None,
    shock_events: Optional[Iterable[Union[ShockEvent, Mapping[str, Any]]]] = None,
    detected_at: Optional[Union[str, datetime]] = None,
) -> AnalysisPackage:
    """
    Assemble an ``AnalysisPackage`` from existing in-memory engine shapes.

    Calls :func:`stage_c_from_context` only (bridge + C2–C5 runner), then
    fills ``status`` / ``status_note`` from the signal lists.

    Parameters
    ----------
    context:
        Optional live-context-style dict (``macro_state``, optionally
        ``fred_series`` / ``fred``). Read only.
    macro_state:
        Analyzer-style dial dict. Wins over ``context["macro_state"]``
        when both are given.
    fred_series:
        FRED-like ``{series_id: [obs, ...]}`` or NormalizedItem list.
        Pass this when C3/C4 should run; stock live context often omits it.
    shock_events:
        Optional structured C5 events. ``None`` or empty → C5 skipped.
    detected_at:
        Optional UTC Z timestamp forwarded to Stage C.

    Returns
    -------
    AnalysisPackage
        Always a concrete object. Incomplete data yields empty or partial
        lists and ``CALM`` status. Assembly failures are recorded in
        ``errors`` rather than raised.
    """
    try:
        result = stage_c_from_context(
            context,
            macro_state=macro_state,
            fred_series=fred_series,
            shock_events=shock_events,
            detected_at=detected_at,
        )
    except Exception as exc:
        as_of = _to_utc_z(detected_at) or _now_utc_z()
        msg = f"assemble_analysis_package: stage_c_from_context failed: {exc}"
        log.warning("%s", msg)
        return _empty_package(as_of=as_of, errors=[msg])

    package = _from_stage_c_result(result)
    log.info(
        "package: as_of=%s regime=%s status=%s hard=%s soft=%s shocks=%s errors=%s",
        package.as_of,
        None
        if package.regime is None
        else getattr(package.regime.regime, "value", package.regime.regime),
        package.status,
        len(package.hard_invalidations),
        len(package.soft_divergences),
        len(package.shocks),
        len(package.errors),
    )
    return package
