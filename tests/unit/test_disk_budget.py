"""Unit tests for the disk budget guard."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app import disk_budget
from app.pipeline_config import PipelineConfig


def _config(tmp_path: Path, **overrides: object) -> PipelineConfig:
    base = dict(
        repo_root=tmp_path,
        agent_runs_root=tmp_path / "agent_runs",
        mwb_contact_email=None,
        sirius_home_dir=None,
        analysis_max_concurrency=2,
        disk_budget_bytes=1_000,
        disk_reserve_bytes=0,
        docker_command_timeout_seconds=60.0,
        mwextract_image="x",
        mwparse_mzml_image="x",
        mwparse_mzxml_image="x",
        parse_count_floor_ratio=0.5,
        model_images={},
    )
    base.update(overrides)
    return PipelineConfig(**base)


def test_refuses_when_required_bytes_exceed_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, disk_budget_bytes=1_000)
    monkeypatch.setattr(disk_budget, "get_config", lambda: config)
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: (0, 0, 10_000_000_000)
    )

    status = disk_budget.check_disk_budget(required_bytes=2_000)
    assert status.refused is True
    assert "budget exhausted" in status.reason


def test_refuses_when_free_disk_would_fall_below_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, disk_budget_bytes=None, disk_reserve_bytes=1_000)
    monkeypatch.setattr(disk_budget, "get_config", lambda: config)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: (0, 0, 1_500))

    status = disk_budget.check_disk_budget(required_bytes=600)
    assert status.refused is True
    assert "reserve" in status.reason


def test_allows_a_fetch_that_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, disk_budget_bytes=1_000_000, disk_reserve_bytes=100)
    monkeypatch.setattr(disk_budget, "get_config", lambda: config)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: (0, 0, 10_000_000))

    status = disk_budget.check_disk_budget(required_bytes=1_000)
    assert status.refused is False
    assert status.reason is None
