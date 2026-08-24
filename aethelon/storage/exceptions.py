# -*- coding: utf-8 -*-
"""Domain exceptions for the Stage D storage plane."""

from __future__ import annotations

from typing import Any, Optional


class StorageError(Exception):
    """Base class for storage-layer failures."""

    def __init__(self, message: str, *, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}
