# -*- coding: utf-8 -*-
"""
aethelon.macro.schemas — Stage C1 macro regime & invalidation contracts
======================================================================
Pydantic v2 data contracts only. No classification, scoring, or I/O.

Models
------
  • MacroRegime              — closed set of four textbook regimes
  • RegimeResult             — classified regime + confidence + explanation
  • HardInvalidationSignal   — structural break in core fundamentals
  • SoftDivergenceSignal     — temporary noise vs real fundamental change
  • ExogenousShockSignal     — geopolitical / emergency policy shocks

Style
-----
Aligned with ``models.desk_schemas``:

  - JSON-serializable primitives only (ISO strings for timestamps)
  - ``extra='ignore'`` for stable wire format
  - ``to_json_dict()`` / ``to_json()`` helpers

Timestamps
----------
All timestamp fields are timezone-aware UTC in ISO 8601 **Z** form
(e.g. ``2026-08-11T12:00:00Z``). Naive datetimes are treated as UTC
and normalized to Z; local-offset inputs are converted to UTC first.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

# JSON-safe aliases (same spirit as desk_schemas)
JsonScalar = Union[str, int, float, bool, None]
JsonMap = dict[str, Any]


# =============================================================================
# Macro regimes (closed set for Stage C)
# =============================================================================

class MacroRegime(str, Enum):
    """
    Textbook growth/inflation regimes used by Stage C logic.

    Values are uppercase string enums so JSON dumps stay plain strings.
    """

    REFLATION = "REFLATION"
    STAGFLATION = "STAGFLATION"
    GOLDILOCKS = "GOLDILOCKS"
    DEFLATION = "DEFLATION"


# =============================================================================
# Shared base + helpers
# =============================================================================

class _MacroBase(BaseModel):
    """
    Base for Stage C macro contracts.

    ``extra='ignore'`` keeps older or partial payloads from breaking
    consumers; unknown keys are dropped rather than echoed.
    """

    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True,
        validate_assignment=False,
        from_attributes=True,
        populate_by_name=True,
        use_enum_values=True,
        ser_json_timedelta="iso8601",
    )

    def to_json_dict(self) -> dict[str, Any]:
        """Strict JSON-primitive dict (ISO Z strings, no datetime objects)."""
        return self.model_dump(mode="json", by_alias=True)

    def to_json(self, *, indent: Optional[int] = None) -> str:
        """UTF-8 JSON string for file / socket / future IPC."""
        return self.model_dump_json(by_alias=True, indent=indent)


def _to_utc_z(value: Any) -> Optional[str]:
    """
    Coerce a timestamp to timezone-aware UTC ISO 8601 with a trailing ``Z``.

    Accepts ``datetime`` / ``date``, epoch seconds or ms, or ISO-ish strings.
    Naive datetimes are assumed already-UTC (not local wall-clock).
    Empty or unparseable values become ``None``.
    """
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat(timespec="seconds").replace("+00:00", "Z")

    if isinstance(value, date):
        # Date-only: midnight UTC on that calendar day
        dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        return dt.isoformat(timespec="seconds").replace("+00:00", "Z")

    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return (
                datetime.fromtimestamp(ts, tz=timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            return None

    s = str(value).strip()
    if not s:
        return None

    # Normalize "YYYY-MM-DD HH:MM:SS" → ISO separator
    if len(s) >= 19 and s[10] == " ":
        s = s[:10] + "T" + s[11:]

    # Already Z-terminated
    if s.endswith("Z") or s.endswith("z"):
        body = s[:-1]
        try:
            # Support fractional seconds if present
            if "." in body:
                dt = datetime.fromisoformat(body)
            else:
                dt = datetime.fromisoformat(body)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        except ValueError:
            return s.upper() if s.endswith("z") else s

    # Offset form (+00:00 / -05:00) or bare naive ISO
    try:
        # fromisoformat handles +00:00 offsets; strip trailing Z already handled
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        # Leave non-ISO strings alone only if they look intentional; prefer None
        return None


def _clamp_confidence(v: Any) -> Optional[float]:
    """Confidence in [0, 1]. Values > 1 treated as percent and scaled."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f > 1.0:
        f = f / 100.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _str_list(v: Any) -> list[str]:
    """Coerce optional list-ish input into a list of short strings."""
    if v is None or v == "":
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    try:
        return [str(x).strip() for x in v if x is not None and str(x).strip()]
    except TypeError:
        return []


# =============================================================================
# Regime result
# =============================================================================

class RegimeResult(_MacroBase):
    """
    Outcome of a macro-regime classification step (contracts only).

    Carries the chosen ``MacroRegime``, a confidence score in [0, 1],
    and a short human-readable explanation. Optional ``reasoning_chain``
    and ``market_narrative`` are reserved for later Stage C steps.
    """

    regime: MacroRegime = Field(
        ...,
        description="One of REFLATION | STAGFLATION | GOLDILOCKS | DEFLATION",
    )
    confidence: float = Field(
        ...,
        description="Classifier confidence in [0, 1]",
        ge=0.0,
        le=1.0,
    )
    explanation: str = Field(
        ...,
        description="Short plain-language reason for this regime label",
        min_length=1,
    )
    as_of: Optional[str] = Field(
        default=None,
        description="Observation time as UTC ISO 8601 Z",
    )
    reasoning_chain: list[str] = Field(
        default_factory=list,
        description="Optional ordered short steps that led to this result",
    )
    market_narrative: Optional[str] = Field(
        default=None,
        description="Optional short market narrative for desk consumers",
    )
    schema_version: str = Field(
        default="macro_regime_v1",
        description="Contract version for forward-compatible consumers",
    )

    @field_validator("regime", mode="before")
    @classmethod
    def _norm_regime(cls, v: Any) -> Any:
        if v is None or v == "":
            raise ValueError("regime is required")
        if isinstance(v, MacroRegime):
            return v
        # Normalize casing/separators only — no semantic aliases (contracts only).
        return str(v).strip().upper().replace(" ", "_").replace("-", "_")

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> float:
        f = _clamp_confidence(v)
        if f is None:
            raise ValueError("confidence is required and must be numeric")
        return f

    @field_validator("explanation", mode="before")
    @classmethod
    def _explanation_str(cls, v: Any) -> str:
        s = str(v or "").strip()
        if not s:
            raise ValueError("explanation is required")
        return s

    @field_validator("as_of", mode="before")
    @classmethod
    def _as_of_z(cls, v: Any) -> Optional[str]:
        return _to_utc_z(v)

    @field_validator("reasoning_chain", mode="before")
    @classmethod
    def _chain(cls, v: Any) -> list[str]:
        return _str_list(v)

    @field_validator("market_narrative", mode="before")
    @classmethod
    def _narrative(cls, v: Any) -> Optional[str]:
        if v is None or v == "":
            return None
        return str(v).strip() or None


# =============================================================================
# Invalidation / divergence / shock signals
# =============================================================================

class HardInvalidationSignal(_MacroBase):
    """
    Structural shift that would invalidate the active macro thesis.

    Intended for core FRED / hard-data breaks (Stage C step 3). This is a
    data contract only — no detection logic lives here.
    """

    signal_id: str = Field(
        ...,
        description="Stable identifier for this invalidation event",
        min_length=1,
    )
    title: str = Field(
        ...,
        description="Short human-readable label",
        min_length=1,
    )
    summary: str = Field(
        default="",
        description="Brief description of the structural break",
    )
    severity: float = Field(
        default=1.0,
        description="Relative severity in [0, 1] (1.0 = full invalidation)",
        ge=0.0,
        le=1.0,
    )
    active: bool = Field(
        default=True,
        description="Whether this signal currently invalidates the thesis",
    )
    indicator_ids: list[str] = Field(
        default_factory=list,
        description="Related series / indicator ids (e.g. FRED codes)",
    )
    related_regime: Optional[MacroRegime] = Field(
        default=None,
        description="Regime that was active or threatened, if known",
    )
    detected_at: Optional[str] = Field(
        default=None,
        description="Detection time as UTC ISO 8601 Z",
    )
    as_of: Optional[str] = Field(
        default=None,
        description="Data observation time as UTC ISO 8601 Z",
    )
    reasoning_chain: list[str] = Field(
        default_factory=list,
        description="Optional ordered short steps supporting this signal",
    )
    market_narrative: Optional[str] = Field(
        default=None,
        description="Optional short market narrative for desk consumers",
    )
    details: Optional[JsonMap] = Field(
        default=None,
        description="Optional free-form JSON-safe extras (no secrets)",
    )
    schema_version: str = Field(
        default="hard_invalidation_v1",
        description="Contract version for forward-compatible consumers",
    )

    @field_validator("signal_id", "title", mode="before")
    @classmethod
    def _required_str(cls, v: Any) -> str:
        s = str(v or "").strip()
        if not s:
            raise ValueError("field is required")
        return s

    @field_validator("summary", mode="before")
    @classmethod
    def _summary_str(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v).strip()

    @field_validator("severity", mode="before")
    @classmethod
    def _sev(cls, v: Any) -> float:
        f = _clamp_confidence(v)
        return 1.0 if f is None else f

    @field_validator("indicator_ids", "reasoning_chain", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _str_list(v)

    @field_validator("related_regime", mode="before")
    @classmethod
    def _opt_regime(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, MacroRegime):
            return v
        return str(v).strip().upper().replace(" ", "_").replace("-", "_")

    @field_validator("detected_at", "as_of", mode="before")
    @classmethod
    def _ts_z(cls, v: Any) -> Optional[str]:
        return _to_utc_z(v)

    @field_validator("market_narrative", mode="before")
    @classmethod
    def _narrative(cls, v: Any) -> Optional[str]:
        if v is None or v == "":
            return None
        return str(v).strip() or None


class SoftDivergenceSignal(_MacroBase):
    """
    Soft divergence: temporary noise vs a real fundamental change.

    Used by Stage C step 4. Contract only — no scoring or NLP here.
    """

    signal_id: str = Field(
        ...,
        description="Stable identifier for this divergence event",
        min_length=1,
    )
    title: str = Field(
        ...,
        description="Short human-readable label",
        min_length=1,
    )
    summary: str = Field(
        default="",
        description="Brief description of the divergence",
    )
    strength: float = Field(
        default=0.5,
        description="Divergence strength in [0, 1]",
        ge=0.0,
        le=1.0,
    )
    is_noise: Optional[bool] = Field(
        default=None,
        description="True if judged temporary noise; False if real shift; None if unknown",
    )
    indicator_ids: list[str] = Field(
        default_factory=list,
        description="Related series / indicator ids",
    )
    related_regime: Optional[MacroRegime] = Field(
        default=None,
        description="Regime context, if known",
    )
    detected_at: Optional[str] = Field(
        default=None,
        description="Detection time as UTC ISO 8601 Z",
    )
    as_of: Optional[str] = Field(
        default=None,
        description="Data observation time as UTC ISO 8601 Z",
    )
    reasoning_chain: list[str] = Field(
        default_factory=list,
        description="Optional ordered short steps supporting this signal",
    )
    market_narrative: Optional[str] = Field(
        default=None,
        description="Optional short market narrative for desk consumers",
    )
    details: Optional[JsonMap] = Field(
        default=None,
        description="Optional free-form JSON-safe extras (no secrets)",
    )
    schema_version: str = Field(
        default="soft_divergence_v1",
        description="Contract version for forward-compatible consumers",
    )

    @field_validator("signal_id", "title", mode="before")
    @classmethod
    def _required_str(cls, v: Any) -> str:
        s = str(v or "").strip()
        if not s:
            raise ValueError("field is required")
        return s

    @field_validator("summary", mode="before")
    @classmethod
    def _summary_str(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v).strip()

    @field_validator("strength", mode="before")
    @classmethod
    def _strength(cls, v: Any) -> float:
        f = _clamp_confidence(v)
        return 0.5 if f is None else f

    @field_validator("indicator_ids", "reasoning_chain", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _str_list(v)

    @field_validator("related_regime", mode="before")
    @classmethod
    def _opt_regime(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, MacroRegime):
            return v
        return str(v).strip().upper().replace(" ", "_").replace("-", "_")

    @field_validator("detected_at", "as_of", mode="before")
    @classmethod
    def _ts_z(cls, v: Any) -> Optional[str]:
        return _to_utc_z(v)

    @field_validator("market_narrative", mode="before")
    @classmethod
    def _narrative(cls, v: Any) -> Optional[str]:
        if v is None or v == "":
            return None
        return str(v).strip() or None


class ExogenousShockSignal(_MacroBase):
    """
    Exogenous shock: geopolitical or emergency central-bank style events.

    Used by Stage C step 5 (shock isolator). Contract only.
    """

    signal_id: str = Field(
        ...,
        description="Stable identifier for this shock event",
        min_length=1,
    )
    title: str = Field(
        ...,
        description="Short human-readable label",
        min_length=1,
    )
    summary: str = Field(
        default="",
        description="Brief description of the shock",
    )
    shock_type: str = Field(
        default="UNSPECIFIED",
        description="Coarse type tag, e.g. GEOPOLITICAL | CENTRAL_BANK | OTHER",
    )
    severity: float = Field(
        default=0.5,
        description="Relative severity in [0, 1]",
        ge=0.0,
        le=1.0,
    )
    active: bool = Field(
        default=True,
        description="Whether the shock is still considered active",
    )
    source_refs: list[str] = Field(
        default_factory=list,
        description="Optional source ids / URLs / headline refs (no secrets)",
    )
    related_regime: Optional[MacroRegime] = Field(
        default=None,
        description="Regime context, if known",
    )
    detected_at: Optional[str] = Field(
        default=None,
        description="Detection time as UTC ISO 8601 Z",
    )
    event_at: Optional[str] = Field(
        default=None,
        description="Event time as UTC ISO 8601 Z (when the shock occurred)",
    )
    as_of: Optional[str] = Field(
        default=None,
        description="Assessment time as UTC ISO 8601 Z",
    )
    reasoning_chain: list[str] = Field(
        default_factory=list,
        description="Optional ordered short steps supporting this signal",
    )
    market_narrative: Optional[str] = Field(
        default=None,
        description="Optional short market narrative for desk consumers",
    )
    details: Optional[JsonMap] = Field(
        default=None,
        description="Optional free-form JSON-safe extras (no secrets)",
    )
    schema_version: str = Field(
        default="exogenous_shock_v1",
        description="Contract version for forward-compatible consumers",
    )

    @field_validator("signal_id", "title", mode="before")
    @classmethod
    def _required_str(cls, v: Any) -> str:
        s = str(v or "").strip()
        if not s:
            raise ValueError("field is required")
        return s

    @field_validator("summary", mode="before")
    @classmethod
    def _summary_str(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v).strip()

    @field_validator("shock_type", mode="before")
    @classmethod
    def _shock_type(cls, v: Any) -> str:
        if v is None or v == "":
            return "UNSPECIFIED"
        return str(v).strip().upper().replace(" ", "_").replace("-", "_")

    @field_validator("severity", mode="before")
    @classmethod
    def _sev(cls, v: Any) -> float:
        f = _clamp_confidence(v)
        return 0.5 if f is None else f

    @field_validator("source_refs", "reasoning_chain", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _str_list(v)

    @field_validator("related_regime", mode="before")
    @classmethod
    def _opt_regime(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, MacroRegime):
            return v
        return str(v).strip().upper().replace(" ", "_").replace("-", "_")

    @field_validator("detected_at", "event_at", "as_of", mode="before")
    @classmethod
    def _ts_z(cls, v: Any) -> Optional[str]:
        return _to_utc_z(v)

    @field_validator("market_narrative", mode="before")
    @classmethod
    def _narrative(cls, v: Any) -> Optional[str]:
        if v is None or v == "":
            return None
        return str(v).strip() or None
