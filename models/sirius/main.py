"""Convert parsed spectra to SIRIUS input and normalize SIRIUS summaries."""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
from candidates import candidate_id, write_candidates
from contracts import ELIGIBILITY_SCHEMA, MODEL_SCHEMA_VERSION, PREDICTION_SCHEMA
from preprocess import feature_id as _feature_id
from preprocess import prepare as prepare_sirius
from runtime import atomic_directory, utc_now, write_json, write_rows
from spectra import qc_metrics, read_spectra

QUALITY_SCHEMA = pa.schema([
    pa.field("schema_version", pa.string(), nullable=False),
    pa.field("study_id", pa.string(), nullable=False),
    pa.field("spectrum_id", pa.string(), nullable=False),
    pa.field("sirius_feature_id", pa.string(), nullable=False),
    pa.field("overall_feature_quality", pa.string()),
    pa.field("peak_count", pa.int32(), nullable=False),
    pa.field("total_ion_current", pa.float64(), nullable=False),
    pa.field("base_peak_intensity", pa.float64(), nullable=False),
    pa.field("minimum_mz", pa.float64()),
    pa.field("maximum_mz", pa.float64()),
    pa.field("spectral_entropy", pa.float64(), nullable=False),
    pa.field("invalid_peak_count", pa.int32(), nullable=False),
    pa.field("precursor_present", pa.bool_(), nullable=False),
    pa.field("centroid_status", pa.string()),
])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--spectra", type=Path, required=True)
    result.add_argument("--output-root", type=Path, required=True)
    result.add_argument("--run-id", required=True)
    result.add_argument("--sirius-command", default="sirius")
    result.add_argument("--databases", default="bio,pubchem")
    result.add_argument("--top-k", type=int, default=50)
    result.add_argument("--compound-timeout", type=int, default=300)
    result.add_argument("--prepare-only", action="store_true")
    return result


def _value(row: dict, *names: str) -> str | None:
    folded = {key.casefold().replace("_", ""): value for key, value in row.items()}
    for name in names:
        value = folded.get(name.casefold().replace("_", ""))
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _normalize_adduct(value: str | None) -> str | None:
    """Collapse SIRIUS's spaced adduct notation ("[M + Na]+") to the
    unspaced form ("[M+Na]+") every downstream model's adduct lookup table
    (e.g. ms_pred's ion2mass/ion2onehot_pos) actually uses."""

    return value.replace(" ", "") if value else value


def _summary_rows(directory: Path, pattern: str) -> list[dict]:
    result = []
    for path in sorted(directory.rglob(pattern)):
        delimiter = "\t" if path.suffix.casefold() == ".tsv" else ","
        with path.open(encoding="utf-8-sig", newline="") as stream:
            result.extend(csv.DictReader(stream, delimiter=delimiter))
    return result


def run(args: argparse.Namespace) -> int:
    if args.top_k < 1:
        raise ValueError("--top-k must be positive.")
    spectra = read_spectra(args.spectra)
    if not spectra:
        return 2
    study_id = spectra[0]["study_id"]
    target = args.output_root.resolve() / study_id / "annotations" / "sirius" / args.run_id
    temporary, publish = atomic_directory(target)
    input_dir = temporary / "sirius_input"
    summaries = temporary / "summaries"
    input_dir.mkdir()
    summaries.mkdir()
    mapping, reasons = prepare_sirius(spectra, input_dir)
    eligibility = []
    quality = []
    for row in spectra:
        feature = _feature_id(row["spectrum_id"])
        mapping[feature] = row["spectrum_id"]
        reason = reasons[row["spectrum_id"]]
        eligibility.append({
            "schema_version": MODEL_SCHEMA_VERSION, "study_id": study_id,
            "spectrum_id": row["spectrum_id"], "candidate_id": None,
            "model_name": "sirius",
            "status": "eligible" if reason is None else "skipped", "reason": reason,
        })
        metrics = qc_metrics(row)
        quality.append({
            "schema_version": MODEL_SCHEMA_VERSION, "study_id": study_id,
            "spectrum_id": row["spectrum_id"], "sirius_feature_id": feature,
            "overall_feature_quality": None, **metrics,
        })
    write_rows(temporary / "eligibility.parquet", eligibility, ELIGIBILITY_SCHEMA)
    if not any(row["status"] == "eligible" for row in eligibility):
        write_rows(temporary / "acquisition_quality.parquet", quality, QUALITY_SCHEMA)
        write_json(temporary / "run_report.json", {"schema_version": MODEL_SCHEMA_VERSION,
                   "status": "no_eligible_spectra", "study_id": study_id, "run_id": args.run_id})
        publish()
        return 2
    if args.prepare_only:
        write_rows(temporary / "acquisition_quality.parquet", quality, QUALITY_SCHEMA)
        write_json(temporary / "feature_mapping.json", mapping)
        write_json(temporary / "run_report.json", {"schema_version": MODEL_SCHEMA_VERSION,
                   "status": "prepared", "study_id": study_id, "run_id": args.run_id})
        publish()
        return 0
    project = temporary / f"{study_id}_{args.run_id}.sirius"
    command = [args.sirius_command, "--input", str(input_dir), "--project", str(project),
               "formulas", "--compound-timeout", str(args.compound_timeout),
               "fingerprints", "classes", "structures", "--database", args.databases,
               "write-summaries", "--output", str(summaries),
               "--feature-quality-summary", "--top-k-summary", str(args.top_k)]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        suffix = f" Last output: {detail[-1]}" if detail else ""
        raise RuntimeError(f"SIRIUS exited with code {completed.returncode}.{suffix}")
    console = f"{completed.stdout}\n{completed.stderr}".casefold()
    if "login" in console and ("required" in console or "not logged" in console):
        matches = [
            line for line in f"{completed.stdout}\n{completed.stderr}".splitlines()
            if "login" in line.casefold()
        ]
        raise RuntimeError(
            "SIRIUS requires an authenticated account for structure discovery. "
            f"Matched line(s): {matches}"
        )
    structure_rows = _summary_rows(summaries, "structure_identifications*.tsv")
    structure_rows += _summary_rows(summaries, "structure_identifications*.csv")
    if not structure_rows:
        raise RuntimeError(
            "SIRIUS produced no structure-identification summaries; verify login and database access."
        )
    candidate_rows = {}
    ranks = {}
    for source in structure_rows:
        feature = _value(source, "mappingFeatureId", "featureId")
        if feature not in mapping:
            continue
        spectrum_id = mapping[feature]
        smiles = _value(source, "smiles", "canonicalSmiles")
        inchikey = _value(source, "inchikey", "InChIkey2D", "InChIKey")
        formula = _value(source, "molecularFormula", "formula")
        if not any((smiles, inchikey, formula)):
            continue
        cid = candidate_id(inchikey, smiles, formula)
        rank_text = _value(source, "rank", "structureRank")
        rank = int(float(rank_text)) if rank_text else ranks.get(spectrum_id, 0) + 1
        ranks[spectrum_id] = max(ranks.get(spectrum_id, 0), rank)
        score_text = _value(source, "confidenceScore", "CSI:FingerIDScore", "score")
        score = float(score_text) if score_text else None
        adduct = _normalize_adduct(_value(source, "adduct", "ionType"))
        normalized = {
            "schema_version": MODEL_SCHEMA_VERSION, "study_id": study_id,
            "spectrum_id": spectrum_id, "candidate_id": cid, "canonical_smiles": smiles,
            "standardized_smiles": smiles, "inchikey": inchikey, "molecular_formula": formula,
            "adduct": adduct, "charge": None,
            "candidate_source": "sirius", "source_database": _value(source, "database", "links"),
            "source_rank": rank, "is_reference": False, "formula_source": "sirius",
            "adduct_source": "sirius",
        }
        prediction = {
            "schema_version": MODEL_SCHEMA_VERSION, "study_id": study_id,
            "spectrum_id": spectrum_id, "candidate_id": cid, "model_name": "sirius",
            "run_id": args.run_id, "checkpoint_fingerprint": None, "status": "success",
            "eligibility_reason": None, "raw_score": score, "rank": rank,
            "candidate_count": None, "rank_score": None,
            "adduct": adduct, "collision_energy": None,
            "collision_energy_source": None,
        }
        key = (spectrum_id, cid)
        previous = candidate_rows.get(key)
        if previous is None or rank < previous[0]["source_rank"]:
            candidate_rows[key] = (normalized, prediction)
    normalized = [value[0] for value in candidate_rows.values()]
    predictions = [value[1] for value in candidate_rows.values()]
    counts = {}
    for row in predictions:
        counts[row["spectrum_id"]] = counts.get(row["spectrum_id"], 0) + 1
    by_spectrum = {}
    for row in predictions:
        by_spectrum.setdefault(row["spectrum_id"], []).append(row)
    for rows in by_spectrum.values():
        rows.sort(key=lambda item: (item["rank"], -(item["raw_score"] or float("-inf"))))
        count = len(rows)
        for ordinal, row in enumerate(rows, 1):
            row["rank"] = ordinal
            row["candidate_count"] = count
            row["rank_score"] = 1.0 if count == 1 else 1.0 - (ordinal - 1) / (count - 1)
    quality_lookup = {row["sirius_feature_id"]: row for row in quality}
    all_summary = _summary_rows(summaries, "*.tsv") + _summary_rows(summaries, "*.csv")
    for source in all_summary:
        feature = _value(source, "mappingFeatureId", "featureId")
        label = _value(source, "overallFeatureQuality")
        if feature in quality_lookup and label:
            quality_lookup[feature]["overall_feature_quality"] = label.upper()
    write_candidates(temporary / "candidate_sets.parquet", normalized)
    write_rows(temporary / "predictions.parquet", predictions, PREDICTION_SCHEMA)
    write_rows(temporary / "acquisition_quality.parquet", quality, QUALITY_SCHEMA)
    write_json(temporary / "feature_mapping.json", mapping)
    write_json(temporary / "run_report.json", {
        "schema_version": MODEL_SCHEMA_VERSION, "status": "success", "model_name": "sirius",
        "study_id": study_id, "run_id": args.run_id, "databases": args.databases,
        "counts": {"spectra": len(spectra), "candidates": len(normalized)},
        "completed_utc": utc_now(),
    })
    publish()
    print(f"sirius: {len(normalized):,} candidates for {study_id}")
    print(f"Output: {target}")
    return 0


def main() -> int:
    try:
        return run(parser().parse_args())
    except Exception as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
