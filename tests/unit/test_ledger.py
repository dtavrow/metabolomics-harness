"""Unit tests for the append-only run ledger."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import ledger
from app.pipeline_config import PipelineConfig


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = PipelineConfig(
        repo_root=tmp_path,
        agent_runs_root=tmp_path / "agent_runs",
        mwb_contact_email=None,
        sirius_home_dir=None,
        analysis_max_concurrency=2,
        disk_budget_bytes=None,
        disk_reserve_bytes=0,
        docker_command_timeout_seconds=60.0,
        mwextract_image="x",
        mwparse_mzml_image="x",
        mwparse_mzxml_image="x",
        parse_count_floor_ratio=0.5,
        model_images={},
    )
    monkeypatch.setattr(ledger, "get_config", lambda: config)


def test_round_trips_a_ledger_row() -> None:
    ledger.append_ledger_row("ST004496", "fetch", state="fetched", ms2_expected=137_629)

    row = ledger.read_ledger_row("ST004496", "fetch")
    assert row is not None
    assert row["state"] == "fetched"
    assert row["ms2_expected"] == 137_629
    assert row["study_id"] == "ST004496"


def test_missing_row_reads_as_none() -> None:
    assert ledger.read_ledger_row("ST004496", "fetch") is None


def test_later_write_overwrites_the_same_stage() -> None:
    ledger.append_ledger_row("ST004496", "fetch", state="failed")
    ledger.append_ledger_row("ST004496", "fetch", state="fetched")

    row = ledger.read_ledger_row("ST004496", "fetch")
    assert row["state"] == "fetched"
