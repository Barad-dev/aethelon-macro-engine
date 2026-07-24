# -*- coding: utf-8 -*-
"""
models/desk_schemas.py — Pydantic v2 contracts for Research Desk payloads
=========================================================================
Mirrors the dict shape produced by `research_desk_data.build_research_desk()`
so the UI and future MasterThesis synthesis can rely on typed boundaries.

Pydantic v2:
  - model_config = ConfigDict(...)
  - field_validator / model_validator
  - model_validate / model_dump
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# =============================================================================
# Shared base
# =============================================================================

class _DeskBase(BaseModel):
    """Allow forward-compatible extra keys; coerce simple types leniently."""

    model_config = ConfigDict(
        extra="allow",
        str_strip_whitespace=True,
        validate_assignment=False,
        from_attributes=True,
        populate_by_name=True,
    )


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


class MacroStateSection(_DeskBase):
    """Section 1 — current textbook MacroState (+ desk availability flags)."""

    ok: bool = True
    available: bool = False
    section: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None
    regime: Optional[str] = None
    confidence: Optional[float] = None
    lesson: Optional[str] = None
    dials: MacroDials = Field(default_factory=MacroDials)
    scores: MacroScores = Field(default_factory=MacroScores)
    summary_line: Optional[str] = None
    as_of: Optional[str] = None
    rules_version: Optional[str] = None
    raw: Optional[dict[str, Any]] = None
    data: Optional[Any] = None  # error-path placeholder from _section_error

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v: Any) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

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

    @field_validator("symbol", mode="before")
    @classmethod
    def _upper_symbol(cls, v: Any) -> str:
        return str(v or "").upper()


class InstrumentThesesSection(_DeskBase):
    ok: bool = True
    available: bool = False
    section: Optional[str] = None
    error: Optional[str] = None
    symbols: list[str] = Field(default_factory=list)
    theses: list[InstrumentThesisCard] = Field(default_factory=list)
    by_symbol: dict[str, Any] = Field(default_factory=dict)
    count: int = 0
    data: Optional[Any] = None

    @field_validator("by_symbol", mode="before")
    @classmethod
    def _coerce_by_symbol(cls, v: Any) -> Any:
        return v if isinstance(v, dict) else {}


# =============================================================================
# Regime history
# =============================================================================

class RegimeDistributionRow(_DeskBase):
    regime: str
    count: int = 0
    pct: float = 0.0

    @field_validator("pct", mode="before")
    @classmethod
    def _pct_float(cls, v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0


class RegimeTimelineRow(_DeskBase):
    as_of: Optional[str] = None
    regime: Optional[str] = None
    growth: Optional[str] = None
    inflation: Optional[str] = None
    policy: Optional[str] = None
    liquidity: Optional[str] = None
    risk: Optional[str] = None
    confidence: Optional[float] = None


class RegimeHistorySection(_DeskBase):
    ok: bool = True
    available: bool = False
    section: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None
    # `from` is reserved in Python — expose via alias matching desk JSON key
    range_from: Optional[str] = Field(default=None, alias="from")
    range_to: Optional[str] = Field(default=None, alias="to")
    n_snapshots: int = 0
    unit: Optional[str] = None
    distribution: list[RegimeDistributionRow] = Field(default_factory=list)
    regimes: dict[str, int] = Field(default_factory=dict)
    growth: dict[str, Any] = Field(default_factory=dict)
    inflation: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    liquidity: dict[str, Any] = Field(default_factory=dict)
    risk: dict[str, Any] = Field(default_factory=dict)
    timeline: list[RegimeTimelineRow] = Field(default_factory=list)
    summary_text: str = ""
    data: Optional[Any] = None

    @field_validator("n_snapshots", mode="before")
    @classmethod
    def _n_int(cls, v: Any) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0


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
    instrument_signals: dict[str, Any] = Field(default_factory=dict)

    @field_validator("surprise_raw", "surprise_pct", mode="before")
    @classmethod
    def _opt_float(cls, v: Any) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


class SampleStudyBlock(_DeskBase):
    query: Optional[dict[str, Any]] = None
    n_events: int = 0
    symbol_reactions: Optional[dict[str, Any]] = None
    events_preview: Optional[Any] = None
    note: Optional[str] = None
    report_text: Optional[str] = None

    @field_validator("n_events", mode="before")
    @classmethod
    def _n_events_int(cls, v: Any) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0


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
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


class ResearchDeskPayload(_DeskBase):
    """
    Full Research Desk aggregate — primary typed product of build_research_desk().
    """

    schema_version: str = "research_desk_v1"
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

    @model_validator(mode="after")
    def _sync_all_ok(self) -> "ResearchDeskPayload":
        # If caller omitted all_ok, derive from sections_ok
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
        Serialize back to the legacy plain-dict shape expected by the Qt UI.
        Uses aliases so `from` / `to` keys are restored on regime_history.
        """
        return self.model_dump(mode="python", by_alias=True)


def parse_research_desk(data: dict[str, Any]) -> ResearchDeskPayload:
    """Validate an arbitrary desk dict into a ResearchDeskPayload."""
    return ResearchDeskPayload.model_validate(data)
