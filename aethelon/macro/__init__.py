# -*- coding: utf-8 -*-
"""
aethelon.macro — Stage C causal reasoning & macro logic

Public surface:
  C1 contracts:
    • MacroRegime, RegimeResult
    • HardInvalidationSignal, SoftDivergenceSignal, ExogenousShockSignal
  C2 classifier (pure, offline):
    • RegimeInputs
    • classify_regime / classify_regime_from_labels / classify_regime_from_dict

No storage, GUI, network, or news_engine wiring.
"""

from aethelon.macro.regime import (
    RegimeInputs,
    classify_regime,
    classify_regime_from_dict,
    classify_regime_from_labels,
)
from aethelon.macro.schemas import (
    ExogenousShockSignal,
    HardInvalidationSignal,
    MacroRegime,
    RegimeResult,
    SoftDivergenceSignal,
)

__all__ = [
    # C1 contracts
    "MacroRegime",
    "RegimeResult",
    "HardInvalidationSignal",
    "SoftDivergenceSignal",
    "ExogenousShockSignal",
    # C2 classifier
    "RegimeInputs",
    "classify_regime",
    "classify_regime_from_labels",
    "classify_regime_from_dict",
]
