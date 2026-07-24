# -*- coding: utf-8 -*-
"""
research_desk_view.py — Research Desk UI View (Sub-step 5.2)
=============================================================
Premium dark-slate PySide6 dashboard that consumes:

    research_desk_data.build_research_desk()

Four sections:
  1. Macro State Hero Banner  (regime, dials, lesson)
  2. Instrument Theses Grid   (XAUUSD / EURUSD / GBPUSD / USDCHF)
  3. Regime History           (distribution bars + summary)
  4. Event Study Ledger       (surprise table + sample reactions)

Data is always fetched off the UI thread via QThread workers so the
window stays responsive. Safe when sections are missing/empty.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import (
    Qt,
    QThread,
    Signal,
    QObject,
    QTimer,
    Slot,
)
from PySide6.QtGui import QFont, QColor, QPalette
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QFrame,
    QScrollArea,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QSizePolicy,
    QAbstractItemView,
    QGraphicsDropShadowEffect,
)


# =============================================================================
# DESIGN SYSTEM — dark slate / graphite / charcoal
# =============================================================================

class DeskTheme:
    BG          = "#0B0F14"   # app charcoal
    PANEL       = "#121820"   # elevated panel
    PANEL_ALT   = "#161D27"   # card surface
    PANEL_HOVER = "#1A2330"
    BORDER      = "#2A3544"   # sharp graphite border
    BORDER_SOFT = "#1E2836"
    TEXT        = "#E6EBF2"
    TEXT_DIM    = "#8B97A8"
    TEXT_MUTED  = "#5C6B7A"
    ACCENT      = "#5B9FD4"   # cool steel blue
    ACCENT_SOFT = "#2E4A66"
    GOLD        = "#D4A84B"
    EMERALD     = "#2ECC71"   # bullish
    CRIMSON     = "#E74C3C"   # bearish
    AMBER       = "#F0B429"   # neutral / caution
    LESSON_BG   = "#0F1A24"
    LESSON_BORDER = "#2A4A62"
    TABLE_ALT   = "#0F141C"
    WHITE       = "#FFFFFF"

    @staticmethod
    def qss() -> str:
        T = DeskTheme
        return f"""
        QWidget#ResearchDeskRoot {{
            background-color: {T.BG};
            color: {T.TEXT};
            font-family: "Segoe UI", "Inter", "Helvetica Neue", sans-serif;
            font-size: 13px;
        }}
        QScrollArea#DeskScroll {{
            background: transparent;
            border: none;
        }}
        QWidget#DeskScrollInner {{
            background-color: {T.BG};
        }}
        QFrame#SectionPanel {{
            background-color: {T.PANEL};
            border: 1px solid {T.BORDER};
            border-radius: 10px;
        }}
        QFrame#HeroPanel {{
            background-color: {T.PANEL};
            border: 1px solid {T.BORDER};
            border-radius: 12px;
        }}
        QFrame#MetricCard {{
            background-color: {T.PANEL_ALT};
            border: 1px solid {T.BORDER_SOFT};
            border-radius: 8px;
        }}
        QFrame#ThesisCard {{
            background-color: {T.PANEL_ALT};
            border: 1px solid {T.BORDER};
            border-radius: 10px;
        }}
        QFrame#LessonBox {{
            background-color: {T.LESSON_BG};
            border: 1px solid {T.LESSON_BORDER};
            border-radius: 8px;
        }}
        QLabel#SectionTitle {{
            color: {T.ACCENT};
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1.4px;
            text-transform: uppercase;
        }}
        QLabel#HeroRegime {{
            color: {T.WHITE};
            font-size: 28px;
            font-weight: 700;
            letter-spacing: 0.5px;
        }}
        QLabel#HeroMeta {{
            color: {T.TEXT_DIM};
            font-size: 12px;
        }}
        QLabel#BadgeBull {{
            background-color: rgba(46, 204, 113, 0.15);
            color: {T.EMERALD};
            border: 1px solid {T.EMERALD};
            border-radius: 4px;
            padding: 3px 10px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.8px;
        }}
        QLabel#BadgeBear {{
            background-color: rgba(231, 76, 60, 0.15);
            color: {T.CRIMSON};
            border: 1px solid {T.CRIMSON};
            border-radius: 4px;
            padding: 3px 10px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.8px;
        }}
        QLabel#BadgeNeutral {{
            background-color: rgba(240, 180, 41, 0.12);
            color: {T.AMBER};
            border: 1px solid {T.AMBER};
            border-radius: 4px;
            padding: 3px 10px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.8px;
        }}
        QLabel#BadgeConf {{
            background-color: {T.ACCENT_SOFT};
            color: {T.WHITE};
            border: 1px solid {T.ACCENT};
            border-radius: 14px;
            padding: 4px 14px;
            font-size: 13px;
            font-weight: 700;
        }}
        QLabel#CardTitle {{
            color: {T.WHITE};
            font-size: 15px;
            font-weight: 700;
        }}
        QLabel#CardBody {{
            color: {T.TEXT};
            font-size: 12px;
        }}
        QLabel#CardMuted {{
            color: {T.TEXT_DIM};
            font-size: 11px;
        }}
        QLabel#DialName {{
            color: {T.TEXT_DIM};
            font-size: 10px;
            font-weight: 600;
            letter-spacing: 0.6px;
        }}
        QLabel#DialValue {{
            color: {T.WHITE};
            font-size: 13px;
            font-weight: 600;
        }}
        QLabel#EmptyState {{
            color: {T.TEXT_MUTED};
            font-size: 12px;
            font-style: italic;
        }}
        QLabel#StatusChip {{
            color: {T.TEXT_DIM};
            font-size: 11px;
            padding: 2px 0;
        }}
        QProgressBar#DistBar {{
            background-color: {T.BORDER_SOFT};
            border: 1px solid {T.BORDER};
            border-radius: 4px;
            text-align: center;
            color: {T.WHITE};
            font-size: 10px;
            font-weight: 600;
            min-height: 16px;
            max-height: 18px;
        }}
        QProgressBar#DistBar::chunk {{
            background-color: {T.ACCENT};
            border-radius: 3px;
        }}
        QTableWidget#SurpriseTable {{
            background-color: {T.PANEL_ALT};
            alternate-background-color: {T.TABLE_ALT};
            color: {T.TEXT};
            border: 1px solid {T.BORDER_SOFT};
            border-radius: 6px;
            gridline-color: {T.BORDER_SOFT};
            selection-background-color: {T.ACCENT_SOFT};
            selection-color: {T.WHITE};
            font-size: 11px;
        }}
        QTableWidget#SurpriseTable::item {{
            padding: 4px 6px;
        }}
        QHeaderView::section {{
            background-color: {T.PANEL};
            color: {T.ACCENT};
            border: none;
            border-bottom: 1px solid {T.BORDER};
            border-right: 1px solid {T.BORDER_SOFT};
            padding: 6px 8px;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.6px;
        }}
        QScrollBar:vertical {{
            background: {T.BG};
            width: 10px;
            margin: 0;
        }}
        QScrollBar::handle:vertical {{
            background: {T.BORDER};
            border-radius: 4px;
            min-height: 30px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0;
        }}
        QScrollBar:horizontal {{
            background: {T.BG};
            height: 10px;
        }}
        QScrollBar::handle:horizontal {{
            background: {T.BORDER};
            border-radius: 4px;
            min-width: 30px;
        }}
        """


# =============================================================================
# ASYNC DATA WORKER
# =============================================================================

class _DeskFetchWorker(QObject):
    """Runs build_research_desk() off the UI thread."""

    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, db_path: Optional[str] = None, ctx: Optional[dict] = None):
        super().__init__()
        self._db_path = db_path
        self._ctx = ctx

    @Slot()
    def run(self) -> None:
        try:
            from research_desk_data import build_research_desk
            desk = build_research_desk(db_path=self._db_path, ctx=self._ctx)
            self.finished.emit(desk if isinstance(desk, dict) else {})
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# =============================================================================
# SMALL UI PRIMITIVES
# =============================================================================

def _apply_elevation(widget: QWidget, blur: int = 24, y_offset: int = 4) -> None:
    """Subtle panel elevation via drop shadow."""
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(blur)
    shadow.setOffset(0, y_offset)
    shadow.setColor(QColor(0, 0, 0, 90))
    widget.setGraphicsEffect(shadow)


def _make_badge(text: str, kind: str = "neutral") -> QLabel:
    lbl = QLabel(text.upper() if text else "—")
    lbl.setAlignment(Qt.AlignCenter)
    if kind == "bull":
        lbl.setObjectName("BadgeBull")
    elif kind == "bear":
        lbl.setObjectName("BadgeBear")
    else:
        lbl.setObjectName("BadgeNeutral")
    lbl.setFixedHeight(24)
    return lbl


def _bias_kind(bias: Optional[str]) -> str:
    b = (bias or "").upper()
    if b in ("BULLISH", "BULL"):
        return "bull"
    if b in ("BEARISH", "BEAR"):
        return "bear"
    return "neutral"


def _fmt_pct(conf: Any) -> str:
    try:
        v = float(conf)
        if v <= 1.0:
            v *= 100.0
        return f"{v:.0f}%"
    except (TypeError, ValueError):
        return "—"


def _safe_str(val: Any, fallback: str = "—") -> str:
    if val is None:
        return fallback
    s = str(val).strip()
    return s if s else fallback


def _truncate(text: Any, n: int = 280) -> str:
    s = _safe_str(text, "")
    if not s:
        return "—"
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


# =============================================================================
# SECTION WIDGETS
# =============================================================================

class MacroHeroBanner(QFrame):
    """Section 1 — Regime hero + dials + lesson callout."""

    DIAL_ORDER = (
        ("growth", "GROWTH"),
        ("inflation", "INFLATION"),
        ("policy", "POLICY"),
        ("liquidity", "LIQUIDITY"),
        ("risk", "VOLATILITY"),  # risk dial surfaces as volatility in the UI
    )

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("HeroPanel")
        _apply_elevation(self, blur=28, y_offset=3)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        title = QLabel("MACRO STATE")
        title.setObjectName("SectionTitle")
        root.addWidget(title)

        # Top row: regime + confidence
        top = QHBoxLayout()
        top.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(4)
        self.regime_lbl = QLabel("—")
        self.regime_lbl.setObjectName("HeroRegime")
        self.meta_lbl = QLabel("Awaiting macro snapshot…")
        self.meta_lbl.setObjectName("HeroMeta")
        left.addWidget(self.regime_lbl)
        left.addWidget(self.meta_lbl)
        top.addLayout(left, stretch=1)

        self.conf_badge = QLabel("—")
        self.conf_badge.setObjectName("BadgeConf")
        self.conf_badge.setAlignment(Qt.AlignCenter)
        self.conf_badge.setMinimumWidth(88)
        top.addWidget(self.conf_badge, alignment=Qt.AlignTop)
        root.addLayout(top)

        # Dials grid
        dials_wrap = QFrame()
        dials_wrap.setObjectName("MetricCard")
        dials_layout = QHBoxLayout(dials_wrap)
        dials_layout.setContentsMargins(12, 10, 12, 10)
        dials_layout.setSpacing(8)

        self._dial_value_labels: dict[str, QLabel] = {}
        for key, label in self.DIAL_ORDER:
            cell = QVBoxLayout()
            cell.setSpacing(3)
            name = QLabel(label)
            name.setObjectName("DialName")
            name.setAlignment(Qt.AlignCenter)
            val = QLabel("—")
            val.setObjectName("DialValue")
            val.setAlignment(Qt.AlignCenter)
            val.setWordWrap(True)
            cell.addWidget(name)
            cell.addWidget(val)
            dials_layout.addLayout(cell, stretch=1)
            self._dial_value_labels[key] = val

        root.addWidget(dials_wrap)

        # Lesson callout
        lesson_frame = QFrame()
        lesson_frame.setObjectName("LessonBox")
        lesson_l = QVBoxLayout(lesson_frame)
        lesson_l.setContentsMargins(14, 12, 14, 12)
        lesson_l.setSpacing(6)
        lesson_title = QLabel("LESSON LEARNED")
        lesson_title.setObjectName("SectionTitle")
        self.lesson_body = QLabel("No lesson available yet.")
        self.lesson_body.setObjectName("CardBody")
        self.lesson_body.setWordWrap(True)
        self.lesson_body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lesson_l.addWidget(lesson_title)
        lesson_l.addWidget(self.lesson_body)
        root.addWidget(lesson_frame)

        self._empty = QLabel("")
        self._empty.setObjectName("EmptyState")
        self._empty.setWordWrap(True)
        self._empty.hide()
        root.addWidget(self._empty)

    def update_from(self, macro: dict) -> None:
        if not isinstance(macro, dict) or not macro.get("ok"):
            err = macro.get("error") if isinstance(macro, dict) else "section error"
            self.regime_lbl.setText("UNAVAILABLE")
            self.meta_lbl.setText(_safe_str(err, "Macro section failed"))
            self.conf_badge.setText("—")
            self.lesson_body.setText("—")
            for lbl in self._dial_value_labels.values():
                lbl.setText("—")
            return

        if not macro.get("available"):
            self.regime_lbl.setText("NO DATA")
            self.meta_lbl.setText(
                _safe_str(macro.get("message"), "Run the engine or backfill_macro_history.py")
            )
            self.conf_badge.setText("—")
            self.lesson_body.setText("No MacroState in database yet.")
            for lbl in self._dial_value_labels.values():
                lbl.setText("—")
            return

        regime = _safe_str(macro.get("regime"), "UNKNOWN")
        conf_txt = _fmt_pct(macro.get("confidence"))
        self.regime_lbl.setText(regime)
        self.conf_badge.setText(conf_txt)

        as_of = _safe_str(macro.get("as_of"))
        rules = _safe_str(macro.get("rules_version"), "")
        summary = _safe_str(macro.get("summary_line"), "")
        meta_bits = [f"As of {as_of}"]
        if rules and rules != "—":
            meta_bits.append(f"rules {rules}")
        if summary and summary != "—":
            meta_bits.append(summary[:90])
        self.meta_lbl.setText("  ·  ".join(meta_bits))

        dials = macro.get("dials") or {}
        for key, lbl in self._dial_value_labels.items():
            # UI maps "risk" → Volatility label
            lbl.setText(_safe_str(dials.get(key), "—").replace("_", " ").title())

        lesson = macro.get("lesson")
        if lesson:
            self.lesson_body.setText(str(lesson).strip())
        else:
            self.lesson_body.setText("No lesson text on this snapshot.")


class InstrumentThesesGrid(QFrame):
    """Section 2 — 2×2 thesis cards for the four tracked symbols."""

    SYMBOLS = ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF")

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("SectionPanel")
        _apply_elevation(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("INSTRUMENT THESES")
        title.setObjectName("SectionTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.count_lbl = QLabel("")
        self.count_lbl.setObjectName("StatusChip")
        header.addWidget(self.count_lbl)
        root.addLayout(header)

        grid = QGridLayout()
        grid.setSpacing(12)
        self._cards: dict[str, dict[str, QLabel]] = {}

        for i, sym in enumerate(self.SYMBOLS):
            card, refs = self._build_card(sym)
            grid.addWidget(card, i // 2, i % 2)
            self._cards[sym] = refs

        root.addLayout(grid)

    def _build_card(self, symbol: str) -> tuple[QFrame, dict[str, QLabel]]:
        card = QFrame()
        card.setObjectName("ThesisCard")
        card.setMinimumHeight(168)
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        top = QHBoxLayout()
        sym_lbl = QLabel(symbol)
        sym_lbl.setObjectName("CardTitle")
        top.addWidget(sym_lbl)
        top.addStretch(1)
        badge = _make_badge("—", "neutral")
        top.addWidget(badge)
        lay.addLayout(top)

        thesis = QLabel("Loading thesis…")
        thesis.setObjectName("CardBody")
        thesis.setWordWrap(True)
        thesis.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(thesis)

        inv_title = QLabel("INVALIDATION")
        inv_title.setObjectName("DialName")
        lay.addWidget(inv_title)

        inv = QLabel("—")
        inv.setObjectName("CardMuted")
        inv.setWordWrap(True)
        inv.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(inv)
        lay.addStretch(1)

        return card, {"badge": badge, "thesis": thesis, "invalidation": inv}

    def update_from(self, block: dict) -> None:
        if not isinstance(block, dict) or not block.get("ok"):
            err = block.get("error") if isinstance(block, dict) else "error"
            self.count_lbl.setText(_safe_str(err))
            for refs in self._cards.values():
                refs["badge"].setText("—")
                refs["badge"].setObjectName("BadgeNeutral")
                refs["badge"].style().unpolish(refs["badge"])
                refs["badge"].style().polish(refs["badge"])
                refs["thesis"].setText("Thesis unavailable.")
                refs["invalidation"].setText("—")
            return

        by_symbol = block.get("by_symbol") or {}
        theses_list = block.get("theses") or []
        # Prefer ordered list for availability flags
        ordered = {t.get("symbol"): t for t in theses_list if t.get("symbol")}

        count = int(block.get("count") or 0)
        self.count_lbl.setText(f"{count} / {len(self.SYMBOLS)} active")

        for sym in self.SYMBOLS:
            item = ordered.get(sym) or by_symbol.get(sym) or {}
            refs = self._cards[sym]
            bias = item.get("current_bias")
            kind = _bias_kind(bias)
            badge: QLabel = refs["badge"]
            badge.setText(_safe_str(bias, "N/A").upper())
            badge.setObjectName(
                "BadgeBull" if kind == "bull"
                else "BadgeBear" if kind == "bear"
                else "BadgeNeutral"
            )
            badge.style().unpolish(badge)
            badge.style().polish(badge)

            if not item.get("available", True) and not item.get("active_thesis"):
                refs["thesis"].setText("No active thesis for this symbol.")
                refs["invalidation"].setText("—")
                continue

            body = item.get("reason_short") or item.get("active_thesis") or "—"
            inv = item.get("invalidation_short") or item.get("invalidation_triggers") or "—"
            refs["thesis"].setText(_truncate(body, 320))
            refs["invalidation"].setText(_truncate(inv, 220))


class RegimeHistoryPanel(QFrame):
    """Section 3 — distribution bars + recent shift summary."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("SectionPanel")
        _apply_elevation(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("REGIME HISTORY & DISTRIBUTION")
        title.setObjectName("SectionTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.range_lbl = QLabel("")
        self.range_lbl.setObjectName("StatusChip")
        header.addWidget(self.range_lbl)
        root.addLayout(header)

        self.bars_host = QVBoxLayout()
        self.bars_host.setSpacing(6)
        root.addLayout(self.bars_host)

        self.summary_lbl = QLabel("")
        self.summary_lbl.setObjectName("CardBody")
        self.summary_lbl.setWordWrap(True)
        self.summary_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.summary_lbl)

        self._empty = QLabel("No regime history yet. Run backfill_macro_history.py.")
        self._empty.setObjectName("EmptyState")
        self._empty.setWordWrap(True)
        self._empty.hide()
        root.addWidget(self._empty)

        self._bar_rows: list[QWidget] = []

    def _clear_bars(self) -> None:
        while self.bars_host.count():
            item = self.bars_host.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._bar_rows.clear()

    def update_from(self, block: dict) -> None:
        self._clear_bars()

        if not isinstance(block, dict) or not block.get("ok"):
            self._empty.setText(
                _safe_str(
                    block.get("error") if isinstance(block, dict) else None,
                    "History section failed",
                )
            )
            self._empty.show()
            self.summary_lbl.setText("")
            self.range_lbl.setText("")
            return

        if not block.get("available"):
            self._empty.show()
            self.summary_lbl.setText("")
            self.range_lbl.setText("")
            return

        self._empty.hide()
        n = block.get("n_snapshots") or 0
        fr = _safe_str(block.get("from"))
        to = _safe_str(block.get("to"))
        self.range_lbl.setText(f"{fr} → {to}  ·  {n} snapshots")

        dist = list(block.get("distribution") or [])
        # Sort by pct desc for scannability
        dist.sort(key=lambda r: float(r.get("pct") or 0), reverse=True)

        for row in dist[:10]:
            name = _safe_str(row.get("regime"), "?")
            pct = float(row.get("pct") or 0)
            cnt = row.get("count") or 0

            row_w = QWidget()
            row_l = QHBoxLayout(row_w)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.setSpacing(8)

            name_lbl = QLabel(name)
            name_lbl.setObjectName("DialValue")
            name_lbl.setMinimumWidth(140)
            name_lbl.setMaximumWidth(180)

            bar = QProgressBar()
            bar.setObjectName("DistBar")
            bar.setRange(0, 1000)
            bar.setValue(int(max(0.0, min(100.0, pct)) * 10))
            bar.setFormat(f"{pct:.1f}%  ({cnt})")
            bar.setTextVisible(True)

            row_l.addWidget(name_lbl)
            row_l.addWidget(bar, stretch=1)
            self.bars_host.addWidget(row_w)
            self._bar_rows.append(row_w)

        # Concise shift summary from timeline (last few regime changes)
        timeline = block.get("timeline") or []
        shift_note = self._shift_summary(timeline)
        summary_text = (block.get("summary_text") or "").strip()
        if shift_note:
            body = shift_note
            if summary_text:
                # First non-empty line of engine summary as secondary context
                first = next(
                    (ln.strip() for ln in summary_text.splitlines() if ln.strip()),
                    "",
                )
                if first:
                    body = f"{shift_note}\n{first[:200]}"
            self.summary_lbl.setText(body)
        elif summary_text:
            self.summary_lbl.setText(_truncate(summary_text, 400))
        else:
            self.summary_lbl.setText("No recent regime shifts detected in the window.")

    @staticmethod
    def _shift_summary(timeline: list) -> str:
        if not timeline:
            return ""
        # timeline is oldest→newest from aggregator
        changes: list[str] = []
        prev = None
        for row in timeline:
            reg = row.get("regime")
            if reg and reg != prev:
                if prev is not None:
                    changes.append(
                        f"{_safe_str(row.get('as_of'))}: {prev} → {reg}"
                    )
                prev = reg
        if not changes:
            last = timeline[-1] if timeline else {}
            return (
                f"Stable regime {_safe_str(last.get('regime'))} "
                f"across {len(timeline)} snapshots."
            )
        recent = changes[-4:]
        return "Recent shifts:  " + "  ·  ".join(recent)


class EventStudyPanel(QFrame):
    """Section 4 — surprise ledger table + sample reaction strip."""

    COLS = (
        "Time",
        "Family",
        "Title",
        "Ccy",
        "Actual",
        "Fcst",
        "Surprise",
        "Beat/Miss",
        "Regime",
    )

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("SectionPanel")
        _apply_elevation(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("EVENT STUDY & SURPRISE LEDGER")
        title.setObjectName("SectionTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.ledger_lbl = QLabel("")
        self.ledger_lbl.setObjectName("StatusChip")
        header.addWidget(self.ledger_lbl)
        root.addLayout(header)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setObjectName("SurpriseTable")
        self.table.setHorizontalHeaderLabels(list(self.COLS))
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(True)
        self.table.setMinimumHeight(200)
        self.table.setMaximumHeight(320)
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        for col in (0, 1, 3, 4, 5, 6, 7, 8):
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        root.addWidget(self.table)

        self.study_lbl = QLabel("")
        self.study_lbl.setObjectName("CardMuted")
        self.study_lbl.setWordWrap(True)
        self.study_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.study_lbl)

        self._empty = QLabel("No surprises logged yet. High-impact calendar releases will appear here.")
        self._empty.setObjectName("EmptyState")
        self._empty.setWordWrap(True)
        self._empty.hide()
        root.addWidget(self._empty)

    def update_from(self, block: dict) -> None:
        self.table.setRowCount(0)

        if not isinstance(block, dict) or not block.get("ok"):
            self._empty.setText(
                _safe_str(
                    block.get("error") if isinstance(block, dict) else None,
                    "Event study section failed",
                )
            )
            self._empty.show()
            self.table.hide()
            self.study_lbl.setText("")
            self.ledger_lbl.setText("")
            return

        ledger = block.get("ledger_size") or 0
        recent = list(block.get("recent_surprises") or [])
        self.ledger_lbl.setText(
            f"Ledger {ledger}  ·  showing {len(recent)}"
        )

        if not recent:
            self._empty.show()
            self.table.hide()
        else:
            self._empty.hide()
            self.table.show()
            self.table.setRowCount(len(recent))
            for r, ev in enumerate(recent):
                surprise = ev.get("surprise_raw")
                try:
                    surprise_s = f"{float(surprise):+.3g}" if surprise is not None else "—"
                except (TypeError, ValueError):
                    surprise_s = _safe_str(surprise)

                vals = [
                    _safe_str(ev.get("event_time"))[:19],
                    _safe_str(ev.get("event_family")),
                    _truncate(ev.get("title"), 60),
                    _safe_str(ev.get("currency")),
                    _safe_str(ev.get("actual_raw")),
                    _safe_str(ev.get("forecast_raw")),
                    surprise_s,
                    _safe_str(ev.get("beat_miss")),
                    _safe_str(ev.get("regime")),
                ]
                for c, val in enumerate(vals):
                    item = QTableWidgetItem(val)
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    # Color beat/miss & surprise direction lightly
                    if c == 7:
                        bm = (ev.get("beat_miss") or "").lower()
                        if bm == "beat":
                            item.setForeground(QColor(DeskTheme.EMERALD))
                        elif bm == "miss":
                            item.setForeground(QColor(DeskTheme.CRIMSON))
                    if c == 6:
                        try:
                            sv = float(ev.get("surprise_raw"))
                            if sv > 0:
                                item.setForeground(QColor(DeskTheme.EMERALD))
                            elif sv < 0:
                                item.setForeground(QColor(DeskTheme.CRIMSON))
                        except (TypeError, ValueError):
                            pass
                    self.table.setItem(r, c, item)

        # Sample study reaction strip
        study = block.get("sample_study") or {}
        q = study.get("query") or {}
        n_ev = study.get("n_events") or 0
        fam = q.get("event_family") or "—"
        reg = q.get("regime") or "any"
        parts = [
            f"Sample study: {fam}  ·  n={n_ev}  ·  regime={reg}",
        ]
        reactions = study.get("symbol_reactions") or {}
        for sym in ("XAUUSD", "EURUSD", "GBPUSD", "USDCHF"):
            st = reactions.get(sym) or {}
            if not st:
                continue
            parts.append(
                f"{sym} {st.get('dominant_direction') or '—'} "
                f"(UP {st.get('pct_up', 0)}% / DOWN {st.get('pct_down', 0)}%)"
            )
        note = study.get("note")
        if note:
            parts.append(str(note)[:160])
        self.study_lbl.setText("  ·  ".join(parts) if n_ev or reactions else "No sample reaction study available yet.")


# =============================================================================
# MAIN RESEARCH DESK VIEW
# =============================================================================

class ResearchDeskView(QWidget):
    """
    Full Research Desk tab content.

    Usage:
        desk = ResearchDeskView()
        desk.refresh()                  # async DB-only fetch
        desk.refresh(ctx=news_context)  # prefer live engine fields
    """

    data_updated = Signal(dict)
    fetch_failed = Signal(str)

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        db_path: Optional[str] = None,
        auto_refresh_seconds: int = 0,
    ):
        super().__init__(parent)
        self.setObjectName("ResearchDeskRoot")
        self._db_path = db_path
        self._last_desk: Optional[dict] = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[_DeskFetchWorker] = None
        self._fetching = False

        self.setStyleSheet(DeskTheme.qss())
        self._build_ui()

        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self.refresh)
        if auto_refresh_seconds and auto_refresh_seconds > 0:
            self._auto_timer.start(int(auto_refresh_seconds) * 1000)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("DeskScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        inner = QWidget()
        inner.setObjectName("DeskScrollInner")
        self._inner_layout = QVBoxLayout(inner)
        self._inner_layout.setContentsMargins(16, 16, 16, 16)
        self._inner_layout.setSpacing(14)

        # Status strip
        status_row = QHBoxLayout()
        self.status_lbl = QLabel("Research Desk · idle")
        self.status_lbl.setObjectName("StatusChip")
        status_row.addWidget(self.status_lbl)
        status_row.addStretch(1)
        self.gen_lbl = QLabel("")
        self.gen_lbl.setObjectName("StatusChip")
        status_row.addWidget(self.gen_lbl)
        self._inner_layout.addLayout(status_row)

        self.hero = MacroHeroBanner()
        self.theses = InstrumentThesesGrid()
        self.history = RegimeHistoryPanel()
        self.events = EventStudyPanel()

        self._inner_layout.addWidget(self.hero)
        self._inner_layout.addWidget(self.theses)
        self._inner_layout.addWidget(self.history)
        self._inner_layout.addWidget(self.events)
        self._inner_layout.addStretch(1)

        scroll.setWidget(inner)
        outer.addWidget(scroll)

    # ------------------------------------------------------------------ Data
    @property
    def last_desk(self) -> Optional[dict]:
        return self._last_desk

    def set_auto_refresh(self, seconds: int) -> None:
        if seconds and seconds > 0:
            self._auto_timer.start(int(seconds) * 1000)
        else:
            self._auto_timer.stop()

    @Slot()
    def refresh(self, ctx: Optional[dict] = None) -> None:
        """Kick off a non-blocking build_research_desk() fetch."""
        if self._fetching:
            return
        self._fetching = True
        self.status_lbl.setText("Research Desk · loading…")

        thread = QThread(self)
        worker = _DeskFetchWorker(db_path=self._db_path, ctx=ctx)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.finished.connect(self._on_desk_ready)
        worker.failed.connect(self._on_desk_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_thread_finished)

        self._thread = thread
        self._worker = worker
        thread.start()

    def apply_desk(self, desk: dict) -> None:
        """Apply an already-built desk payload on the UI thread (sync)."""
        self._render(desk if isinstance(desk, dict) else {})

    @Slot(dict)
    def _on_desk_ready(self, desk: dict) -> None:
        self._fetching = False
        self._render(desk)
        self.data_updated.emit(desk)

    @Slot(str)
    def _on_desk_failed(self, message: str) -> None:
        self._fetching = False
        self.status_lbl.setText(f"Research Desk · error: {message}")
        self.fetch_failed.emit(message)

    @Slot()
    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._fetching = False

    def _render(self, desk: dict) -> None:
        self._last_desk = desk
        self.hero.update_from(desk.get("macro_state") or {})
        self.theses.update_from(desk.get("instrument_theses") or {})
        self.history.update_from(desk.get("regime_history") or {})
        self.events.update_from(desk.get("event_study") or {})

        gen = _safe_str(desk.get("generated_at"), "")
        schema = _safe_str(desk.get("schema_version"), "")
        ok = desk.get("all_ok")
        header = desk.get("header") or {}
        regime = header.get("regime") or "—"
        conf = _fmt_pct(header.get("confidence"))
        self.gen_lbl.setText(f"{schema}  ·  {gen}" if gen else schema)
        state = "ready" if ok else "partial"
        self.status_lbl.setText(
            f"Research Desk · {state}  ·  {regime} ({conf})"
        )


# =============================================================================
# STANDALONE SMOKE WINDOW
# =============================================================================

def run_research_desk_standalone(refresh_interval: int = 60) -> int:
    """Open a minimal window hosting only the Research Desk (dev / smoke test)."""
    import sys
    from PySide6.QtWidgets import QApplication, QMainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")

    # Force dark palette base
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(DeskTheme.BG))
    pal.setColor(QPalette.WindowText, QColor(DeskTheme.TEXT))
    pal.setColor(QPalette.Base, QColor(DeskTheme.PANEL_ALT))
    pal.setColor(QPalette.AlternateBase, QColor(DeskTheme.PANEL))
    pal.setColor(QPalette.Text, QColor(DeskTheme.TEXT))
    pal.setColor(QPalette.Button, QColor(DeskTheme.PANEL))
    pal.setColor(QPalette.ButtonText, QColor(DeskTheme.TEXT))
    pal.setColor(QPalette.Highlight, QColor(DeskTheme.ACCENT_SOFT))
    pal.setColor(QPalette.HighlightedText, QColor(DeskTheme.WHITE))
    app.setPalette(pal)

    win = QMainWindow()
    win.setWindowTitle("News Engine v5 — Research Desk")
    win.resize(1280, 900)
    desk = ResearchDeskView(auto_refresh_seconds=refresh_interval)
    win.setCentralWidget(desk)
    win.show()
    desk.refresh()
    return app.exec()


if __name__ == "__main__":
    import sys
    interval = 60
    if len(sys.argv) > 1:
        try:
            interval = int(sys.argv[1])
        except ValueError:
            pass
    raise SystemExit(run_research_desk_standalone(interval))
