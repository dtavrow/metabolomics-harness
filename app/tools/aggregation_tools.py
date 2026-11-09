"""Merge each requested model's predictions into one campaign comparison table.

Adapted from the spirit of models/export_campaign_spreadsheet.py (same
per-model predictions.parquet -> one comparison sheet idea), generalized to
an arbitrary study id and model set instead of that script's two hardcoded
studies, and writing TSV instead of XLSX to avoid adding a new dependency
this harness otherwise has no use for.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from app.app_utils.progress_bus import get_progress_bus
from app.docker_environment import DockerPipelineEnvironment
from app.ledger import append_ledger_row, utc_now
from app.winners_db import compute_winners, record_winners

_environment = DockerPipelineEnvironment()


def _read_rows(path: Path) -> list[dict[str, Any]]:
    return pq.read_table(path).to_pylist()


def _spectra_lookup(study_id: str) -> dict[str, dict[str, Any]]:
    """spectrum_id -> {source_file, scan_number}, read straight from the
    study's own parsed spectra.parquet (mzml or mzxml, whichever parsed)."""

    for file_format in ("mzml", "mzxml"):
        spectra_path = _environment.parsed_output_dir(study_id, file_format) / "spectra.parquet"
        if spectra_path.is_file():
            table = pq.read_table(spectra_path, columns=["spectrum_id", "source_file", "scan_number"])
            return {row["spectrum_id"]: row for row in table.to_pylist()}
    return {}


def _candidates_lookup(study_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    """(spectrum_id, candidate_id) -> {canonical_smiles, inchikey,
    molecular_formula}, read from the study's own candidates.parquet -- by
    the time any model has produced a predictions.parquet, this is
    guaranteed to already be the spectrum-scoped, model-facing schema (see
    docker_environment.py's run_analysis_model), so spectrum_id is always
    present here."""

    for file_format in ("mzml", "mzxml"):
        candidates_path = _environment.parsed_output_dir(study_id, file_format) / "candidates.parquet"
        if candidates_path.is_file():
            table = pq.read_table(
                candidates_path,
                columns=["spectrum_id", "candidate_id", "canonical_smiles", "inchikey", "molecular_formula"],
            )
            return {(row["spectrum_id"], row["candidate_id"]): row for row in table.to_pylist()}
    return {}


def _write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def _rank_one_agreement(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """For each spectrum, which candidate did each model rank first, and do
    they agree."""

    by_spectrum: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.get("rank") == 1:
            by_spectrum.setdefault(row["spectrum_id"], {})[row["model_name"]] = row["candidate_id"]

    agreeing = sum(1 for picks in by_spectrum.values() if len(set(picks.values())) == 1)
    return {
        "spectra_with_a_rank_one_pick": len(by_spectrum),
        "spectra_where_all_models_agree": agreeing,
    }


def aggregate_model_campaign_results(
    study_id: str, model_names: list[str], run_id: str,
) -> dict[str, Any]:
    """Merge every requested model's predictions.parquet for one study/run
    into a single comparison TSV.

    Args:
        study_id: The Workbench accession these predictions belong to.
        model_names: The models actually run for this study (only models
            that actually produced a predictions.parquet are included; the
            rest are reported back as missing_models).
        run_id: The run id shared by all of these models' analysis calls.
    """

    get_progress_bus().publish(
        stage="aggregation", key=study_id, status="running",
        detail="merging model predictions...",
    )

    rows: list[dict[str, Any]] = []
    missing_models: list[str] = []
    for model_name in model_names:
        predictions_path = _environment.predictions_path(model_name, study_id, run_id)
        if predictions_path.is_file():
            rows.extend(_read_rows(predictions_path))
        else:
            missing_models.append(model_name)

    if not rows:
        get_progress_bus().publish(
            stage="aggregation", key=study_id, status="error",
            detail="no_predictions_found",
        )
        append_ledger_row(
            study_id, "aggregate", state="no_predictions_found",
            run_id=run_id, model_names=model_names, missing_models=missing_models,
        )
        return {
            "success": False,
            "status": "no_predictions_found",
            "missing_models": missing_models,
        }

    output_path = (
        _environment.study_output_dir(study_id) / study_id / "annotations"
        / "campaign_comparison" / f"{run_id}.tsv"
    )
    # The scoring containers run as root (see run_analysis_model), so
    # annotations/ can be root-owned; this host process can't mkdir a new
    # subdirectory under it otherwise.
    _environment.ensure_host_writable_dir(output_path.parent)
    _write_tsv(output_path, rows)

    get_progress_bus().publish(
        stage="aggregation", key=study_id, status="done",
        detail=f"{len(rows)} rows written",
    )
    models_included = sorted({row["model_name"] for row in rows})
    agreement = _rank_one_agreement(rows)

    winners = compute_winners(rows, _spectra_lookup(study_id), _candidates_lookup(study_id))
    record_winners(study_id, run_id, winners, recorded_utc=utc_now())

    append_ledger_row(
        study_id, "aggregate", state="aggregated",
        run_id=run_id, output_tsv=str(output_path), rows_written=len(rows),
        models_included=models_included, missing_models=missing_models,
        agreement=agreement, winners_recorded=len(winners),
    )
    return {
        "success": True,
        "status": "aggregated",
        "output_tsv": str(output_path),
        "rows_written": len(rows),
        "models_included": models_included,
        "missing_models": missing_models,
        "agreement": agreement,
        "winners_recorded": len(winners),
    }
