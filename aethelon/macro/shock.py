# -*- coding: utf-8 -*-
"""
aethelon.macro.shock — Stage C5 exogenous shock isolator
========================================================
Pure, offline isolator for **exogenous** events: geopolitical shocks and
emergency central-bank actions. This is *not* a FRED print detector.

Returns ``ExogenousShockSignal`` objects. Empty list when nothing is
clearly a shock or inputs are incomplete.

Design
------
  * Precision over recall: normal scheduled macro (FOMC, CPI, NFP, …)
    never becomes a shock.
  * Does **not** reuse C3/C4 series-threshold logic.
  * No network, database, GUI, or ``news_engine`` imports.
  * Does not change C2 / C3 / C4 behavior.
  * Incomplete or ambiguous events → no signal.
  * Timestamps are UTC ISO 8601 Z via the C1 schema validators.

How an event qualifies
----------------------
A structured event must present at least one **explicit** marker:

  * ``shock_type`` / ``kind`` on the closed allow-list below, or
  * a boolean flag such as ``emergency_cb`` / ``geopolitical`` / ``kinetic``

A title or headline alone is not enough. Rejected kinds (scheduled
prints, regular policy meetings) are dropped even if flags are sloppy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Union

from aethelon.core.logger import get_logger
from aethelon.macro.schemas import ExogenousShockSignal, MacroRegime, _to_utc_z

log = get_logger(__name__)

# =============================================================================
# Closed kind → (shock_type, default severity)
# =============================================================================

# Emergency / unscheduled central-bank operations (not a regular FOMC hike)
_CENTRAL_BANK_KINDS: dict[str, float] = {
    "EMERGENCY_RATE_CUT": 0.90,
    "EMERGENCY_RATE_HIKE": 0.88,
    "EMERGENCY_QE": 0.86,
    "EMERGENCY_LIQUIDITY": 0.88,
    "SWAP_LINE": 0.84,
    "LENDER_OF_LAST_RESORT": 0.90,
    "YCC_EMERGENCY": 0.82,
    "BANK_HOLIDAY": 0.86,
}

# Geopolitical / kinetic / supply-route shocks
_GEOPOLITICAL_KINDS: dict[str, float] = {
    "WAR": 0.94,
    "INVASION": 0.94,
    "MISSILE_STRIKE": 0.88,
    "TERROR_ATTACK": 0.86,
    "SANCTIONS_SHOCK": 0.80,
    "OIL_BLOCKADE": 0.86,
    "STRAIT_CLOSURE": 0.88,
    "NUCLEAR": 0.95,
    "COUP": 0.82,
}

# Other systemic infrastructure / market-structure shocks
_OTHER_KINDS: dict[str, float] = {
    "MARKET_HALT": 0.78,
    "TRADING_HALT_SYSTEMIC": 0.80,
    "SOVEREIGN_DEFAULT": 0.86,
    "PAYMENT_SYSTEM_FAILURE": 0.84,
    "CYBER_CRITICAL": 0.80,
}

# Scheduled / ordinary macro — never a C5 shock
_REJECT_KINDS: frozenset[str] = frozenset(
    {
        "FOMC",
        "FOMC_MEETING",
        "SCHEDULED_FOMC",
        "SCHEDULED_MEETING",
        "REGULAR_HIKE",
        "REGULAR_CUT",
        "CPI",
        "PCE",
        "NFP",
        "PAYROLLS",
        "UNRATE",
        "GDP",
        "PPI",
        "RETAIL_SALES",
        "SPEECH",
        "MINUTES",
        "TESTIMONY",
        "AUCTION",
        "DATA_PRINT",
        "MACRO_PRINT",
    }
)

_KIND_TO_TYPE: dict[str, str] = {}
_KIND_TO_SEV: dict[str, float] = {}
for _k, _s in _CENTRAL_BANK_KINDS.items():
    _KIND_TO_TYPE[_k] = "CENTRAL_BANK"
    _KIND_TO_SEV[_k] = _s
for _k, _s in _GEOPOLITICAL_KINDS.items():
    _KIND_TO_TYPE[_k] = "GEOPOLITICAL"
    _KIND_TO_SEV[_k] = _s
for _k, _s in _OTHER_KINDS.items():
    _KIND_TO_TYPE[_k] = "OTHER"
    _KIND_TO_SEV[_k] = _s

# Aliases folded into the closed set (normalization only, not NLP)
_KIND_ALIASES: dict[str, str] = {
    "EMERGENCY_CUT": "EMERGENCY_RATE_CUT",
    "EMERGENCY_HIKE": "EMERGENCY_RATE_HIKE",
    "QE_EMERGENCY": "EMERGENCY_QE",
    "EMERGENCY_EASING": "EMERGENCY_QE",
    "LIQUIDITY_FACILITY": "EMERGENCY_LIQUIDITY",
    "DISCOUNT_WINDOW_EMERGENCY": "LENDER_OF_LAST_RESORT",
    "LOLR": "LENDER_OF_LAST_RESORT",
    "FX_SWAP": "SWAP_LINE",
    "CENTRAL_BANK_SWAP": "SWAP_LINE",
    "WAR_OUTBREAK": "WAR",
    "MILITARY_INVASION": "INVASION",
    "BLOCKADE": "OIL_BLOCKADE",
    "HORMUZ": "STRAIT_CLOSURE",
    "DEFAULT": "SOVEREIGN_DEFAULT",
    "HALT": "MARKET_HALT",
}


# =============================================================================
# Input container
# =============================================================================

@dataclass(frozen=True)
class ShockEvent:
    """
    Structured exogenous-event description (caller-supplied; no fetching).

    A shock is emitted only when ``kind`` / ``shock_type`` is on the
    allow-list, or when an explicit boolean flag is True. A title alone
    is never enough.
    """

    event_id: Optional[str] = None
    title: Optional[str] = None
    summary: str = ""
    kind: Optional[str] = None
    shock_type: Optional[str] = None
    event_at: Optional[Union[str, datetime]] = None
    as_of: Optional[Union[str, datetime]] = None
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    related_regime: Optional[Union[MacroRegime, str]] = None
    severity_hint: Optional[float] = None
    active: bool = True
    # Explicit flags — any True is a structured marker
    emergency_cb: bool = False
    geopolitical: bool = False
    kinetic: bool = False
    confirmed: Optional[bool] = None


# =============================================================================
# Helpers
# =============================================================================

def _now_utc_z() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _norm_token(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    s = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    return s or None


def _as_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y"):
        return True
    if s in ("0", "false", "no", "n"):
        return False
    return None


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_refs(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        s = value.strip()
        return (s,) if s else ()
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if item is None or item == "":
                continue
            out.append(str(item).strip())
        return tuple(x for x in out if x)
    return ()


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


def _canonical_kind(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    token = _norm_token(raw)
    if token is None:
        return None
    token = _KIND_ALIASES.get(token, token)
    return token


def _flag_kind(event: ShockEvent) -> Optional[str]:
    """Map explicit boolean flags to a default kind (only if kind missing)."""
    if event.kinetic:
        return "WAR"
    if event.geopolitical:
        return "SANCTIONS_SHOCK"
    if event.emergency_cb:
        return "EMERGENCY_LIQUIDITY"
    return None


def _resolve_type_and_severity(
    kind: Optional[str],
    shock_type_hint: Optional[str],
    severity_hint: Optional[float],
) -> Optional[tuple[str, str, float]]:
    """
    Return ``(kind, shock_type, severity)`` or ``None`` if not allow-listed.

    ``shock_type`` hint may only *confirm* a family; it cannot invent a
    kind that is not on the list.
    """
    if kind is None or kind not in _KIND_TO_TYPE:
        return None

    shock_type = _KIND_TO_TYPE[kind]
    hinted = _norm_token(shock_type_hint)
    if hinted in ("GEOPOLITICAL", "CENTRAL_BANK", "OTHER") and hinted != shock_type:
        # Caller family disagrees with the kind map — treat as ambiguous
        return None

    default_sev = _KIND_TO_SEV[kind]
    if severity_hint is None:
        sev = default_sev
    else:
        # Never inflate above the documented default for that kind
        hint = max(0.0, min(1.0, float(severity_hint)))
        sev = min(default_sev, hint)
        if sev < 0.20:
            # Near-zero hint means "not actually a shock"
            return None
    return kind, shock_type, sev


def _coerce_event(raw: Union[ShockEvent, Mapping[str, Any], Any]) -> Optional[ShockEvent]:
    if isinstance(raw, ShockEvent):
        return raw
    if not isinstance(raw, Mapping):
        log.debug("shock: ignore non-mapping event type=%s", type(raw))
        return None

    kind = raw.get("kind") or raw.get("event_kind") or raw.get("tag")
    shock_type = raw.get("shock_type") or raw.get("type")
    return ShockEvent(
        event_id=(
            str(raw["event_id"]).strip()
            if raw.get("event_id") not in (None, "")
            else (
                str(raw["id"]).strip()
                if raw.get("id") not in (None, "")
                else None
            )
        ),
        title=raw.get("title") or raw.get("headline") or raw.get("name"),  # type: ignore[arg-type]
        summary=str(raw.get("summary") or raw.get("text") or ""),
        kind=kind if kind is None else str(kind),
        shock_type=shock_type if shock_type is None else str(shock_type),
        event_at=raw.get("event_at") or raw.get("occurred_at") or raw.get("date"),  # type: ignore[arg-type]
        as_of=raw.get("as_of"),  # type: ignore[arg-type]
        source_refs=_str_refs(
            raw.get("source_refs") or raw.get("sources") or raw.get("refs")
        ),
        related_regime=raw.get("related_regime") or raw.get("regime"),  # type: ignore[arg-type]
        severity_hint=_as_float(raw.get("severity_hint") or raw.get("severity")),
        active=False if _as_bool(raw.get("active")) is False else True,
        emergency_cb=bool(_as_bool(raw.get("emergency_cb")) or False),
        geopolitical=bool(_as_bool(raw.get("geopolitical")) or False),
        kinetic=bool(_as_bool(raw.get("kinetic")) or False),
        confirmed=_as_bool(raw.get("confirmed")),
    )


def _stable_id(event: ShockEvent, kind: str, event_at: Optional[str]) -> str:
    if event.event_id:
        return f"shock:{event.event_id}"
    day = (event_at or "undated")[:10]
    slug = kind.lower()
    return f"shock:{slug}:{day}"


# =============================================================================
# Public API
# =============================================================================

def isolate_exogenous_shocks(
    events: Optional[Iterable[Union[ShockEvent, Mapping[str, Any]]]] = None,
    *,
    related_regime: Optional[Union[MacroRegime, str]] = None,
    detected_at: Optional[Union[str, datetime]] = None,
) -> list[ExogenousShockSignal]:
    """
    Isolate exogenous shocks from structured event records.

    Parameters
    ----------
    events:
        Iterable of ``ShockEvent`` or dicts. Useful keys: ``event_id``,
        ``title``, ``kind`` / ``shock_type``, ``event_at``, ``source_refs``,
        ``emergency_cb``, ``geopolitical``, ``kinetic``, ``severity_hint``.
    related_regime:
        Optional Stage C regime attached when the event itself has none.
    detected_at:
        Detection timestamp (UTC Z). Defaults to now (UTC).

    Returns
    -------
    list[ExogenousShockSignal]
        Empty when nothing is on the allow-list or inputs are incomplete.
        Ordered by descending severity, then ``signal_id``.
    """
    detected = _to_utc_z(detected_at) or _now_utc_z()
    default_regime = _regime_or_none(related_regime)
    signals: list[ExogenousShockSignal] = []
    seen: set[str] = set()

    for raw in events or []:
        event = _coerce_event(raw)
        if event is None:
            continue

        if event.confirmed is False:
            log.debug("shock: skip explicitly unconfirmed event_id=%s", event.event_id)
            continue

        kind_in = _canonical_kind(event.kind) or _canonical_kind(event.shock_type)
        if kind_in in _REJECT_KINDS:
            log.debug("shock: reject scheduled/macro kind=%s", kind_in)
            continue

        if kind_in is None or kind_in not in _KIND_TO_TYPE:
            kind_in = _flag_kind(event)

        resolved = _resolve_type_and_severity(
            kind_in, event.shock_type, event.severity_hint
        )
        if resolved is None:
            log.debug(
                "shock: skip ambiguous/incomplete event_id=%s kind=%s",
                event.event_id,
                kind_in,
            )
            continue

        kind, shock_type, severity = resolved
        event_at = _to_utc_z(event.event_at)
        as_of = _to_utc_z(event.as_of) or event_at or detected
        title = (event.title or "").strip() or f"{kind.replace('_', ' ').title()} shock"
        summary = (event.summary or "").strip() or (
            f"Structured {shock_type} shock tagged {kind}."
        )
        regime = _regime_or_none(event.related_regime) or default_regime
        signal_id = _stable_id(event, kind, event_at)

        if signal_id in seen:
            continue
        seen.add(signal_id)

        chain = [
            "Stage C5 isolator: exogenous events only (not FRED print rules)",
            f"kind={kind} → shock_type={shock_type}",
            f"severity={severity:.2f} (hint={event.severity_hint!r})",
        ]
        if event.emergency_cb or event.geopolitical or event.kinetic:
            chain.append(
                "flags: "
                + ", ".join(
                    name
                    for name, on in (
                        ("emergency_cb", event.emergency_cb),
                        ("geopolitical", event.geopolitical),
                        ("kinetic", event.kinetic),
                    )
                    if on
                )
            )
        if event_at:
            chain.append(f"event_at={event_at}")
        else:
            chain.append("event_at missing — used detection/as_of for assessment time")
        if event.source_refs:
            chain.append(f"source_refs={len(event.source_refs)}")

        signals.append(
            ExogenousShockSignal(
                signal_id=signal_id,
                title=title,
                summary=summary,
                shock_type=shock_type,
                severity=severity,
                active=bool(event.active),
                source_refs=list(event.source_refs),
                related_regime=regime,
                detected_at=detected,
                event_at=event_at,
                as_of=as_of,
                reasoning_chain=chain,
                market_narrative=None,
                details={
                    "kind": kind,
                    "confirmed": event.confirmed,
                },
            )
        )

    signals.sort(key=lambda s: (-float(s.severity), s.signal_id))

    if signals:
        log.info(
            "shock: isolated %s signal(s): %s",
            len(signals),
            ", ".join(s.signal_id for s in signals),
        )
    else:
        log.debug("shock: no exogenous shocks isolated")

    return signals


def isolate_exogenous_shocks_from_dicts(
    events: Iterable[Mapping[str, Any]],
    *,
    related_regime: Optional[Union[MacroRegime, str]] = None,
    detected_at: Optional[Union[str, datetime]] = None,
) -> list[ExogenousShockSignal]:
    """Thin helper for a list of in-memory event dicts only."""
    return isolate_exogenous_shocks(
        events,
        related_regime=related_regime,
        detected_at=detected_at,
    )
