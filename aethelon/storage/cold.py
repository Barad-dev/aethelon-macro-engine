# -*- coding: utf-8 -*-
"""
L3 Cold Vault — long-term archive layer (stub).

Responsibilities (intended)
---------------------------
Hold compressed multi-year history for backfill
(``backfill_macro_history.py``) and regime backtesting.

This phase only defines the interface. ``archive`` / ``retrieve`` are
no-ops so later Stage D work can fill them in without a rewrite.
Nothing is written to disk here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Union

from aethelon.core.logger import get_logger
from aethelon.storage.timeutil import utc_now_iso_z

__all__ = ["ColdVault", "default_cold_vault_dir"]

log = get_logger(__name__)

PathLike = Union[str, Path]

_VAULT_SUBDIR = "cold_vault"


def default_cold_vault_dir() -> Path:
    """
    Intended vault directory under the AppData data root.

    The directory is **not** created by this stub.
    """
    from paths import get_data_dir

    return get_data_dir() / _VAULT_SUBDIR


class ColdVault:
    """
    Stub L3 archive.

    ``is_ready()`` is False until a later prompt implements compression
    and retrieval. Calls are logged at debug and otherwise ignored.
    """

    def __init__(self, vault_dir: Optional[PathLike] = None) -> None:
        self._vault_dir = (
            Path(vault_dir).expanduser() if vault_dir is not None else default_cold_vault_dir()
        )

    @property
    def vault_dir(self) -> Path:
        """Intended archive root. May not exist yet."""
        return self._vault_dir

    def is_ready(self) -> bool:
        """True when the vault can archive and retrieve. Stub: always False."""
        return False

    def readiness(self) -> dict[str, Any]:
        """
        Tiny status blob for later wiring.

        Stub always reports ``ready=False`` with reason ``stub``.
        ``checked_at`` is ISO 8601 UTC Z.
        """
        return {
            "ready": False,
            "reason": "stub",
            "vault_dir": str(self._vault_dir),
            "checked_at": utc_now_iso_z(),
        }

    def archive(self, payload: Mapping[str, Any]) -> None:
        """
        Persist a payload into the cold vault.

        Stub: does not write. ``payload`` is accepted so call sites can
        be wired before the real pipeline exists.
        """
        log.debug(
            "L3 Cold Vault stub: archive ignored (%s keys) dir=%s",
            len(payload),
            self._vault_dir,
        )

    def retrieve(self, query: Mapping[str, Any]) -> list[Any]:
        """
        Load archived records matching ``query``.

        Stub: always returns an empty list.
        """
        log.debug(
            "L3 Cold Vault stub: retrieve ignored query_keys=%s dir=%s",
            list(query.keys()),
            self._vault_dir,
        )
        return []

    def __repr__(self) -> str:
        return f"ColdVault(vault_dir={str(self._vault_dir)!r}, ready={self.is_ready()})"
