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

"""Persisted record of "winning" candidates: spectra where 3 or more scoring
models independently agree on the same candidate with a normalized score of
at least 0.8.

rank_score, not raw_score, is the field compared against the threshold --
raw_score's scale differs per model (see models/common/contracts.py), while
rank_score is every model's own prediction normalized into [0, 1] by rank,
so 0.8 means the same thing regardless of which model produced it.

Storage is a plain SQLite file under pipeline_runs/agent_runs/, the same
"simplest thing that survives a restart" choice app/ledger.py already made
for its own JSON records -- this is just the first table-shaped record this
harness has needed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.pipeline_config import get_config

#: A spectrum/candidate pair only counts as a winner once this many distinct
#: models each scored it (by rank_score) at or above this threshold.
MIN_MODELS = 3
SCORE_THRESHOLD = 0.8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS winners (
    study_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    spectrum_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    file_name TEXT,
    scan_number INTEGER,
    canonical_smiles TEXT,
    inchikey TEXT,
    molecular_formula TEXT,
    model_count INTEGER NOT NULL,
    model_names TEXT NOT NULL,
    recorded_utc TEXT NOT NULL,
    PRIMARY KEY (study_id, run_id, spectrum_id, candidate_id)
);
"""


def winners_db_path() -> Path:
    path = get_config().agent_runs_root / "winners.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(winners_db_path())
    connection.execute(_SCHEMA)
    return connection


def compute_winners(
    prediction_rows: list[dict[str, Any]],
    spectra_by_id: dict[str, dict[str, Any]],
    candidates_by_key: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group a study/run's merged predictions by (spectrum_id, candidate_id)
    and keep only the pairs at least MIN_MODELS distinct models each scored
    (by rank_score) at or above SCORE_THRESHOLD.

    Args:
        prediction_rows: One campaign's merged predictions.parquet rows
            (same rows aggregate_model_campaign_results already writes to
            the comparison TSV), each with spectrum_id/candidate_id/
            model_name/rank_score.
        spectra_by_id: spectrum_id -> {"source_file", "scan_number"}, read
            from the study's own spectra.parquet.
        candidates_by_key: (spectrum_id, candidate_id) -> {"canonical_smiles",
            "inchikey", "molecular_formula"}, read from the study's own
            candidates.parquet.
    """

    models_by_pair: dict[tuple[str, str], set[str]] = {}
    for row in prediction_rows:
        score = row.get("rank_score")
        if score is None or score < SCORE_THRESHOLD:
            continue
        key = (row["spectrum_id"], row["candidate_id"])
        models_by_pair.setdefault(key, set()).add(row["model_name"])

    winners = []
    for (spectrum_id, candidate_id), model_names in models_by_pair.items():
        if len(model_names) < MIN_MODELS:
            continue
        spectrum = spectra_by_id.get(spectrum_id, {})
        candidate = candidates_by_key.get((spectrum_id, candidate_id), {})
        winners.append({
            "spectrum_id": spectrum_id,
            "candidate_id": candidate_id,
            "file_name": spectrum.get("source_file"),
            "scan_number": spectrum.get("scan_number"),
            "canonical_smiles": candidate.get("canonical_smiles"),
            "inchikey": candidate.get("inchikey"),
            "molecular_formula": candidate.get("molecular_formula"),
            "model_count": len(model_names),
            "model_names": sorted(model_names),
        })
    return winners


def record_winners(
    study_id: str, run_id: str, winners: list[dict[str, Any]], recorded_utc: str,
) -> None:
    """Replace this (study_id, run_id)'s winner rows with a freshly computed
    set -- a rerun with different scores can shrink the winner set, so this
    is a full replace, not an additive upsert."""

    with _connect() as connection:
        connection.execute(
            "DELETE FROM winners WHERE study_id = ? AND run_id = ?", (study_id, run_id),
        )
        connection.executemany(
            """
            INSERT INTO winners (
                study_id, run_id, spectrum_id, candidate_id, file_name,
                scan_number, canonical_smiles, inchikey, molecular_formula,
                model_count, model_names, recorded_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    study_id, run_id, winner["spectrum_id"], winner["candidate_id"],
                    winner["file_name"], winner["scan_number"],
                    winner["canonical_smiles"], winner["inchikey"],
                    winner["molecular_formula"],
                    winner["model_count"], json.dumps(winner["model_names"]),
                    recorded_utc,
                )
                for winner in winners
            ],
        )
        connection.commit()


def list_winners() -> list[dict[str, Any]]:
    """Every recorded winner, across every study and run, most-agreed-upon
    first."""

    with _connect() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT study_id, run_id, spectrum_id, candidate_id, file_name,
                   scan_number, canonical_smiles, inchikey, molecular_formula,
                   model_count, model_names, recorded_utc
            FROM winners
            ORDER BY model_count DESC, study_id, spectrum_id
            """
        ).fetchall()
    return [{**dict(row), "model_names": json.loads(row["model_names"])} for row in rows]
