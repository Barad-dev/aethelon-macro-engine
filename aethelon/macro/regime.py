# -*- coding: utf-8 -*-
"""
aethelon.macro.regime — Stage C2 pure macro-regime classifier
=============================================================
Maps structured growth × inflation inputs to a Stage C ``RegimeResult``.

This module is pure and offline:

  * No network, database, GUI, or ``news_engine`` imports
  * No hard-invalidation / soft-divergence / shock logic
  * Reuses ``MacroRegime`` and ``RegimeResult`` from ``aethelon.macro.schemas``

Textbook mapping (Stage C four regimes only)
--------------------------------------------
Coarse poles:

  * Growth **expanding**: STRONG, TREND (and mild aliases)
  * Growth **soft**:      WEAK, CONTRACTING
  * Inflation **hot**:    HIGH, ELEVATED
  * Inflation **cool**:   TARGET, LOW

Matrix::

                    inflation HOT          inflation COOL
  growth EXPANDING  REFLATION              GOLDILOCKS
  growth SOFT       STAGFLATION            DEFLATION

Optional policy / liquidity / risk dials never invent a fifth regime.
They only adjust confidence and the explanation / reasoning chain.

Incomplete inputs
-----------------
If growth or inflation is missing/ambiguous, the classifier still returns
one of the four regimes (the contract has no UNKNOWN), but with **low
confidence** and an explicit explanation. It does not invent dial labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Union

from aethelon.macro.schemas import MacroRegime, RegimeResult, _to_utc_z

# ---------------------------------------------------------------------------
# Label poles (uppercase, after normalization)
# ---------------------------------------------------------------------------

# Growth → expanding side of the matrix
_GROWTH_EXPANDING: frozenset[str] = frozenset(
    {
        "STRONG",
        "TREND",
        "EXPANDING",
        "ABOVE_TREND",
        "FIRM",
        "ROBUST",
        "HEALTHY",
    }
)

# Growth → soft / contracting side
_GROWTH_SOFT: frozenset[str] = frozenset(
    {
        "WEAK",
        "CONTRACTING",
        "CONTRACTION",
        "RECESSION",
        "BELOW_TREND",
        "SOFT",
        "SLOW",
        "SLOWDOWN",
    }
)

# Inflation → hot side
_INFLATION_HOT: frozenset[str] = frozenset(
    {
        "HIGH",
        "ELEVATED",
        "HOT",
        "RISING",
        "ABOVE_TARGET",
    }
)

# Inflation → cool side
_INFLATION_COOL: frozenset[str] = frozenset(
    {
        "TARGET",
        "LOW",
        "COOL",
        "COOLING",
        "DISINFLATING",
        "BELOW_TARGET",
        "SUBDUED",
    }
)

# Optional dial normalizations (confidence only)
_POLICY_KNOWN: frozenset[str] = frozenset(
    {"RESTRICTIVE", "NEUTRAL", "ACCOMMODATIVE"}
)
_LIQUIDITY_KNOWN: frozenset[str] = frozenset(
    {"EXPANDING", "STABLE", "TIGHTENING"}
)
_RISK_KNOWN: frozenset[str] = frozenset(
    {"RISK_ON", "NEUTRAL", "RISK_OFF"}
)

# Documented bridge from *legacy* richer regime labels → Stage C four.
# Used only when growth/inflation poles cannot be resolved from dials.
# Confidence is kept deliberately low when this path is taken.
_LEGACY_REGIME_BRIDGE: dict[str, MacroRegime] = {
    "REFLATION": MacroRegime.REFLATION,
    "OVERHEATING": MacroRegime.REFLATION,
    "GOLDILOCKS": MacroRegime.GOLDILOCKS,
    "DISINFLATION_EXPANSION": MacroRegime.GOLDILOCKS,
    "STAGFLATION": MacroRegime.STAGFLATION,
    # Weak growth + non-hot inflation family → DEFLATION pole in Stage C
    "RECESSION": MacroRegime.DEFLATION,
    "SLOWDOWN": MacroRegime.DEFLATION,
    # Ambiguous / cycle / risk labels: no forced Stage C call via bridge
    # (RISK_OFF, TIGHTENING_CYCLE, EASING_CYCLE, TRANSITION omitted on purpose)
}

# Core matrix: (growth_pole, inflation_pole) → regime
# growth_pole: "EXPANDING" | "SOFT"
# inflation_pole: "HOT" | "COOL"
_REGIME_MATRIX: dict[tuple[str, str], MacroRegime] = {
    ("EXPANDING", "HOT"): MacroRegime.REFLATION,
    ("EXPANDING", "COOL"): MacroRegime.GOLDILOCKS,
    ("SOFT", "HOT"): MacroRegime.STAGFLATION,
    ("SOFT", "COOL"): MacroRegime.DEFLATION,
}

_REGIME_BLURB: dict[MacroRegime, str] = {
    MacroRegime.REFLATION: (
        "Firm/expanding growth with elevated inflation — classic reflation mix."
    ),
    MacroRegime.STAGFLATION: (
        "Soft or contracting growth with elevated inflation — stagflation mix."
    ),
    MacroRegime.GOLDILOCKS: (
        "Healthy growth with inflation near or below target — goldilocks mix."
    ),
    MacroRegime.DEFLATION: (
        "Soft or contracting growth with cool inflation — demand-soft / deflationary mix."
    ),
}

# Score thresholds on a rough 0–10 dial scale (legacy analyzer style).
# Mid-band is treated as ambiguous — we do not invent a pole from noise.
_SCORE_EXPANDING_MIN = 5.5
_SCORE_SOFT_MAX = 4.5
_SCORE_HOT_MIN = 5.5
_SCORE_COOL_MAX = 4.5

# Confidence anchors
_CONF_CLEAR = 0.72
_CONF_CORNER = 0.80  # STRONG+HIGH or CONTRACTING+HIGH style corners
_CONF_MILD = 0.62  # TREND / TARGET style
_CONF_PARTIAL = 0.35  # one pole only
_CONF_LEGACY_BRIDGE = 0.28  # richer legacy label only
_CONF_INSUFFICIENT = 0.15  # almost no usable input
_CONF_FLOOR = 0.10
_CONF_CEIL = 0.95


# =============================================================================
# Input container
# =============================================================================

@dataclass(frozen=True)
class RegimeInputs:
    """
    Structured dials for Stage C regime classification.

    Labels are preferred. Optional 0–10 style scores are a fallback only when
    the matching label is missing. Optional policy/liquidity/risk never force
    a regime by themselves.
    """

    growth: Optional[str] = None
    inflation: Optional[str] = None
    policy: Optional[str] = None
    liquidity: Optional[str] = None
    risk: Optional[str] = None
    growth_score: Optional[float] = None
    inflation_score: Optional[float] = None
    as_of: Optional[Union[str, datetime]] = None
    # Optional legacy richer regime string (adapter only; low-confidence bridge)
    legacy_regime: Optional[str] = None


# =============================================================================
# Normalization helpers
# =============================================================================

def _norm_label(value: Any) -> Optional[str]:
    """Uppercase token; empty → None. No semantic invention."""
    if value is None:
        return None
    s = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    return s or None


def _clamp01(value: float) -> float:
    if value < _CONF_FLOOR:
        return _CONF_FLOOR
    if value > _CONF_CEIL:
        return _CONF_CEIL
    return value


def _growth_pole(
    label: Optional[str],
    score: Optional[float],
) -> tuple[Optional[str], str]:
    """
    Resolve growth to ``EXPANDING``, ``SOFT``, or ``None``.

    Returns ``(pole, note)`` where note explains the choice for the chain.
    """
    if label in _GROWTH_EXPANDING:
        return "EXPANDING", f"growth label '{label}' → EXPANDING pole"
    if label in _GROWTH_SOFT:
        return "SOFT", f"growth label '{label}' → SOFT pole"
    if label is not None:
        # Unknown label: do not invent a pole from the string alone
        return None, f"growth label '{label}' is not a known pole (ignored)"

    if score is None:
        return None, "growth label and score both missing"

    try:
        s = float(score)
    except (TypeError, ValueError):
        return None, "growth score unparseable"

    if s >= _SCORE_EXPANDING_MIN:
        return "EXPANDING", f"growth score {s:.2f} ≥ {_SCORE_EXPANDING_MIN} → EXPANDING"
    if s <= _SCORE_SOFT_MAX:
        return "SOFT", f"growth score {s:.2f} ≤ {_SCORE_SOFT_MAX} → SOFT"
    return None, f"growth score {s:.2f} in mid-band (ambiguous; no pole)"


def _inflation_pole(
    label: Optional[str],
    score: Optional[float],
) -> tuple[Optional[str], str]:
    """Resolve inflation to ``HOT``, ``COOL``, or ``None``."""
    if label in _INFLATION_HOT:
        return "HOT", f"inflation label '{label}' → HOT pole"
    if label in _INFLATION_COOL:
        return "COOL", f"inflation label '{label}' → COOL pole"
    if label is not None:
        return None, f"inflation label '{label}' is not a known pole (ignored)"

    if score is None:
        return None, "inflation label and score both missing"

    try:
        s = float(score)
    except (TypeError, ValueError):
        return None, "inflation score unparseable"

    if s >= _SCORE_HOT_MIN:
        return "HOT", f"inflation score {s:.2f} ≥ {_SCORE_HOT_MIN} → HOT"
    if s <= _SCORE_COOL_MAX:
        return "COOL", f"inflation score {s:.2f} ≤ {_SCORE_COOL_MAX} → COOL"
    return None, f"inflation score {s:.2f} in mid-band (ambiguous; no pole)"


def _base_confidence(
    *,
    growth_label: Optional[str],
    inflation_label: Optional[str],
    g_pole: str,
    i_pole: str,
) -> float:
    """Confidence when both poles are resolved from primary inputs."""
    # Corner cases: very clear textbook extremes
    if growth_label == "STRONG" and inflation_label in ("HIGH", "ELEVATED"):
        return _CONF_CORNER
    if growth_label == "CONTRACTING" and inflation_label in ("HIGH", "ELEVATED"):
        return _CONF_CORNER
    if growth_label == "CONTRACTING" and inflation_label == "LOW":
        return _CONF_CORNER
    # Milder interior cells
    if growth_label == "TREND" or inflation_label == "TARGET":
        return _CONF_MILD
    if g_pole == "EXPANDING" and i_pole == "COOL":
        return _CONF_MILD
    return _CONF_CLEAR


def _apply_optional_dials(
    confidence: float,
    *,
    policy: Optional[str],
    liquidity: Optional[str],
    risk: Optional[str],
    regime: MacroRegime,
    chain: list[str],
) -> float:
    """
    Haircut or slight boost from optional dials. Never changes the regime.

    Conservative rules:
      * RISK_OFF → modest confidence haircut + note
      * Restrictive + tightening while goldilocks/deflation → small haircut
        (policy may be fighting a different problem; do not re-label)
      * Accommodative + expanding liquidity while stagflation → small haircut
    """
    conf = confidence

    if risk == "RISK_OFF":
        conf -= 0.08
        chain.append(
            "risk=RISK_OFF: short-term fear may dominate; confidence reduced "
            "(regime still from growth×inflation only)"
        )
    elif risk == "RISK_ON":
        chain.append("risk=RISK_ON noted (no regime change)")
    elif risk is not None and risk not in _RISK_KNOWN:
        chain.append(f"risk='{risk}' unrecognized (ignored)")

    if policy is not None and policy not in _POLICY_KNOWN:
        chain.append(f"policy='{policy}' unrecognized (ignored)")
    if liquidity is not None and liquidity not in _LIQUIDITY_KNOWN:
        chain.append(f"liquidity='{liquidity}' unrecognized (ignored)")

    if (
        policy == "RESTRICTIVE"
        and liquidity == "TIGHTENING"
        and regime in (MacroRegime.GOLDILOCKS, MacroRegime.DEFLATION)
    ):
        conf -= 0.05
        chain.append(
            "policy restrictive + liquidity tightening while growth×inflation "
            "is mild/soft — confidence trimmed (no TIGHTENING_CYCLE label in Stage C)"
        )

    if (
        policy == "ACCOMMODATIVE"
        and liquidity == "EXPANDING"
        and regime == MacroRegime.STAGFLATION
    ):
        conf -= 0.05
        chain.append(
            "easy policy/liquidity alongside stagflation mix — confidence trimmed"
        )

    if policy is not None and policy in _POLICY_KNOWN:
        chain.append(f"policy={policy} (context only)")
    if liquidity is not None and liquidity in _LIQUIDITY_KNOWN:
        chain.append(f"liquidity={liquidity} (context only)")

    return conf


# =============================================================================
# Public classifier
# =============================================================================

def classify_regime(inputs: RegimeInputs) -> RegimeResult:
    """
    Classify a Stage C macro regime from structured dials.

    Parameters
    ----------
    inputs:
        ``RegimeInputs`` with growth/inflation labels (preferred) and optional
        scores, policy/liquidity/risk context, and ``as_of``.

    Returns
    -------
    RegimeResult
        Always one of REFLATION | STAGFLATION | GOLDILOCKS | DEFLATION.
        Incomplete inputs yield low confidence and a clear explanation.
    """
    growth = _norm_label(inputs.growth)
    inflation = _norm_label(inputs.inflation)
    policy = _norm_label(inputs.policy)
    liquidity = _norm_label(inputs.liquidity)
    risk = _norm_label(inputs.risk)
    legacy = _norm_label(inputs.legacy_regime)

    chain: list[str] = []
    chain.append("Stage C2 classifier: growth×inflation matrix only (four regimes)")

    g_pole, g_note = _growth_pole(growth, inputs.growth_score)
    i_pole, i_note = _inflation_pole(inflation, inputs.inflation_score)
    chain.append(g_note)
    chain.append(i_note)

    as_of = _to_utc_z(inputs.as_of)

    # --- Path A: both poles known → textbook matrix ---
    if g_pole is not None and i_pole is not None:
        regime = _REGIME_MATRIX[(g_pole, i_pole)]
        conf = _base_confidence(
            growth_label=growth,
            inflation_label=inflation,
            g_pole=g_pole,
            i_pole=i_pole,
        )
        # Labels missing but scores drove poles → slightly lower confidence
        if growth is None or inflation is None:
            conf = min(conf, 0.55)
            chain.append("one or both poles came from scores rather than labels")

        chain.append(f"matrix ({g_pole}, {i_pole}) → {regime.value}")
        conf = _apply_optional_dials(
            conf,
            policy=policy,
            liquidity=liquidity,
            risk=risk,
            regime=regime,
            chain=chain,
        )
        conf = _clamp01(conf)
        explanation = (
            f"{_REGIME_BLURB[regime]} "
            f"(growth={growth or g_pole}, inflation={inflation or i_pole}; "
            f"conf={conf:.0%})."
        )
        return RegimeResult(
            regime=regime,
            confidence=round(conf, 3),
            explanation=explanation,
            as_of=as_of,
            reasoning_chain=chain,
            market_narrative=None,
        )

    # --- Path B: only one pole known → partial, low confidence ---
    if g_pole is not None or i_pole is not None:
        if g_pole == "EXPANDING":
            # Without inflation, lean goldilocks (milder call) not reflation
            regime = MacroRegime.GOLDILOCKS
            why = "growth expanding but inflation unknown — mild GOLDILOCKS default"
        elif g_pole == "SOFT":
            # Without inflation, lean deflation (demand soft) not stagflation
            regime = MacroRegime.DEFLATION
            why = "growth soft but inflation unknown — mild DEFLATION default"
        elif i_pole == "HOT":
            # Without growth, do not assume stagflation; mild reflation lean is also
            # inventing — prefer low-conf GOLDILOCKS? Hot inflation alone is closer
            # to caution: use REFLATION only with very low conf is still inventing.
            # Conservative: GOLDILOCKS with note that inflation is hot but growth missing.
            regime = MacroRegime.GOLDILOCKS
            why = (
                "inflation HOT but growth unknown — no full matrix cell; "
                "conservative GOLDILOCKS placeholder"
            )
        else:
            # i_pole == COOL, growth unknown
            regime = MacroRegime.GOLDILOCKS
            why = (
                "inflation COOL but growth unknown — no full matrix cell; "
                "conservative GOLDILOCKS placeholder"
            )

        conf = _CONF_PARTIAL
        chain.append(why)
        conf = _apply_optional_dials(
            conf,
            policy=policy,
            liquidity=liquidity,
            risk=risk,
            regime=regime,
            chain=chain,
        )
        conf = _clamp01(conf)
        explanation = (
            f"Incomplete dials: {why}. "
            f"Treat confidence {conf:.0%} as a soft placeholder, not a firm call."
        )
        return RegimeResult(
            regime=regime,
            confidence=round(conf, 3),
            explanation=explanation,
            as_of=as_of,
            reasoning_chain=chain,
            market_narrative=None,
        )

    # --- Path C: legacy richer regime string only (documented bridge) ---
    if legacy is not None and legacy in _LEGACY_REGIME_BRIDGE:
        regime = _LEGACY_REGIME_BRIDGE[legacy]
        conf = _CONF_LEGACY_BRIDGE
        chain.append(
            f"no growth/inflation poles; bridged legacy regime '{legacy}' → "
            f"{regime.value} (low confidence)"
        )
        conf = _apply_optional_dials(
            conf,
            policy=policy,
            liquidity=liquidity,
            risk=risk,
            regime=regime,
            chain=chain,
        )
        conf = _clamp01(conf)
        explanation = (
            f"Growth/inflation dials missing; used documented legacy bridge "
            f"'{legacy}' → {regime.value}. Confidence kept low ({conf:.0%})."
        )
        return RegimeResult(
            regime=regime,
            confidence=round(conf, 3),
            explanation=explanation,
            as_of=as_of,
            reasoning_chain=chain,
            market_narrative=None,
        )

    if legacy is not None:
        chain.append(
            f"legacy regime '{legacy}' has no Stage C bridge "
            f"(RISK_OFF / cycle / TRANSITION-style labels are not mapped)"
        )

    # --- Path D: insufficient input ---
    regime = MacroRegime.GOLDILOCKS
    conf = _CONF_INSUFFICIENT
    chain.append(
        "insufficient growth/inflation input — conservative GOLDILOCKS placeholder"
    )
    conf = _apply_optional_dials(
        conf,
        policy=policy,
        liquidity=liquidity,
        risk=risk,
        regime=regime,
        chain=chain,
    )
    conf = _clamp01(conf)
    explanation = (
        "Insufficient growth/inflation inputs to classify a Stage C regime. "
        f"Returning GOLDILOCKS as a neutral placeholder with confidence {conf:.0%}; "
        "do not treat this as a real macro call."
    )
    return RegimeResult(
        regime=regime,
        confidence=round(conf, 3),
        explanation=explanation,
        as_of=as_of,
        reasoning_chain=chain,
        market_narrative=None,
    )


def classify_regime_from_labels(
    *,
    growth: Optional[str] = None,
    inflation: Optional[str] = None,
    policy: Optional[str] = None,
    liquidity: Optional[str] = None,
    risk: Optional[str] = None,
    growth_score: Optional[float] = None,
    inflation_score: Optional[float] = None,
    as_of: Optional[Union[str, datetime]] = None,
) -> RegimeResult:
    """
    Convenience wrapper around :func:`classify_regime` with explicit kwargs.

    Same pure rules; useful for call sites that do not want a dataclass.
    """
    return classify_regime(
        RegimeInputs(
            growth=growth,
            inflation=inflation,
            policy=policy,
            liquidity=liquidity,
            risk=risk,
            growth_score=growth_score,
            inflation_score=inflation_score,
            as_of=as_of,
        )
    )


def classify_regime_from_dict(state: Mapping[str, Any]) -> RegimeResult:
    """
    Thin in-memory adapter for dicts shaped like the legacy macro analyzer output.

    Expected keys (all optional, read only if present)::

        growth, inflation, policy, liquidity, risk,
        growth_score, inflation_score, as_of, regime

    ``regime`` is treated as a *legacy* richer label and used only when
    growth/inflation poles cannot be resolved (see ``_LEGACY_REGIME_BRIDGE``).

    Does **not** import or call ``macro_state_analyzer``, ``news_engine``,
    GUI, or any database layer.
    """
    if not isinstance(state, Mapping):
        return classify_regime(RegimeInputs())

    def _opt_float(key: str) -> Optional[float]:
        raw = state.get(key)
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    return classify_regime(
        RegimeInputs(
            growth=state.get("growth"),  # type: ignore[arg-type]
            inflation=state.get("inflation"),  # type: ignore[arg-type]
            policy=state.get("policy"),  # type: ignore[arg-type]
            liquidity=state.get("liquidity"),  # type: ignore[arg-type]
            risk=state.get("risk"),  # type: ignore[arg-type]
            growth_score=_opt_float("growth_score"),
            inflation_score=_opt_float("inflation_score"),
            as_of=state.get("as_of"),  # type: ignore[arg-type]
            legacy_regime=state.get("regime"),  # type: ignore[arg-type]
        )
    )
