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
  C3 hard invalidation (pure, offline):
    • SeriesChange
    • detect_hard_invalidations / detect_hard_invalidations_from_fred
  C4 soft divergence (pure, offline):
    • detect_soft_divergences / detect_soft_divergences_from_fred
  C5 exogenous shock isolator (pure, offline):
    • ShockEvent
    • isolate_exogenous_shocks / isolate_exogenous_shocks_from_dicts

No storage, GUI, network, or news_engine wiring.
"""

from aethelon.macro.hard_invalidation import (
    SeriesChange,
    detect_hard_invalidations,
    detect_hard_invalidations_from_fred,
)
from aethelon.macro.shock import (
    ShockEvent,
    isolate_exogenous_shocks,
    isolate_exogenous_shocks_from_dicts,
)
from aethelon.macro.soft_divergence import (
    detect_soft_divergences,
    detect_soft_divergences_from_fred,
)
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
    # C3 hard invalidation
    "SeriesChange",
    "detect_hard_invalidations",
    "detect_hard_invalidations_from_fred",
    # C4 soft divergence
    "detect_soft_divergences",
    "detect_soft_divergences_from_fred",
    # C5 exogenous shocks
    "ShockEvent",
    "isolate_exogenous_shocks",
    "isolate_exogenous_shocks_from_dicts",
]
