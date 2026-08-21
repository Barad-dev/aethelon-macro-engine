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
  * assign calm / watch / alert labels (next Stage D step)

Assembly is a thin wrap around existing Stage C helpers
(:func:`aethelon.macro.connect.stage_c_from_context`). Incomplete inputs
yield a valid empty or partial package; assembly never raises to callers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Union

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


def _now_utc_z() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class AnalysisPackage(_MacroBase):
    """
    Reusable analysis snapshot assembled from Stage C outputs.

    ``status`` / ``status_note`` are reserved short text slots for a later
    Stage D step. They stay empty here so this contract can ship first.
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
        description="Reserved short status label; empty until the next Stage D step",
    )
    status_note: str = Field(
        default="",
        description="Reserved one-line status text; empty until the next Stage D step",
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


def _from_stage_c_result(result: StageCResult) -> AnalysisPackage:
    """Map a Stage C snapshot onto the package contract (status fields empty)."""
    as_of = _to_utc_z(result.as_of) or _now_utc_z()
    return AnalysisPackage(
        regime=result.regime,
        hard_invalidations=list(result.hard_invalidations),
        soft_divergences=list(result.soft_divergences),
        shocks=list(result.shocks),
        as_of=as_of,
        status="",
        status_note="",
        errors=list(result.errors),
    )


def _empty_package(*, as_of: str, errors: list[str]) -> AnalysisPackage:
    return AnalysisPackage(
        regime=None,
        hard_invalidations=[],
        soft_divergences=[],
        shocks=[],
        as_of=as_of,
        status="",
        status_note="",
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

    Calls :func:`stage_c_from_context` only (bridge + C2–C5 runner). Does
    not invent status labels.

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
        lists and empty status text. Assembly failures are recorded in
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
        "package: as_of=%s regime=%s hard=%s soft=%s shocks=%s errors=%s",
        package.as_of,
        None
        if package.regime is None
        else getattr(package.regime.regime, "value", package.regime.regime),
        len(package.hard_invalidations),
        len(package.soft_divergences),
        len(package.shocks),
        len(package.errors),
    )
    return package
