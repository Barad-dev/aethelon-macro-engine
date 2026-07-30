# -*- coding: utf-8 -*-
"""
aethelon.ingestion — async data acquisition plane (Stage B)

Public surface:
  • AsyncHttpClient / RetryPolicy — resilient HTTP transport
  • WatermarkManager — offline catch-up high-water marks
  • RSSDriver / ForexFactoryDriver / FREDDriver — source drivers
  • Domain exceptions
"""

from aethelon.ingestion.client import AsyncHttpClient, RetryPolicy
from aethelon.ingestion.drivers import (
    BaseDriver,
    FREDDriver,
    ForexFactoryDriver,
    RSSDriver,
)
from aethelon.ingestion.exceptions import (
    IngestionError,
    IngestionNetworkError,
    RateLimitExceededError,
)
from aethelon.ingestion.watermark import WatermarkManager, default_watermark_path

__all__ = [
    # HTTP
    "AsyncHttpClient",
    "RetryPolicy",
    # Watermarks
    "WatermarkManager",
    "default_watermark_path",
    # Drivers
    "BaseDriver",
    "RSSDriver",
    "ForexFactoryDriver",
    "FREDDriver",
    # Errors
    "IngestionError",
    "IngestionNetworkError",
    "RateLimitExceededError",
]
