# -*- coding: utf-8 -*-
"""
models — Pydantic v2 data contracts for the News Engine (Stage A)

Public exports are the Research Desk payload models used by
`research_desk_data` and the Qt Research Desk view.
"""

from .desk_schemas import (
    DeskHeader,
    EventStudyItem,
    EventStudySection,
    InstrumentThesisCard,
    InstrumentThesesSection,
    MacroDials,
    MacroScores,
    MacroStateSection,
    RegimeDistributionRow,
    RegimeHistorySection,
    RegimeTimelineRow,
    ResearchDeskPayload,
    SampleStudyBlock,
    SectionsOk,
)

__all__ = [
    "DeskHeader",
    "EventStudyItem",
    "EventStudySection",
    "InstrumentThesisCard",
    "InstrumentThesesSection",
    "MacroDials",
    "MacroScores",
    "MacroStateSection",
    "RegimeDistributionRow",
    "RegimeHistorySection",
    "RegimeTimelineRow",
    "ResearchDeskPayload",
    "SampleStudyBlock",
    "SectionsOk",
]
