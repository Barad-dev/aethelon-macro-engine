# -*- coding: utf-8 -*-
"""
StorageFoundation — holds L1 / L2 / L3 without wiring GUI or news_engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from aethelon.storage.cold import ColdVault
from aethelon.storage.hot import HotCache
from aethelon.storage.timeutil import utc_now_iso_z
from aethelon.storage.warm import WarmStore

__all__ = ["StorageFoundation", "default_storage"]

PathLike = Union[str, Path]


@dataclass
class StorageFoundation:
    """
    The three Stage D layers in one place.

    L1 snapshots, L2 WAL helpers, and L3 gzip archive. No GUI feed
    and no news_engine rewrite. Callers that need a live stack can
    take this object later without changing construction.
    """

    hot: HotCache
    warm: WarmStore
    cold: ColdVault
    assembled_at: str


def default_storage(
    *,
    db_path: Optional[PathLike] = None,
    vault_dir: Optional[PathLike] = None,
    migrate: bool = True,
) -> StorageFoundation:
    """
    Build the default L1 / L2 / L3 stack.

    L2 uses ``paths.get_db_path`` unless ``db_path`` is given.
    Does not open the database until the first WarmStore connection.
    """
    return StorageFoundation(
        hot=HotCache(),
        warm=WarmStore(db_path=db_path, migrate=migrate),
        cold=ColdVault(vault_dir=vault_dir),
        assembled_at=utc_now_iso_z(),
    )
