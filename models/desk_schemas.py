# -*- coding: utf-8 -*-
"""
models/desk_schemas.py — Pydantic v2 Research Desk contracts (Stage A refined)
==============================================================================
Strict, JSON-serializable payloads for:

  • Qt Research Desk UI
  • Future Go/Rust IPC (zero-overhead: plain JSON types only)
  • MasterThesis synthesis consumers

Polyglot rules
--------------
  - Timestamps are ISO 8601 *strings* (never datetime objects in dumps)
  - No bytes, Decimal, set, or nested custom objects outside this module
  - model_dump(mode="json") / to_json() produce standard JSON primitives

Pydantic v2:
  - model_config = ConfigDict(...)
  - field_validator / model_validator
  - Field(alias=...) for reserved keys (`from` / `to`)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# JSON-safe scalar / structure aliases (Go/Rust IPC contract)
JsonScalar = Union[str, int, float, bool, None]
JsonMap = dict[str, Any]  # values must themselves be JSON-safe after mode="json"

BiasDirection = Literal["BULLISH", "BEARISH", "NEUTRAL", "MIXED", "UNKNOWN"]
HorizonTag = Literal["immediate", "intraday", "swing", "macro", "24h", "72h"]


# =============================================================================
# Shared base + helpers
# =============================================================================

class _DeskBase(BaseModel):
    """
    Base for all desk models.

    extra='ignore' keeps the wire format stable for polyglot consumers
    (unknown keys from older engines are dropped, not echoed).
    """

    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True,
        validate_assignment=False,
        from_attributes=True,
        populate_by_name=True,
        ser_json_timedelta="iso8601",
    )

    def to_json_dict(self) -> dict[str, Any]:
        """Strict JSON-primitive dict (ISO strings, no datetime objects)."""
        return self.model_dump(mode="json", by_alias=True)

    def to_json(self, *, indent: Optional[int] = None) -> str:
        """UTF-8 JSON string for file / socket / Go-Rust IPC."""
        return self.model_dump_json(by_alias=True, indent=indent)


def _to_iso8601(value: Any) -> Optional[str]:
    """
    Coerce timestamps to ISO 8601 strings.

    Accepts datetime/date, epoch seconds/ms, or already-ISO strings.
    Returns None for empty/unparseable values.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            # Treat naive as local wall-clock; emit without forcing UTC rewrite
            return dt.isoformat(timespec="seconds")
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        # Heuristic: ms vs s epochs
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    # Normalize common "YYYY-MM-DD HH:MM:SS" → ISO
    if len(s) >= 19 and s[10] == " ":
        s = s[:10] + "T" + s[11:]
    return s


def _opt_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _opt_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _clamp_prob(v: Any) -> Optional[float]:
    """Probability in [0, 1]. Values > 1 treated as percent and scaled."""
    f = _opt_float(v)
    if f is None:
        return None
    if f > 1.0:
        f = f / 100.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


# =============================================================================
# Market impact (shared by macro + thesis)
# =============================================================================

class MarketImpact(_DeskBase):
    """
    Direct directional bias, probability, and execution implications.

    Designed for charting bridges and discretionary desks — not order tickets.
    """

    direction: BiasDirection = "UNKNOWN"
    probability: Optional[float] = Field(
        default=None,
        description="Conviction/probability in [0, 1]",
        ge=0.0,
        le=1.0,
    )
    horizon: Optional[str] = Field(
        default=None,
        description="Time horizon tag, e.g. swing | macro | 72h",
    )
    execution_note: Optional[str] = Field(
        default=None,
        description="How a discretionary trader might frame risk / timing",
    )
    symbols_affected: list[str] = Field(default_factory=list)
    invalidation: Optional[str] = Field(
        default=None,
        description="What would void this directional read",
    )
    not_advice: bool = Field(
        default=True,
        description="Always true — macro context only, never trade instructions",
    )

    @field_validator("direction", mode="before")
    @classmethod
    def _norm_direction(cls, v: Any) -> str:
        if v is None or v == "":
            return "UNKNOWN"
        s = str(v).strip().upper()
        aliases = {
            "BULL": "BULLISH",
            "BEAR": "BEARISH",
            "LONG": "BULLISH",
            "SHORT": "BEARISH",
            "FLAT": "NEUTRAL",
            "NONE": "NEUTRAL",
        }
        s = aliases.get(s, s)
        if s not in ("BULLISH", "BEARISH", "NEUTRAL", "MIXED", "UNKNOWN"):
            return "UNKNOWN"
        return s

    @field_validator("probability", mode="before")
    @classmethod
    def _prob(cls, v: Any) -> Optional[float]:
        return _clamp_prob(v)

    @field_validator("symbols_affected", mode="before")
    @classmethod
    def _syms(cls, v: Any) -> list[str]:
        if not v:
            return []
        if isinstance(v, str):
            return [v.upper()]
        return [str(x).upper() for x in v if x]


# =============================================================================
# Macro state
# =============================================================================

class MacroDials(_DeskBase):
    growth: Optional[str] = None
    inflation: Optional[str] = None
    policy: Optional[str] = None
    liquidity: Optional[str] = None
    risk: Optional[str] = None


class MacroScores(_DeskBase):
    growth: Optional[float] = None
    inflation: Optional[float] = None
    policy: Optional[float] = None
    liquidity: Optional[float] = None
    risk: Optional[float] = None

    @field_validator(
        "growth", "inflation", "policy", "liquidity", "risk", mode="before"
    )
    @classmethod
    def _scores_float(cls, v: Any) -> Optional[float]:
        return _opt_float(v)


class MacroStateSection(_DeskBase):
    """Section 1 — textbook MacroState + plain-language + market impact."""

    ok: bool = True
    available: bool = False
    section: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None
    regime: Optional[str] = None
    confidence: Optional[float] = None
    lesson: Optional[str] = None
    # NEW — polyglot / executive fields
    layman_meaning: Optional[str] = Field(
        default=None,
        description="Plain-language explanation of the macro regime logic",
    )
    market_impact: Optional[MarketImpact] = Field(
        default=None,
        description="Directional bias + probability + execution framing",
    )
    dials: MacroDials = Field(default_factory=MacroDials)
    scores: MacroScores = Field(default_factory=MacroScores)
    summary_line: Optional[str] = None
    as_of: Optional[str] = Field(
        default=None,
        description="ISO 8601 date or datetime string",
    )
    rules_version: Optional[str] = None
    raw: Optional[JsonMap] = None
    data: Optional[Any] = None

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v: Any) -> Optional[float]:
        return _clamp_prob(v)

    @field_validator("as_of", mode="before")
    @classmethod
    def _iso_as_of(cls, v: Any) -> Optional[str]:
        return _to_iso8601(v)

    @field_validator("dials", mode="before")
    @classmethod
    def _empty_dials(cls, v: Any) -> Any:
        return v if v is not None else {}

    @field_validator("scores", mode="before")
    @classmethod
    def _empty_scores(cls, v: Any) -> Any:
        return v if v is not None else {}


# =============================================================================
# Instrument theses
# =============================================================================

class InstrumentThesisCard(_DeskBase):
    symbol: str
    current_bias: Optional[str] = None
    active_thesis: Optional[str] = None
    invalidation_triggers: Optional[str] = None
    regime: Optional[str] = None
    macro_as_of: Optional[str] = None
    last_updated: Optional[str] = None
    playbook_version: Optional[str] = None
    reason_short: Optional[str] = None
    invalidation_short: Optional[str] = None
    available: bool = False
    # NEW — polyglot / executive fields
    layman_meaning: Optional[str] = Field(
        default=None,
        description="Plain-language explanation of why this bias is held",
    )
    market_impact: Optional[MarketImpact] = Field(
        default=None,
        description="Directional bias + probability + execution framing for the pair",
    )

    @field_validator("symbol", mode="before")
    @classmethod
    def _upper_symbol(cls, v: Any) -> str:
        return str(v or "").upper()

    @field_validator("macro_as_of", "last_updated", mode="before")
    @classmethod
    def _iso_ts(cls, v: Any) -> Optional[str]:
        return _to_iso8601(v)

    @field_validator("current_bias", mode="before")
    @classmethod
    def _norm_bias(cls, v: Any) -> Optional[str]:
        if v is None or v == "":
            return None
        s = str(v).strip().upper()
        aliases = {"BULL": "BULLISH", "BEAR": "BEARISH"}
        return aliases.get(s, s)


class InstrumentThesesSection(_DeskBase):
    ok: bool = True
    available: bool = False
    section: Optional[str] = None
    error: Optional[str] = None
    symbols: list[str] = Field(default_factory=list)
    theses: list[InstrumentThesisCard] = Field(default_factory=list)
    by_symbol: dict[str, InstrumentThesisCard] = Field(default_factory=dict)
    count: int = 0
    data: Optional[Any] = None

    @field_validator("by_symbol", mode="before")
    @classmethod
    def _coerce_by_symbol(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return {}
        # Ensure nested cards validate
        out: dict[str, Any] = {}
        for k, val in v.items():
            if isinstance(val, InstrumentThesisCard):
                out[str(k).upper()] = val
            elif isinstance(val, dict):
                card = dict(val)
                card.setdefault("symbol", k)
                out[str(k).upper()] = card
            else:
                out[str(k).upper()] = val
        return out

    @model_validator(mode="after")
    def _sync_count(self) -> "InstrumentThesesSection":
        if self.theses and not self.count:
            self.count = sum(1 for t in self.theses if t.available)
        return self


# =============================================================================
# Regime history
# =============================================================================

class RegimeDistributionRow(_DeskBase):
    regime: str
    count: int = 0
    pct: float = 0.0

    @field_validator("count", mode="before")
    @classmethod
    def _cnt(cls, v: Any) -> int:
        return _opt_int(v) or 0

    @field_validator("pct", mode="before")
    @classmethod
    def _pct_float(cls, v: Any) -> float:
        return _opt_float(v) or 0.0


class RegimeTimelineRow(_DeskBase):
    as_of: Optional[str] = None
    regime: Optional[str] = None
    growth: Optional[str] = None
    inflation: Optional[str] = None
    policy: Optional[str] = None
    liquidity: Optional[str] = None
    risk: Optional[str] = None
    confidence: Optional[float] = None

    @field_validator("as_of", mode="before")
    @classmethod
    def _iso(cls, v: Any) -> Optional[str]:
        return _to_iso8601(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> Optional[float]:
        return _clamp_prob(v)


class RegimeHistorySection(_DeskBase):
    ok: bool = True
    available: bool = False
    section: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None
    range_from: Optional[str] = Field(default=None, alias="from")
    range_to: Optional[str] = Field(default=None, alias="to")
    n_snapshots: int = 0
    unit: Optional[str] = None
    distribution: list[RegimeDistributionRow] = Field(default_factory=list)
    regimes: dict[str, int] = Field(default_factory=dict)
    growth: JsonMap = Field(default_factory=dict)
    inflation: JsonMap = Field(default_factory=dict)
    policy: JsonMap = Field(default_factory=dict)
    liquidity: JsonMap = Field(default_factory=dict)
    risk: JsonMap = Field(default_factory=dict)
    timeline: list[RegimeTimelineRow] = Field(default_factory=list)
    summary_text: str = ""
    data: Optional[Any] = None

    @field_validator("range_from", "range_to", mode="before")
    @classmethod
    def _iso_range(cls, v: Any) -> Optional[str]:
        return _to_iso8601(v)

    @field_validator("n_snapshots", mode="before")
    @classmethod
    def _n_int(cls, v: Any) -> int:
        return _opt_int(v) or 0


# =============================================================================
# Event study / surprises
# =============================================================================

class EventStudyItem(_DeskBase):
    event_key: Optional[str] = None
    event_family: Optional[str] = None
    title: Optional[str] = None
    currency: Optional[str] = None
    event_time: Optional[str] = None
    actual_raw: Optional[str] = None
    forecast_raw: Optional[str] = None
    surprise_raw: Optional[float] = None
    surprise_pct: Optional[float] = None
    surprise_direction: Optional[str] = None
    beat_miss: Optional[str] = None
    regime: Optional[str] = None
    impact: Optional[int] = None
    instrument_signals: JsonMap = Field(default_factory=dict)

    @field_validator("event_time", mode="before")
    @classmethod
    def _iso_event(cls, v: Any) -> Optional[str]:
        return _to_iso8601(v)

    @field_validator("surprise_raw", "surprise_pct", mode="before")
    @classmethod
    def _opt_f(cls, v: Any) -> Optional[float]:
        return _opt_float(v)

    @field_validator("impact", mode="before")
    @classmethod
    def _imp(cls, v: Any) -> Optional[int]:
        return _opt_int(v)

    @field_validator("actual_raw", "forecast_raw", mode="before")
    @classmethod
    def _raw_str(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        return str(v)


class SampleStudyBlock(_DeskBase):
    query: Optional[JsonMap] = None
    n_events: int = 0
    symbol_reactions: Optional[JsonMap] = None
    events_preview: Optional[Any] = None
    note: Optional[str] = None
    report_text: Optional[str] = None

    @field_validator("n_events", mode="before")
    @classmethod
    def _n_events_int(cls, v: Any) -> int:
        return _opt_int(v) or 0


class EventStudySection(_DeskBase):
    ok: bool = True
    available: bool = False
    section: Optional[str] = None
    error: Optional[str] = None
    ledger_size: int = 0
    recent_surprises: list[EventStudyItem] = Field(default_factory=list)
    recent_count: int = 0
    sample_study: SampleStudyBlock = Field(default_factory=SampleStudyBlock)
    horizons_supported: list[str] = Field(
        default_factory=lambda: ["immediate", "24h", "72h"]
    )
    data: Optional[Any] = None

    @field_validator("ledger_size", "recent_count", mode="before")
    @classmethod
    def _ints(cls, v: Any) -> int:
        return _opt_int(v) or 0

    @field_validator("sample_study", mode="before")
    @classmethod
    def _empty_sample(cls, v: Any) -> Any:
        return v if v is not None else {}


# =============================================================================
# Top-level Research Desk payload
# =============================================================================

class SectionsOk(_DeskBase):
    macro_state: bool = False
    instrument_theses: bool = False
    regime_history: bool = False
    event_study: bool = False


class DeskHeader(_DeskBase):
    regime: Optional[str] = None
    confidence: Optional[float] = None
    as_of: Optional[str] = None
    thesis_count: int = 0
    history_snapshots: int = 0
    surprise_ledger: int = 0

    @field_validator("confidence", mode="before")
    @classmethod
    def _hdr_conf(cls, v: Any) -> Optional[float]:
        return _clamp_prob(v)

    @field_validator("as_of", mode="before")
    @classmethod
    def _hdr_as_of(cls, v: Any) -> Optional[str]:
        return _to_iso8601(v)

    @field_validator("thesis_count", "history_snapshots", "surprise_ledger", mode="before")
    @classmethod
    def _hdr_ints(cls, v: Any) -> int:
        return _opt_int(v) or 0


class ResearchDeskPayload(_DeskBase):
    """
    Full Research Desk aggregate — primary typed product of build_research_desk().

    Wire format is stable JSON for Python UI and future Go/Rust sidecars.
    """

    schema_version: str = "research_desk_v2"
    generated_at: Optional[str] = None
    db_path: Optional[str] = None
    sections_ok: SectionsOk = Field(default_factory=SectionsOk)
    all_ok: bool = False
    macro_state: MacroStateSection = Field(default_factory=MacroStateSection)
    instrument_theses: InstrumentThesesSection = Field(
        default_factory=InstrumentThesesSection
    )
    regime_history: RegimeHistorySection = Field(default_factory=RegimeHistorySection)
    event_study: EventStudySection = Field(default_factory=EventStudySection)
    header: DeskHeader = Field(default_factory=DeskHeader)

    @field_validator("generated_at", mode="before")
    @classmethod
    def _gen_iso(cls, v: Any) -> Optional[str]:
        return _to_iso8601(v)

    @model_validator(mode="after")
    def _sync_all_ok(self) -> "ResearchDeskPayload":
        ok = self.sections_ok
        derived = bool(
            ok.macro_state
            and ok.instrument_theses
            and ok.regime_history
            and ok.event_study
        )
        if not self.all_ok and derived:
            self.all_ok = derived
        return self

    def to_desk_dict(self) -> dict[str, Any]:
        """
        JSON-mode dict for UI / IPC (aliases restored: from/to).
        Prefer this over mode='python' for polyglot consumers.
        """
        return self.model_dump(mode="json", by_alias=True)

    def to_python_dict(self) -> dict[str, Any]:
        """Python-native dump (still ISO strings for timestamps)."""
        return self.model_dump(mode="python", by_alias=True)


def parse_research_desk(data: dict[str, Any]) -> ResearchDeskPayload:
    """Validate an arbitrary desk dict into a ResearchDeskPayload."""
    return ResearchDeskPayload.model_validate(data)


def dumps_research_desk(payload: ResearchDeskPayload, *, indent: Optional[int] = 2) -> str:
    """Serialize a validated payload to polyglot JSON text."""
    return payload.to_json(indent=indent)
