"""Disk budget guard, checked before every Workbench study fetch.

Mirrors ``download_worker.py``'s existing ``budget_state``/reserve logic: a
fetch that would start over budget is refused outright rather than started
and truncated, because a half-downloaded archive costs more than a refusal.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.pipeline_config import get_config


@dataclass(frozen=True)
class DiskBudgetStatus:
    managed_bytes: int
    disk_free_bytes: int
    budget_bytes: float | None
    budget_remaining_bytes: float | None
    reserve_bytes: float
    refused: bool
    reason: str | None


def _directory_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(entry.stat().st_size for entry in path.rglob("*") if entry.is_file())


def check_disk_budget(required_bytes: float = 0.0) -> DiskBudgetStatus:
    """Report current budget usage and whether a fetch of ``required_bytes`` fits.

    ``managed_bytes`` is measured from the filesystem under
    ``pipeline_runs/agent_runs``, not accumulated from ledger records, so a
    restart or a manual deletion is picked up immediately.
    """

    config = get_config()
    managed_bytes = _directory_bytes(config.agent_runs_root)
    _total, _used, free_bytes = shutil.disk_usage(str(config.repo_root))

    budget_remaining = None
    if config.disk_budget_bytes is not None:
        budget_remaining = config.disk_budget_bytes - managed_bytes

    over_budget = (
        budget_remaining is not None and budget_remaining - required_bytes <= 0
    )
    under_reserve = free_bytes - required_bytes <= config.disk_reserve_bytes

    reason = None
    if over_budget:
        reason = (
            f"global byte budget exhausted: {managed_bytes / 1e9:.1f} GB used of "
            f"{config.disk_budget_bytes / 1e9:.1f} GB "  # type: ignore[union-attr]
            f"(need {required_bytes / 1e9:.1f} GB more)"
        )
    elif under_reserve:
        reason = (
            f"free disk {free_bytes / 1e9:.1f} GB would fall at or below the "
            f"{config.disk_reserve_bytes / 1e9:.1f} GB reserve after fetching "
            f"{required_bytes / 1e9:.1f} GB"
        )

    return DiskBudgetStatus(
        managed_bytes=managed_bytes,
        disk_free_bytes=free_bytes,
        budget_bytes=config.disk_budget_bytes,
        budget_remaining_bytes=budget_remaining,
        reserve_bytes=config.disk_reserve_bytes,
        refused=reason is not None,
        reason=reason,
    )
