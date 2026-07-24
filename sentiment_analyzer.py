# -*- coding: utf-8 -*-
"""
sentiment_analyzer.py — Advanced NLP Sentiment Analyzer for Financial News
============================================================================
Uses VADER (NLTK) with a custom financial lexicon for domain-specific
sentiment analysis. Falls back to an enhanced keyword-based approach
if NLTK is not installed.
"""

from __future__ import annotations

import re
from typing import Optional

# ── VADER integration (optional) ────────────────────────────────────────────

_VADER_AVAILABLE = False
_VADER_INSTANCE = None

try:
    from nltk.sentiment.vader import SentimentIntensityAnalyzer
    _VADER_AVAILABLE = True
except ImportError:
    try:
        import nltk
        nltk.download("vader_lexicon", quiet=True)
        from nltk.sentiment.vader import SentimentIntensityAnalyzer
        _VADER_AVAILABLE = True
    except Exception:
        pass

# ── Financial lexicon for VADER augmentation ───────────────────────────────

FINANCIAL_LEXICON: dict[str, float] = {
    "hawkish": 0.8, "hike": 0.5, "hikes": 0.5, "hiked": 0.5,
    "beat": 0.7, "beats": 0.7, "beaten": 0.5,
    "surge": 0.8, "surges": 0.8, "surged": 0.8,
    "rally": 0.6, "rallies": 0.6, "rallied": 0.6,
    "strong": 0.5, "stronger": 0.6, "strongest": 0.7,
    "robust": 0.6, "solid": 0.5, "resilient": 0.5,
    "above": 0.3, "exceeds": 0.5, "exceeded": 0.5,
    "expansion": 0.5, "expanding": 0.5,
    "recovery": 0.5, "recovering": 0.5,
    "boom": 0.7, "booming": 0.7,
    "dovish": -0.8, "cut": -0.4, "cuts": -0.4, "cutting": -0.4,
    "miss": -0.7, "misses": -0.7, "missed": -0.6,
    "plunge": -0.8, "plunges": -0.8, "plunged": -0.8,
    "collapse": -0.9, "collapses": -0.9, "collapsed": -0.9,
    "weak": -0.5, "weaker": -0.6, "weakest": -0.7,
    "below": -0.3,
    "contraction": -0.6, "contracting": -0.6,
    "recession": -0.7, "recessionary": -0.8,
    "crisis": -0.8, "crash": -0.9,
    "slump": -0.6, "slumping": -0.6,
    "decline": -0.5, "declining": -0.5,
    "deteriorate": -0.6, "deteriorating": -0.6,
    "fomc": 0.0, "ecb": 0.0, "boe": 0.0, "snb": 0.0, "boj": 0.0,
    "powell": 0.0, "lagarde": 0.0, "bailey": 0.0,
    "cpi": 0.0, "nfp": 0.0, "gdp": 0.0, "pmi": 0.0,
    "inflation": 0.0, "deflation": -0.3,
    "unemployment": 0.0, "payrolls": 0.0,
    "treasury": 0.0, "yield": 0.0, "bond": 0.0,
    "dollar": 0.0, "euro": 0.0, "pound": 0.0,
    "gold": 0.0, "silver": 0.0,
}

KEYWORD_INSTRUMENT_MAP: dict[str, dict[str, str]] = {
    "non-farm payroll": {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "nfp":              {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "cpi":              {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BULL"},
    "inflation":        {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BULL"},
    "pce":              {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BULL"},
    "core cpi":         {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BULL"},
    "core pce":         {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BULL"},
    "federal reserve":  {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "fomc":             {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "fed":              {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "ecb":              {"EURUSD": "BULL"},
    "european central bank": {"EURUSD": "BULL"},
    "boe":              {"GBPUSD": "BULL"},
    "bank of england":  {"GBPUSD": "BULL"},
    "snb":              {"USDCHF": "BEAR"},
    "swiss national bank": {"USDCHF": "BEAR"},
    "boj":              {"USDCHF": "NEUTRAL", "XAUUSD": "BULL"},
    "bank of japan":    {"USDCHF": "NEUTRAL", "XAUUSD": "BULL"},
    "rate hike":        {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "rate cut":         {"EURUSD": "BULL", "GBPUSD": "BULL", "USDCHF": "BEAR", "XAUUSD": "BULL"},
    "rate decision":    {"EURUSD": "NEUTRAL", "GBPUSD": "NEUTRAL", "USDCHF": "NEUTRAL", "XAUUSD": "NEUTRAL"},
    "interest rate":    {"EURUSD": "NEUTRAL", "GBPUSD": "NEUTRAL", "USDCHF": "NEUTRAL", "XAUUSD": "NEUTRAL"},
    "monetary policy":  {"EURUSD": "NEUTRAL", "GBPUSD": "NEUTRAL", "USDCHF": "NEUTRAL", "XAUUSD": "NEUTRAL"},
    "quantitative easing": {"EURUSD": "BULL", "GBPUSD": "BULL", "USDCHF": "BEAR", "XAUUSD": "BULL"},
    "qe":               {"EURUSD": "BULL", "GBPUSD": "BULL", "USDCHF": "BEAR", "XAUUSD": "BULL"},
    "taper":            {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "balance sheet":    {"EURUSD": "NEUTRAL", "GBPUSD": "NEUTRAL", "USDCHF": "NEUTRAL", "XAUUSD": "NEUTRAL"},
    "gdp":              {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "unemployment":     {"EURUSD": "NEUTRAL", "GBPUSD": "NEUTRAL", "USDCHF": "NEUTRAL", "XAUUSD": "NEUTRAL"},
    "retail sales":     {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "pmi":              {"EURUSD": "NEUTRAL", "GBPUSD": "NEUTRAL", "XAUUSD": "NEUTRAL", "USDCHF": "NEUTRAL"},
    "ism":              {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "consumer confidence": {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "jobless claims":   {"EURUSD": "NEUTRAL", "GBPUSD": "NEUTRAL", "USDCHF": "NEUTRAL", "XAUUSD": "NEUTRAL"},
    "housing":          {"EURUSD": "NEUTRAL", "GBPUSD": "NEUTRAL", "XAUUSD": "NEUTRAL", "USDCHF": "NEUTRAL"},
    "durable goods":    {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "trade balance":    {"EURUSD": "NEUTRAL", "GBPUSD": "NEUTRAL", "USDCHF": "NEUTRAL", "XAUUSD": "NEUTRAL"},
    "current account":  {"EURUSD": "NEUTRAL", "GBPUSD": "NEUTRAL", "USDCHF": "NEUTRAL", "XAUUSD": "NEUTRAL"},
    "dollar":           {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "strong dollar":    {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "weak dollar":      {"EURUSD": "BULL", "GBPUSD": "BULL", "USDCHF": "BEAR", "XAUUSD": "BULL"},
    "dxy":              {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "dollar index":     {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "euro":             {"EURUSD": "BULL"},
    "pound":            {"GBPUSD": "BULL"},
    "sterling":         {"GBPUSD": "BULL"},
    "franc":            {"USDCHF": "BEAR"},
    "swiss":            {"USDCHF": "BEAR"},
    "yen":              {"USDCHF": "NEUTRAL", "XAUUSD": "BULL"},
    "gold":             {"XAUUSD": "BULL"},
    "xauusd":           {"XAUUSD": "BULL"},
    "xau":              {"XAUUSD": "BULL"},
    "silver":           {"XAUUSD": "BULL"},
    "precious metal":   {"XAUUSD": "BULL"},
    "commodity":        {"XAUUSD": "BULL"},
    "oil":              {"XAUUSD": "BULL"},
    "crude":            {"XAUUSD": "BULL"},
    "powell":           {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "NEUTRAL"},
    "lagarde":          {"EURUSD": "BULL"},
    "bailey":           {"GBPUSD": "BULL"},
    "yellen":           {"EURUSD": "NEUTRAL", "GBPUSD": "NEUTRAL", "XAUUSD": "NEUTRAL"},
    "brainard":         {"EURUSD": "NEUTRAL", "GBPUSD": "NEUTRAL", "XAUUSD": "NEUTRAL"},
    "williams":         {"EURUSD": "NEUTRAL", "GBPUSD": "NEUTRAL", "XAUUSD": "NEUTRAL"},
    "hawkish":          {"EURUSD": "BEAR", "GBPUSD": "BEAR", "USDCHF": "BULL", "XAUUSD": "BEAR"},
    "dovish":           {"EURUSD": "BULL", "GBPUSD": "BULL", "USDCHF": "BEAR", "XAUUSD": "BULL"},
    "safe haven":       {"XAUUSD": "BULL", "USDCHF": "BEAR"},
    "risk off":         {"XAUUSD": "BULL", "USDCHF": "BULL"},
    "risk on":          {"XAUUSD": "BEAR", "USDCHF": "BEAR"},
    "flight to safety": {"XAUUSD": "BULL", "USDCHF": "BULL"},
    "risk appetite":    {"XAUUSD": "BEAR"},
    "risk aversion":    {"XAUUSD": "BULL"},
    "geopolit":         {"XAUUSD": "BULL"},
    "war":              {"XAUUSD": "BULL"},
    "conflict":         {"XAUUSD": "BULL"},
    "tariff":           {"XAUUSD": "BULL", "EURUSD": "BEAR"},
    "trade war":        {"XAUUSD": "BULL"},
    "sanction":         {"XAUUSD": "BULL"},
    "embargo":          {"XAUUSD": "BULL"},
    "crisis":           {"XAUUSD": "BULL"},
    "uncertainty":      {"XAUUSD": "BULL"},
    "tension":          {"XAUUSD": "BULL"},
    "escalation":       {"XAUUSD": "BULL"},
    "treasury":         {"XAUUSD": "BEAR", "EURUSD": "NEUTRAL"},
    "yield":            {"XAUUSD": "BEAR"},
    "bond":             {"XAUUSD": "BEAR"},
    "10-year":          {"XAUUSD": "BEAR"},
    "2-year":           {"XAUUSD": "BEAR"},
    "recession":        {"EURUSD": "NEUTRAL", "GBPUSD": "BEAR", "USDCHF": "BEAR", "XAUUSD": "BULL"},
    "contraction":      {"XAUUSD": "BULL"},
    "slowdown":         {"XAUUSD": "BULL"},
    "recovery":         {"XAUUSD": "BEAR"},
    "expansion":        {"XAUUSD": "BEAR"},
    "soft landing":     {"XAUUSD": "BEAR"},
    "hard landing":     {"XAUUSD": "BULL"},
    "selloff":          {"XAUUSD": "BULL"},
    "correction":       {"XAUUSD": "BULL"},
    "volatility":       {"XAUUSD": "BULL"},
    "stagflation":      {"XAUUSD": "BULL"},
    "deficit":          {"XAUUSD": "BULL"},
    "debt":             {"XAUUSD": "BULL"},
    "default":          {"XAUUSD": "BULL"},
}

KEYWORD_BASE_WEIGHT: dict[str, float] = {
    "non-farm payroll": 1.0, "nfp": 1.0, "cpi": 1.0, "inflation": 0.9,
    "pce": 0.9, "core cpi": 0.9, "core pce": 0.9,
    "federal reserve": 0.9, "fomc": 1.0, "fed": 0.8,
    "ecb": 0.7, "european central bank": 0.7,
    "boe": 0.7, "bank of england": 0.7, "snb": 0.6,
    "swiss national bank": 0.6, "boj": 0.6, "bank of japan": 0.6,
    "rate hike": 0.9, "rate cut": 0.9, "rate decision": 0.7,
    "interest rate": 0.6, "monetary policy": 0.6,
    "quantitative easing": 0.7, "qe": 0.7, "taper": 0.7,
    "balance sheet": 0.5,
    "gdp": 0.8, "unemployment": 0.7, "retail sales": 0.6,
    "pmi": 0.6, "ism": 0.6, "consumer confidence": 0.6,
    "jobless claims": 0.6, "housing": 0.4, "durable goods": 0.5,
    "trade balance": 0.4, "current account": 0.4,
    "dollar": 0.5, "strong dollar": 0.7, "weak dollar": 0.7,
    "dxy": 0.7, "dollar index": 0.7,
    "gold": 0.4, "xauusd": 0.6, "xau": 0.5, "silver": 0.4,
    "precious metal": 0.5, "commodity": 0.4, "oil": 0.4, "crude": 0.4,
    "euro": 0.4, "pound": 0.4, "sterling": 0.4, "franc": 0.4,
    "swiss": 0.4, "yen": 0.4,
    "powell": 0.7, "lagarde": 0.6, "bailey": 0.5,
    "yellen": 0.5, "brainard": 0.4, "williams": 0.4,
    "hawkish": 0.8, "dovish": 0.8,
    "safe haven": 0.7, "risk off": 0.6, "risk on": 0.6,
    "flight to safety": 0.7, "risk appetite": 0.5, "risk aversion": 0.5,
    "geopolit": 0.6, "war": 0.7, "conflict": 0.6, "tariff": 0.7,
    "trade war": 0.7, "sanction": 0.6, "embargo": 0.6, "crisis": 0.7,
    "uncertainty": 0.5, "tension": 0.5, "escalation": 0.6,
    "treasury": 0.5, "yield": 0.6, "bond": 0.5, "10-year": 0.6, "2-year": 0.5,
    "recession": 0.8, "contraction": 0.6, "slowdown": 0.6,
    "recovery": 0.5, "expansion": 0.5, "soft landing": 0.6, "hard landing": 0.7,
    "selloff": 0.5, "correction": 0.5, "volatility": 0.4,
    "stagflation": 0.7, "deficit": 0.5, "debt": 0.5, "default": 0.6,
}

NEGATION_WORDS = {
    "not", "no", "never", "without", "fails", "failed", "failing",
    "unable", "cannot", "won't", "isn't", "wasn't", "doesn't", "didn't",
    "hasn't", "haven't", "n't", "denies", "denied", "rejects", "rejected",
    "unlikely", "ruled out", "dismisses", "dismissed", "downplays", "downplayed",
}

INTENSIFIER_WORDS: dict[str, float] = {
    "sharply": 1.4, "significantly": 1.3, "massively": 1.5, "unexpectedly": 1.3,
    "surge": 1.4, "surges": 1.4, "surged": 1.4, "plunge": 1.4, "plunges": 1.4,
    "plunged": 1.4, "soar": 1.3, "soars": 1.3, "soared": 1.3, "collapse": 1.5,
    "collapses": 1.5, "collapsed": 1.5, "record": 1.3, "shock": 1.4,
    "shocks": 1.4, "shocked": 1.4, "dramatically": 1.4, "steeply": 1.3,
    "slightly": 0.7, "marginally": 0.6, "modestly": 0.8, "narrowly": 0.7,
    "barely": 0.5, "scarcely": 0.5, "gently": 0.7,
}

BULLISH_WORDS = {"beat", "beats", "surges", "rises", "higher", "above", "stronger",
                 "strong", "rally", "rallies", "jumps", "gains", "hawkish", "hike",
                 "robust", "solid", "resilient", "expansion", "recovery", "boom"}
BEARISH_WORDS = {"miss", "misses", "falls", "drops", "lower", "below", "weaker",
                 "weak", "decline", "plunges", "cuts", "dovish", "cut", "recession",
                 "crisis", "crash", "slump", "contraction", "collapse"}

# ── Entity patterns ─────────────────────────────────────────────────────────

CURRENCY_PATTERNS = {
    "USD": [r"\bUSD\b", r"\bU\.S\.?\s*dollar\b", r"\bbuck\b", r"\bgreenback\b"],
    "EUR": [r"\bEUR\b", r"\beuro\b", r"\beuros?\b"],
    "GBP": [r"\bGBP\b", r"\bpound\b", r"\bsterling\b", r"\bcable\b"],
    "CHF": [r"\bCHF\b", r"\bswiss\s+franc\b", r"\bfranc\b"],
    "JPY": [r"\bJPY\b", r"\byen\b", r"\bjapanese\s+yen\b"],
    "XAU": [r"\bXAU\b", r"\bgold\b", r"\bprecious\s+metal\b"],
    "AUD": [r"\bAUD\b", r"\baustralian\s+dollar\b", r"\baussie\b"],
    "NZD": [r"\bNZD\b", r"\bnz\s+dollar\b", r"\bkiwi\b"],
    "CAD": [r"\bCAD\b", r"\bcanadian\s+dollar\b", r"\bloonie\b"],
}

CENTRAL_BANK_PATTERNS = {
    "Fed": [r"\bFederal\s+Reserve\b", r"\bFed\b", r"\bFOMC\b", r"\bPowell\b"],
    "ECB":    [r"\bECB\b", r"\bEuropean\s+Central\s+Bank\b", r"\bLagarde\b"],
    "BOE":    [r"\bBOE\b", r"\bBank\s+of\s+England\b", r"\bBailey\b"],
    "SNB":    [r"\bSNB\b", r"\bSwiss\s+National\s+Bank\b"],
    "BOJ":    [r"\bBOJ\b", r"\bBank\s+of\s+Japan\b", r"\bKuroda\b", r"\bUeda\b"],
    "RBA":    [r"\bRBA\b", r"\bReserve\s+Bank\s+of\s+Australia\b"],
    "BOC":    [r"\bBOC\b", r"\bBank\s+of\s+Canada\b"],
}

INDICATOR_PATTERNS = {
    "CPI":          [r"\bCPI\b", r"\bconsumer\s+price\s+index\b"],
    "PCE":          [r"\bPCE\b", r"\bpersonal\s+consumption\s+expenditure\b"],
    "NFP":          [r"\bNFP\b", r"\bnon-?farm\s+payroll\b"],
    "GDP":          [r"\bGDP\b", r"\bgross\s+domestic\s+product\b"],
    "PMI":          [r"\bPMI\b", r"\bpurchasing\s+manager\b"],
    "ISM":          [r"\bISM\b"],
    "Unemployment": [r"\bunemployment\b", r"\bjobless\s+claims\b"],
    "RetailSales":  [r"\bretail\s+sales\b"],
    "Housing":      [r"\bhousing\s+starts\b", r"\bexisting\s+home\s+sales\b",
 r"\bnew\s+home\s+sales\b", r"\bbuilding\s+permits\b"],
    "Yield":        [r"\byield\b", r"\btreasury\b", r"\bbond\b", r"\b10-?year\b",
                    r"\b2-?year\b"],
    "TradeBalance": [r"\btrade\s+balance\b", r"\bcurrent\s+account\b"],
}

INSTRUMENT_PATTERNS = {
    "EURUSD": [r"\bEUR/?USD\b", r"\beuro\s+dollar\b"],
    "GBPUSD": [r"\bGBP/?USD\b", r"\bpound\s+dollar\b", r"\bcable\b"],
    "USDCHF": [r"\bUSD/?CHF\b", r"\bdollar\s+franc\b"],
    "USDJPY": [r"\bUSD/?JPY\b", r"\bdollar\s+yen\b"],
    "XAUUSD": [r"\bXAU/?USD\b", r"\bgold\b"],
    "AUDUSD": [r"\bAUD/?USD\b", r"\baussie\s+dollar\b"],
    "NZDUSD": [r"\bNZD/?USD\b", r"\bkiwi\s+dollar\b"],
    "USDCAD": [r"\bUSD/?CAD\b", r"\bdollar\s+loonie\b"],
}


class FinancialSentimentAnalyzer:
    """
    Advanced sentiment analyzer for financial news.

    Uses VADER (NLTK) with a custom financial lexicon if available,
    otherwise falls back to an enhanced keyword-based approach.
    """

    _NEGATION_WINDOW = 30
    _INTENSITY_WINDOW = 25

    def __init__(self):
        global _VADER_INSTANCE
        if _VADER_AVAILABLE and _VADER_INSTANCE is None:
            try:
                _VADER_INSTANCE = SentimentIntensityAnalyzer()
                _VADER_INSTANCE.lexicon.update(FINANCIAL_LEXICON)
            except Exception:
                pass
        self._vader = _VADER_INSTANCE
        self.method = "VADER+Financial" if self._vader else "Enhanced-Keyword"

    def analyze(self, text: str) -> dict:
        """Analyze text and return comprehensive sentiment scores."""
        if not text or not text.strip():
            return self._empty_result()

        if self._vader:
            scores = self._vader.polarity_scores(text)
            general_tone = scores["compound"]
        else:
            general_tone = self._keyword_sentiment(text)

        instrument_weights = self._score_instruments(text)
        instrument_labels = self._weights_to_labels(instrument_weights)
        entities = self._extract_entities(text)
        intensity = self._assess_intensity(text)

        return {
            "general_tone": general_tone,
            "instrument_weights": instrument_weights,
            "instrument_labels": instrument_labels,
            "entities": entities,
            "intensity": intensity,
            "method": self.method,
        }

    def _empty_result(self) -> dict:
        return {
            "general_tone": 0.0,
            "instrument_weights": {},
            "instrument_labels": {},
            "entities": {"currencies": [], "central_banks": [],
                         "indicators": [], "instruments": []},
            "intensity": 1.0,
            "method": self.method,
        }

    def _keyword_sentiment(self, text: str) -> float:
        lower = text.lower()
        words = set(re.findall(r"\b\w+\b", lower))
        bull = len(words & BULLISH_WORDS)
        bear = len(words & BEARISH_WORDS)
        total = bull + bear
        return (bull - bear) / total if total > 0 else 0.0

    def _score_instruments(self, text: str) -> dict[str, float]:
        lower = text.lower()
        weights: dict[str, float] = {}

        for kw, mapping in KEYWORD_INSTRUMENT_MAP.items():
            for m in re.finditer(rf"\b{re.escape(kw)}\b", lower):
                start, end = m.span()

                window_before = lower[max(0, start - self._NEGATION_WINDOW):start]
                window_after  = lower[end:end + self._NEGATION_WINDOW]
                negated = any(re.search(rf"\b{re.escape(neg)}\b", window_before) or
                              re.search(rf"\b{re.escape(neg)}\b", window_after)
                              for neg in NEGATION_WORDS)

                window_around = lower[max(0, start - self._INTENSITY_WINDOW):
                                      end + self._INTENSITY_WINDOW]
                intensity = 1.0
                for word, mult in INTENSIFIER_WORDS.items():
                    if re.search(rf"\b{re.escape(word)}\b", window_around):
                        if mult >= 1:
                            intensity = max(intensity, mult)
                        else:
                            intensity = min(intensity, mult)

                base = KEYWORD_BASE_WEIGHT.get(kw, 0.6)
                sign_flip = -1 if negated else 1

                for inst, direction in mapping.items():
                    sign = {"BULL": 1, "BEAR": -1, "NEUTRAL": 0}.get(direction, 0)
                    contribution = sign * base * intensity * sign_flip
                    weights[inst] = weights.get(inst, 0.0) + contribution

        return weights

    @staticmethod
    def _weights_to_labels(weights: dict[str, float]) -> dict[str, str]:
        labels = {}
        for inst, w in weights.items():
            if w > 0.15:
                labels[inst] = "BULL"
            elif w < -0.15:
                labels[inst] = "BEAR"
            else:
                labels[inst] = "NEUTRAL"
        return labels

    def _extract_entities(self, text: str) -> dict[str, list[str]]:
        found = {"currencies": [], "central_banks": [],
                 "indicators": [], "instruments": []}

        for label, patterns in CURRENCY_PATTERNS.items():
            if any(re.search(p, text, re.IGNORECASE) for p in patterns):
                if label not in found["currencies"]:
                    found["currencies"].append(label)

        for label, patterns in CENTRAL_BANK_PATTERNS.items():
            if any(re.search(p, text, re.IGNORECASE) for p in patterns):
                if label not in found["central_banks"]:
                    found["central_banks"].append(label)

        for label, patterns in INDICATOR_PATTERNS.items():
            if any(re.search(p, text, re.IGNORECASE) for p in patterns):
                if label not in found["indicators"]:
                    found["indicators"].append(label)

        for label, patterns in INSTRUMENT_PATTERNS.items():
            if any(re.search(p, text, re.IGNORECASE) for p in patterns):
                if label not in found["instruments"]:
                    found["instruments"].append(label)

        return found

    def _assess_intensity(self, text: str) -> float:
        lower = text.lower()
        intensity = 1.0
        for word, mult in INTENSIFIER_WORDS.items():
            if re.search(rf"\b{re.escape(word)}\b", lower):
                if mult >= 1:
                    intensity = max(intensity, mult)
                else:
                    intensity = min(intensity, mult)
        return intensity