# -*- coding: utf-8 -*-
"""
aethelon.macro.connect — Stage C6.3 read-only soft connection
=============================================================
Thin helper: existing in-memory project shapes → ``StageCResult``.

Does exactly two things:

  1. :func:`aethelon.macro.bridge.build_stage_c_inputs`
  2. :func:`aethelon.macro.runner.run_stage_c`

No SQLite writes, no GUI, no ``news_engine`` import, no headline NLP.
C2–C5 and C6.1/C6.2 logic are not modified.

Typical call shapes
-------------------
  * Live context dict (``get_news_context``-like): ``macro_state`` key
  * Explicit ``macro_state`` + optional FRED store map
  * Optional structured ``shock_events`` — omitted → C5 skipped
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Union

from aethelon.core.logger import get_logger
from aethelon.macro.bridge import build_stage_c_inputs
from aethelon.macro.runner import StageCResult, run_stage_c
from aethelon.macro.shock import ShockEvent

log = get_logger(__name__)


def stage_c_from_context(
    context: Optional[Mapping[str, Any]] = None,
    *,
    macro_state: Optional[Mapping[str, Any]] = None,
    fred_series: Optional[Union[Mapping[str, Any], Iterable[Any]]] = None,
    shock_events: Optional[Iterable[Union[ShockEvent, Mapping[str, Any]]]] = None,
    detected_at: Optional[Union[str, datetime]] = None,
) -> StageCResult:
    """
    Produce a ``StageCResult`` from existing in-memory engine shapes.

    Parameters
    ----------
    context:
        Optional dict in the live-context style (keys such as
        ``macro_state``, optionally ``fred_series`` / ``fred`` if a
        caller attached the store map). Read only.
    macro_state:
        Analyzer-style dial dict. Wins over ``context["macro_state"]``
        when both are given (C6.1 behaviour).
    fred_series:
        FRED-like ``{series_id: [obs, ...]}`` or NormalizedItem list.
        The stock ``get_news_context`` payload does **not** include raw
        observations — pass the store map here when C3/C4 should run.
    shock_events:
        Optional structured C5 events. ``None`` or empty → C5 skipped.
    detected_at:
        Optional UTC Z timestamp forwarded to the runner.

    Returns
    -------
    StageCResult
        Always a concrete object. Incomplete data yields partial/empty
        signal lists (same as C6.2). Never writes to disk.
    """
    inputs = build_stage_c_inputs(
        macro_state=macro_state,
        fred_series=fred_series,
        context=context,
    )
    log.info(
        "connect: bridging then run_stage_c (shock_events %s)",
        "provided" if shock_events is not None else "absent",
    )
    return run_stage_c(
        inputs,
        shock_events=shock_events,
        detected_at=detected_at,
    )
