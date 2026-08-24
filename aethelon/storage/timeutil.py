# -*- coding: utf-8 -*-
"""
UTC timestamp helpers for the Stage D storage plane.

All timestamps are timezone-aware UTC in ISO 8601 **Z** form
(e.g. ``2026-08-23T12:00:00Z``). Naive datetimes are treated as UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = [
    "to_utc",
    "to_iso_z",
    "utc_now",
    "utc_now_iso_z",
    "parse_iso_utc",
]


def to_utc(dt: datetime) -> datetime:
    """Normalize any datetime to timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso_z(dt: datetime) -> str:
    """Format as ISO 8601 UTC with a ``Z`` suffix (second precision)."""
    utc = to_utc(dt).replace(microsecond=0)
    return utc.isoformat().replace("+00:00", "Z")


def utc_now() -> datetime:
    """Current time as timezone-aware UTC."""
    return datetime.now(timezone.utc)


def utc_now_iso_z() -> str:
    """Current UTC time as ISO 8601 Z."""
    return to_iso_z(utc_now())


def parse_iso_utc(value: str) -> datetime:
    """
    Parse an ISO 8601 string into timezone-aware UTC.

    Accepts ``...Z``, ``...+00:00``, and naive forms (treated as UTC).
    """
    s = (value or "").strip()
    if not s:
        raise ValueError("empty timestamp")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if len(s) >= 19 and s[10] == " ":
        s = s[:10] + "T" + s[11:]
    return to_utc(datetime.fromisoformat(s))
