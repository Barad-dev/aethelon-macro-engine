# -*- coding: utf-8 -*-
"""
aethelon.macro.runner — Stage C6.2 offline C2–C5 runner
=======================================================
Takes ``StageCInputs`` (from C6.1) plus optional structured shock events
and returns one result object. Does not change C2–C5 detector logic.

This module does **not**:
  * import ``news_engine`` or GUI
  * write to SQLite
  * parse headlines
  * fetch from the network

Incomplete inputs yield partial results (empty signal lists / low-confidence
regime from C2). A failure in one step does not abort the others.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Union

from aethelon.core.logger import get_logger
from aethelon.macro.bridge import StageCInputs, build_stage_c_inputs
from aethelon.macro.hard_invalidation import detect_hard_invalidations
from aethelon.macro.regime import RegimeInputs, classify_regime
from aethelon.macro.schemas import (
    ExogenousShockSignal,
    HardInvalidationSignal,
    RegimeResult,
    SoftDivergenceSignal,
    _to_utc_z,
)
from aethelon.macro.shock import ShockEvent, isolate_exogenous_shocks
from aethelon.macro.soft_divergence import detect_soft_divergences

log = get_logger(__name__)


def _now_utc_z() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class StageCResult:
    """
    Combined offline snapshot from C2–C5.

    ``regime`` is always a ``RegimeResult`` when C2 ran; ``None`` only if
    classification itself raised (logged, not re-raised). Signal lists are
    empty when nothing fired or that step was skipped/failed.
    """

    regime: Optional[RegimeResult] = None
    hard_invalidations: tuple[HardInvalidationSignal, ...] = ()
    soft_divergences: tuple[SoftDivergenceSignal, ...] = ()
    shocks: tuple[ExogenousShockSignal, ...] = ()
    errors: tuple[str, ...] = ()
    as_of: Optional[str] = None
    schema_version: str = "stage_c_result_v1"

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-primitive dict (ISO Z strings, enum values as strings)."""
        return {
            "schema_version": self.schema_version,
            "as_of": self.as_of,
            "regime": None if self.regime is None else self.regime.to_json_dict(),
            "hard_invalidations": [s.to_json_dict() for s in self.hard_invalidations],
            "soft_divergences": [s.to_json_dict() for s in self.soft_divergences],
            "shocks": [s.to_json_dict() for s in self.shocks],
            "errors": list(self.errors),
        }


def _coerce_inputs(
    inputs: Optional[StageCInputs],
    *,
    macro_state: Optional[Mapping[str, Any]],
    fred_series: Optional[Union[Mapping[str, Any], Iterable[Any]]],
    context: Optional[Mapping[str, Any]],
) -> StageCInputs:
    if isinstance(inputs, StageCInputs):
        return inputs
    if inputs is None:
        return build_stage_c_inputs(
            macro_state=macro_state,
            fred_series=fred_series,  # type: ignore[arg-type]
            context=context,
        )
    log.debug("runner: unrecognized inputs type=%s — using empty StageCInputs", type(inputs))
    return StageCInputs(regime=RegimeInputs(), fred_series={}, series_changes=())


def run_stage_c(
    inputs: Optional[StageCInputs] = None,
    *,
    shock_events: Optional[Iterable[Union[ShockEvent, Mapping[str, Any]]]] = None,
    macro_state: Optional[Mapping[str, Any]] = None,
    fred_series: Optional[Union[Mapping[str, Any], Iterable[Any]]] = None,
    context: Optional[Mapping[str, Any]] = None,
    detected_at: Optional[Union[str, datetime]] = None,
) -> StageCResult:
    """
    Run C2–C5 on bridged in-memory inputs.

    Parameters
    ----------
    inputs:
        ``StageCInputs`` from :func:`build_stage_c_inputs`. If omitted,
        ``macro_state`` / ``fred_series`` / ``context`` are bridged first
        (C6.1, read-only).
    shock_events:
        Optional structured C5 events. Omitted or empty → no shock pass
        (no headline NLP).
    detected_at:
        Shared UTC Z timestamp for C3–C5. Defaults to now (UTC).

    Returns
    -------
    StageCResult
        Always a concrete object. Per-step exceptions are recorded in
        ``errors`` and logged; other steps still run.
    """
    detected = _to_utc_z(detected_at) or _now_utc_z()
    errors: list[str] = []

    bundle = _coerce_inputs(
        inputs,
        macro_state=macro_state,
        fred_series=fred_series,
        context=context,
    )

    regime: Optional[RegimeResult] = None
    related: Optional[str] = None
    try:
        regime = classify_regime(bundle.regime)
        related = str(regime.regime) if regime is not None else None
    except Exception as exc:
        msg = f"C2 classify_regime failed: {exc}"
        errors.append(msg)
        log.warning("%s", msg)

    as_of = None
    if regime is not None:
        as_of = regime.as_of
    if not as_of:
        as_of = _to_utc_z(bundle.regime.as_of)
    if not as_of:
        as_of = detected

    changes = list(bundle.series_changes)
    fred_map = bundle.fred_series or {}

    hard: list[HardInvalidationSignal] = []
    try:
        hard = detect_hard_invalidations(
            changes=changes or None,
            fred_series=fred_map or None,
            related_regime=related,
            detected_at=detected,
        )
    except Exception as exc:
        msg = f"C3 detect_hard_invalidations failed: {exc}"
        errors.append(msg)
        log.warning("%s", msg)

    soft: list[SoftDivergenceSignal] = []
    try:
        soft = detect_soft_divergences(
            changes=changes or None,
            fred_series=fred_map or None,
            related_regime=related,
            detected_at=detected,
        )
    except Exception as exc:
        msg = f"C4 detect_soft_divergences failed: {exc}"
        errors.append(msg)
        log.warning("%s", msg)

    shocks: list[ExogenousShockSignal] = []
    event_list = list(shock_events) if shock_events is not None else []
    if event_list:
        try:
            shocks = isolate_exogenous_shocks(
                event_list,
                related_regime=related,
                detected_at=detected,
            )
        except Exception as exc:
            msg = f"C5 isolate_exogenous_shocks failed: {exc}"
            errors.append(msg)
            log.warning("%s", msg)
    else:
        log.debug("runner: no structured shock events — C5 skipped")

    log.info(
        "runner: regime=%s conf=%s hard=%s soft=%s shocks=%s errors=%s",
        None if regime is None else getattr(regime.regime, "value", regime.regime),
        None if regime is None else regime.confidence,
        len(hard),
        len(soft),
        len(shocks),
        len(errors),
    )
    return StageCResult(
        regime=regime,
        hard_invalidations=tuple(hard),
        soft_divergences=tuple(soft),
        shocks=tuple(shocks),
        errors=tuple(errors),
        as_of=as_of,
    )
