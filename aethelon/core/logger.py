# -*- coding: utf-8 -*-
"""
aethelon.core.logger — Canonical logging facade for the Aethelon package.

Delegates to the Stage A singleton in ``utils.logger`` so AppData paths,
ISO 8601 UTC formatting, and TimedRotatingFileHandler behaviour stay unified:

  Windows : %APPDATA%\\Aethelon\\logs\\app.log
  Other   : ~/.aethelon/logs/app.log
"""

from __future__ import annotations

from utils.logger import (  # noqa: F401 — re-export surface
    APP_NAME,
    LOGGER_NAME,
    get_logger,
    is_configured,
    log_path,
    setup_logging,
)

__all__ = [
    "APP_NAME",
    "LOGGER_NAME",
    "get_logger",
    "is_configured",
    "log_path",
    "setup_logging",
]
