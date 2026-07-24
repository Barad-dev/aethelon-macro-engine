# -*- coding: utf-8 -*-
"""
macro_state_analyzer.py — Textbook Macro Regime Engine
=======================================================
Turns raw FRED numbers into a structured MacroState:

  growth · inflation · policy · liquidity · risk · regime

Designed as a *learning* layer: every snapshot includes a plain-English
"lesson" explaining the economic logic (not just labels).

Does NOT require numpy/pandas — pure Python + SQLite.

Integration:
  - Table `macro_state` is created by news_engine._ensure_db_schema()
  - Call MacroStateAnalyzer(...).analyze_and_save(fred_data) after FRED refresh
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


# =============================================================================
# SCHEMA (also applied in news_engine._ensure_db_schema for one-stop init)
# =============================================================================

MACRO_STATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS macro_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    growth TEXT NOT NULL,
    inflation TEXT NOT NULL,
    policy TEXT NOT NULL,
    liquidity TEXT NOT NULL,
    risk TEXT NOT NULL,
    regime TEXT NOT NULL,
    growth_score REAL,
    inflation_score REAL,
    policy_score REAL,
    liquidity_score REAL,
    risk_score REAL,
    confidence REAL,
    metrics_json TEXT,
    lesson TEXT,
    rules_version TEXT,
    created_at TEXT NOT NULL
)
"""

MACRO_STATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_macro_state_as_of ON macro_state(as_of DESC)
"""

# One snapshot per calendar date + rules version (safe re-runs / no duplicates)
MACRO_STATE_UNIQUE_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_macro_state_asof_rules
ON macro_state(as_of, rules_version)
"""

# Bump when thresholds/logic change so history stays interpretable
RULES_VERSION = "textbook_v1"

# Series required to reconstruct historical dials (monthly backfill)
BACKFILL_SERIES = (
    "CPIAUCSL", "PCEPI", "PCEPILFE",  # PCEPILFE = Core PCE (FRED id; not "COREPCE")
    "T10YIE",
    "GDP", "UNRATE", "PAYEMS",
    "FEDFUNDS", "DGS10", "DGS2",
    "M2SL", "WALCL", "VIXCLS",
)

# Default DB path — %APPDATA%\Quantamental\data\ (see paths.py)
try:
    from paths import get_db_path_str as _resolve_db
    def _default_db() -> str:
        return _resolve_db(migrate=True)
except Exception:  # pragma: no cover — paths always present in prod tree
    def _default_db() -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_engine_store.db")

_DEFAULT_DB = None  # resolved lazily via _default_db() so migration runs once
_DB_LOCK = threading.RLock()


# =============================================================================
# TEXTBOOK RULES (thresholds)
# =============================================================================
# These are *teaching approximations* of common macro frameworks:
#   • Growth / inflation matrix (business-cycle & "four regimes" intuition)
#   • Taylor-rule spirit for policy (real rate ≈ policy rate − inflation)
#   • Liquidity via money (M2) and Fed balance sheet (WALCL)
#   • Risk appetite via VIX (fear gauge)
#
# They are not official Fed definitions. Ranges are deliberately simple
# so you can read them and learn.

# --- Inflation (expected / realized proxies, in percent) ---
# Fed long-run PCE target ≈ 2%. "Elevated" starts clearly above that.
INFLATION_HIGH = 4.0       # %  — clearly too hot (1970s-style worry zone)
INFLATION_ELEVATED = 2.5   # %  — above target, sticky risk
INFLATION_TARGET_LO = 1.5  # %  — near dual-mandate comfort
INFLATION_TARGET_HI = 2.5  # %
INFLATION_LOW = 1.5        # %  — below target / disinflation pressure

# --- Growth proxies ---
# Unemployment: US "full employment" often discussed near ~4% (not exact NAIRU).
UNRATE_STRONG = 3.8        # %  — tight labor market → strong growth signal
UNRATE_TREND = 4.5         # %  — roughly normal
UNRATE_WEAK = 5.5          # %  — slack building
UNRATE_CONTRACT = 6.5      # %  — recession-style labor damage

# Real GDP QoQ annualized (if we can form it); else we lean on jobs.
GDP_STRONG = 2.5           # % annualized
GDP_TREND = 1.5
GDP_WEAK = 0.5
GDP_CONTRACT = 0.0         # ≤ 0 → contracting

# Payrolls MoM (000s) — rough rule of thumb for "healthy" job growth
PAYEMS_STRONG = 200.0
PAYEMS_WEAK = 50.0
PAYEMS_CONTRACT = -50.0

# --- Policy (Taylor-rule spirit) ---
# Real policy rate = FEDFUNDS − inflation proxy
# Restrictive: real rate clearly positive; accommodative: clearly negative
REAL_RATE_RESTRICTIVE = 1.0    # %
REAL_RATE_ACCOMMODATIVE = -0.5 # %
# Nominal Fed funds "emergency low" vs "high"
FEDFUNDS_LOW = 1.0
FEDFUNDS_HIGH = 4.5

# --- Liquidity ---
# M2 YoY (or short-run annualized): expanding money → easier financial conditions
M2_EXPAND_YOY = 4.0        # %
M2_TIGHT_YOY = 0.0         # %  — flat/shrinking M2
# Fed balance sheet MoM % of level: QT vs QE
WALCL_EXPAND_PCT = 0.3     # % MoM expansion
WALCL_TIGHT_PCT = -0.3     # % MoM shrinkage (QT)

# --- Risk (VIX) ---
VIX_RISK_OFF = 25.0        # elevated fear
VIX_NEUTRAL_HI = 20.0
VIX_RISK_ON = 15.0         # complacent / risk appetite

# Dimension labels (stored in DB)
GROWTH_LABELS = ("STRONG", "TREND", "WEAK", "CONTRACTING")
INFLATION_LABELS = ("HIGH", "ELEVATED", "TARGET", "LOW")
POLICY_LABELS = ("RESTRICTIVE", "NEUTRAL", "ACCOMMODATIVE")
LIQUIDITY_LABELS = ("EXPANDING", "STABLE", "TIGHTENING")
RISK_LABELS = ("RISK_ON", "NEUTRAL", "RISK_OFF")

# Growth×Inflation regime map (classic teaching matrix)
# Rows = growth, Cols = inflation → regime name
REGIME_MATRIX = {
    # (growth, inflation): regime
    ("STRONG", "HIGH"):      "OVERHEATING",
    ("STRONG", "ELEVATED"):  "REFLATION",
    ("STRONG", "TARGET"):    "GOLDILOCKS",
    ("STRONG", "LOW"):       "GOLDILOCKS",
    ("TREND",  "HIGH"):      "REFLATION",
    ("TREND",  "ELEVATED"):  "REFLATION",
    ("TREND",  "TARGET"):    "GOLDILOCKS",
    ("TREND",  "LOW"):       "DISINFLATION_EXPANSION",
    ("WEAK",   "HIGH"):      "STAGFLATION",
    ("WEAK",   "ELEVATED"):  "STAGFLATION",
    ("WEAK",   "TARGET"):    "SLOWDOWN",
    ("WEAK",   "LOW"):       "SLOWDOWN",
    ("CONTRACTING", "HIGH"):     "STAGFLATION",
    ("CONTRACTING", "ELEVATED"): "STAGFLATION",
    ("CONTRACTING", "TARGET"):   "RECESSION",
    ("CONTRACTING", "LOW"):      "RECESSION",
}

REGIME_LESSONS = {
    "OVERHEATING": (
        "Growth is strong and inflation is hot. Textbooks call this late-cycle heat: "
        "demand may be outrunning supply. Central banks often tighten. "
        "Gold can struggle if real rates rise; the dollar may firm with hawkish policy."
    ),
    "REFLATION": (
        "Activity is firming while prices run above comfort. Classic reflation: "
        "recovering demand lifts growth and inflation together. Policy may lean hawkish "
        "if inflation is sticky. Risk assets and commodities often do well early; "
        "gold depends on whether real yields rise faster than inflation fears."
    ),
    "GOLDILOCKS": (
        "Growth is healthy and inflation is near target — the 'just right' mix. "
        "Historically friendly for risk assets; gold less urgent as a hedge. "
        "Policy can stay patient unless something breaks."
    ),
    "DISINFLATION_EXPANSION": (
        "Growth continues while inflation cools toward or below target. "
        "Often a soft-landing narrative: good for bonds and sometimes equities; "
        "gold may lag if real rates stay positive."
    ),
    "STAGFLATION": (
        "Growth is weak (or falling) while inflation stays high — the painful mix. "
        "Supply shocks and 1970s-style episodes are the textbook examples. "
        "Policy faces a trade-off: fight prices (hurt growth) or support growth (risk more inflation). "
        "Gold often finds support as a real-asset / uncertainty hedge; growth FX can suffer."
    ),
    "SLOWDOWN": (
        "Growth is soft but inflation is not the main fire. Demand is cooling. "
        "Markets watch for recession risk and earlier rate cuts. "
        "Duration (bonds) can help; cyclical assets may lag."
    ),
    "RECESSION": (
        "Output/labor signals look contractionary and inflation is not elevated. "
        "Classic demand-side recession: policy usually eases. "
        "Safe havens and rate-cut expectations matter; gold can rise if real rates fall."
    ),
    "TIGHTENING_CYCLE": (
        "The macro mix is dominated by restrictive policy (high real rates). "
        "Financial conditions bite growth with a lag. Watch credit and labor for cracks."
    ),
    "EASING_CYCLE": (
        "Policy is clearly accommodative (low or negative real rates). "
        "Liquidity and lower rates support risk assets and often gold over time."
    ),
    "RISK_OFF": (
        "Fear dominates (elevated VIX / stress). Flight-to-safety can override the "
        "growth-inflation matrix short term — dollar, yen, Swiss franc, gold bid."
    ),
    "TRANSITION": (
        "Signals disagree or data is incomplete. Do not force a strong view — "
        "wait for confirmation from jobs, inflation, and the Fed path."
    ),
}


# =============================================================================
# FRED SERIES KEYS WE USE
# =============================================================================

# series_id → role in the framework
SERIES_ROLES = {
    "CPIAUCSL": "cpi_index",       # CPI all items (index) → short-run inflation impulse
    "PCEPI": "pce_index",
    "PCEPILFE": "core_pce_index",  # Core PCE (ex food & energy) — correct FRED id
    "COREPCE": "core_pce_index",   # alias kept for older store keys
    "T10YIE": "breakeven_10y",     # market expected inflation (%)
    "GDP": "gdp_level",            # nominal GDP level (quarterly)
    "UNRATE": "unemployment",      # %
    "PAYEMS": "payrolls",          # thousands
    "FEDFUNDS": "fed_funds",       # %
    "DGS10": "yield_10y",
    "DGS2": "yield_2y",
    "M2SL": "m2",                  # money supply
    "WALCL": "fed_balance_sheet",  # Fed assets
    "VIXCLS": "vix",
}


# =============================================================================
# NUMERIC HELPERS (no numpy)
# =============================================================================

def _f(val: Any) -> Optional[float]:
    try:
        if val is None or val == ".":
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _obs_values(obs_list: list[dict], max_n: int = 24) -> list[tuple[str, float]]:
    """Return [(date, value), ...] newest first, skipping missing."""
    out: list[tuple[str, float]] = []
    for row in (obs_list or [])[:max_n]:
        v = _f(row.get("value"))
        if v is None:
            continue
        out.append((str(row.get("date", "")), v))
    return out


def _latest(obs_list: list[dict]) -> Optional[float]:
    vals = _obs_values(obs_list, 1)
    return vals[0][1] if vals else None


def _prev(obs_list: list[dict]) -> Optional[float]:
    vals = _obs_values(obs_list, 2)
    return vals[1][1] if len(vals) > 1 else None


def _pct_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / abs(old) * 100.0


def _annualized_from_mom(mom_pct: Optional[float], periods_per_year: int = 12) -> Optional[float]:
    """Compound MoM % into rough annualized %: (1+r)^n - 1."""
    if mom_pct is None:
        return None
    r = mom_pct / 100.0
    try:
        return ((1.0 + r) ** periods_per_year - 1.0) * 100.0
    except Exception:
        return mom_pct * periods_per_year


def _trend_direction(obs_list: list[dict], n: int = 3) -> Optional[str]:
    """up / down / flat from first vs last of newest n points."""
    vals = _obs_values(obs_list, n)
    if len(vals) < 2:
        return None
    newest, oldest = vals[0][1], vals[-1][1]
    if oldest == 0:
        return None
    chg = (newest - oldest) / abs(oldest) * 100.0
    if chg > 0.15:
        return "up"
    if chg < -0.15:
        return "down"
    return "flat"


# =============================================================================
# DIMENSION SCORERS
# =============================================================================

def score_inflation(fred: dict) -> tuple[str, float, dict, str]:
    """
    Label inflation: HIGH | ELEVATED | TARGET | LOW

    Learning logic:
      Markets and the Fed care about *price pressure relative to ~2%*.
      We blend:
        1) 10y breakeven (T10YIE) — what markets *expect*
        2) Short-run CPI/PCE impulse — what is *happening now*
    """
    metrics: dict[str, Any] = {}
    parts: list[str] = []
    score = 0.0  # higher = hotter inflation
    weight = 0.0

    be = _latest(fred.get("T10YIE", []))
    if be is not None:
        metrics["breakeven_10y_pct"] = be
        score += be * 2.0
        weight += 2.0
        parts.append(f"Markets price ~{be:.2f}% inflation over 10 years (breakevens).")

    # Prefer true YoY when we have ~12 months of CPI/PCE (historical ledger);
    # fall back to short-run annualized MoM only if needed.
    cpi_vals = _obs_values(fred.get("CPIAUCSL", []), 14)
    if len(cpi_vals) >= 13:
        yoy = _pct_change(cpi_vals[0][1], cpi_vals[12][1])
        metrics["cpi_yoy_pct"] = yoy
        if yoy is not None:
            score += yoy * 2.0
            weight += 2.0
            parts.append(f"CPI year-over-year ≈ {yoy:.1f}%.")
    elif len(cpi_vals) >= 2:
        mom = _pct_change(cpi_vals[0][1], cpi_vals[1][1])
        ann = _annualized_from_mom(mom, 12)
        metrics["cpi_mom_pct"] = mom
        metrics["cpi_ann_approx_pct"] = ann
        if ann is not None:
            # Cap extreme MoM noise (single-month annualization is volatile)
            ann_use = max(-5.0, min(15.0, ann))
            score += ann_use * 1.0
            weight += 1.0
            parts.append(f"CPI short-run pace ≈ {ann_use:.1f}% annualized (from latest month; noisy).")

    pce_vals = _obs_values(fred.get("PCEPI", []), 14)
    if len(pce_vals) >= 13:
        yoy = _pct_change(pce_vals[0][1], pce_vals[12][1])
        metrics["pce_yoy_pct"] = yoy
        if yoy is not None:
            score += yoy * 1.5
            weight += 1.5
            parts.append(f"PCE year-over-year ≈ {yoy:.1f}%.")
    elif len(pce_vals) >= 2:
        mom = _pct_change(pce_vals[0][1], pce_vals[1][1])
        ann = _annualized_from_mom(mom, 12)
        metrics["pce_ann_approx_pct"] = ann
        if ann is not None:
            ann_use = max(-5.0, min(15.0, ann))
            score += ann_use * 0.8
            weight += 0.8
            parts.append(f"PCE short-run pace ≈ {ann_use:.1f}% annualized.")

    # Preferred single "inflation proxy" for labels:
    # 1) CPI YoY  2) PCE YoY  3) breakeven  4) short-run approx
    proxy = (
        metrics.get("cpi_yoy_pct")
        if metrics.get("cpi_yoy_pct") is not None
        else metrics.get("pce_yoy_pct")
        if metrics.get("pce_yoy_pct") is not None
        else be
        if be is not None
        else metrics.get("cpi_ann_approx_pct")
        or metrics.get("pce_ann_approx_pct")
    )
    metrics["inflation_proxy_pct"] = proxy

    if proxy is None:
        return "TARGET", 0.0, metrics, "Not enough inflation data — defaulting to TARGET (neutral teaching default)."

    if proxy >= INFLATION_HIGH:
        label = "HIGH"
    elif proxy >= INFLATION_ELEVATED:
        label = "ELEVATED"
    elif proxy >= INFLATION_LOW:
        label = "TARGET"
    else:
        label = "LOW"

    # Score 0–10 for dashboards
    # map ~0–6% proxy into 0–10
    heat = max(0.0, min(10.0, (proxy / 6.0) * 10.0))
    lesson = " ".join(parts) + f" Rule: ≥{INFLATION_HIGH}% → HIGH, ≥{INFLATION_ELEVATED}% → ELEVATED, ~2% band → TARGET, <{INFLATION_LOW}% → LOW."
    return label, heat, metrics, lesson


def score_growth(fred: dict) -> tuple[str, float, dict, str]:
    """
    Label growth: STRONG | TREND | WEAK | CONTRACTING

    Learning logic:
      Growth is about whether the economy is expanding capacity and demand.
      We use the labor market (timely) and GDP (slower, authoritative).
      High unemployment / falling payrolls = weak or contracting growth.
    """
    metrics: dict[str, Any] = {}
    parts: list[str] = []
    votes: dict[str, float] = {g: 0.0 for g in GROWTH_LABELS}

    un = _latest(fred.get("UNRATE", []))
    if un is not None:
        metrics["unemployment_pct"] = un
        parts.append(f"Unemployment is {un:.1f}%.")
        if un <= UNRATE_STRONG:
            votes["STRONG"] += 2.0
        elif un <= UNRATE_TREND:
            votes["TREND"] += 2.0
        elif un <= UNRATE_WEAK:
            votes["WEAK"] += 2.0
        else:
            votes["CONTRACTING"] += 2.0
        # Rising unemployment is a classic recession warning (Sahm-rule spirit)
        un_prev = _prev(fred.get("UNRATE", []))
        if un_prev is not None and un - un_prev >= 0.3:
            votes["WEAK"] += 1.0
            votes["CONTRACTING"] += 0.5
            parts.append("Unemployment is rising quickly — a caution flag for growth.")

    pay = _obs_values(fred.get("PAYEMS", []), 3)
    if len(pay) >= 2:
        # PAYEMS is level of employment (thousands); MoM change ≈ jobs added
        delta = pay[0][1] - pay[1][1]
        metrics["payrolls_mom_thousands"] = delta
        parts.append(f"Payrolls changed by about {delta:+.0f}k in the latest month.")
        if delta >= PAYEMS_STRONG:
            votes["STRONG"] += 1.5
        elif delta >= PAYEMS_WEAK:
            votes["TREND"] += 1.5
        elif delta >= PAYEMS_CONTRACT:
            votes["WEAK"] += 1.5
        else:
            votes["CONTRACTING"] += 2.0

    gdp = _obs_values(fred.get("GDP", []), 3)
    if len(gdp) >= 2:
        qoq = _pct_change(gdp[0][1], gdp[1][1])
        # Quarterly % → rough annualized
        ann = _annualized_from_mom(qoq, 4) if qoq is not None else None
        metrics["gdp_qoq_pct"] = qoq
        metrics["gdp_ann_approx_pct"] = ann
        if ann is not None:
            parts.append(f"GDP pace ≈ {ann:.1f}% annualized (from latest quarter).")
            if ann >= GDP_STRONG:
                votes["STRONG"] += 1.5
            elif ann >= GDP_TREND:
                votes["TREND"] += 1.5
            elif ann > GDP_CONTRACT:
                votes["WEAK"] += 1.5
            else:
                votes["CONTRACTING"] += 2.0

    if sum(votes.values()) <= 0:
        return "TREND", 5.0, metrics, "Not enough growth data — defaulting to TREND."

    label = max(votes, key=votes.get)
    # Strength score 0–10 (STRONG=high)
    order = {"CONTRACTING": 1.5, "WEAK": 4.0, "TREND": 6.5, "STRONG": 9.0}
    strength = order.get(label, 5.0)
    lesson = " ".join(parts) + (
        f" Rule of thumb: unemployment ≤{UNRATE_STRONG}% or solid job gains → STRONG; "
        f"~{UNRATE_TREND}% → TREND; rising slack → WEAK; deep labor damage → CONTRACTING."
    )
    return label, strength, metrics, lesson


def score_policy(fred: dict, inflation_proxy: Optional[float]) -> tuple[str, float, dict, str]:
    """
    Label policy: RESTRICTIVE | NEUTRAL | ACCOMMODATIVE

    Learning logic (Taylor-rule spirit):
      What matters for the economy is the *real* policy rate:
          real rate ≈ Fed funds − inflation
      If real rates are clearly positive, policy is fighting inflation (restrictive).
      If negative, policy is still stimulating (accommodative).
    """
    metrics: dict[str, Any] = {}
    parts: list[str] = []

    ff = _latest(fred.get("FEDFUNDS", []))
    metrics["fed_funds_pct"] = ff
    if ff is None:
        return "NEUTRAL", 5.0, metrics, "No Fed funds data — policy labeled NEUTRAL."

    parts.append(f"Fed funds rate is {ff:.2f}%.")
    infl = inflation_proxy if inflation_proxy is not None else 2.0
    metrics["inflation_used_for_real_rate"] = infl
    real = ff - infl
    metrics["real_policy_rate_pct"] = real
    parts.append(f"Real policy rate ≈ {ff:.2f}% − {infl:.2f}% = {real:.2f}%.")

    if real >= REAL_RATE_RESTRICTIVE or ff >= FEDFUNDS_HIGH:
        label = "RESTRICTIVE"
        stance = 8.5  # high = tight
    elif real <= REAL_RATE_ACCOMMODATIVE or ff <= FEDFUNDS_LOW:
        label = "ACCOMMODATIVE"
        stance = 2.0
    else:
        label = "NEUTRAL"
        stance = 5.0

    # Yield curve slope (2s10s) — inversion often warns of tight past policy
    y2 = _latest(fred.get("DGS2", []))
    y10 = _latest(fred.get("DGS10", []))
    if y2 is not None and y10 is not None:
        slope = y10 - y2
        metrics["yield_curve_2s10s_pct"] = slope
        parts.append(f"Yield curve 10y−2y ≈ {slope:+.2f} pp.")
        if slope < -0.25:
            parts.append("An inverted curve often means markets expect tighter conditions / slower growth ahead.")

    lesson = " ".join(parts) + (
        f" Textbook rule: real rate ≥ +{REAL_RATE_RESTRICTIVE}% → RESTRICTIVE; "
        f"≤ {REAL_RATE_ACCOMMODATIVE}% → ACCOMMODATIVE; else NEUTRAL."
    )
    return label, stance, metrics, lesson


def score_liquidity(fred: dict) -> tuple[str, float, dict, str]:
    """
    Label liquidity: EXPANDING | STABLE | TIGHTENING

    Learning logic:
      Liquidity is the 'fuel' in the financial system.
      Rising M2 and a growing Fed balance sheet (QE) → EXPANDING.
      Falling M2 / QT (shrinking WALCL) → TIGHTENING.
    """
    metrics: dict[str, Any] = {}
    parts: list[str] = []
    votes = {"EXPANDING": 0.0, "STABLE": 0.0, "TIGHTENING": 0.0}

    m2 = _obs_values(fred.get("M2SL", []), 4)
    if len(m2) >= 2:
        mom = _pct_change(m2[0][1], m2[1][1])
        ann = _annualized_from_mom(mom, 12)
        metrics["m2_mom_pct"] = mom
        metrics["m2_ann_approx_pct"] = ann
        if ann is not None:
            parts.append(f"M2 money supply pace ≈ {ann:.1f}% annualized.")
            if ann >= M2_EXPAND_YOY:
                votes["EXPANDING"] += 2.0
            elif ann <= M2_TIGHT_YOY:
                votes["TIGHTENING"] += 2.0
            else:
                votes["STABLE"] += 1.5

    wal = _obs_values(fred.get("WALCL", []), 3)
    if len(wal) >= 2:
        mom = _pct_change(wal[0][1], wal[1][1])
        metrics["fed_balance_sheet_mom_pct"] = mom
        if mom is not None:
            parts.append(f"Fed balance sheet changed {mom:+.2f}% recently.")
            if mom >= WALCL_EXPAND_PCT:
                votes["EXPANDING"] += 2.0
                parts.append("Growing Fed assets ≈ QE-style liquidity injection.")
            elif mom <= WALCL_TIGHT_PCT:
                votes["TIGHTENING"] += 2.0
                parts.append("Shrinking Fed assets ≈ QT — liquidity withdrawal.")
            else:
                votes["STABLE"] += 1.0

    if sum(votes.values()) <= 0:
        return "STABLE", 5.0, metrics, "Not enough liquidity data — defaulting to STABLE."

    label = max(votes, key=votes.get)
    liq_score = {"TIGHTENING": 2.0, "STABLE": 5.0, "EXPANDING": 8.5}.get(label, 5.0)
    lesson = " ".join(parts) + " More money/credit → EXPANDING; QT/shrinking money → TIGHTENING."
    return label, liq_score, metrics, lesson


def score_risk(fred: dict) -> tuple[str, float, dict, str]:
    """
    Label risk: RISK_ON | NEUTRAL | RISK_OFF

    Learning logic:
      VIX is the market's fear gauge (expected equity volatility).
      High VIX → investors want safety (RISK_OFF).
      Low VIX → risk appetite (RISK_ON).
    """
    metrics: dict[str, Any] = {}
    vix = _latest(fred.get("VIXCLS", []))
    metrics["vix"] = vix
    if vix is None:
        return "NEUTRAL", 5.0, metrics, "No VIX data — risk labeled NEUTRAL."

    if vix >= VIX_RISK_OFF:
        label = "RISK_OFF"
        score = 2.0  # low = risk-off for a 0–10 'appetite' scale
        note = "Fear is elevated — investors often flee to USD, bonds, gold."
    elif vix <= VIX_RISK_ON:
        label = "RISK_ON"
        score = 8.5
        note = "Fear is low — markets are comfortable taking risk."
    else:
        label = "NEUTRAL"
        score = 5.0
        note = "Volatility is mid-range — no clear panic or euphoria."

    lesson = f"VIX is {vix:.1f}. {note} Rule: ≥{VIX_RISK_OFF} RISK_OFF, ≤{VIX_RISK_ON} RISK_ON, else NEUTRAL."
    return label, score, metrics, lesson


def classify_regime(
    growth: str,
    inflation: str,
    policy: str,
    liquidity: str,
    risk: str,
) -> tuple[str, float, str]:
    """
    Combine dimensions into one teaching regime.

    Priority (simple & transparent):
      1. Extreme RISK_OFF can override short term
      2. Growth × Inflation matrix (core textbook map)
      3. Policy cycle tag if matrix is mild but policy is extreme
    """
    # 1) Risk shock overlay
    if risk == "RISK_OFF":
        base = REGIME_MATRIX.get((growth, inflation), "TRANSITION")
        if base in ("STAGFLATION", "RECESSION"):
            regime = base  # macro damage + fear
            conf = 0.75
        else:
            regime = "RISK_OFF"
            conf = 0.7
        return regime, conf, REGIME_LESSONS.get(regime, REGIME_LESSONS["TRANSITION"])

    # 2) Core matrix
    regime = REGIME_MATRIX.get((growth, inflation), "TRANSITION")
    conf = 0.65

    # 3) Policy emphasis when growth/inflation look 'normal'
    if regime in ("GOLDILOCKS", "DISINFLATION_EXPANSION", "SLOWDOWN", "TRANSITION"):
        if policy == "RESTRICTIVE" and liquidity == "TIGHTENING":
            regime = "TIGHTENING_CYCLE"
            conf = 0.7
        elif policy == "ACCOMMODATIVE" and liquidity == "EXPANDING":
            regime = "EASING_CYCLE"
            conf = 0.7

    # Stagflation confidence boost when both dimensions clear
    if growth in ("WEAK", "CONTRACTING") and inflation in ("HIGH", "ELEVATED"):
        regime = "STAGFLATION"
        conf = 0.8

    lesson = REGIME_LESSONS.get(regime, REGIME_LESSONS["TRANSITION"])
    return regime, conf, lesson


# =============================================================================
# ANALYZER CLASS
# =============================================================================

class MacroStateAnalyzer:
    """
    Reads raw FRED observation dicts, applies textbook rules, saves MacroState.

    Parameters
    ----------
    db_path : str
        SQLite file (same as news_engine by default).
    auto_save : bool
        If True, analyze_and_save() persists automatically.
    """

    def __init__(self, db_path: Optional[str] = None, auto_save: bool = True):
        self.db_path = db_path or _default_db()
        self.auto_save = auto_save
        self._ensure_table()

    # ── DB ────────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _ensure_table(self) -> None:
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute(MACRO_STATE_TABLE_SQL)
                conn.execute(MACRO_STATE_INDEX_SQL)
                conn.execute(MACRO_STATE_UNIQUE_SQL)
                # Clean accidental duplicate (as_of, rules_version) rows from older runs
                self._dedupe_existing_rows(conn)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _normalize_as_of(value: Any) -> str:
        """Store timeline keys as YYYY-MM-DD (one row per day per rules_version)."""
        if value is None:
            return date.today().isoformat()
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        s = str(value).strip()
        if not s:
            return date.today().isoformat()
        # ISO datetime → date
        if "T" in s:
            s = s.split("T", 1)[0]
        if " " in s:
            s = s.split(" ", 1)[0]
        # Already YYYY-MM-DD
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date().isoformat()
        except Exception:
            return date.today().isoformat()

    @staticmethod
    def _dedupe_existing_rows(conn: sqlite3.Connection) -> None:
        """Keep newest id for each (as_of, rules_version); drop older duplicates."""
        try:
            conn.execute(
                "DELETE FROM macro_state WHERE id NOT IN ("
                "  SELECT MAX(id) FROM macro_state GROUP BY as_of, rules_version"
                ")"
            )
        except Exception:
            pass

    def save_state(self, state: dict) -> int:
        """
        Upsert one MacroState row.

        Safe to re-run: unique on (as_of, rules_version) — replaces prior row
        for that date instead of inserting a duplicate.
        """
        as_of = self._normalize_as_of(state.get("as_of"))
        rules = state.get("rules_version") or RULES_VERSION
        created = state.get("created_at") or datetime.now().isoformat(timespec="seconds")
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM macro_state WHERE as_of = ? AND rules_version = ?",
                    (as_of, rules),
                )
                cur = conn.execute(
                    "INSERT INTO macro_state ("
                    "as_of, growth, inflation, policy, liquidity, risk, regime, "
                    "growth_score, inflation_score, policy_score, liquidity_score, risk_score, "
                    "confidence, metrics_json, lesson, rules_version, created_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        as_of,
                        state.get("growth"),
                        state.get("inflation"),
                        state.get("policy"),
                        state.get("liquidity"),
                        state.get("risk"),
                        state.get("regime"),
                        state.get("growth_score"),
                        state.get("inflation_score"),
                        state.get("policy_score"),
                        state.get("liquidity_score"),
                        state.get("risk_score"),
                        state.get("confidence"),
                        json.dumps(state.get("metrics") or {}, default=str),
                        state.get("lesson"),
                        rules,
                        created,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def latest_state(self) -> Optional[dict]:
        """Load the most recent MacroState from SQLite."""
        with _DB_LOCK:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT id, as_of, growth, inflation, policy, liquidity, risk, regime, "
                    "growth_score, inflation_score, policy_score, liquidity_score, risk_score, "
                    "confidence, metrics_json, lesson, rules_version, created_at "
                    "FROM macro_state ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
                if not row:
                    return None
                return self._row_to_dict(row)
            finally:
                conn.close()

    def history(self, limit: int = 30) -> list[dict]:
        with _DB_LOCK:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT id, as_of, growth, inflation, policy, liquidity, risk, regime, "
                    "growth_score, inflation_score, policy_score, liquidity_score, risk_score, "
                    "confidence, metrics_json, lesson, rules_version, created_at "
                    "FROM macro_state ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
                return [self._row_to_dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

    @staticmethod
    def _row_to_dict(row: tuple) -> dict:
        (
            rid, as_of, growth, inflation, policy, liquidity, risk, regime,
            g_s, i_s, p_s, l_s, r_s, conf, metrics_json, lesson, rules_version, created_at,
        ) = row
        try:
            metrics = json.loads(metrics_json) if metrics_json else {}
        except Exception:
            metrics = {}
        return {
            "id": rid,
            "as_of": as_of,
            "growth": growth,
            "inflation": inflation,
            "policy": policy,
            "liquidity": liquidity,
            "risk": risk,
            "regime": regime,
            "growth_score": g_s,
            "inflation_score": i_s,
            "policy_score": p_s,
            "liquidity_score": l_s,
            "risk_score": r_s,
            "confidence": conf,
            "metrics": metrics,
            "lesson": lesson,
            "rules_version": rules_version,
            "created_at": created_at,
        }

    # ── Core analysis ─────────────────────────────────────────────────────

    def analyze(self, fred_data: dict, as_of: Optional[Any] = None) -> dict:
        """
        Apply textbook rules to a FRED dict {series_id: [observations...]}.

        Parameters
        ----------
        fred_data : dict
            Newest-first observation lists (FRED style).
        as_of : optional
            Snapshot date (defaults to today). Stored as YYYY-MM-DD.

        Returns a MacroState dict (not necessarily saved).
        """
        fred = fred_data or {}
        now = datetime.now()
        as_of_key = self._normalize_as_of(as_of or now)

        inflation_label, inflation_score, infl_m, infl_lesson = score_inflation(fred)
        growth_label, growth_score, growth_m, growth_lesson = score_growth(fred)
        policy_label, policy_score, pol_m, pol_lesson = score_policy(
            fred, infl_m.get("inflation_proxy_pct")
        )
        liquidity_label, liquidity_score, liq_m, liq_lesson = score_liquidity(fred)
        risk_label, risk_score, risk_m, risk_lesson = score_risk(fred)

        regime, confidence, regime_lesson = classify_regime(
            growth_label, inflation_label, policy_label, liquidity_label, risk_label
        )

        metrics = {
            "inflation": infl_m,
            "growth": growth_m,
            "policy": pol_m,
            "liquidity": liq_m,
            "risk": risk_m,
            "snapshot_as_of": as_of_key,
        }

        # One student-friendly narrative
        lesson = (
            f"REGIME = {regime} (confidence {confidence:.0%}) as of {as_of_key}.\n\n"
            f"1) GROWTH = {growth_label}\n   {growth_lesson}\n\n"
            f"2) INFLATION = {inflation_label}\n   {infl_lesson}\n\n"
            f"3) POLICY = {policy_label}\n   {pol_lesson}\n\n"
            f"4) LIQUIDITY = {liquidity_label}\n   {liq_lesson}\n\n"
            f"5) RISK = {risk_label}\n   {risk_lesson}\n\n"
            f"6) WHY THIS REGIME\n   {regime_lesson}"
        )

        return {
            "as_of": as_of_key,
            "growth": growth_label,
            "inflation": inflation_label,
            "policy": policy_label,
            "liquidity": liquidity_label,
            "risk": risk_label,
            "regime": regime,
            "growth_score": round(growth_score, 2),
            "inflation_score": round(inflation_score, 2),
            "policy_score": round(policy_score, 2),
            "liquidity_score": round(liquidity_score, 2),
            "risk_score": round(risk_score, 2),
            "confidence": round(confidence, 3),
            "metrics": metrics,
            "lesson": lesson,
            "rules_version": RULES_VERSION,
            "created_at": now.isoformat(timespec="seconds"),
            # Friendly aliases for dashboards
            "summary_line": (
                f"{as_of_key} | {regime}: growth={growth_label}, inflation={inflation_label}, "
                f"policy={policy_label}, liquidity={liquidity_label}, risk={risk_label}"
            ),
        }

    def analyze_and_save(self, fred_data: dict, as_of: Optional[Any] = None) -> dict:
        """Analyze FRED data and persist to SQLite. Returns state (+ id if saved)."""
        state = self.analyze(fred_data, as_of=as_of)
        if self.auto_save:
            try:
                rid = self.save_state(state)
                state["id"] = rid
            except Exception as exc:
                state["save_error"] = str(exc)
        return state

    # ── Historical ledger / backfill ──────────────────────────────────────

    def load_fred_from_db(self) -> dict[str, list[dict]]:
        """Load all FRED series currently stored in SQLite."""
        out: dict[str, list[dict]] = {}
        with _DB_LOCK:
            conn = self._connect()
            try:
                cur = conn.execute("SELECT series_id, observations_json FROM fred_series")
                for sid, blob in cur.fetchall():
                    try:
                        obs = json.loads(blob) if blob else []
                    except Exception:
                        obs = []
                    if obs:
                        out[str(sid)] = obs
            finally:
                conn.close()
        return out

    def persist_fred_series_to_db(self, fred_history: dict[str, list[dict]]) -> int:
        """Write extended observation lists into fred_series (merge/replace)."""
        n = 0
        with _DB_LOCK:
            conn = self._connect()
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS fred_series ("
                    "series_id TEXT PRIMARY KEY, observations_json TEXT, updated_at TEXT)"
                )
                now = datetime.now().isoformat(timespec="seconds")
                for sid, obs in fred_history.items():
                    # Store newest-first for engine compatibility
                    ordered = self._sort_obs_newest_first(obs)
                    conn.execute(
                        "INSERT OR REPLACE INTO fred_series "
                        "(series_id, observations_json, updated_at) VALUES (?,?,?)",
                        (sid, json.dumps(ordered, default=str), now),
                    )
                    n += 1
                conn.commit()
            finally:
                conn.close()
        return n

    @staticmethod
    def _sort_obs_newest_first(obs: list[dict]) -> list[dict]:
        def _key(row: dict) -> str:
            return str(row.get("date") or "")
        return sorted(obs, key=_key, reverse=True)

    @staticmethod
    def _sort_obs_oldest_first(obs: list[dict]) -> list[dict]:
        def _key(row: dict) -> str:
            return str(row.get("date") or "")
        return sorted(obs, key=_key)

    def fetch_fred_history(
        self,
        api_key: str,
        series_ids: Optional[list[str]] = None,
        start: str = "2022-01-01",
        end: Optional[str] = None,
        sleep_s: float = 0.12,
    ) -> dict[str, list[dict]]:
        """
        Download multi-year FRED history for backfill series.

        The live engine only keeps ~5 points per series; history needs more.
        """
        if requests is None:
            raise RuntimeError("Install 'requests' to fetch FRED history: pip install requests")
        end = end or date.today().isoformat()
        series_ids = list(series_ids or BACKFILL_SERIES)
        headers = {
            "User-Agent": "MacroStateAnalyzer/1.0 (educational backfill)",
            "Accept": "application/json",
        }
        result: dict[str, list[dict]] = {}
        for sid in series_ids:
            try:
                r = requests.get(
                    "https://api.stlouisfed.org/fred/series/observations",
                    params={
                        "series_id": sid,
                        "api_key": api_key,
                        "file_type": "json",
                        "observation_start": start,
                        "observation_end": end,
                        "sort_order": "asc",
                    },
                    headers=headers,
                    timeout=30,
                )
                r.raise_for_status()
                obs = r.json().get("observations") or []
                # Keep only valid numeric rows
                clean = []
                for row in obs:
                    if row.get("value") in (None, ".", ""):
                        continue
                    clean.append({"date": row.get("date"), "value": row.get("value")})
                if clean:
                    result[sid] = clean
                    print(f"   [FRED] {sid}: {len(clean)} observations ({start} → {end})")
                else:
                    print(f"   [FRED] {sid}: empty")
            except Exception as exc:
                print(f"   [FRED] {sid}: FAILED — {exc}")
            time.sleep(sleep_s)
        return result

    def merge_fred_histories(
        self,
        *sources: dict[str, list[dict]],
    ) -> dict[str, list[dict]]:
        """Merge multiple FRED dicts; prefer more observations; de-dupe by date."""
        merged: dict[str, dict[str, dict]] = defaultdict(dict)
        for src in sources:
            for sid, obs in (src or {}).items():
                for row in obs:
                    d = str(row.get("date") or "")[:10]
                    if not d:
                        continue
                    merged[sid][d] = {"date": d, "value": row.get("value")}
        out: dict[str, list[dict]] = {}
        for sid, by_date in merged.items():
            out[sid] = self._sort_obs_oldest_first(list(by_date.values()))
        return out

    def _month_end_dates(self, start: date, end: date) -> list[date]:
        """Inclusive list of calendar month-end dates between start and end."""
        if end < start:
            return []
        dates: list[date] = []
        y, m = start.year, start.month
        guard = 0
        while guard < 600:  # max 50 years
            guard += 1
            last = monthrange(y, m)[1]
            d = date(y, m, last)
            if d > end:
                # final partial month: use end date if we have any prior data month
                if not dates or dates[-1].month != end.month or dates[-1].year != end.year:
                    if start <= end:
                        dates.append(end)
                break
            if d >= start:
                dates.append(d)
            if m == 12:
                y, m = y + 1, 1
            else:
                m += 1
        return dates

    def slice_fred_as_of(
        self,
        fred_history: dict[str, list[dict]],
        as_of: date,
        lookback: int = 18,
    ) -> dict[str, list[dict]]:
        """
        Point-in-time FRED view: only observations dated on/before as_of,
        newest-first, capped to `lookback` rows (enough for MoM/QoQ rules).
        """
        as_of_s = as_of.isoformat()
        snap: dict[str, list[dict]] = {}
        for sid, obs in fred_history.items():
            eligible = [
                row for row in obs
                if str(row.get("date") or "")[:10] <= as_of_s
            ]
            if not eligible:
                continue
            newest_first = self._sort_obs_newest_first(eligible)[:lookback]
            snap[sid] = newest_first
        return snap

    def timeline_dates_from_history(
        self,
        fred_history: dict[str, list[dict]],
        start: Optional[str] = None,
        end: Optional[str] = None,
        frequency: str = "M",
    ) -> list[date]:
        """
        Build snapshot dates.

        frequency:
          'M' — month-end (default, good for macro)
          'Q' — quarter-end
          'D' — every distinct observation date (dense; slower)
        """
        all_dates: list[date] = []
        for obs in fred_history.values():
            for row in obs:
                try:
                    all_dates.append(datetime.strptime(str(row["date"])[:10], "%Y-%m-%d").date())
                except Exception:
                    continue
        if not all_dates:
            return []
        d0 = max(min(all_dates), datetime.strptime(start or "1900-01-01", "%Y-%m-%d").date()) if start else min(all_dates)
        d1 = min(max(all_dates), datetime.strptime(end or "2999-12-31", "%Y-%m-%d").date()) if end else max(all_dates)
        if start:
            d0 = max(d0, datetime.strptime(start, "%Y-%m-%d").date())
        if end:
            d1 = min(d1, datetime.strptime(end, "%Y-%m-%d").date())

        if frequency.upper() == "D":
            uniq = sorted({d for d in all_dates if d0 <= d <= d1})
            return uniq
        if frequency.upper() == "Q":
            months = self._month_end_dates(d0, d1)
            return [d for d in months if d.month in (3, 6, 9, 12)]
        # Monthly default
        return self._month_end_dates(d0, d1)

    def backfill_history(
        self,
        fred_history: Optional[dict[str, list[dict]]] = None,
        start: str = "2022-01-01",
        end: Optional[str] = None,
        frequency: str = "M",
        min_series: int = 4,
        save: bool = True,
        progress_every: int = 6,
    ) -> dict:
        """
        Loop historical FRED points → MacroState timeline → SQLite ledger.

        Safe to re-run: upserts by (as_of, rules_version).

        Returns stats + states list.
        """
        end = end or date.today().isoformat()
        if fred_history is None:
            fred_history = self.load_fred_from_db()
        if not fred_history:
            return {
                "ok": False,
                "error": "No FRED history available. Fetch from API or populate fred_series.",
                "saved": 0,
                "states": [],
            }

        # Normalize / merge dates
        fred_history = self.merge_fred_histories(fred_history)
        dates = self.timeline_dates_from_history(fred_history, start=start, end=end, frequency=frequency)
        if not dates:
            return {"ok": False, "error": "No timeline dates in range.", "saved": 0, "states": []}

        states: list[dict] = []
        saved = 0
        skipped = 0
        print(f"   [BACKFILL] {len(dates)} snapshots from {dates[0]} → {dates[-1]} "
              f"(freq={frequency}, rules={RULES_VERSION})")

        for i, d in enumerate(dates):
            snap = self.slice_fred_as_of(fred_history, d, lookback=18)
            if len(snap) < min_series:
                skipped += 1
                continue
            # Need at least one growth and one inflation proxy for a meaningful state
            has_growth = any(k in snap for k in ("UNRATE", "PAYEMS", "GDP"))
            has_infl = any(k in snap for k in ("T10YIE", "CPIAUCSL", "PCEPI", "COREPCE"))
            if not (has_growth and has_infl):
                skipped += 1
                continue

            state = self.analyze(snap, as_of=d)
            if save:
                rid = self.save_state(state)
                state["id"] = rid
                saved += 1
            states.append(state)
            if progress_every and (i + 1) % progress_every == 0:
                print(f"   [BACKFILL] … {i + 1}/{len(dates)}  last={state['summary_line']}")

        summary = self.summarize_history(states if states else None, start=start, end=end)
        return {
            "ok": True,
            "saved": saved,
            "computed": len(states),
            "skipped": skipped,
            "start": str(dates[0]),
            "end": str(dates[-1]),
            "rules_version": RULES_VERSION,
            "states": states,
            "summary": summary,
        }

    def summarize_history(
        self,
        states: Optional[list[dict]] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> dict:
        """
        Count months (or rows) spent in each regime / dial label.
        If states is None, load from DB for rules_version.
        """
        if states is None:
            states = self.history(limit=10000)
            # history() returns newest first; filter by rules + range
            filtered = []
            for s in states:
                if s.get("rules_version") != RULES_VERSION:
                    continue
                a = str(s.get("as_of") or "")[:10]
                if start and a < start:
                    continue
                if end and a > end:
                    continue
                filtered.append(s)
            states = sorted(filtered, key=lambda x: str(x.get("as_of")))

        if not states:
            return {"n": 0, "regimes": {}, "message": "No macro_state rows to summarize."}

        regime_c = Counter(s.get("regime") for s in states)
        growth_c = Counter(s.get("growth") for s in states)
        infl_c = Counter(s.get("inflation") for s in states)
        policy_c = Counter(s.get("policy") for s in states)
        liq_c = Counter(s.get("liquidity") for s in states)
        risk_c = Counter(s.get("risk") for s in states)

        as_ofs = sorted(str(s.get("as_of"))[:10] for s in states)
        return {
            "n": len(states),
            "from": as_ofs[0],
            "to": as_ofs[-1],
            "unit": "snapshots (monthly if backfilled with freq=M)",
            "regimes": dict(regime_c.most_common()),
            "growth": dict(growth_c.most_common()),
            "inflation": dict(infl_c.most_common()),
            "policy": dict(policy_c.most_common()),
            "liquidity": dict(liq_c.most_common()),
            "risk": dict(risk_c.most_common()),
        }

    def format_history_summary(self, summary: dict) -> str:
        """Pretty text block for CLI printout."""
        if not summary or summary.get("n", 0) == 0:
            return "No historical macro states available."
        lines = [
            f"From {summary.get('from')} to {summary.get('to')}: "
            f"{summary.get('n')} {summary.get('unit', 'snapshots')}",
            "",
            "Regime time spent:",
        ]
        n = max(int(summary.get("n") or 1), 1)
        for regime, cnt in (summary.get("regimes") or {}).items():
            pct = 100.0 * cnt / n
            lines.append(f"  • {regime:<24} {cnt:>4}  ({pct:5.1f}%)")
        lines.append("")
        lines.append("Growth dial:")
        for k, cnt in (summary.get("growth") or {}).items():
            lines.append(f"  • {k:<16} {cnt:>4}")
        lines.append("Inflation dial:")
        for k, cnt in (summary.get("inflation") or {}).items():
            lines.append(f"  • {k:<16} {cnt:>4}")
        lines.append("Policy dial:")
        for k, cnt in (summary.get("policy") or {}).items():
            lines.append(f"  • {k:<16} {cnt:>4}")
        lines.append("Liquidity dial:")
        for k, cnt in (summary.get("liquidity") or {}).items():
            lines.append(f"  • {k:<16} {cnt:>4}")
        lines.append("Risk dial:")
        for k, cnt in (summary.get("risk") or {}).items():
            lines.append(f"  • {k:<16} {cnt:>4}")
        return "\n".join(lines)


# =============================================================================
# MODULE SELF-TEST
# =============================================================================

if __name__ == "__main__":
    # Tiny synthetic FRED-like payload for offline demo
    demo = {
        "T10YIE": [{"date": "2026-07-01", "value": "2.3"}],
        "UNRATE": [{"date": "2026-06-01", "value": "4.1"}, {"date": "2026-05-01", "value": "4.0"}],
        "PAYEMS": [{"date": "2026-06-01", "value": "158000"}, {"date": "2026-05-01", "value": "157850"}],
        "FEDFUNDS": [{"date": "2026-06-01", "value": "4.33"}],
        "VIXCLS": [{"date": "2026-07-15", "value": "14.2"}],
        "M2SL": [{"date": "2026-05-01", "value": "21000"}, {"date": "2026-04-01", "value": "20950"}],
        "WALCL": [{"date": "2026-07-01", "value": "7200000"}, {"date": "2026-06-01", "value": "7220000"}],
        "CPIAUCSL": [{"date": "2026-06-01", "value": "314.0"}, {"date": "2026-05-01", "value": "313.2"}],
        "DGS2": [{"date": "2026-07-01", "value": "4.0"}],
        "DGS10": [{"date": "2026-07-01", "value": "4.2"}],
    }
    analyzer = MacroStateAnalyzer(auto_save=False)
    result = analyzer.analyze(demo)
    print(result["summary_line"])
    print()
    print(result["lesson"])
