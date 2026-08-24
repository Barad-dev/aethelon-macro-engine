# -*- coding: utf-8 -*-
"""
aethelon.storage — Stage D 3-layer storage foundation

Public surface
--------------
  L1 Hot Cache (in-memory RAM, live UI snapshots):
    • HotCache
    • LatestSnapshot / cache_key
    • DASHBOARD_NAMESPACE / DASHBOARD_NAME
  L2 Warm Store (AppData SQLite, WAL mode, recent history):
    • WarmStore
    • WarmHealth
    • default_warm_db_path
  L3 Cold Vault (long-term archive; stub in this phase):
    • ColdVault
    • default_cold_vault_dir
  Stack:
    • StorageFoundation
    • default_storage
  Shared:
    • StorageError
    • utc_now_iso_z / to_iso_z / parse_iso_utc

No GUI wiring, no news_engine rewrite, no user-data wipe.
"""

from aethelon.storage.cold import ColdVault, default_cold_vault_dir
from aethelon.storage.exceptions import StorageError
from aethelon.storage.foundation import StorageFoundation, default_storage
from aethelon.storage.hot import (
    DASHBOARD_NAME,
    DASHBOARD_NAMESPACE,
    HotCache,
    LatestSnapshot,
    cache_key,
)
from aethelon.storage.timeutil import parse_iso_utc, to_iso_z, utc_now_iso_z
from aethelon.storage.warm import WarmHealth, WarmStore, default_warm_db_path

__all__ = [
    "HotCache",
    "LatestSnapshot",
    "cache_key",
    "DASHBOARD_NAME",
    "DASHBOARD_NAMESPACE",
    "WarmStore",
    "WarmHealth",
    "ColdVault",
    "StorageFoundation",
    "StorageError",
    "default_storage",
    "default_warm_db_path",
    "default_cold_vault_dir",
    "utc_now_iso_z",
    "to_iso_z",
    "parse_iso_utc",
]
