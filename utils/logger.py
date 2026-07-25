# -*- coding: utf-8 -*-
"""
utils/logger.py — Centralized singleton logger (Stage A)
========================================================
Primary log path:
  Windows : %APPDATA%\\Aethelon\\logs\\app.log
  Other   : ~/.aethelon/logs/app.log

Handlers:
  • Console (stdout) — real-time debugging
  • TimedRotatingFileHandler — midnight rotation, 7-day retention

Format (ISO 8601 UTC):
  [YYYY-MM-DDTHH:MM:SSZ] [LEVEL] [MODULE]: MESSAGE

Usage:
    from utils.logger import get_logger
    log = get_logger(__name__)
    log.info("engine started")
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "Aethelon"
LOG_FILENAME = "app.log"
LOGGER_NAME = "aethelon"  # root app logger name (singleton)

_lock = threading.RLock()
_configured = False
_log_file: Optional[Path] = None


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def get_logs_dir() -> Path:
    """
    Resolve the durable logs directory.

    Windows → %APPDATA%\\Aethelon\\logs
    Other   → ~/.aethelon/logs
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if not base:
            base = str(Path.home() / "AppData" / "Roaming")
        root = Path(base) / APP_NAME
    else:
        root = Path.home() / f".{APP_NAME.lower()}"
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs


def log_path() -> Path:
    """Absolute path to the active app.log file."""
    global _log_file
    if _log_file is None:
        _log_file = get_logs_dir() / LOG_FILENAME
    return _log_file


# ---------------------------------------------------------------------------
# Formatter — ISO 8601 UTC
# ---------------------------------------------------------------------------

class UtcIsoFormatter(logging.Formatter):
    """
    Emit: [YYYY-MM-DDTHH:MM:SSZ] [LEVEL] [MODULE]: MESSAGE
    """

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        # Always Zulu / UTC, second precision
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def format(self, record: logging.LogRecord) -> str:
        record.asctime = self.formatTime(record)
        level = record.levelname
        module = record.name
        # Use getMessage() so %-style and extra args work
        msg = record.getMessage()
        base = f"[{record.asctime}] [{level}] [{module}]: {msg}"
        if record.exc_info:
            # Append traceback without breaking the primary line contract
            exc_text = self.formatException(record.exc_info)
            return f"{base}\n{exc_text}"
        return base


_FORMATTER = UtcIsoFormatter()


# ---------------------------------------------------------------------------
# Singleton setup
# ---------------------------------------------------------------------------

def setup_logging(
    level: int | str = logging.INFO,
    *,
    force: bool = False,
) -> logging.Logger:
    """
    Configure the process-wide Aethelon logger once (idempotent).

    Parameters
    ----------
    level : int | str
        Root level for the app logger (DEBUG/INFO/WARNING/ERROR/CRITICAL).
    force : bool
        If True, tear down existing handlers and reconfigure (tests).
    """
    global _configured, _log_file

    with _lock:
        logger = logging.getLogger(LOGGER_NAME)

        if isinstance(level, str):
            level = getattr(logging, level.upper(), logging.INFO)

        if _configured and not force:
            logger.setLevel(level)
            return logger

        if force:
            for h in list(logger.handlers):
                logger.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
            _configured = False

        logger.setLevel(level)
        logger.propagate = False  # avoid double-print via root

        # --- Console (stdout) ---
        console = logging.StreamHandler(stream=sys.stdout)
        console.setLevel(level)
        console.setFormatter(_FORMATTER)
        logger.addHandler(console)

        # --- Timed rotating file (midnight, 7 backups) ---
        path = log_path()
        file_handler = TimedRotatingFileHandler(
            filename=str(path),
            when="midnight",
            interval=1,
            backupCount=7,
            encoding="utf-8",
            utc=True,  # rotate on UTC midnight to match ISO Z timestamps
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(_FORMATTER)
        # Suffix for rotated files: app.log.YYYY-MM-DD
        file_handler.suffix = "%Y-%m-%d"
        logger.addHandler(file_handler)

        _configured = True
        _log_file = path
        return logger


def get_logger(name: Optional[str] = None, level: int | str = logging.INFO) -> logging.Logger:
    """
    Return a child logger under the Aethelon singleton.

    Ensures setup_logging() has run. Pass ``__name__`` from call sites
    so records show the real module in [MODULE].
    """
    setup_logging(level=level)
    if not name or name == LOGGER_NAME:
        return logging.getLogger(LOGGER_NAME)
    # Child loggers inherit handlers via hierarchy if propagate=True on child
    # Parent has propagate=False; children should propagate TO parent.
    child = logging.getLogger(f"{LOGGER_NAME}.{name}" if not name.startswith(LOGGER_NAME) else name)
    child.setLevel(logging.getLogger(LOGGER_NAME).level)
    child.propagate = True
    return child


def is_configured() -> bool:
    return _configured


# ---------------------------------------------------------------------------
# Self-test / Stage A verification entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log = get_logger("utils.logger")
    path = log_path()
    log.debug("logger self-test DEBUG probe")
    log.info("Stage A central logger online — path=%s", path)
    log.warning("logger self-test WARNING probe")
    print(f"LOG_FILE={path}")
    print(f"EXISTS={path.is_file()}")
    if path.is_file():
        tail = path.read_text(encoding="utf-8").strip().splitlines()[-3:]
        print("TAIL:")
        for line in tail:
            print(" ", line)
