# -*- coding: utf-8 -*-
"""
aethelon.macro — Stage C causal reasoning & macro logic (contracts first)

Public surface (C1):
  • MacroRegime
  • RegimeResult
  • HardInvalidationSignal
  • SoftDivergenceSignal
  • ExogenousShockSignal

No classification, storage, GUI, or live-data wiring yet.
"""

from aethelon.macro.schemas import (
    ExogenousShockSignal,
    HardInvalidationSignal,
    MacroRegime,
    RegimeResult,
    SoftDivergenceSignal,
)

__all__ = [
    "MacroRegime",
    "RegimeResult",
    "HardInvalidationSignal",
    "SoftDivergenceSignal",
    "ExogenousShockSignal",
]
