"""Unit tests for the disk budget guard."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app import disk_budget
from app.pipeline_config import PipelineConfig


def _config(tmp_path: Path, **overrides: object) -> PipelineConfig:
    base = {
        "repo_root": tmp_path,
        "agent_runs_root": tmp_path / "agent_runs",
        "mwb_contact_email": None,
        "sirius_home_dir": None,
        "analysis_max_concurrency": 2,
        "disk_budget_bytes": 1_000,
        "disk_reserve_bytes": 0,
        "study_footprint_budget_bytes": None,
        "docker_command_timeout_seconds": 60.0,
        "mwextract_image": "x",
        "mwparse_mzml_image": "x",
        "mwparse_mzxml_image": "x",
        "parse_count_floor_ratio": 0.5,
        "model_images": {},
    }
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


def test_oversized_when_required_bytes_alone_exceed_the_whole_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single study whose own ask exceeds the whole budget can never fit,
    no matter how much other usage frees up -- distinct from a transient
    ``refused`` that a later retry could resolve."""

    config = _config(tmp_path, disk_budget_bytes=1_000, disk_reserve_bytes=0)
    monkeypatch.setattr(disk_budget, "get_config", lambda: config)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: (0, 0, 10_000_000_000))

    status = disk_budget.check_disk_budget(required_bytes=2_000)
    assert status.oversized is True
    assert status.refused is True


def test_not_oversized_when_only_temporarily_over_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused because other studies are using the budget right now, but
    this study's own ask would fit a freshly-freed budget -- not oversized."""

    config = _config(tmp_path, disk_budget_bytes=1_000, disk_reserve_bytes=0)
    monkeypatch.setattr(disk_budget, "get_config", lambda: config)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: (0, 0, 10_000_000_000))

    status = disk_budget.check_disk_budget(required_bytes=500)
    assert status.oversized is False


def test_study_footprint_within_budget_is_not_oversized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, study_footprint_budget_bytes=1_000)
    monkeypatch.setattr(disk_budget, "get_config", lambda: config)
    raw_dir = tmp_path / "raw" / "ST000001"
    processed_dir = tmp_path / "processed" / "ST000001"
    raw_dir.mkdir(parents=True)
    processed_dir.mkdir(parents=True)
    (raw_dir / "a.bin").write_bytes(b"x" * 100)
    (processed_dir / "b.bin").write_bytes(b"x" * 100)

    status = disk_budget.check_study_footprint("ST000001", raw_dir, processed_dir)
    assert status.total_bytes == 200
    assert status.oversized is False
    assert status.reason is None


def test_study_footprint_over_budget_is_oversized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, study_footprint_budget_bytes=100)
    monkeypatch.setattr(disk_budget, "get_config", lambda: config)
    raw_dir = tmp_path / "raw" / "ST000001"
    processed_dir = tmp_path / "processed" / "ST000001"
    raw_dir.mkdir(parents=True)
    processed_dir.mkdir(parents=True)
    (raw_dir / "a.bin").write_bytes(b"x" * 60)
    (processed_dir / "b.bin").write_bytes(b"x" * 60)

    status = disk_budget.check_study_footprint("ST000001", raw_dir, processed_dir)
    assert status.total_bytes == 120
    assert status.oversized is True
    assert "exceeds the" in status.reason


def test_check_study_footprint_reads_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, study_footprint_budget_bytes=100)
    monkeypatch.setattr(disk_budget, "get_config", lambda: config)
    raw_dir = tmp_path / "raw" / "ST000001"
    processed_dir = tmp_path / "processed" / "ST000001"

    status = disk_budget.check_study_footprint("ST000001", raw_dir, processed_dir)
    assert status.budget_bytes == 100
    assert status.oversized is False
