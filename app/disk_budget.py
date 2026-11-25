"""Disk budget guards, checked before every Workbench study fetch and every
model run.

Mirrors ``download_worker.py``'s existing ``budget_state``/reserve logic: a
fetch that would start over budget is refused outright rather than started
and truncated, because a half-downloaded archive costs more than a refusal.

Two independent guards live here:

- ``check_disk_budget`` -- a *global* ceiling on raw payload bytes, summed
  across every study's ``raw/<STUDY_ID>/`` directory. Temporary: once older
  studies' raw payloads are deleted, a refused fetch can succeed later.
- ``check_study_footprint`` -- a *per-study* ceiling on raw + processed
  combined. A study already at or over this is unsatisfiable no matter how
  much budget frees up elsewhere, so callers should record it as oversized
  (``app.ledger.record_oversized_study``) and skip it for good, not retry.
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
    #: True when ``required_bytes`` alone exceeds the whole budget -- this
    #: fetch can never fit, even starting from zero other usage, so it is
    #: not a transient refusal a later retry could resolve.
    oversized: bool
    reason: str | None


@dataclass(frozen=True)
class StudyFootprintStatus:
    study_id: str
    raw_bytes: int
    processed_bytes: int
    total_bytes: int
    budget_bytes: float | None
    oversized: bool
    reason: str | None


def _directory_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(entry.stat().st_size for entry in path.rglob("*") if entry.is_file())


def check_disk_budget(required_bytes: float = 0.0) -> DiskBudgetStatus:
    """Report current *global* raw-payload budget usage and whether a fetch
    of ``required_bytes`` more fits.

    ``managed_bytes`` is measured from the filesystem under
    ``pipeline_runs/agent_runs/raw``, not accumulated from ledger records, so
    a restart or a manual deletion is picked up immediately. Only the raw
    Workbench payload counts against this budget -- parsed/processed output
    is durable and kept after the raw payload is deleted, so it is not part
    of this ceiling (see ``check_study_footprint`` for a cap that does
    include it, per study).
    """

    config = get_config()
    managed_bytes = _directory_bytes(config.agent_runs_root / "raw")
    _total, _used, free_bytes = shutil.disk_usage(str(config.repo_root))

    budget_remaining = None
    if config.disk_budget_bytes is not None:
        budget_remaining = config.disk_budget_bytes - managed_bytes

    over_budget = (
        budget_remaining is not None and budget_remaining - required_bytes <= 0
    )
    under_reserve = free_bytes - required_bytes <= config.disk_reserve_bytes
    oversized = (
        config.disk_budget_bytes is not None
        and required_bytes >= config.disk_budget_bytes
    )

    reason = None
    if over_budget:
        reason = (
            f"global raw-data budget exhausted: {managed_bytes / 1e9:.1f} GB used "
            f"of {config.disk_budget_bytes / 1e9:.1f} GB "  # type: ignore[union-attr]
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
        oversized=oversized,
        reason=reason,
    )


def check_study_footprint(
    study_id: str, raw_study_dir: Path, processed_study_dir: Path,
) -> StudyFootprintStatus:
    """Report one study's current raw + processed footprint against the
    per-study budget.

    Call this before fetch/parse/run-model for a study that already has data
    on disk (a prior partial run, or a known blowup like SIRIUS project-space
    bloat) -- if it is already over budget, there is no point starting more
    work on it; the caller should record it as oversized and skip instead.
    """

    config = get_config()
    raw_bytes = _directory_bytes(raw_study_dir)
    processed_bytes = _directory_bytes(processed_study_dir)
    total_bytes = raw_bytes + processed_bytes

    budget = config.study_footprint_budget_bytes
    oversized = budget is not None and total_bytes > budget

    reason = None
    if oversized:
        reason = (
            f"{study_id} footprint {total_bytes / 1e9:.1f} GB "
            f"(raw {raw_bytes / 1e9:.1f} GB + processed {processed_bytes / 1e9:.1f} GB) "
            f"exceeds the {budget / 1e9:.1f} GB per-study budget"  # type: ignore[union-attr]
        )

    return StudyFootprintStatus(
        study_id=study_id,
        raw_bytes=raw_bytes,
        processed_bytes=processed_bytes,
        total_bytes=total_bytes,
        budget_bytes=budget,
        oversized=oversized,
        reason=reason,
    )
