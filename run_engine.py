# -*- coding: utf-8 -*-
"""
run_engine.py — Main Entry Point for the Context-Aware News Engine
====================================================================
Run everything from here:
    python run_engine.py              # One-shot analysis
    python run_engine.py --live        # Continuous monitoring (dashboard mode)
    python run_engine.py --live 30     # Continuous monitoring (30s refresh)
    python run_engine.py --test        # Run built-in test cases
    python run_engine.py --news-only   # Force refresh + print summary
    python run_engine.py --status # Quick listener status snapshot
    python run_engine.py --help        # Show help

LIVE MODE:
 Instead of scrolling text every cycle, the dashboard clears the screen
    and redraws in place — like a digital trading terminal.  All sections
    update in their fixed positions with a live countdown timer.
"""

from __future__ import annotations

import sys
import time
import signal
import shutil
from datetime import datetime

from news_engine import (
    get_news_context,
    start_news_listener,
    stop_news_listener,
    listener_status,
)
from sentiment_analyzer import FinancialSentimentAnalyzer
from pattern_engine import (
    detect_patterns,
    analyze_signal_convergence,
    detect_regime,
    analyze_forward_calendar,
    analyze_pressure_trend,
    get_memory_snapshot,
    remember_event,
    record_pressure,
)


# =============================================================================
# ANSI ESCAPE CODES  (terminal dashboard rendering)
# =============================================================================

# ANSI codes for screen control
ANSI_CLEAR = "\033[2J"
ANSI_HOME = "\033[H"
ANSI_HIDE_CURSOR = "\033[?25l"
ANSI_SHOW_CURSOR = "\033[?25h"
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_RED = "\033[91m"
ANSI_GREEN = "\033[92m"
ANSI_YELLOW = "\033[93m"
ANSI_BLUE = "\033[94m"
ANSI_MAGENTA = "\033[95m"
ANSI_CYAN = "\033[96m"
ANSI_WHITE = "\033[97m"


def _terminal_width() -> int:
    """Get terminal width, default to 100."""
    try:
        return shutil.get_terminal_size((100, 40)).columns
    except Exception:
        return 100


def _clear_screen():
    """Clear the screen and move cursor to home position."""
    sys.stdout.write(ANSI_CLEAR + ANSI_HOME)
    sys.stdout.flush()


def _print_dashboard(content: str):
    """Clear screen and print dashboard content in place."""
    _clear_screen()
    sys.stdout.write(content)
    sys.stdout.flush()


def _color(text: str, color_code: str) -> str:
    """Wrap text in ANSI color."""
    return f"{color_code}{text}{ANSI_RESET}"


def _bold(text: str) -> str:
    return f"{ANSI_BOLD}{text}{ANSI_RESET}"


def _dim(text: str) -> str:
    return f"{ANSI_DIM}{text}{ANSI_RESET}"


def _center(text: str, width: int = None) -> str:
    """Center text within terminal width."""
    if width is None:
        width = _terminal_width()
    pad = max(0, (width - len(text)) // 2)
    return " " * pad + text


def _separator(width: int = None, char: str = "─") -> str:
    """Create a full-width separator line."""
    if width is None:
        width = _terminal_width()
    return char * min(width, 120)


def _double_separator(width: int = None) -> str:
    """Create a double-line separator."""
    if width is None:
        width = _terminal_width()
    return "═" * min(width, 120)


def _section_header(title: str) -> str:
    """Create a visually distinct section header with spacing."""
    w = min(_terminal_width(), 120)
    pad = (w - len(title)) // 2
    left = "╔" + "═" * max(0, pad - 1)
    right = "═" * max(0, w - len(left) - len(title) - 1) + "╗"
    return f"\n  {left} {title} {right}\n"


def _box_header(title: str) -> str:
    """Create a section box header with improved spacing."""
    w = min(_terminal_width(), 110)
    inner = f"  {title}  "
    pad_each = max(0, (w - len(inner)) // 2)
    line = "═" * w
    centered = "═" * pad_each + inner + "═" * max(0, w - len(inner) - pad_each)
    return f"\n  {centered}\n"


# =============================================================================
# BUILT-IN TEST CASES
# =============================================================================

TEST_EVENTS = [
    {
        "title": "US CPI comes in hotter than expected at 3.5% vs 3.4% forecast",
        "summary": "Consumer prices rose more than anticipated, keeping inflationary pressure elevated.",
        "source": "TestFeed",
        "datetime": datetime.now(),
        "impact": 3,
        "actual": "3.5%",
        "forecast": "3.4%",
        "previous": "3.4%",
        "currency": "USD",
    },
    {
        "title": "Federal Reserve Chair Powell signals rate cuts may be coming sooner than expected",
        "summary": "Powell struck a dovish tone, suggesting the Fed could begin easing policy at upcoming meetings.",
        "source": "TestFeed",
        "datetime": datetime.now(),
        "impact": 3,
        "actual": "",
        "forecast": "",
        "previous": "",
        "currency": "USD",
    },
    {
        "title": "Non-farm payrolls surge by 250K, sharply beating expectations of 180K",
        "summary": "The labor market remains resilient with unexpected strength in hiring.",
        "source": "TestFeed",
        "datetime": datetime.now(),
        "impact": 3,
        "actual": "250K",
        "forecast": "180K",
        "previous": "200K",
        "currency": "USD",
    },
    {
        "title": "Geopolitical tensions escalate as new sanctions imposed",
        "summary": "Rising conflict drives safe-haven demand for gold and the Swiss franc.",
        "source": "TestFeed",
        "datetime": datetime.now(),
        "impact": 2,
        "actual": "",
        "forecast": "",
        "previous": "",
        "currency": "USD",
    },
    {
        "title": "ECB President Lagarde hints at possible rate cut in June",
        "summary": "Lagarde's dovish remarks suggest the ECB may ease before the Fed.",
        "source": "TestFeed",
        "datetime": datetime.now(),
        "impact": 3,
        "actual": "",
        "forecast": "",
        "previous": "",
        "currency": "EUR",
    },
    {
        "title": "Gold prices plunge as dollar strengthens on hawkish Fed rhetoric",
        "summary": "Gold sold off sharply after several Fed officials suggested rates may stay higher for longer.",
        "source": "TestFeed",
        "datetime": datetime.now(),
        "impact": 2,
        "actual": "",
        "forecast": "",
        "previous": "",
        "currency": "XAU",
    },
    {
        "title": "US GDP growth slows significantly, raising recession fears",
        "summary": "Economic output contracted unexpectedly, fueling recession concerns.",
        "source": "TestFeed",
        "datetime": datetime.now(),
        "impact": 3,
        "actual": "",
        "forecast": "",
        "previous": "",
        "currency": "USD",
    },
    {
        "title": "Inflation did NOT rise as expected, surprising markets",
        "summary": "Consumer prices remained flat, contradicting forecasts for an increase.",
        "source": "TestFeed",
        "datetime": datetime.now(),
        "impact": 3,
        "actual": "0.0%",
        "forecast": "0.3%",
        "previous": "0.2%",
        "currency": "USD",
    },
]


def run_tests():
    """Run built-in test cases to validate the engine."""
    sep = "═" * 90
    print(f"\n{sep}")
    print("  🧪  NEWS ENGINE v5.0 — BUILT-IN TEST SUITE")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{sep}\n")

    # ── Test 1: Sentiment Analyzer ──
    print("── Test 1: Sentiment Analyzer ──────────────────────────────────────")
    analyzer = FinancialSentimentAnalyzer()
    print(f"  Method: {analyzer.method}\n")

    test_texts = [
        "CPI comes in hotter than expected, inflation surges dramatically",
        "Fed cuts rates unexpectedly, dovish pivot catches markets off guard",
        "Inflation did NOT rise as expected, surprising markets",
        "Gold prices plunge sharply as dollar strengthens on hawkish Fed",
        "ECB President Lagarde hints at possible rate cut, dovish tone",
        "Non-farm payrolls beat expectations, labor market remains resilient",
        "Geopolitical tensions escalate, safe haven demand surges",
        "GDP growth slows significantly, recession fears mount",
    ]

    for text in test_texts:
        result = analyzer.analyze(text)
        tone = result["general_tone"]
        tone_label = "BULLISH" if tone > 0.2 else ("BEARISH" if tone < -0.2 else "NEUTRAL")
        sigs = "  ".join(f"{k}:{v}" for k, v in result["instrument_labels"].items())
        ents = []
        if result["entities"]["currencies"]:
            ents.append(f"CCY:{','.join(result['entities']['currencies'])}")
        if result["entities"]["central_banks"]:
            ents.append(f"CB:{','.join(result['entities']['central_banks'])}")
        if result["entities"]["indicators"]:
            ents.append(f"IND:{','.join(result['entities']['indicators'])}")
        ent_str = " | ".join(ents) if ents else "no entities"

        print(f"  📰 {text[:70]}...")
        print(f"     Tone: {tone_label} ({tone:+.3f})  |  Intensity: {result['intensity']:.1f}")
        print(f"     Signals: {sigs if sigs else 'none'}")
        print(f"     Entities: {ent_str}")
        print()

    # ── Test 2: Pattern Detection ──
    print("── Test 2: Pattern Detection & Context Memory ──────────────────────")

    from news_engine import analyze_ff_event, analyze_rss_item
    analyzed = []
    for ev in TEST_EVENTS:
        if ev.get("source") == "TestFeed":
            item = {
                "title": ev["title"],
                "summary": ev.get("summary", ""),
                "source": "TestFeed",
                "datetime": ev["datetime"],
                "link": "",
            }
            result = analyze_rss_item(item)
        else:
            result = analyze_ff_event(ev)
        analyzed.append(result)
        remember_event(result)

    for i, ev in enumerate(analyzed):
        weights = ev.get("instrument_weights", {})
        for inst, w in weights.items():
            record_pressure(inst, w, ev.get("datetime", datetime.now()))

    patterns = detect_patterns(lookback_hours=72)
    print(f"\n  Detected {len(patterns)} pattern(s):\n")
    for p in patterns:
        print(f"  🔍 {p['pattern_name']}  (conviction: {p['conviction']:.0%})")
        print(f"     {p['description']}")
        print(f"     Matches: {p['match_count']}/{p['min_required']}  |  "
              f"Gold implication: {p['gold_implication']}")
        print()

    # ── Test 3: Signal Convergence ──
    print("── Test 3: Signal Convergence Analysis ─────────────────────────────")
    convergence = analyze_signal_convergence(analyzed)
    for inst in ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF"):
        conv = convergence.get(inst, {})
        conflict = "⚠️ " if conv.get("conflict") else "✅ "
        print(f"  {conflict}{inst:<8} → {conv.get('direction', 'N/A'):<7}  "
              f"conviction:{conv.get('conviction', 0):.0%}  "
              f"agreement:{conv.get('agreement', 0):.0%}  "
              f"sources:{conv.get('source_count', 0)}")
        print(f"           {conv.get('note', '')}")
    print()

    # ── Test 4: Regime Detection ──
    print("── Test 4: Regime Detection ──────────────────────────────────────────")
    regime = detect_regime(analyzed, patterns)
    print(f"\n  📊 Current Regime: {regime['regime']}  (confidence: {regime['confidence']:.0%})")
    print(f"     {regime['description']}")
    print(f"     Gold bias: {regime['gold_bias']}  |  Dollar bias: {regime['dollar_bias']}")
    print(f"     All scores: {regime['all_scores']}")
    print()

    # ── Test 5: Pressure Trends ──
    print("── Test 5: Pressure Trend Analysis ─────────────────────────────────")
    for inst in ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF"):
        trend = analyze_pressure_trend(inst)
        print(f"  {inst:<8} → {trend['trend']:<22}  "
              f"slope:{trend['slope']:+.3f}  "
              f"accel:{trend['acceleration']:+.3f}  "
              f"vol:{trend['volatility']:.2f}  "
              f"pts:{trend['data_points']}")
    print()

    # ── Test 6: Memory Snapshot ──
    print("── Test 6: Memory Snapshot ──────────────────────────────────────────")
    snap = get_memory_snapshot()
    print(f"  Events remembered: {snap['total_events_remembered']}")
    print(f"  Pressure data points: {snap['pressure_points']}")
    print(f"  Event log sizes: {snap['event_log_sizes']}")
    print(f"  Detected patterns (total): {snap['detected_patterns']}")
    print(f"  Current regime: {snap['current_regime']} ({snap['regime_confidence']:.0%})")
    print()

    print(f"{'═' * 90}")
    print("  ✅ All tests completed successfully.")
    print(f"{'═' * 90}\n")


# =============================================================================
# DASHBOARD RENDERER  (the "digital tab" — updates in place)
# =============================================================================

def _render_header(cycle: int, total_lines_hint: int = 0) -> str:
    """Render the fixed top header of the dashboard."""
    w = min(_terminal_width(), 120)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []

    lines.append(_color(_double_separator(), ANSI_CYAN))
    title = "📰  INSTITUTIONAL NEWS & MACRO ANALYTICAL ENGINE  ·  v5.0 Context-Aware"
    lines.append(_color(_center(title, w), ANSI_CYAN))
    lines.append(_color(_center(f"Live Dashboard  |  {now_str}  |  Cycle #{cycle}", w), ANSI_DIM))
    lines.append(_color(_double_separator(), ANSI_CYAN))
    lines.append("")  # breathing room
    return "\n".join(lines)


def _render_listener_status() -> str:
    """Render listener status bar with nice spacing."""
    status = listener_status()
    dot = _color("🟢 LIVE", ANSI_GREEN) if status["alive"] else _color("🔴 OFFLINE", ANSI_RED)
    totals = status["store_totals"]
    lines = []

    # Source states with spacing
    src_lines = []
    for name, state in status.get("sources", {}).items():
        last_attempt = state.get("last_attempt")
        if isinstance(last_attempt, datetime) and last_attempt.year > 2000:
            la_str = last_attempt.strftime("%H:%M:%S")
        else:
            la_str = "never"
        note = state.get("last_note", "")
        src_lines.append(f"    {_color(name.upper(), ANSI_YELLOW):<10}  "
 f"last: {la_str}  |  {note}")

    lines.append(f"  Listener: {dot}")
    lines.append("")
    lines.append(f"  Store →  FF Events: {_color(str(totals.get('ff_events', 0)), ANSI_WHITE)} "
                 f"RSS Items: {_color(str(totals.get('rss_items', 0)), ANSI_WHITE)}   "
                 f"FRED Series: {_color(str(totals.get('fred_series', 0)), ANSI_WHITE)}")
    lines.append("")
    lines.append(f"  {_color('Source States:', ANSI_BLUE)}")
    for sl in src_lines:
        lines.append(sl)
    lines.append("")
    lines.append(f"  {_color('Sentiment Engine:', ANSI_BLUE)}  "
                 f"{_color(FinancialSentimentAnalyzer().method, ANSI_MAGENTA)}")
    lines.append("")
    return "\n".join(lines)


def _render_regime(regime: dict) -> str:
    """Render the macro regime section with clear spacing."""
    if not regime:
        return ""
    lines = []
    lines.append(_color(_box_header("📊  MACRO REGIME"), ANSI_BLUE))
    lines.append("")

    regime_name = regime.get("regime", "N/A")
    confidence = regime.get("confidence", 0)
    conf_color = ANSI_GREEN if confidence > 0.5 else (ANSI_YELLOW if confidence > 0.3 else ANSI_RED)

    lines.append(f"     {_bold(regime_name)}  "
                 f"(confidence: {_color(f'{confidence:.0%}', conf_color)})")
    lines.append("")
    lines.append(f"     {_dim(regime.get('description', ''))}")
    lines.append("")
    lines.append(f"     Gold Bias:    {_color(regime.get('gold_bias', 'N/A'), ANSI_YELLOW)}")
    lines.append(f"     Dollar Bias: {_color(regime.get('dollar_bias', 'N/A'), ANSI_YELLOW)}")
    lines.append("")

    # Show all regime scores as a mini bar chart
    all_scores = regime.get("all_scores", {})
    if all_scores:
        lines.append(f"     {_color('All Regime Scores:', ANSI_BLUE)}")
        lines.append("")
        max_score = max(all_scores.values()) if all_scores else 1
        for r_name, r_score in sorted(all_scores.items(), key=lambda x: -x[1]):
            bar_len = int((r_score / max_score) * 25) if max_score > 0 else 0
            bar = "█" * bar_len
            is_dominant = r_name == regime_name
            color = ANSI_GREEN if is_dominant else ANSI_DIM
            lines.append(f"       {_color(f'{r_name:<14}', color)}  "
                         f"{_color(bar, color)}  {r_score:.2f}")
        lines.append("")

    return "\n".join(lines)


def _render_patterns(patterns: list[dict]) -> str:
    """Render detected patterns with good spacing."""
    lines = []
    lines.append(_color(_box_header("🔍  DETECTED PATTERNS"), ANSI_MAGENTA))
    lines.append("")

    if not patterns:
        lines.append(f"     {_dim('No recurring themes detected in current window.')}")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"     {_color(f'{len(patterns)} pattern(s) active:', ANSI_WHITE)}")
    lines.append("")

    for p in patterns[:5]:
        conv = p.get("conviction", 0)
        conv_color = ANSI_GREEN if conv > 0.6 else (ANSI_YELLOW if conv > 0.3 else ANSI_RED)

        lines.append(f"     {_color('●', ANSI_MAGENTA)} "
                     f"{_bold(p['pattern_name'])}  "
                     f"({_color(f'conviction: {conv:.0%}', conv_color)})")
        lines.append(f"       {_dim(p['description'])}")
        lines.append(f"       Matches: {p['match_count']}/{p['min_required']} required  |  "
                     f"Window: {p['window_hours']}h  |  "
                     f"Gold: {_color(p['gold_implication'], ANSI_YELLOW)}")
        lines.append("")

        for m in p.get("matching_events", [])[:3]:
            dt = m.get("datetime")
            dt_str = dt.strftime("%m/%d %H:%M") if isinstance(dt, datetime) else "??"
            lines.append(f"         {_dim('└─')} "
                         f"[{dt_str}] ({m.get('source', '')}) "
                         f"{m.get('title', '')[:65]}")
        lines.append("")

    return "\n".join(lines)


def _render_convergence(convergence: dict) -> str:
    """Render signal convergence with spacing."""
    lines = []
    lines.append(_color(_box_header("📡  SIGNAL CONVERGENCE"), ANSI_CYAN))
    lines.append("")

    if not convergence:
        lines.append(f"     {_dim('No convergence data yet.')}")
        lines.append("")
        return "\n".join(lines)

    for inst in ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF"):
        conv = convergence.get(inst, {})
        direction = conv.get("direction", "NEUTRAL")
        conviction = conv.get("conviction", 0.0)
        agreement = conv.get("agreement", 0.0)
        source_count = conv.get("source_count", 0)
        conflict = conv.get("conflict", False)
        note = conv.get("note", "")

        dir_color = ANSI_GREEN if direction == "BULL" else (ANSI_RED if direction == "BEAR" else ANSI_DIM)
        icon = "⚠️ " if conflict else "✅ "

        lines.append(f"     {icon} {_bold(inst):<10} "
                     f"→ {_color(direction, dir_color):<8}  "
                     f"conviction: {conviction:.0%}   "
                     f"agreement: {agreement:.0%}   "
                     f"sources: {source_count}")
        lines.append(f"                  {note}")
        lines.append("")

    return "\n".join(lines)


def _render_trends(trends: dict) -> str:
    """Render pressure trends with spacing."""
    lines = []
    lines.append(_color(_box_header("📈  PRESSURE TRENDS"), ANSI_BLUE))
    lines.append("")

    if not trends:
        lines.append(f"     {_dim('Insufficient data for trend analysis.')}")
        lines.append("")
        return "\n".join(lines)

    for inst in ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF"):
        t = trends.get(inst, {})
        trend_name = t.get("trend", "N/A")
        slope = t.get("slope", 0)
        accel = t.get("acceleration", 0)
        vol = t.get("volatility", 0)
        current = t.get("current", 0)
        pts = t.get("data_points", 0)

        # Color the trend based on direction
        if "BULLISH" in trend_name:
            t_color = ANSI_GREEN
        elif "BEARISH" in trend_name:
            t_color = ANSI_RED
        else:
            t_color = ANSI_DIM

        lines.append(f"     {_bold(inst):<10} → {_color(trend_name, t_color):<24}  "
                     f"slope: {slope:+.3f}   accel: {accel:+.3f}   "
                     f"vol: {vol:.2f}   pts: {pts}")
    lines.append("")

    return "\n".join(lines)


def _render_correlations(correlations: list[dict]) -> str:
    """Render multi-source correlations."""
    lines = []
    lines.append(_color(_box_header("🔗  MULTI-SOURCE CORRELATIONS"), ANSI_YELLOW))
    lines.append("")

    if not correlations:
        lines.append(f"     {_dim('No multi-source correlations detected.')}")
        lines.append("")
        return "\n".join(lines)

    for c in correlations[:5]:
        lines.append(f"     {_color('●', ANSI_YELLOW)} "
                     f"'{_bold(c['theme'])}' —  "
                     f"{c['source_count']} sources, "
                     f"{c['event_count']} events, "
                     f"span: {c['time_span_hours']}h")
        lines.append(f"       {c['note']}")
        lines.append("")

    return "\n".join(lines)


def _render_forward_calendar(forward: list[dict]) -> str:
    """Render forward calendar with spacing."""
    lines = []
    lines.append(_color(_box_header("⏭️  FORWARD CALENDAR — Next 72h High-Impact"), ANSI_GREEN))
    lines.append("")

    if not forward:
        lines.append(f"     {_dim('No high-impact events scheduled in the next 72 hours.')}")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"     {_color(f'{len(forward)} upcoming high-impact event(s):', ANSI_WHITE)}")
    lines.append("")

    for ev in forward[:8]:
        dt = ev.get("datetime")
        dt_str = dt.strftime("%m/%d %H:%M") if isinstance(dt, datetime) else "??"
        urgency = ev.get("urgency", "")
        currency = ev.get("currency", "")
        title = ev.get("title", "")[:60]

        # Color-code urgency
        if urgency == "IMMINENT":
            u_color = ANSI_RED
        elif urgency == "VERY SOON":
            u_color = ANSI_YELLOW
        elif urgency == "WITHIN 24H":
            u_color = ANSI_CYAN
        else:
            u_color = ANSI_DIM

        lines.append(f"     [{dt_str}] {_color(urgency, u_color):<14} "
                     f"{currency:<4} — {title}")
        lines.append(f"       {_dim(ev.get('pre_positioning_note', ''))}")
        lines.append("")

    return "\n".join(lines)


def _render_ff_events(ff_analyzed: list[dict]) -> str:
    """Render Forex Factory events with improved spacing."""
    lines = []
    lines.append(_color(_box_header("📅  FOREX FACTORY ECONOMIC CALENDAR"), ANSI_CYAN))
    lines.append("")

    if not ff_analyzed:
        lines.append(f"     {_dim('No events in current lookback window.')}")
        lines.append("")
        return "\n".join(lines)

    BM_LABEL = {"beat": "✅ BEAT", "miss": "❌ MISS",
                "inline": "➡ INLINE", None: "⏳ PENDING"}

    for tier_label, min_impact in [("🔴 HIGH IMPACT", 3), ("🟡 MEDIUM IMPACT", 2), ("⚪ LOW IMPACT", 1)]:
        tier_events = [e for e in ff_analyzed if e["impact"] == min_impact]
        if not tier_events:
            continue

        lines.append(f"  {_bold(tier_label)}")
        lines.append(f"  {_color(_separator(80, '─'), ANSI_DIM)}")
        lines.append("")

        for ev in tier_events:
            dt = ev.get("datetime")
            dt_str = dt.strftime("%m/%d %H:%M") if dt else "??"
            bm = BM_LABEL.get(ev.get("beat_miss"), "")

            lines.append(f"    [{dt_str}] {ev['currency']} — {_bold(ev['title'])}")
            lines.append("")

            if ev.get("actual") or ev.get("forecast"):
                surprise = ev.get("surprise_magnitude_pct")
                surprise_str = f"  |  Surprise: {surprise:+.1f}%" if surprise is not None else ""
                lines.append(f"      Actual: {ev.get('actual') or '?'} "
                             f"Forecast: {ev.get('forecast') or '?'}   "
                             f"Prev: {ev.get('previous') or '?'}   {bm}{surprise_str}")
                lines.append("")

            if ev.get("instrument_signals"):
                sigs = " ".join(f"{k}: {_color(v, ANSI_GREEN if v == 'BULL' else ANSI_RED if v == 'BEAR' else ANSI_DIM)}"
                                  for k, v in ev["instrument_signals"].items())
                lines.append(f"      Signals → {sigs}")
                lines.append("")

            if ev.get("general_tone") is not None:
                tone = ev["general_tone"]
                tone_str = "BULLISH" if tone > 0.2 else ("BEARISH" if tone < -0.2 else "NEUTRAL")
                tone_color = ANSI_GREEN if tone > 0.2 else (ANSI_RED if tone < -0.2 else ANSI_DIM)
                lines.append(f"      Sentiment: {_color(tone_str, tone_color)} ({tone:+.2f})")
                lines.append("")

            lines.append(f"      {_color('📊 SHORT:', ANSI_BLUE)} {ev['short_term_impact']}")
            lines.append(f"      {_color('📈 LONG:', ANSI_BLUE)}  {ev['long_term_impact']}")
            lines.append("")

            for r in ev.get("macro_reasoning", []):
                lines.append(f"      {_color('🧠', ANSI_MAGENTA)} {r}")
            lines.append("")
            lines.append(f"  {_color(_separator(80, '·'), ANSI_DIM)}")
            lines.append("")

    return "\n".join(lines)


def _render_rss_headlines(rss_analyzed: list[dict]) -> str:
    """Render RSS headlines with improved spacing."""
    lines = []
    lines.append(_color(_box_header("📰  LIVE NEWS HEADLINES — Top 15 Relevant"), ANSI_CYAN))
    lines.append("")

    sorted_rss = sorted(
        rss_analyzed,
        key=lambda x: (-x["impact"],
                       -(x["datetime"].timestamp() if isinstance(x.get("datetime"), datetime) else 0))
    )

    shown = 0
    for item in sorted_rss:
        if not item.get("instrument_signals"):
            continue
        dt = item.get("datetime")
        dt_str = dt.strftime("%m/%d %H:%M") if isinstance(dt, datetime) else "??"
        impact_tier = {3: "🔴 HIGH", 2: "🟡 MEDIUM", 1: "⚪ LOW"}.get(item.get("impact", 1), "—")

        lines.append(f"    [{dt_str}] [{impact_tier}] ({item['source']})")
        lines.append(f"    {_bold(item['title'])}")
        lines.append("")

        sigs = "   ".join(
            f"{k}: {_color(v, ANSI_GREEN if v == 'BULL' else ANSI_RED if v == 'BEAR' else ANSI_DIM)}"
            for k, v in item["instrument_signals"].items()
        )
        lines.append(f"      Signals → {sigs}")

        if item.get("general_tone") is not None:
            tone = item["general_tone"]
            tone_str = "BULLISH" if tone > 0.2 else ("BEARISH" if tone < -0.2 else "NEUTRAL")
            tone_color = ANSI_GREEN if tone > 0.2 else (ANSI_RED if tone < -0.2 else ANSI_DIM)
            lines.append(f"      Sentiment: {_color(tone_str, tone_color)} ({tone:+.2f})")

        lines.append(f"      {_color('📊 SHORT:', ANSI_BLUE)} {item['short_term_impact']}   "
                     f"{_color('📈 LONG:', ANSI_BLUE)} {item['long_term_impact']}")
        lines.append("")

        for r in item.get("macro_reasoning", []):
            lines.append(f"      {_color('🧠', ANSI_MAGENTA)} {r}")
        lines.append("")

        if item.get("link"):
            lines.append(f"      {_color('🔗', ANSI_BLUE)} {item['link'][:80]}")
        lines.append(f"  {_color(_separator(80, '·'), ANSI_DIM)}")
        lines.append("")

        shown += 1
        if shown >= 15:
            break

    if shown == 0:
        lines.append(f"     {_dim('No relevant headlines with instrument signals in current window.')}")
        lines.append("")

    return "\n".join(lines)

def _render_fred(fred_narratives: list[dict]) -> str:
    """Render FRED data with spacing."""
    lines = []
    lines.append(_color(_box_header("🏛️  FRED MACRO INDICATORS"), ANSI_BLUE))
    lines.append("")

    if not fred_narratives:
        lines.append(f"     {_dim('FRED data unavailable (check API key or network).')}")
        lines.append("")
        return "\n".join(lines)

    for n in fred_narratives:
        arrow = "↑" if n["direction"] == "up" else "↓"
        dir_color = ANSI_GREEN if n["direction"] == "up" else ANSI_RED

        # Pre-format values to avoid nested f-strings with backslash escapes
        latest_str = f"{n['latest_value']:.4f}"
        prev_str = f"{n['previous_value']:.4f}"
        change_str = f"{arrow} {abs(n['change_pct']):.2f}%"

        lines.append(f"    {_bold(n['label'])} ({n['series_id']})")
        lines.append("")
        lines.append(f"      Latest: {_color(latest_str, ANSI_WHITE)} "
                     f"({n['latest_date']})   "
                     f"Prev: {_color(prev_str, ANSI_WHITE)} "
                     f"({n['previous_date']})   "
                     f"{_color(change_str, dir_color)}")
        lines.append("")
        lines.append(f"      {_color('📊 SHORT:', ANSI_BLUE)} {n['short_term_impact']}   "
                     f"{_color('📈 LONG:', ANSI_BLUE)} {n['long_term_impact']}")
        lines.append("")

        for r in n.get("macro_reasoning", []):
            lines.append(f"      {_color('🧠', ANSI_MAGENTA)} {r}")
        lines.append("")
        lines.append(f"  {_color(_separator(80, '·'), ANSI_DIM)}")
        lines.append("")

    return "\n".join(lines)


def _render_pressure(pressure: dict) -> str:
    """Render pressure scores with spacing and color."""
    lines = []
    lines.append(_color(_box_header("📊  AGGREGATE NEWS PRESSURE SCORES"), ANSI_YELLOW))
    lines.append("")
    lines.append(f"  {_dim('Positive = net bullish pressure  |  Negative = net bearish pressure')}")
    lines.append("")

    for inst, score in sorted(pressure.items(), key=lambda x: -abs(x[1])):
        bar_len = min(int(abs(score)), 30)
        if score >= 0:
            bar = _color("█" * bar_len, ANSI_GREEN)
            label = _color("BULLISH ↑", ANSI_GREEN) if score > 1 else _color("NEUTRAL →", ANSI_DIM)
        else:
            bar = _color("▓" * bar_len, ANSI_RED)
            label = _color("BEARISH ↓", ANSI_RED) if score < -1 else _color("NEUTRAL →", ANSI_DIM)

        score_color = ANSI_GREEN if score > 1 else (ANSI_RED if score < -1 else ANSI_DIM)
        score_str = f"{score:+6.1f}"
        lines.append(f"  {_bold(inst):<10} {_color(score_str, score_color)}  "
                     f"{bar:<30}  {label}")
    lines.append("")

    return "\n".join(lines)


def _render_footer(cycle: int, interval: int, countdown: int) -> str:
    """Render the dashboard footer with countdown."""
    w = min(_terminal_width(), 120)
    lines = []
    lines.append(_color(_separator(w, "─"), ANSI_DIM))
    lines.append("")
    lines.append(f"  {_color('Cycle', ANSI_DIM)} #{cycle}   "
                 f"{_color('|', ANSI_DIM)}   "
                 f"{_color('Next refresh in', ANSI_DIM)} "
                 f"{_color(f'{countdown}s', ANSI_YELLOW)}   "
                 f"{_color('|', ANSI_DIM)}   "
                 f"{_color('Press Ctrl+C to stop', ANSI_RED)}")
    lines.append("")
    lines.append(_color(_double_separator(w), ANSI_CYAN))
    return "\n".join(lines)


def _render_memory_snapshot(mem: dict) -> str:
    """Render memory snapshot with spacing."""
    if not mem:
        return ""
    lines = []
    lines.append(_color(_box_header("🧠  MEMORY SNAPSHOT"), ANSI_MAGENTA))
    lines.append("")
    lines.append(f"     Events Remembered:   {_color(str(mem.get('total_events_remembered', 0)), ANSI_WHITE)}")
    lines.append(f"     Pressure Data Points: {_color(str(mem.get('pressure_points', {})), ANSI_WHITE)}")
    lines.append(f"     Detected Patterns:   {_color(str(mem.get('detected_patterns', 0)), ANSI_WHITE)}")
    lines.append(f"     Current Regime:      {_color(mem.get('current_regime', 'N/A'), ANSI_YELLOW)}  "
                 f"({mem.get('regime_confidence', 0):.0%})")
    lines.append("")
    return "\n".join(lines)


def _build_dashboard(ctx: dict, cycle: int, interval: int, countdown: int) -> str:
    """Assemble the full dashboard string from context data."""
    parts = []

    # ── Fixed header ──
    parts.append(_render_header(cycle))

    # ── Listener status ──
    parts.append(_render_listener_status())

    # ── Context intelligence sections ──
    cr = ctx.get("context_report", {})
    if cr:
        regime = cr.get("regime", {})
        if regime:
            parts.append(_render_regime(regime))

        patterns = cr.get("patterns", [])
        parts.append(_render_patterns(patterns))

        convergence = cr.get("convergence", {})
        parts.append(_render_convergence(convergence))

        trends = cr.get("pressure_trends", {})
        parts.append(_render_trends(trends))

        correlations = cr.get("correlations", [])
        parts.append(_render_correlations(correlations))

        forward = cr.get("forward_calendar", [])
        parts.append(_render_forward_calendar(forward))

        mem = cr.get("memory_snapshot", {})
        parts.append(_render_memory_snapshot(mem))

    # ── Forex Factory events ──
    parts.append(_render_ff_events(ctx.get("ff_analyzed", [])))

    # ── RSS headlines ──
    parts.append(_render_rss_headlines(ctx.get("rss_analyzed", [])))

    # ── FRED indicators ──
    parts.append(_render_fred(ctx.get("fred_narratives", [])))

    # ── Pressure scores ──
    parts.append(_render_pressure(ctx.get("pressure_scores", {})))

    # ── Footer ──
    parts.append(_render_footer(cycle, interval, countdown))

    return "\n".join(parts)


# =============================================================================
# LIVE MONITORING MODE  (digital dashboard — updates in place)
# =============================================================================

def run_live(interval_seconds: int = 60):
    """
    Run the engine in continuous dashboard mode.
    The screen redraws in place every second — like a digital trading terminal.
    No scrolling text. A live countdown timer ticks down between data refreshes.
    """
    # Hide cursor for a cleaner dashboard look
    sys.stdout.write(ANSI_HIDE_CURSOR)
    sys.stdout.flush()

    # Start the always-on listener
    start_news_listener()

    # Give the listener a moment to do its first pass
    time.sleep(3)

    running = True

    def _signal_handler(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _signal_handler)

    cycle = 0

    while running:
        cycle += 1

        # ── Fetch fresh context ONCE per cycle ──
        ctx = get_news_context(force_refresh=False)

        # ── Countdown loop: redraw the full dashboard every second ──
        # with the updated countdown number.  Using \033[H (cursor to
        #     home) instead of \033[2J (clear screen) eliminates flicker
        #     because the terminal overwrites character-by-character without
        #     a blank frame in between.
        first_render_of_cycle = True

        for countdown in range(interval_seconds, 0, -1):
            if not running:
                break

            # Build the dashboard string with the current countdown
            dashboard = _build_dashboard(ctx, cycle, interval_seconds, countdown)

            if first_render_of_cycle:
                # Full clear on the first render of each cycle — wipes any
                # leftover content from the previous cycle that might be
                # shorter (fewer RSS items, etc.)
                sys.stdout.write(ANSI_CLEAR + ANSI_HOME)
                first_render_of_cycle = False
            else:
                # Just move cursor to home — no clear, no flicker.
                # The new content overwrites the old in place.
                sys.stdout.write(ANSI_HOME)

            # Write the dashboard and clear anything below it
            sys.stdout.write(dashboard)
            sys.stdout.write("\033[J")  # Erase from cursor to end of screen
            sys.stdout.flush()

            # Wait 1 second before the next countdown tick
            time.sleep(1)

    # ── Cleanup on exit ──
    stop_news_listener()
    sys.stdout.write(ANSI_SHOW_CURSOR)
    sys.stdout.flush()
    _clear_screen()
    print("\n  News engine stopped. Goodbye.\n")


# =============================================================================
# ONE-SHOT MODE
# =============================================================================

def run_oneshot():
    """Run a single analysis pass and print the full report."""
    w = min(_terminal_width(), 120)
    print(f"\n{_color(_double_separator(w), ANSI_CYAN)}")
    print(_color(_center("📰  NEWS ENGINE v5.0 — ONE-SHOT ANALYSIS", w), ANSI_CYAN))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(_color(_center(f"Time: {now_str}", w), ANSI_DIM))
    print(f"{_color(_double_separator(w), ANSI_CYAN)}\n")

    print("  Starting always-on listener and doing an initial synchronous pass...\n")
    ctx = get_news_context(force_refresh=True)

    # Render using the dashboard renderer for consistent formatting
    dashboard = _build_dashboard(ctx,1, 0, 0)
    print(dashboard)

    # Print context intelligence summary
    cr = ctx.get("context_report", {})
    if cr:
        print(_color(_box_header("📋  CONTEXT INTELLIGENCE SUMMARY"), ANSI_BLUE))
        print()

        regime = cr.get("regime", {})
        if regime:
            print(f"  {_color('Regime:', ANSI_BLUE)} {_bold(regime.get('regime', 'N/A'))}  "
                  f"(confidence: {regime.get('confidence', 0):.0%})")
            print(f"  {regime.get('description', '')}")
            print(f"  Gold bias: {regime.get('gold_bias', 'N/A')}  |  "
                  f"Dollar bias: {regime.get('dollar_bias', 'N/A')}")
            print()

        patterns = cr.get("patterns", [])
        if patterns:
            print(f"  {_color(f'Detected Patterns ({len(patterns)}):', ANSI_BLUE)}")
            for p in patterns[:5]:
                print(f" • {p['pattern_name']}  (conviction: {p['conviction']:.0%})")
                print(f"      {p['description']}")
                print(f"      Gold implication: {p['gold_implication']}")
                print()

        convergence = cr.get("convergence", {})
        if convergence:
            print(f"  {_color('Signal Convergence:', ANSI_BLUE)}")
            for inst in ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF"):
                conv = convergence.get(inst, {})
                conflict = "⚠️ " if conv.get("conflict") else "✅ "
                print(f"    {conflict}{inst:<10} → {conv.get('direction', 'N/A'):<8}  "
                      f"conviction: {conv.get('conviction', 0):.0%}  "
                      f"agreement: {conv.get('agreement', 0):.0%}  "
                      f"sources: {conv.get('source_count', 0)}")
                if conv.get("note"):
                    print(f"                {conv['note']}")
            print()

        trends = cr.get("pressure_trends", {})
        if trends:
            print(f"  {_color('Pressure Trends:', ANSI_BLUE)}")
            for inst in ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF"):
                t = trends.get(inst, {})
                print(f"    {inst:<10} → {t.get('trend', 'N/A'):<24}  "
                      f"slope: {t.get('slope', 0):+.3f}  "
                      f"accel: {t.get('acceleration', 0):+.3f}  "
                      f"vol: {t.get('volatility', 0):.2f}")
            print()

        correlations = cr.get("correlations", [])
        if correlations:
            print(f"  {_color(f'Multi-Source Correlations ({len(correlations)}):', ANSI_BLUE)}")
            for c in correlations[:5]:
                print(f"    • '{c['theme']}' — {c['source_count']} sources, "
                      f"{c['event_count']} events, span: {c['time_span_hours']}h")
                print(f"      {c['note']}")
            print()

        forward = cr.get("forward_calendar", [])
        if forward:
            print(f"  {_color('Forward Calendar (next 72h):', ANSI_BLUE)}")
            for ev in forward[:8]:
                dt = ev.get("datetime")
                dt_str = dt.strftime("%m/%d %H:%M") if isinstance(dt, datetime) else "??"
                print(f"    [{dt_str}] {ev.get('urgency', ''):<12} "
                      f"{ev.get('currency', '')} — {ev.get('title', '')[:60]}")
                print(f"      {ev.get('pre_positioning_note', '')}")
            print()
        else:
            print(f"  {_color('Forward Calendar:', ANSI_BLUE)} No high-impact events in next 72h.")
            print()

        mem = cr.get("memory_snapshot", {})
        if mem:
            print(f"  {_color('Memory Snapshot:', ANSI_BLUE)}")
            print(f"    Events remembered: {mem.get('total_events_remembered', 0)}")
            print(f"    Pressure data points: {mem.get('pressure_points', {})}")
            print(f"    Current regime: {mem.get('current_regime', 'N/A')} "
                  f"({mem.get('regime_confidence', 0):.0%})")
            print()

    print(f"\n{_color(_double_separator(w), ANSI_CYAN)}")
    print(_color(_center("Analysis complete. Listener still running in background.", w), ANSI_DIM))
    print(_color(_center("Use --live for continuous dashboard, --test for test cases.", w), ANSI_DIM))
    print(f"{_color(_double_separator(w), ANSI_CYAN)}\n")


# =============================================================================
# NEWS-ONLY MODE
# =============================================================================

def run_news_only():
    """Force a synchronous refresh and print the summary."""
    w = min(_terminal_width(), 120)
    print(f"\n{_color(_double_separator(w), ANSI_CYAN)}")
    print(_color(_center("📰  NEWS ENGINE v5.0 — NEWS-ONLY (FORCE REFRESH)", w), ANSI_CYAN))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(_color(_center(f"Time: {now_str}", w), ANSI_DIM))
    print(f"{_color(_double_separator(w), ANSI_CYAN)}\n")

    ctx = get_news_context(force_refresh=True, high_impact_only=False)
    dashboard = _build_dashboard(ctx, 1, 0, 0)
    print(dashboard)


# =============================================================================
# STATUS MODE
# =============================================================================

def run_status():
    """Print a quick status snapshot of the listener and store."""
    w = min(_terminal_width(), 80)
    print(f"\n{_color(_separator(w), ANSI_CYAN)}")
    print(_color(_center("📊  NEWS ENGINE v5.0 — STATUS", w), ANSI_CYAN))
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(_color(_center(now_str, w), ANSI_DIM))
    print(f"{_color(_separator(w), ANSI_CYAN)}")

    status = listener_status()
    dot = _color("🟢 LIVE", ANSI_GREEN) if status["alive"] else _color("🔴 OFFLINE", ANSI_RED)
    print(f"\n  Listener: {dot}")
    if status.get("started_at"):
        print(f"  Started:  {status['started_at'].strftime('%Y-%m-%d %H:%M:%S')}")

    print(f"\n  {_color('Source States:', ANSI_BLUE)}")
    for name, state in status.get("sources", {}).items():
        last_attempt = state.get("last_attempt")
        if isinstance(last_attempt, datetime) and last_attempt.year > 2000:
            la_str = last_attempt.strftime("%H:%M:%S")
        else:
            la_str = "never"
        print(f"    {name:<10}  last: {la_str}  |  {state.get('last_note', '')}")

    print(f"\n  {_color('Store Totals:', ANSI_BLUE)}")
    totals = status.get("store_totals", {})
    print(f"    FF Events:   {totals.get('ff_events', 0)}")
    print(f"    RSS Items:   {totals.get('rss_items', 0)}")
    print(f"    FRED Series: {totals.get('fred_series', 0)}")

    try:
        from paths import describe_paths
        from news_engine import DB_PATH
        info = describe_paths()
        print(f"\n  {_color('Data Paths (AppData):', ANSI_BLUE)}")
        print(f"    App data:  {info.get('app_data_dir')}")
        print(f"    DB path:   {info.get('db_path')}")
        print(f"    DB exists: {info.get('db_exists')}  ({info.get('db_size_bytes', 0)} bytes)")
        print(f"    Engine DB: {DB_PATH}")
    except Exception as path_exc:
        print(f"\n  Path info unavailable: {path_exc}")

    print(f"\n{_color(_separator(w), ANSI_CYAN)}\n")


# =============================================================================
# HELP
# =============================================================================

def print_help():
    w = min(_terminal_width(), 80)
    print(f"\n{_color(_double_separator(w), ANSI_CYAN)}")
    print(_color(_center("NEWS ENGINE v5.0 - Context-Aware News & Macro Engine", w), ANSI_CYAN))
    print(f"{_color(_double_separator(w), ANSI_CYAN)}")
    print("""
  USAGE:
    python run_engine.py              One-shot analysis (default)
    python run_engine.py --live       Live terminal dashboard (60s refresh)
    python run_engine.py --live 30    Live terminal dashboard (30s refresh)
    python run_engine.py --gui        GUI dashboard with tabs (60s refresh)
    python run_engine.py --gui 30     GUI dashboard with tabs (30s refresh)
    python run_engine.py --test       Run built-in test suite
    python run_engine.py --news-only  Force refresh + print dashboard
    python run_engine.py --status     Quick listener status
    python run_engine.py --help       Show this help

  GUI MODE:
    Opens the PySide6 desktop shell (Research Desk first).
    Requires: pip install PySide6
    Tabs: Research Desk | Overview | Regime | Patterns | Signals |
          Calendar | News | FRED | Pressure
    Research Desk = premium 4-section thesis view (Sub-step 5.2).
    Legacy text tabs retained for Stage C deprecation later.
    Controls: Pause/Resume, Force Refresh, adjustable interval
    Keyboard: Ctrl+P=pause  Ctrl+R=refresh  Esc=quit

  LIVE TERMINAL MODE:
    Screen redraws in place every cycle.
    Note: terminal mode does not support scrolling (use --gui for that).

  FILES:
    run_engine.py           Main entry point (this file)
    paths.py                AppData path resolver (%APPDATA%\\Aethelon)
    models/                 Pydantic v2 Research Desk contracts
    gui_qt_dashboard.py     PySide6 main window (Research Desk + legacy)
    research_desk_view.py   Research Desk 4-section view (5.2)
    research_desk_data.py   Desk payload aggregator (5.1)
    news_engine.py          Core engine
    sentiment_analyzer.py   NLP sentiment
    pattern_engine.py       Context memory, patterns, regime
    backfill_macro_history.py  MacroState history builder (ops CLI)

  DATA:
    Windows: %%APPDATA%%\\Aethelon\\data\\news_engine_store.db
    Other:   ~/.aethelon/data/news_engine_store.db
    Legacy Quantamental / install-local DBs are auto-migrated on first run.

  INSTALL:
    pip install requests feedparser PySide6 pydantic
    pip install nltk   (optional - enables VADER sentiment)
""")
    print(f"{_color(_double_separator(w), ANSI_CYAN)}")


def _launch_gui(interval: int = 60) -> None:
    """Launch the PySide6 Research Desk shell (gui_qt_dashboard)."""
    print("  Starting GUI dashboard...")
    print(f"  Refresh interval: {interval}s")
    print("  Backend: PySide6 (gui_qt_dashboard.py)")
    print()
    try:
        from gui_qt_dashboard import run_qt_dashboard
    except ModuleNotFoundError as e:
        missing = str(e)
        print("  ERROR: Could not start the PySide6 GUI.")
        if "PySide6" in missing or "pyside6" in missing.lower():
            print("  PySide6 is not installed.")
            print("  Install with:  pip install PySide6")
        else:
            print(f"  Missing module: {e}")
            print("  Ensure gui_qt_dashboard.py and research_desk_view.py")
            print("  are in the same folder as run_engine.py.")
        raise SystemExit(1) from e
    except Exception as e:
        print("  ERROR: Could not import gui_qt_dashboard.")
        print(f"  {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        raise SystemExit(1) from e

    try:
        code = run_qt_dashboard(refresh_interval=interval)
        raise SystemExit(code if isinstance(code, int) else 0)
    except SystemExit:
        raise
    except Exception as e:
        print("  ERROR: GUI failed to start.")
        print(f"  {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        raise SystemExit(1) from e


def main():
    args = sys.argv[1:]

    if not args:
        run_oneshot()
        return

    mode = args[0].lower()

    if mode in ("--help", "-h", "help"):
        print_help()

    elif mode == "--test":
        run_tests()

    elif mode == "--live":
        interval = 60
        if len(args) > 1:
            try:
                interval = int(args[1])
            except ValueError:
                pass
        run_live(interval)

    elif mode == "--gui":
        interval = 60
        if len(args) > 1:
            try:
                interval = int(args[1])
            except ValueError:
                pass
        _launch_gui(interval)

    elif mode == "--news-only":
        run_news_only()

    elif mode == "--status":
        run_status()

    else:
        print(f"  Unknown mode: {mode}")
        print()
        print_help()


if __name__ == "__main__":
    main()
