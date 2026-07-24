# -*- coding: utf-8 -*-
"""
instrument_thesis.py — Living Instrument Theses from Macro Regimes
==================================================================
Connects textbook MacroState regimes to the 4 FX/metal symbols tracked
by the v5 news engine:

  XAUUSD  ·  EURUSD  ·  GBPUSD  ·  USDCHF

Each symbol gets a living "thesis":
  • current_bias          BULLISH / BEARISH / NEUTRAL
  • active_thesis         why we hold that bias (regime-linked)
  • invalidation_triggers what would force us to change our mind

Theory sketch (how an economist maps regimes → these pairs):

  XAUUSD (gold priced in USD)
    Real rates, USD, and uncertainty. Gold likes lower real rates,
    easier policy, inflation hedges, and risk-off / stagflation.
    Gold dislikes restrictive policy that lifts real yields.

  EURUSD / GBPUSD (dollar crosses)
    Relative growth + relative rates + risk appetite.
    Strong US / hawkish Fed / risk-off dollar bid → pressure on EUR & GBP.
    Soft US / dovish Fed / risk-on → support for EUR & GBP.

  USDCHF (dollar vs Swiss franc)
    CHF is a classic safe haven (like gold, opposite of risk FX).
    Risk-off and Swiss / defensive demand → CHF bid → USDCHF BEARISH.
    Risk-on + firm USD → USDCHF BULLISH.
    (Note: bias is always for the *pair as quoted*, base/quote.)

Integration:
  - Table created by news_engine schema init and InstrumentThesisEngine
  - Call update_from_macro_state(macro_state) whenever MacroState changes
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Optional


# =============================================================================
# TRACKED SYMBOLS (must match news_engine pressure / sentiment keys)
# =============================================================================

TRACKED_SYMBOLS = ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF")

BIAS_BULLISH = "BULLISH"
BIAS_BEARISH = "BEARISH"
BIAS_NEUTRAL = "NEUTRAL"

# =============================================================================
# SCHEMA
# =============================================================================

INSTRUMENT_THESIS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS instrument_thesis (
    symbol TEXT NOT NULL,
    current_bias TEXT NOT NULL,
    active_thesis TEXT NOT NULL,
    invalidation_triggers TEXT NOT NULL,
    regime TEXT,
    growth TEXT,
    inflation TEXT,
    policy TEXT,
    liquidity TEXT,
    risk TEXT,
    confidence REAL,
    macro_as_of TEXT,
    playbook_version TEXT,
    last_updated TEXT NOT NULL,
    PRIMARY KEY (symbol)
)
"""

INSTRUMENT_THESIS_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS instrument_thesis_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    current_bias TEXT NOT NULL,
    active_thesis TEXT NOT NULL,
    invalidation_triggers TEXT NOT NULL,
    regime TEXT,
    macro_as_of TEXT,
    playbook_version TEXT,
    recorded_at TEXT NOT NULL
)
"""

PLAYBOOK_VERSION = "fx_metal_v1"

try:
    from paths import get_db_path_str as _resolve_db
    def _default_db() -> str:
        return _resolve_db(migrate=True)
except Exception:  # pragma: no cover
    def _default_db() -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_engine_store.db")

_DB_LOCK = threading.RLock()


# =============================================================================
# MACRO PLAYBOOK — regime → bias + thesis + invalidation per symbol
# =============================================================================
# Keys: regime name from MacroStateAnalyzer
# Values: dict symbol → {bias, thesis, invalidation}
#
# Biases are for the *quoted pair* (e.g. EURUSD BULLISH = euro up vs dollar).

def _entry(bias: str, thesis: str, invalidation: str) -> dict:
    return {"bias": bias, "thesis": thesis, "invalidation": invalidation}


# Shared invalidation snippets (composed into full strings)
_INV = {
    "us_growth_surprise": "US growth/jobs re-accelerate sharply vs Europe/UK",
    "us_growth_collapse": "US labor market cracks (unemployment jumps, payrolls collapse)",
    "inflation_reheat": "US inflation re-accelerates and forces a hawkish Fed repricing",
    "inflation_collapse": "inflation collapses and real-rate path shifts abruptly",
    "fed_dovish": "Fed turns clearly dovish (cuts priced aggressively / guidance softens)",
    "fed_hawkish": "Fed turns more hawkish (higher for longer, QT accelerated)",
    "risk_on": "risk appetite returns (VIX falls, equities stabilize)",
    "risk_off": "risk-off shock (VIX spike, flight to USD/CHF/gold)",
    "real_rates_up": "US real yields rise materially (nominal up and/or inflation expectations down)",
    "real_rates_down": "US real yields fall (easing + sticky inflation or growth scare)",
    "ecb_hawkish": "ECB turns hawkish relative to Fed (EUR rates support)",
    "boe_hawkish": "BoE turns hawkish relative to Fed (GBP rates support)",
    "ecb_dovish": "ECB dovish surprise / euro-area growth scare",
    "boe_dovish": "BoE dovish surprise / UK growth scare",
    "snb_shift": "SNB policy stance shifts sharply vs Fed",
}

PLAYBOOK: dict[str, dict[str, dict]] = {
    # ── Classic growth × inflation matrix ───────────────────────────────
    "REFLATION": {
        "XAUUSD": _entry(
            BIAS_NEUTRAL,
            "Reflation = firm demand + inflation still above comfort. Gold is mixed: "
            "inflation-hedge demand helps, but a still-firm Fed / positive real-rate "
            "backdrop can cap upside. Wait for either sticky inflation with easier "
            "policy (gold up) or higher real yields (gold down).",
            " | ".join([
                "Bias flips BULLISH if " + _INV["real_rates_down"] + " or " + _INV["fed_dovish"],
                "Bias flips BEARISH if " + _INV["real_rates_up"] + " or " + _INV["fed_hawkish"],
            ]),
        ),
        "EURUSD": _entry(
            BIAS_BEARISH,
            "In US-led reflation, higher US nominal growth and sticky inflation often "
            "support the dollar via rate differentials. EURUSD tends to struggle unless "
            "the euro area is reflating *faster* than the US.",
            " | ".join([
                _INV["fed_dovish"],
                _INV["ecb_hawkish"],
                "euro-area growth clearly outpaces the US",
            ]),
        ),
        "GBPUSD": _entry(
            BIAS_BEARISH,
            "Same dollar-support logic as EURUSD in US reflation: firmer US data and "
            "sticky US inflation keep rate-support under USD. GBP needs relative UK "
            "outperformance or a softer Fed to reverse.",
            " | ".join([
                _INV["fed_dovish"],
                _INV["boe_hawkish"],
                "UK data clearly outpaces US data",
            ]),
        ),
        "USDCHF": _entry(
            BIAS_BULLISH,
            "Reflation with risk appetite usually reduces safe-haven CHF demand while "
            "a firm USD (US rates/growth) supports the dollar side → USDCHF bias up.",
            " | ".join([
                _INV["risk_off"],
                _INV["fed_dovish"],
                _INV["snb_shift"] + " toward much tighter CHF policy",
            ]),
        ),
    },
    "GOLDILOCKS": {
        "XAUUSD": _entry(
            BIAS_BEARISH,
            "Goldilocks (healthy growth, inflation near target) reduces the urgency of "
            "gold as an inflation or crisis hedge. Opportunity cost of holding non-yielding "
            "gold stays relevant if policy is not actively easing.",
            " | ".join([
                _INV["risk_off"],
                _INV["inflation_reheat"],
                _INV["fed_dovish"] + " / " + _INV["real_rates_down"],
            ]),
        ),
        "EURUSD": _entry(
            BIAS_NEUTRAL,
            "Balanced growth and contained inflation leave EURUSD driven more by relative "
            "data and central-bank nuances than by a single macro extreme. No strong "
            "structural tilt without a clear US–EU growth/rate gap.",
            " | ".join([
                "US data hot → BEARISH EURUSD (" + _INV["fed_hawkish"] + ")",
                "US data soft / EU strong → BULLISH EURUSD",
            ]),
        ),
        "GBPUSD": _entry(
            BIAS_NEUTRAL,
            "Similar to EURUSD: goldilocks is a range environment for GBPUSD until relative "
            "UK vs US growth or BoE vs Fed diverges.",
            " | ".join([
                "US outperformance → BEARISH GBPUSD",
                "UK outperformance or " + _INV["fed_dovish"] + " → BULLISH GBPUSD",
            ]),
        ),
        "USDCHF": _entry(
            BIAS_NEUTRAL,
            "With risk calm and inflation near target, CHF safe-haven premium fades and "
            "USDCHF tracks modest rate differentials — no extreme bias.",
            " | ".join([_INV["risk_off"] + " → BEARISH USDCHF", _INV["fed_hawkish"] + " → BULLISH USDCHF"]),
        ),
    },
    "OVERHEATING": {
        "XAUUSD": _entry(
            BIAS_BEARISH,
            "Overheating (strong growth + hot inflation) usually means a hawkish policy "
            "response and rising real-rate risk. That is typically a headwind for gold "
            "until something breaks in growth or markets.",
            " | ".join([
                _INV["us_growth_collapse"],
                _INV["fed_dovish"],
                "policy error / financial stress (" + _INV["risk_off"] + ")",
            ]),
        ),
        "EURUSD": _entry(
            BIAS_BEARISH,
            "Hot US demand and likely Fed hawkishness support USD vs EUR. EURUSD stays "
            "under pressure while US overheating narrative dominates.",
            " | ".join([_INV["fed_dovish"], _INV["ecb_hawkish"], "US growth rolls over hard"]),
        ),
        "GBPUSD": _entry(
            BIAS_BEARISH,
            "USD strength from overheating US conditions weighs on GBPUSD unless the UK "
            "is overheating even more and the BoE out-hawks the Fed.",
            " | ".join([_INV["fed_dovish"], _INV["boe_hawkish"], "US growth rolls over hard"]),
        ),
        "USDCHF": _entry(
            BIAS_BULLISH,
            "Hawkish USD + still-functioning risk markets → dollar bid vs CHF. Safe-haven "
            "CHF demand is secondary until risk breaks.",
            " | ".join([_INV["risk_off"], _INV["fed_dovish"]]),
        ),
    },
    "STAGFLATION": {
        "XAUUSD": _entry(
            BIAS_BULLISH,
            "Stagflation (weak growth + high inflation) is gold’s textbook regime: real "
            "assets and uncertainty hedges are bid, while policy is trapped between "
            "fighting prices and supporting growth.",
            " | ".join([
                "growth re-accelerates into a clean expansion (exits stagflation)",
                _INV["inflation_collapse"] + " with rising real rates",
                "aggressive successful disinflation without crisis",
            ]),
        ),
        "EURUSD": _entry(
            BIAS_NEUTRAL,
            "Stagflation is messy for EURUSD: weak growth is euro-negative, but a soft "
            "or conflicted Fed (if US is also stagnating) can hurt the dollar. Direction "
            "depends on *which side* stagnates more — default neutral with wide risk.",
            " | ".join([
                "US stagflation worse than Europe → BULLISH EURUSD",
                "Europe worse / ECB more constrained → BEARISH EURUSD",
            ]),
        ),
        "GBPUSD": _entry(
            BIAS_NEUTRAL,
            "Same relative-stagflation logic as EURUSD. GBP is sensitive to UK-specific "
            "energy/fiscal shocks; keep bias neutral until relative data clears.",
            " | ".join([
                "UK-specific inflation shock without Fed ease → BEARISH GBPUSD",
                "US-led stagflation + " + _INV["fed_dovish"] + " → BULLISH GBPUSD",
            ]),
        ),
        "USDCHF": _entry(
            BIAS_BEARISH,
            "Stagflation raises uncertainty and often supports CHF as a safe haven. "
            "Unless the dollar is the sole clean safe asset, USDCHF leans lower (CHF bid).",
            " | ".join([
                "USD uniquely preferred as safe haven vs CHF",
                _INV["risk_on"] + " with firm US rates",
            ]),
        ),
    },
    "SLOWDOWN": {
        "XAUUSD": _entry(
            BIAS_BULLISH,
            "Slowdown with contained inflation raises odds of easier policy ahead. Falling "
            "rate expectations and growth anxiety tend to support gold.",
            " | ".join([
                "growth re-accelerates without cuts (" + _INV["us_growth_surprise"] + ")",
                _INV["inflation_reheat"] + " forcing higher real rates",
            ]),
        ),
        "EURUSD": _entry(
            BIAS_BULLISH,
            "If the slowdown is US-led, rate-cut odds and a softer dollar support EURUSD. "
            "(If Europe is the epicenter, this bias would reverse — watch relative data.)",
            " | ".join([
                "Europe is the weak link, not the US",
                _INV["us_growth_surprise"],
                _INV["fed_hawkish"],
            ]),
        ),
        "GBPUSD": _entry(
            BIAS_BULLISH,
            "US-led slowdown → softer USD and easier Fed path usually lift GBPUSD, unless "
            "UK data is even weaker.",
            " | ".join([
                "UK growth scare dominates",
                _INV["us_growth_surprise"],
                _INV["boe_dovish"] + " much more than Fed",
            ]),
        ),
        "USDCHF": _entry(
            BIAS_BEARISH,
            "Growth anxiety + lower US yields often support CHF (and weigh on USDCHF), "
            "especially if risk softens with the slowdown.",
            " | ".join([_INV["risk_on"] + " with resilient US data", _INV["fed_hawkish"]]),
        ),
    },
    "RECESSION": {
        "XAUUSD": _entry(
            BIAS_BULLISH,
            "Demand-side recession → aggressive easing expectations and lower real rates. "
            "Gold historically benefits as policy pivots and uncertainty rises "
            "(with the caveat of forced liquidation in the *first* panic days).",
            " | ".join([
                "V-shaped recovery without deep cuts",
                "forced de-leveraging smash in metals (short-lived)",
                _INV["inflation_collapse"] + " already fully priced with rising real rates",
            ]),
        ),
        "EURUSD": _entry(
            BIAS_BULLISH,
            "US recession typically means Fed cutting cycles and USD softness over the "
            "medium term → constructive for EURUSD once the initial dollar-funding squeeze fades.",
            " | ".join([
                "global dollar funding squeeze (short-term USD spike)",
                "Europe in deeper recession than the US",
            ]),
        ),
        "GBPUSD": _entry(
            BIAS_BULLISH,
            "Same medium-term Fed-easing / softer-USD logic as EURUSD, subject to UK "
            "not being the weaker economy.",
            " | ".join([
                "UK recession deeper / BoE constrained",
                "acute dollar squeeze phase",
            ]),
        ),
        "USDCHF": _entry(
            BIAS_BEARISH,
            "Recession + risk stress → CHF safe-haven demand and lower US yields → USDCHF lower. "
            "Exception: pure USD funding crisis can lift USD temporarily.",
            " | ".join([
                "dollar funding crisis (USD spike vs all)",
                "rapid risk recovery without CHF demand",
            ]),
        ),
    },
    "DISINFLATION_EXPANSION": {
        "XAUUSD": _entry(
            BIAS_BEARISH,
            "Expansion with cooling inflation is often a soft-landing narrative: real rates "
            "can stay positive and gold’s hedge demand fades.",
            " | ".join([_INV["risk_off"], _INV["inflation_reheat"], _INV["fed_dovish"]]),
        ),
        "EURUSD": _entry(
            BIAS_NEUTRAL,
            "Soft landing is generally risk-friendly but pair direction depends on whether "
            "the US or euro area disinflates with stronger relative growth.",
            " | ".join(["clear US–EU growth divergence", "Fed vs ECB path divergence"]),
        ),
        "GBPUSD": _entry(
            BIAS_NEUTRAL,
            "Soft-landing regime → range-prone GBPUSD until relative UK/US data picks a side.",
            " | ".join(["clear UK–US growth divergence", "BoE vs Fed path divergence"]),
        ),
        "USDCHF": _entry(
            BIAS_BULLISH,
            "Risk-on soft landing reduces CHF haven bid; resilient USD rates can support USDCHF.",
            " | ".join([_INV["risk_off"], _INV["fed_dovish"]]),
        ),
    },
    # ── Policy / risk overlays ──────────────────────────────────────────
    "TIGHTENING_CYCLE": {
        "XAUUSD": _entry(
            BIAS_BEARISH,
            "Restrictive policy and QT raise real-rate and liquidity headwinds for gold. "
            "This is the classic 'higher for longer' pressure regime on XAUUSD.",
            " | ".join([
                "Fed pivot / " + _INV["fed_dovish"],
                _INV["us_growth_collapse"],
                _INV["risk_off"] + " with falling real yields",
            ]),
        ),
        "EURUSD": _entry(
            BIAS_BEARISH,
            "US tightening cycles historically support the dollar via rate differentials "
            "and capital flows → EURUSD bias down while the cycle is active.",
            " | ".join([_INV["fed_dovish"], "ECB tightening even more aggressively", "US growth break"]),
        ),
        "GBPUSD": _entry(
            BIAS_BEARISH,
            "Same USD rate-support logic: Fed tightening cycle weighs on GBPUSD unless "
            "BoE is tighter still and UK data is robust.",
            " | ".join([_INV["fed_dovish"], _INV["boe_hawkish"], "US growth break"]),
        ),
        "USDCHF": _entry(
            BIAS_BULLISH,
            "Higher US real rates in a tightening cycle typically support USD vs CHF "
            "when risk markets are not in freefall.",
            " | ".join([_INV["risk_off"], _INV["fed_dovish"]]),
        ),
    },
    "EASING_CYCLE": {
        "XAUUSD": _entry(
            BIAS_BULLISH,
            "Accommodative policy and expanding liquidity lower the opportunity cost of "
            "gold and often lift XAUUSD over the cycle.",
            " | ".join([
                "easing reversed (" + _INV["fed_hawkish"] + ")",
                "inflation crushed with rising real rates",
            ]),
        ),
        "EURUSD": _entry(
            BIAS_BULLISH,
            "Fed easing cycles usually soften the USD medium-term → constructive EURUSD, "
            "unless the ECB eases even more aggressively.",
            " | ".join([_INV["ecb_dovish"] + " more than Fed", _INV["fed_hawkish"], "US exceptional growth"]),
        ),
        "GBPUSD": _entry(
            BIAS_BULLISH,
            "Fed easing supports GBPUSD via softer USD, barring a deeper UK-specific bust.",
            " | ".join([_INV["boe_dovish"] + " more than Fed", _INV["fed_hawkish"], "UK crisis"]),
        ),
        "USDCHF": _entry(
            BIAS_BEARISH,
            "Lower US yields and easier policy reduce USD support; CHF can firm on lower "
            "global yields / residual caution → USDCHF bias down.",
            " | ".join([_INV["risk_on"] + " with US re-acceleration", _INV["fed_hawkish"]]),
        ),
    },
    "RISK_OFF": {
        "XAUUSD": _entry(
            BIAS_BULLISH,
            "Risk-off is a classic gold bid: flight to safety and uncertainty premium. "
            "(Watch for rare cash-raising liquidations in the first crash hours.)",
            " | ".join([_INV["risk_on"], "forced liquidation phase dominates price action"]),
        ),
        "EURUSD": _entry(
            BIAS_BEARISH,
            "In risk-off, the dollar often acts as a funding / reserve safe asset. EURUSD "
            "typically falls as investors buy USD and cut risk exposures.",
            " | ".join([_INV["risk_on"], "USD-specific crisis that hurts the dollar"]),
        ),
        "GBPUSD": _entry(
            BIAS_BEARISH,
            "GBP is a pro-cyclical G10 currency; risk-off and dollar demand usually push GBPUSD down.",
            " | ".join([_INV["risk_on"], "UK safe-haven narrative (rare)"]),
        ),
        "USDCHF": _entry(
            BIAS_BEARISH,
            "CHF is a premier safe haven. Risk-off → CHF bid. Even if USD is also bought, "
            "CHF often outperforms enough that USDCHF trends lower in sustained stress. "
            "Net bias: BEARISH USDCHF (CHF strength).",
            " | ".join([
                "pure dollar funding squeeze where USD outperforms CHF",
                _INV["risk_on"],
            ]),
        ),
    },
    "TRANSITION": {
        "XAUUSD": _entry(
            BIAS_NEUTRAL,
            "Mixed macro signals — no high-conviction gold stance until growth, inflation, "
            "and policy dials align.",
            "Clear regime resolution (e.g. confirmed REFLATION, STAGFLATION, or RECESSION).",
        ),
        "EURUSD": _entry(
            BIAS_NEUTRAL,
            "Transition regime: wait for relative data and Fed/ECB clarity before committing.",
            "Sustained US–EU growth or policy divergence.",
        ),
        "GBPUSD": _entry(
            BIAS_NEUTRAL,
            "Transition regime: wait for relative UK/US data and BoE/Fed clarity.",
            "Sustained UK–US growth or policy divergence.",
        ),
        "USDCHF": _entry(
            BIAS_NEUTRAL,
            "Transition regime: risk and rate signals conflict — stay neutral on USDCHF.",
            "Clear risk-off or risk-on with a consistent rate differential.",
        ),
    },
}


# =============================================================================
# DIAL TILTS — refine base playbook using policy / risk / liquidity
# =============================================================================

def _bias_rank(bias: str) -> int:
    return {BIAS_BEARISH: -1, BIAS_NEUTRAL: 0, BIAS_BULLISH: 1}.get(bias, 0)


def _rank_to_bias(rank: int) -> str:
    if rank > 0:
        return BIAS_BULLISH
    if rank < 0:
        return BIAS_BEARISH
    return BIAS_NEUTRAL


def _clamp_rank(r: int) -> int:
    return max(-1, min(1, r))


def apply_dial_tilts(
    symbol: str,
    base_bias: str,
    macro: dict,
) -> tuple[str, list[str]]:
    """
    Nudge the regime baseline using policy / liquidity / risk dials.
    Returns (adjusted_bias, list of tilt notes for the thesis text).
    """
    rank = _bias_rank(base_bias)
    notes: list[str] = []
    policy = (macro.get("policy") or "").upper()
    liquidity = (macro.get("liquidity") or "").upper()
    risk = (macro.get("risk") or "").upper()
    inflation = (macro.get("inflation") or "").upper()

    # --- Gold: real-rate / liquidity / risk sensitive ---
    if symbol == "XAUUSD":
        if policy == "RESTRICTIVE":
            rank -= 1
            notes.append("Policy RESTRICTIVE → higher real-rate headwind for gold (−).")
        elif policy == "ACCOMMODATIVE":
            rank += 1
            notes.append("Policy ACCOMMODATIVE → lower opportunity cost for gold (+).")
        if liquidity == "TIGHTENING":
            rank -= 1
            notes.append("Liquidity TIGHTENING (QT/money squeeze) → gold (−).")
        elif liquidity == "EXPANDING":
            rank += 1
            notes.append("Liquidity EXPANDING → gold (+).")
        if risk == "RISK_OFF":
            rank += 1
            notes.append("Risk RISK_OFF → safe-haven gold (+).")
        if inflation in ("HIGH", "ELEVATED") and policy != "RESTRICTIVE":
            rank += 1
            notes.append("Elevated inflation without tight policy → gold hedge (+).")

    # --- EUR / GBP: USD rate differential & risk ---
    if symbol in ("EURUSD", "GBPUSD"):
        if policy == "RESTRICTIVE":
            rank -= 1
            notes.append("US policy RESTRICTIVE → USD support → pair (−).")
        elif policy == "ACCOMMODATIVE":
            rank += 1
            notes.append("US policy ACCOMMODATIVE → softer USD → pair (+).")
        if risk == "RISK_OFF":
            rank -= 1
            notes.append("Risk-off dollar bid → pair (−).")
        elif risk == "RISK_ON" and policy != "RESTRICTIVE":
            rank += 1
            notes.append("Risk-on with non-restrictive US policy → pair (+).")

    # --- USDCHF: risk-off CHF vs USD rates ---
    if symbol == "USDCHF":
        if risk == "RISK_OFF":
            rank -= 1
            notes.append("Risk-off → CHF haven bid → USDCHF (−).")
        elif risk == "RISK_ON":
            rank += 1
            notes.append("Risk-on → less CHF haven demand → USDCHF (+).")
        if policy == "RESTRICTIVE":
            rank += 1
            notes.append("US RESTRICTIVE policy → USD rate support → USDCHF (+).")
        elif policy == "ACCOMMODATIVE":
            rank -= 1
            notes.append("US ACCOMMODATIVE policy → softer USD → USDCHF (−).")

    return _rank_to_bias(_clamp_rank(rank)), notes


def build_thesis_for_symbol(symbol: str, macro: dict) -> dict:
    """
    Build one InstrumentThesis dict from a MacroState snapshot.
    """
    symbol = symbol.upper()
    if symbol not in TRACKED_SYMBOLS:
        raise ValueError(f"Unsupported symbol {symbol}; expected one of {TRACKED_SYMBOLS}")

    regime = (macro.get("regime") or "TRANSITION").upper()
    if regime not in PLAYBOOK:
        regime = "TRANSITION"

    base = PLAYBOOK[regime][symbol]
    bias, tilt_notes = apply_dial_tilts(symbol, base["bias"], macro)

    dial_line = (
        f"Macro dials — growth={macro.get('growth')}, inflation={macro.get('inflation')}, "
        f"policy={macro.get('policy')}, liquidity={macro.get('liquidity')}, risk={macro.get('risk')}."
    )
    thesis_parts = [
        f"Regime: {regime} (confidence {float(macro.get('confidence') or 0):.0%}).",
        base["thesis"],
        dial_line,
    ]
    if tilt_notes:
        thesis_parts.append("Dial tilts applied: " + " ".join(tilt_notes))
    if bias != base["bias"]:
        thesis_parts.append(
            f"Baseline playbook bias was {base['bias']}; after dial tilts → {bias}."
        )

    inv = base["invalidation"]
    # Add dial-aware invalidation reminders
    extra_inv = []
    if symbol == "XAUUSD" and bias == BIAS_BULLISH:
        extra_inv.append(_INV["real_rates_up"])
    if symbol == "XAUUSD" and bias == BIAS_BEARISH:
        extra_inv.append(_INV["real_rates_down"] + " or " + _INV["risk_off"])
    if symbol in ("EURUSD", "GBPUSD") and bias == BIAS_BEARISH:
        extra_inv.append(_INV["fed_dovish"])
    if symbol in ("EURUSD", "GBPUSD") and bias == BIAS_BULLISH:
        extra_inv.append(_INV["fed_hawkish"] + " or " + _INV["risk_off"])
    if extra_inv:
        inv = inv + " | Also watch: " + " | ".join(extra_inv)

    return {
        "symbol": symbol,
        "current_bias": bias,
        "active_thesis": " ".join(thesis_parts),
        "invalidation_triggers": inv,
        "regime": regime,
        "growth": macro.get("growth"),
        "inflation": macro.get("inflation"),
        "policy": macro.get("policy"),
        "liquidity": macro.get("liquidity"),
        "risk": macro.get("risk"),
        "confidence": macro.get("confidence"),
        "macro_as_of": macro.get("as_of"),
        "playbook_version": PLAYBOOK_VERSION,
        "last_updated": datetime.now().isoformat(timespec="seconds"),
        "baseline_bias": base["bias"],
    }


def build_all_theses(macro: dict) -> list[dict]:
    return [build_thesis_for_symbol(sym, macro) for sym in TRACKED_SYMBOLS]


# =============================================================================
# ENGINE — persist & update
# =============================================================================

class InstrumentThesisEngine:
    """
    Maintains living InstrumentThesis rows in SQLite.
    Call update_from_macro_state() whenever MacroState changes.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _default_db()
        self._ensure_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _ensure_tables(self) -> None:
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute(INSTRUMENT_THESIS_TABLE_SQL)
                conn.execute(INSTRUMENT_THESIS_HISTORY_SQL)
                conn.commit()
            finally:
                conn.close()

    def save_theses(self, theses: list[dict], write_history: bool = True) -> int:
        """Upsert current theses (one row per symbol). Optionally append history."""
        n = 0
        with _DB_LOCK:
            conn = self._connect()
            try:
                for t in theses:
                    conn.execute(
                        "INSERT INTO instrument_thesis ("
                        "symbol, current_bias, active_thesis, invalidation_triggers, "
                        "regime, growth, inflation, policy, liquidity, risk, "
                        "confidence, macro_as_of, playbook_version, last_updated"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(symbol) DO UPDATE SET "
                        "current_bias=excluded.current_bias, "
                        "active_thesis=excluded.active_thesis, "
                        "invalidation_triggers=excluded.invalidation_triggers, "
                        "regime=excluded.regime, growth=excluded.growth, "
                        "inflation=excluded.inflation, policy=excluded.policy, "
                        "liquidity=excluded.liquidity, risk=excluded.risk, "
                        "confidence=excluded.confidence, macro_as_of=excluded.macro_as_of, "
                        "playbook_version=excluded.playbook_version, "
                        "last_updated=excluded.last_updated",
                        (
                            t["symbol"], t["current_bias"], t["active_thesis"],
                            t["invalidation_triggers"], t.get("regime"), t.get("growth"),
                            t.get("inflation"), t.get("policy"), t.get("liquidity"),
                            t.get("risk"), t.get("confidence"), t.get("macro_as_of"),
                            t.get("playbook_version", PLAYBOOK_VERSION), t["last_updated"],
                        ),
                    )
                    if write_history:
                        conn.execute(
                            "INSERT INTO instrument_thesis_history ("
                            "symbol, current_bias, active_thesis, invalidation_triggers, "
                            "regime, macro_as_of, playbook_version, recorded_at"
                            ") VALUES (?,?,?,?,?,?,?,?)",
                            (
                                t["symbol"], t["current_bias"], t["active_thesis"],
                                t["invalidation_triggers"], t.get("regime"),
                                t.get("macro_as_of"), t.get("playbook_version", PLAYBOOK_VERSION),
                                t["last_updated"],
                            ),
                        )
                    n += 1
                conn.commit()
            finally:
                conn.close()
        return n

    def update_from_macro_state(self, macro: dict, write_history: bool = True) -> list[dict]:
        """Main entry: MacroState dict → build theses → save → return list."""
        if not macro or not macro.get("regime"):
            return []
        theses = build_all_theses(macro)
        self.save_theses(theses, write_history=write_history)
        return theses

    def get_all(self) -> list[dict]:
        with _DB_LOCK:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT symbol, current_bias, active_thesis, invalidation_triggers, "
                    "regime, growth, inflation, policy, liquidity, risk, confidence, "
                    "macro_as_of, playbook_version, last_updated "
                    "FROM instrument_thesis ORDER BY symbol"
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            finally:
                conn.close()

    def get_one(self, symbol: str) -> Optional[dict]:
        symbol = symbol.upper()
        with _DB_LOCK:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT symbol, current_bias, active_thesis, invalidation_triggers, "
                    "regime, growth, inflation, policy, liquidity, risk, confidence, "
                    "macro_as_of, playbook_version, last_updated "
                    "FROM instrument_thesis WHERE symbol = ?",
                    (symbol,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
            finally:
                conn.close()

    @staticmethod
    def format_summary(theses: list[dict], macro: Optional[dict] = None) -> str:
        lines = []
        lines.append("=" * 72)
        lines.append("  INSTRUMENT THESES  (regime → bias playbook)")
        lines.append("=" * 72)
        if macro:
            lines.append(
                f"  Macro regime: {macro.get('regime')}  |  as_of: {macro.get('as_of')}  |  "
                f"growth={macro.get('growth')} inflation={macro.get('inflation')} "
                f"policy={macro.get('policy')} risk={macro.get('risk')}"
            )
            lines.append("")
        # Stable symbol order
        by_sym = {t["symbol"]: t for t in theses}
        for sym in TRACKED_SYMBOLS:
            t = by_sym.get(sym)
            if not t:
                lines.append(f"  {sym}: (no thesis yet)")
                continue
            bias = t.get("current_bias", "?")
            icon = {"BULLISH": "▲", "BEARISH": "▼", "NEUTRAL": "◆"}.get(bias, "·")
            lines.append(f"  {icon} {sym:<8}  {bias}")
            thesis = (t.get("active_thesis") or "")[:280]
            lines.append(f"      Thesis: {thesis}{'…' if len(t.get('active_thesis') or '') > 280 else ''}")
            inv = (t.get("invalidation_triggers") or "")[:200]
            lines.append(f"      Invalidation: {inv}{'…' if len(t.get('invalidation_triggers') or '') > 200 else ''}")
            lines.append("")
        lines.append("=" * 72)
        return "\n".join(lines)


# =============================================================================
# CLI / self-test
# =============================================================================

if __name__ == "__main__":
    from macro_state_analyzer import MacroStateAnalyzer

    analyzer = MacroStateAnalyzer(auto_save=False)
    engine = InstrumentThesisEngine()

    macro = analyzer.latest_state()
    if not macro:
        print("No MacroState in DB — using a demo REFLATION snapshot.")
        macro = {
            "as_of": datetime.now().date().isoformat(),
            "regime": "REFLATION",
            "growth": "TREND",
            "inflation": "ELEVATED",
            "policy": "RESTRICTIVE",
            "liquidity": "EXPANDING",
            "risk": "NEUTRAL",
            "confidence": 0.7,
        }

    theses = engine.update_from_macro_state(macro, write_history=True)
    print(InstrumentThesisEngine.format_summary(theses, macro))
    print(f"  Saved {len(theses)} theses to instrument_thesis (playbook {PLAYBOOK_VERSION}).")
