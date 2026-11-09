# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for app/winners_db.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import winners_db
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
    monkeypatch.setattr(winners_db, "get_config", lambda: config)


def _prediction(spectrum_id, candidate_id, model_name, rank_score):
    return {
        "spectrum_id": spectrum_id, "candidate_id": candidate_id,
        "model_name": model_name, "rank_score": rank_score,
    }


def test_winner_needs_at_least_three_models_above_threshold() -> None:
    rows = [
        _prediction("spec:1", "candidate:a", "flare", 0.9),
        _prediction("spec:1", "candidate:a", "glacier", 0.85),
        # Only two models scored this pair >= 0.8 -- not a winner.
    ]
    assert winners_db.compute_winners(rows, {}, {}) == []


def test_winner_recorded_once_three_models_agree_above_threshold() -> None:
    rows = [
        _prediction("spec:1", "candidate:a", "flare", 0.9),
        _prediction("spec:1", "candidate:a", "glacier", 0.85),
        _prediction("spec:1", "candidate:a", "iceberg", 0.8),
        _prediction("spec:1", "candidate:a", "jestr", 0.1),  # below threshold, doesn't count
        _prediction("spec:1", "candidate:b", "flare", 0.95),  # different candidate, ignored
    ]
    spectra_by_id = {"spec:1": {"source_file": "run1.mzML", "scan_number": 42}}
    candidates_by_key = {
        ("spec:1", "candidate:a"): {
            "canonical_smiles": "OCC1OC(O)...",
            "inchikey": "WQZGKKKJIJFFOK-GASJEMHNSA-N",
            "molecular_formula": "C6H12O6",
        },
    }

    winners = winners_db.compute_winners(rows, spectra_by_id, candidates_by_key)

    assert len(winners) == 1
    winner = winners[0]
    assert winner["spectrum_id"] == "spec:1"
    assert winner["candidate_id"] == "candidate:a"
    assert winner["file_name"] == "run1.mzML"
    assert winner["scan_number"] == 42
    assert winner["inchikey"] == "WQZGKKKJIJFFOK-GASJEMHNSA-N"
    assert winner["molecular_formula"] == "C6H12O6"
    assert winner["model_count"] == 3
    assert winner["model_names"] == ["flare", "glacier", "iceberg"]


def test_record_and_list_round_trip() -> None:
    winners = [{
        "spectrum_id": "spec:1", "candidate_id": "candidate:a",
        "file_name": "run1.mzML", "scan_number": 42,
        "canonical_smiles": "OCC1OC(O)...", "inchikey": "WQZGKKKJIJFFOK-GASJEMHNSA-N",
        "molecular_formula": "C6H12O6",
        "model_count": 3, "model_names": ["flare", "glacier", "iceberg"],
    }]
    winners_db.record_winners("ST004496", "st004496-1-0-0", winners, recorded_utc="2026-01-01T00:00:00+00:00")

    listed = winners_db.list_winners()
    assert len(listed) == 1
    row = listed[0]
    assert row["study_id"] == "ST004496"
    assert row["run_id"] == "st004496-1-0-0"
    assert row["inchikey"] == "WQZGKKKJIJFFOK-GASJEMHNSA-N"
    assert row["model_names"] == ["flare", "glacier", "iceberg"]


def test_rerun_replaces_rather_than_accumulates() -> None:
    first = [{
        "spectrum_id": "spec:1", "candidate_id": "candidate:a",
        "file_name": "run1.mzML", "scan_number": 42,
        "canonical_smiles": "x", "inchikey": "WQZGKKKJIJFFOK-GASJEMHNSA-N",
        "molecular_formula": "C6H12O6",
        "model_count": 3, "model_names": ["flare", "glacier", "iceberg"],
    }]
    winners_db.record_winners("ST004496", "run-1", first, recorded_utc="2026-01-01T00:00:00+00:00")
    winners_db.record_winners("ST004496", "run-1", [], recorded_utc="2026-01-02T00:00:00+00:00")

    assert winners_db.list_winners() == []
