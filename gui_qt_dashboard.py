# -*- coding: utf-8 -*-
"""
gui_qt_dashboard.py — PySide6 Main Window (Research Desk + Legacy Tabs)
=======================================================================
Production shell for Sub-step 5.2:

  • Tab 0: Research Desk (premium 4-section view)
  • Tabs 1–8: Legacy sections (Overview, Regime, Patterns, Signals,
    Calendar, News, FRED, Pressure) — text views preserved so Stage C
    deprecation can happen later without breaking today's workflow.

Data paths:
  • Engine context via get_news_context() on a worker thread
  • Research Desk via build_research_desk(ctx=...) on its own worker
    (ResearchDeskView.refresh) so the UI never blocks on I/O.

Launch:
    python run_engine.py --gui
    python run_engine.py --gui 30
    python gui_qt_dashboard.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from typing import Any, Optional

from PySide6.QtCore import Qt, QThread, QObject, Signal, Slot, QTimer
from PySide6.QtGui import QColor, QFont, QPalette, QAction, QKeySequence, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QPlainTextEdit,
    QStatusBar,
    QFrame,
    QSizePolicy,
)

from research_desk_view import ResearchDeskView, DeskTheme


# =============================================================================
# ENGINE WORKER (non-blocking get_news_context)
# =============================================================================

class _EngineFetchWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, force: bool = False):
        super().__init__()
        self._force = force

    @Slot()
    def run(self) -> None:
        try:
            from news_engine import get_news_context
            ctx = get_news_context(force_refresh=self._force)
            self.finished.emit(ctx if isinstance(ctx, dict) else {})
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# =============================================================================
# LEGACY TEXT TAB
# =============================================================================

class LegacyTextTab(QWidget):
    """Simple scrollable monospaced text surface for pre-Stage-C tabs."""

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.title = title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self.summary = QLabel("  Loading…")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(
            f"background:{DeskTheme.PANEL}; color:{DeskTheme.WHITE};"
            f"border:1px solid {DeskTheme.BORDER}; border-radius:6px;"
            f"padding:8px 12px; font-weight:600;"
        )
        lay.addWidget(self.summary)

        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.body.setStyleSheet(
            f"QPlainTextEdit {{"
            f" background:{DeskTheme.PANEL_ALT}; color:{DeskTheme.TEXT};"
            f" border:1px solid {DeskTheme.BORDER_SOFT}; border-radius:6px;"
            f" padding:10px; font-family: Consolas, 'Cascadia Mono', monospace;"
            f" font-size: 11px;"
            f"}}"
        )
        lay.addWidget(self.body, stretch=1)

    def set_content(self, summary: str, body: str) -> None:
        # Preserve scroll if user is reading mid-document
        bar = self.body.verticalScrollBar()
        pos = bar.value()
        at_bottom = pos >= (bar.maximum() - 4)

        self.summary.setText("  " + (summary or ""))
        self.body.setPlainText(body or "")

        if at_bottom:
            bar.setValue(bar.maximum())
        else:
            bar.setValue(pos)


# =============================================================================
# MAIN WINDOW
# =============================================================================

class NewsEngineQtDashboard(QMainWindow):
    """
    Primary desktop shell: Research Desk first, legacy tabs retained.
    """

    LEGACY_TABS = (
        ("overview", "Overview"),
        ("regime", "Regime"),
        ("patterns", "Patterns"),
        ("signals", "Signals"),
        ("calendar", "Calendar"),
        ("news", "News"),
        ("fred", "FRED"),
        ("pressure", "Pressure"),
    )

    def __init__(self, refresh_interval: int = 60):
        super().__init__()
        self.refresh_interval = max(10, int(refresh_interval))
        self._paused = False
        self._cycle = 0
        self._countdown = self.refresh_interval
        self._last_ctx: Optional[dict] = None
        self._fetching = False
        self._thread: Optional[QThread] = None
        self._worker: Optional[_EngineFetchWorker] = None

        self.setWindowTitle("News Engine v5.0 — Research Desk")
        self.resize(1360, 920)
        self.setMinimumSize(1020, 680)

        self._apply_dark_chrome()
        self._build_ui()
        self._bind_shortcuts()

        # Start background news listener (same as tk GUI)
        try:
            from news_engine import start_news_listener
            start_news_listener()
        except Exception as exc:
            self._set_status(f"Listener start failed: {exc}")

        # Timers
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start(1000)

        # Initial fetch shortly after show (listener warm-up)
        QTimer.singleShot(800, lambda: self._fetch_engine(force=False))

    # ------------------------------------------------------------------ theme
    def _apply_dark_chrome(self) -> None:
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget#CentralRoot {{
                background-color: {DeskTheme.BG};
                color: {DeskTheme.TEXT};
                font-family: "Segoe UI", "Inter", sans-serif;
            }}
            QFrame#ControlBar {{
                background-color: {DeskTheme.PANEL};
                border-bottom: 1px solid {DeskTheme.BORDER};
            }}
            QPushButton {{
                background-color: {DeskTheme.ACCENT_SOFT};
                color: {DeskTheme.WHITE};
                border: 1px solid {DeskTheme.ACCENT};
                border-radius: 6px;
                padding: 6px 14px;
                font-weight: 600;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {DeskTheme.ACCENT};
            }}
            QPushButton:pressed {{
                background-color: {DeskTheme.BORDER};
            }}
            QPushButton#DangerBtn {{
                border-color: {DeskTheme.CRIMSON};
                background-color: rgba(231, 76, 60, 0.18);
            }}
            QLabel#CtrlLabel {{
                color: {DeskTheme.TEXT_DIM};
                font-size: 12px;
            }}
            QLabel#LiveChip {{
                color: {DeskTheme.EMERALD};
                font-weight: 700;
                font-size: 12px;
                padding: 2px 8px;
            }}
            QSpinBox {{
                background: {DeskTheme.PANEL_ALT};
                color: {DeskTheme.TEXT};
                border: 1px solid {DeskTheme.BORDER};
                border-radius: 4px;
                padding: 3px 6px;
                min-width: 64px;
            }}
            QTabWidget::pane {{
                border: 1px solid {DeskTheme.BORDER};
                background: {DeskTheme.BG};
                top: -1px;
            }}
            QTabBar::tab {{
                background: {DeskTheme.PANEL_ALT};
                color: {DeskTheme.TEXT_DIM};
                border: 1px solid {DeskTheme.BORDER_SOFT};
                border-bottom: none;
                padding: 8px 16px;
                margin-right: 2px;
                font-weight: 600;
                font-size: 12px;
            }}
            QTabBar::tab:selected {{
                background: {DeskTheme.PANEL};
                color: {DeskTheme.WHITE};
                border-color: {DeskTheme.ACCENT};
                border-bottom: 2px solid {DeskTheme.ACCENT};
            }}
            QTabBar::tab:hover {{
                color: {DeskTheme.TEXT};
            }}
            QStatusBar {{
                background: {DeskTheme.PANEL};
                color: {DeskTheme.TEXT_DIM};
                border-top: 1px solid {DeskTheme.BORDER};
                font-size: 11px;
            }}
            """
        )
        pal = self.palette()
        pal.setColor(QPalette.Window, QColor(DeskTheme.BG))
        pal.setColor(QPalette.WindowText, QColor(DeskTheme.TEXT))
        pal.setColor(QPalette.Base, QColor(DeskTheme.PANEL_ALT))
        pal.setColor(QPalette.Text, QColor(DeskTheme.TEXT))
        pal.setColor(QPalette.Button, QColor(DeskTheme.PANEL))
        pal.setColor(QPalette.ButtonText, QColor(DeskTheme.TEXT))
        pal.setColor(QPalette.Highlight, QColor(DeskTheme.ACCENT_SOFT))
        pal.setColor(QPalette.HighlightedText, QColor(DeskTheme.WHITE))
        self.setPalette(pal)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("CentralRoot")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Control bar
        bar = QFrame()
        bar.setObjectName("ControlBar")
        bar_l = QHBoxLayout(bar)
        bar_l.setContentsMargins(12, 8, 12, 8)
        bar_l.setSpacing(10)

        self.btn_pause = QPushButton("Pause")
        self.btn_pause.clicked.connect(self._toggle_pause)
        bar_l.addWidget(self.btn_pause)

        self.btn_refresh = QPushButton("Force Refresh")
        self.btn_refresh.clicked.connect(lambda: self._fetch_engine(force=True))
        bar_l.addWidget(self.btn_refresh)

        lbl_int = QLabel("Interval")
        lbl_int.setObjectName("CtrlLabel")
        bar_l.addWidget(lbl_int)

        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(10, 300)
        self.spin_interval.setValue(self.refresh_interval)
        self.spin_interval.setSuffix(" s")
        self.spin_interval.valueChanged.connect(self._on_interval_changed)
        bar_l.addWidget(self.spin_interval)

        bar_l.addSpacing(12)
        self.listener_chip = QLabel("Listener: …")
        self.listener_chip.setObjectName("LiveChip")
        bar_l.addWidget(self.listener_chip)

        bar_l.addStretch(1)

        self.cycle_lbl = QLabel("Cycle #0")
        self.cycle_lbl.setObjectName("CtrlLabel")
        bar_l.addWidget(self.cycle_lbl)

        root.addWidget(bar)

        # Tabs
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        # Tab 0 — Research Desk (primary)
        self.desk = ResearchDeskView(auto_refresh_seconds=0)
        self.tabs.addTab(self.desk, "Research Desk")

        # Legacy tabs
        self.legacy: dict[str, LegacyTextTab] = {}
        for tab_id, title in self.LEGACY_TABS:
            w = LegacyTextTab(title)
            self.legacy[tab_id] = w
            self.tabs.addTab(w, title)

        root.addWidget(self.tabs, stretch=1)

        # Status bar
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self._set_status("Starting…")

    def _bind_shortcuts(self) -> None:
        act_pause = QAction(self)
        act_pause.setShortcut(QKeySequence("Ctrl+P"))
        act_pause.triggered.connect(self._toggle_pause)
        self.addAction(act_pause)

        act_ref = QAction(self)
        act_ref.setShortcut(QKeySequence("Ctrl+R"))
        act_ref.triggered.connect(lambda: self._fetch_engine(force=True))
        self.addAction(act_ref)

        act_quit = QAction(self)
        act_quit.setShortcut(QKeySequence("Esc"))
        act_quit.triggered.connect(self.close)
        self.addAction(act_quit)

    # ------------------------------------------------------------------ engine
    def _fetch_engine(self, force: bool = False) -> None:
        if self._fetching:
            return
        self._fetching = True
        self._set_status("Fetching engine context…")

        thread = QThread(self)
        worker = _EngineFetchWorker(force=force)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.finished.connect(self._on_engine_ready)
        worker.failed.connect(self._on_engine_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_fetch_flag)

        self._thread = thread
        self._worker = worker
        thread.start()

    @Slot()
    def _clear_fetch_flag(self) -> None:
        self._fetching = False
        self._thread = None
        self._worker = None

    @Slot(dict)
    def _on_engine_ready(self, ctx: dict) -> None:
        self._last_ctx = ctx
        self._cycle += 1
        self._countdown = self.refresh_interval
        self.cycle_lbl.setText(f"Cycle #{self._cycle}")

        # Research Desk: prefer live ctx fields (non-blocking internal worker)
        try:
            self.desk.refresh(ctx=ctx)
        except Exception as exc:
            self._set_status(f"Desk refresh error: {exc}")

        # Legacy tabs
        try:
            self._render_legacy(ctx)
        except Exception as exc:
            self._set_status(f"Legacy render error: {exc}")

        self._update_listener_chip()
        self._set_status(
            f"Cycle #{self._cycle} ready · next in {self._countdown}s"
        )

    @Slot(str)
    def _on_engine_failed(self, message: str) -> None:
        self._set_status(f"Engine fetch failed: {message}")
        # Still try desk DB-only so Research Desk remains useful offline
        try:
            self.desk.refresh(ctx=None)
        except Exception:
            pass

    # ------------------------------------------------------------------ legacy render
    def _render_legacy(self, ctx: dict) -> None:
        cr = ctx.get("context_report") or {}
        pressure = ctx.get("pressure_scores") or {}
        macro = ctx.get("macro_state") or {}
        theses = ctx.get("instrument_theses") or []
        ff = ctx.get("ff_analyzed") or []
        rss = ctx.get("rss_analyzed") or []
        fred = ctx.get("fred_narratives") or []
        summary = ctx.get("summary") or ""

        # Overview
        regime = cr.get("regime") or {}
        if not regime and macro:
            regime = {
                "regime": macro.get("regime"),
                "confidence": macro.get("confidence"),
            }
        ov_sum = (
            f"Regime {regime.get('regime', '—')} "
            f"({self._pct(regime.get('confidence'))})  ·  "
            f"FF {len(ff)}  RSS {len(rss)}  FRED {len(fred)}"
        )
        ov_body = summary if summary else self._fmt_overview(ctx, cr, pressure, macro, theses)
        self.legacy["overview"].set_content(ov_sum, ov_body)

        # Regime
        rg_lines = ["MACRO / FLOW REGIME", "=" * 72, ""]
        if macro:
            rg_lines += [
                f"Textbook regime : {macro.get('regime')}",
                f"Confidence      : {self._pct(macro.get('confidence'))}",
                f"Growth          : {macro.get('growth')}",
                f"Inflation       : {macro.get('inflation')}",
                f"Policy          : {macro.get('policy')}",
                f"Liquidity       : {macro.get('liquidity')}",
                f"Risk            : {macro.get('risk')}",
                "",
                "Lesson:",
                str(macro.get("lesson") or "—"),
                "",
            ]
        flow = cr.get("regime") or {}
        if flow:
            rg_lines += [
                "News-flow regime:",
                f"  {flow.get('regime')}  conf={self._pct(flow.get('confidence'))}",
                f"  {flow.get('description') or ''}",
            ]
        self.legacy["regime"].set_content(
            f"{macro.get('regime') or flow.get('regime') or '—'} "
            f"({self._pct(macro.get('confidence') or flow.get('confidence'))})",
            "\n".join(rg_lines),
        )

        # Patterns
        patterns = cr.get("patterns") or []
        p_lines = [f"Detected patterns: {len(patterns)}", ""]
        for p in patterns[:40]:
            if isinstance(p, dict):
                p_lines.append(
                    f"• {p.get('name') or p.get('id') or p}: "
                    f"{p.get('description') or ''}"
                )
            else:
                p_lines.append(f"• {p}")
        self.legacy["patterns"].set_content(
            f"{len(patterns)} pattern(s)",
            "\n".join(p_lines) if patterns else "No patterns in current window.",
        )

        # Signals / convergence
        conv = cr.get("signal_convergence") or cr.get("convergence") or {}
        s_lines = ["SIGNAL CONVERGENCE", "=" * 72, ""]
        if isinstance(conv, dict):
            for k, v in conv.items():
                s_lines.append(f"{k}: {v}")
        else:
            s_lines.append(str(conv))
        s_lines.append("")
        s_lines.append("Instrument theses:")
        for t in theses:
            s_lines.append(
                f"  {t.get('symbol')}: {t.get('current_bias')} — "
                f"{str(t.get('active_thesis') or '')[:120]}"
            )
        self.legacy["signals"].set_content(
            f"{len(theses)} thesis row(s)",
            "\n".join(s_lines),
        )

        # Calendar
        forward = cr.get("forward_calendar") or []
        c_lines = ["FORWARD CALENDAR (next catalysts)", "=" * 72, ""]
        for ev in forward[:50]:
            if isinstance(ev, dict):
                c_lines.append(
                    f"[{ev.get('datetime') or ev.get('time') or '?'}] "
                    f"{ev.get('title') or ev}  "
                    f"impact={ev.get('impact')}  {ev.get('currency') or ''}"
                )
            else:
                c_lines.append(f"• {ev}")
        # Also list recent high-impact FF
        c_lines += ["", "Recent high-impact releases:", ""]
        for ev in sorted(
            [e for e in ff if int(e.get("impact") or 0) >= 2],
            key=lambda x: str(x.get("datetime") or ""),
            reverse=True,
        )[:30]:
            c_lines.append(
                f"[{ev.get('datetime')}] {ev.get('title')}  "
                f"A={ev.get('actual')} F={ev.get('forecast')}  "
                f"{ev.get('beat_miss') or ''}"
            )
        self.legacy["calendar"].set_content(
            f"{len(forward)} forward · {len(ff)} analyzed FF",
            "\n".join(c_lines) if c_lines else "No calendar items.",
        )

        # News (RSS)
        n_lines = [f"RSS analyzed: {len(rss)}", ""]
        for item in rss[:60]:
            n_lines.append(
                f"[{item.get('datetime')}] {item.get('source')}: "
                f"{item.get('title')}"
            )
            tone = item.get("general_tone") or item.get("sentiment")
            if tone:
                n_lines.append(f"    tone={tone}")
        self.legacy["news"].set_content(
            f"{len(rss)} item(s)",
            "\n".join(n_lines) if rss else "No recent RSS items.",
        )

        # FRED
        f_lines = [f"FRED narratives: {len(fred)}", ""]
        for row in fred[:80]:
            if isinstance(row, dict):
                f_lines.append(
                    f"• {row.get('series_id') or row.get('name') or '?'}: "
                    f"{row.get('narrative') or row.get('summary') or row}"
                )
            else:
                f_lines.append(f"• {row}")
        self.legacy["fred"].set_content(
            f"{len(fred)} series narrative(s)",
            "\n".join(f_lines) if fred else "No FRED narratives.",
        )

        # Pressure
        pr_lines = [
            "AGGREGATE PRESSURE SCORES",
            "(positive = net bullish · negative = net bearish)",
            "=" * 72,
            "",
        ]
        for inst, score in sorted(
            pressure.items(),
            key=lambda x: -abs(float(x[1] or 0)),
        ):
            try:
                s = float(score)
            except (TypeError, ValueError):
                s = 0.0
            bar = ("+" * int(min(20, abs(s)))) if s else ""
            sign = "+" if s > 0 else ""
            pr_lines.append(f"  {inst:<8}  {sign}{s:>7.2f}  {bar}")
        self.legacy["pressure"].set_content(
            " · ".join(
                f"{k}={float(v):+.1f}" for k, v in list(pressure.items())[:4]
            ) if pressure else "No pressure scores",
            "\n".join(pr_lines),
        )

    @staticmethod
    def _pct(val: Any) -> str:
        try:
            v = float(val)
            if v <= 1.0:
                v *= 100.0
            return f"{v:.0f}%"
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def _fmt_overview(ctx, cr, pressure, macro, theses) -> str:
        lines = [
            "NEWS ENGINE — OVERVIEW",
            "=" * 72,
            f"Generated: {ctx.get('generated_at')}",
            f"Listener : {ctx.get('listener_alive')}",
            "",
            f"Macro regime: {macro.get('regime')} ({macro.get('confidence')})",
            "",
            "Theses:",
        ]
        for t in theses:
            lines.append(f"  {t.get('symbol')}: {t.get('current_bias')}")
        lines += ["", "Pressure:"]
        for k, v in (pressure or {}).items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ controls
    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.btn_pause.setText("Resume" if self._paused else "Pause")
        if not self._paused:
            self._countdown = self.refresh_interval
        self._set_status("PAUSED" if self._paused else f"Resumed · next in {self._countdown}s")

    def _on_interval_changed(self, value: int) -> None:
        self.refresh_interval = int(value)
        self._countdown = self.refresh_interval

    def _on_tick(self) -> None:
        self._update_listener_chip()
        if self._paused:
            return
        if self._countdown > 0:
            self._countdown -= 1
        self.cycle_lbl.setText(
            f"Cycle #{self._cycle}  ·  next {self._countdown}s"
        )
        if self._countdown <= 0 and not self._fetching:
            self._countdown = self.refresh_interval
            self._fetch_engine(force=False)

    def _update_listener_chip(self) -> None:
        try:
            from news_engine import listener_status
            st = listener_status()
            alive = bool(st.get("alive"))
            totals = st.get("store_totals") or {}
            self.listener_chip.setText(
                f"Listener: {'LIVE' if alive else 'OFFLINE'}  "
                f"FF {totals.get('ff_events', 0)}  "
                f"RSS {totals.get('rss_items', 0)}  "
                f"FRED {totals.get('fred_series', 0)}"
            )
            self.listener_chip.setStyleSheet(
                f"color: {DeskTheme.EMERALD if alive else DeskTheme.CRIMSON};"
                f"font-weight:700; font-size:12px; padding:2px 8px;"
            )
        except Exception:
            self.listener_chip.setText("Listener: ?")

    def _set_status(self, text: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        self.status.showMessage(f"  {text}  ·  {now}")

    # ------------------------------------------------------------------ lifecycle
    def closeEvent(self, event) -> None:  # noqa: N802
        self._tick_timer.stop()
        try:
            from news_engine import stop_news_listener
            stop_news_listener()
            time.sleep(0.3)
        except Exception:
            pass
        event.accept()


# =============================================================================
# ENTRY
# =============================================================================

def run_qt_dashboard(refresh_interval: int = 60) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("News Engine v5")
    win = NewsEngineQtDashboard(refresh_interval=refresh_interval)
    win.show()
    return app.exec()


if __name__ == "__main__":
    interval = 60
    if len(sys.argv) > 1:
        try:
            interval = int(sys.argv[1])
        except ValueError:
            pass
    raise SystemExit(run_qt_dashboard(interval))
