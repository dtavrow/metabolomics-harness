"""Export model campaign scores, ranks, candidates, and provenance to XLSX."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter


MODELS = ("flare", "glacier", "iceberg", "jestr")
RUN_ID = "workbench-20261022"


def _rows(path: Path) -> list[dict]:
    return pq.read_table(path).to_pylist()


def _sheet(workbook: Workbook, name: str, rows: list[dict]) -> None:
    sheet = workbook.create_sheet(name)
    if not rows:
        sheet.append(["no_rows"])
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    sheet.append(fields)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for row in rows:
        sheet.append([row.get(field) for field in fields])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, field in enumerate(fields, 1):
        width = min(60, max(12, len(field) + 2))
        sheet.column_dimensions[get_column_letter(index)].width = width


def _write_tsv(path: Path, rows: list[dict]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def _validate_predictions(rows: list[dict], study: str, model: str) -> None:
    grouped = defaultdict(list)
    for row in rows:
        score = row.get("raw_score")
        if score is None or not math.isfinite(float(score)):
            raise ValueError(f"{study} {model} contains a non-finite score.")
        grouped[row["spectrum_id"]].append(row)
    for spectrum_id, group in grouped.items():
        count = len(group)
        if {row["rank"] for row in group} != set(range(1, count + 1)):
            raise ValueError(f"{study} {model} has invalid ranks for {spectrum_id}.")
        if any(row["candidate_count"] != count for row in group):
            raise ValueError(f"{study} {model} has an invalid candidate count for {spectrum_id}.")


def export(campaign: Path, output: Path) -> None:
    all_scores, all_candidates, all_skipped = [], [], []
    all_matches, run_summary, study_summary = [], [], []
    for study in ("ST004067", "ST004496"):
        candidate_dir = campaign / study / "candidates"
        candidates = _rows(candidate_dir / "candidates.parquet")
        candidate_by_key = {(r["spectrum_id"], r["candidate_id"]): r for r in candidates}
        all_candidates.extend(candidates)
        with (candidate_dir / "candidate_report.json").open(encoding="utf-8") as stream:
            study_summary.append(json.load(stream))
        with (candidate_dir / "spectrum_feature_matches.tsv").open(
            encoding="utf-8", newline=""
        ) as stream:
            all_matches.extend(dict(row, study_id=study) for row in csv.DictReader(stream, delimiter="\t"))
        for model in MODELS:
            run_dir = campaign / "results" / study / "annotations" / model / RUN_ID
            predictions = _rows(run_dir / "predictions.parquet")
            _validate_predictions(predictions, study, model)
            predicted = {(r["spectrum_id"], r["candidate_id"]) for r in predictions}
            for row in predictions:
                candidate = candidate_by_key[(row["spectrum_id"], row["candidate_id"])]
                all_scores.append({
                    **row,
                    "canonical_smiles": candidate.get("canonical_smiles"),
                    "inchikey": candidate.get("inchikey"),
                    "molecular_formula": candidate.get("molecular_formula"),
                    "candidate_source": candidate.get("candidate_source"),
                    "source_database": candidate.get("source_database"),
                    "source_rank": candidate.get("source_rank"),
                    "is_reference": candidate.get("is_reference"),
                })
            for candidate in candidates:
                key = (candidate["spectrum_id"], candidate["candidate_id"])
                if key not in predicted:
                    all_skipped.append({
                        "study_id": study, "spectrum_id": key[0], "candidate_id": key[1],
                        "model_name": model, "status": "skipped",
                        "reason": "multi_component_candidate"
                        if "." in candidate["canonical_smiles"] else "not_scored",
                        "canonical_smiles": candidate["canonical_smiles"],
                        "molecular_formula": candidate.get("molecular_formula"),
                    })
            with (run_dir / "run_report.json").open(encoding="utf-8") as stream:
                report = json.load(stream)
            counts = report.pop("counts", {})
            run_summary.append({**report, **{f"count_{key}": value for key, value in counts.items()}})

    all_scores.sort(key=lambda r: (r["study_id"], r["spectrum_id"], r["model_name"], r["rank"]))
    _write_tsv(output.with_suffix(".tsv"), all_scores)
    workbook = Workbook()
    workbook.remove(workbook.active)
    notes = [
        {"topic": "contents", "detail": "Scores and within-spectrum candidate ranks from official baked MassSpecGym model weights."},
        {"topic": "candidate scope", "detail": "ST004067 candidates are MW-MetRef accurate-mass hits resolved through PubChem; ST004496 candidates are submitted study metabolites."},
        {"topic": "SIRIUS", "detail": "No SIRIUS score rows are included because CSI:FingerID requires a logged-in SIRIUS account. A failed anonymous discovery run was not treated as valid output."},
        {"topic": "skips", "detail": "Multi-component salts and complexes are retained in candidate_sets but explicitly skipped by single-molecule models."},
        {"topic": "rank", "detail": "Rank 1 is the model-preferred candidate within that spectrum's eligible candidate set."},
    ]
    _sheet(workbook, "README", notes)
    _sheet(workbook, "scores_rankings", all_scores)
    _sheet(workbook, "skipped_candidates", all_skipped)
    _sheet(workbook, "candidate_sets", all_candidates)
    _sheet(workbook, "feature_matches", all_matches)
    _sheet(workbook, "run_summary", run_summary)
    _sheet(workbook, "study_summary", study_summary)
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(args.campaign, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
