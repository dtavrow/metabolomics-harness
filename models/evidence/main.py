"""Merge model runs into review-ready annotation and leakage evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from candidates import read_candidates
from checkpoints import load_checkpoint_bundle
from contracts import ELIGIBILITY_SCHEMA, MODEL_SCHEMA_VERSION
from runtime import atomic_directory, utc_now, write_json, write_rows
from spectra import read_spectra

MODELS = ("sirius", "flare", "glacier", "iceberg")

EVIDENCE_SCHEMA = pa.schema([
    pa.field("schema_version", pa.string(), nullable=False),
    pa.field("study_id", pa.string(), nullable=False),
    pa.field("spectrum_id", pa.string(), nullable=False),
    pa.field("candidate_id", pa.string(), nullable=False),
    pa.field("sirius_raw_score", pa.float64()), pa.field("sirius_rank", pa.int32()),
    pa.field("sirius_rank_score", pa.float64()),
    pa.field("flare_raw_score", pa.float64()), pa.field("flare_rank", pa.int32()),
    pa.field("flare_rank_score", pa.float64()),
    pa.field("glacier_raw_score", pa.float64()), pa.field("glacier_rank", pa.int32()),
    pa.field("glacier_rank_score", pa.float64()),
    pa.field("iceberg_raw_score", pa.float64()), pa.field("iceberg_rank", pa.int32()),
    pa.field("iceberg_rank_score", pa.float64()),
    pa.field("consensus_rank_score", pa.float64()),
    pa.field("contributing_model_count", pa.int8(), nullable=False),
    pa.field("contributing_models", pa.list_(pa.string()), nullable=False),
])

LEAKAGE_SCHEMA = pa.schema([
    pa.field("schema_version", pa.string(), nullable=False),
    pa.field("study_id", pa.string(), nullable=False),
    pa.field("spectrum_id", pa.string(), nullable=False),
    pa.field("candidate_id", pa.string(), nullable=False),
    pa.field("model_name", pa.string(), nullable=False),
    pa.field("exact_inchikey", pa.string(), nullable=False),
    pa.field("connectivity_block", pa.string(), nullable=False),
    pa.field("standardized_structure", pa.string(), nullable=False),
    pa.field("exact_spectrum_hash", pa.string(), nullable=False),
])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--spectra", type=Path, required=True)
    result.add_argument("--candidates", type=Path, required=True)
    result.add_argument("--model-run", type=Path, action="append", required=True)
    result.add_argument("--checkpoint-bundle", type=Path, action="append", default=[])
    result.add_argument("--output-root", type=Path, required=True)
    result.add_argument("--run-id", required=True)
    return result


def _spectrum_hash(row: dict) -> str:
    pairs = sorted((round(float(mz), 6), max(float(i), 0.0))
                   for mz, i in zip(row["mz_array"], row["intensity_array"]))
    maximum = max((item[1] for item in pairs), default=0.0)
    normalized = [(mz, round(value / maximum, 6) if maximum else 0.0) for mz, value in pairs]
    return hashlib.sha256(json.dumps(normalized, separators=(",", ":")).encode()).hexdigest()


def _identity_data(bundle: Path, model: str) -> dict[str, set[str]]:
    manifest, _ = load_checkpoint_bundle(bundle, model)
    result = {"inchikey": set(), "connectivity": set(), "smiles": set(), "spectrum_hash": set()}
    declared = {str(item["path"]) for item in manifest["files"]}
    for relative in manifest.get("training_identity_tables", []):
        if relative not in declared:
            raise ValueError(f"Training identity table is not checksummed: {relative}")
        path = (bundle / relative).resolve()
        if bundle.resolve() not in path.parents:
            raise ValueError(f"Training identity path escapes bundle: {relative}")
        table = pq.read_table(path)
        for row in table.to_pylist():
            inchikey = row.get("inchikey")
            if inchikey:
                result["inchikey"].add(str(inchikey))
                result["connectivity"].add(str(inchikey).split("-")[0])
            if row.get("standardized_smiles"):
                result["smiles"].add(str(row["standardized_smiles"]))
            if row.get("spectrum_hash"):
                result["spectrum_hash"].add(str(row["spectrum_hash"]))
    result["available"] = bool(manifest.get("training_identity_tables"))
    return result


def _state(available: bool, value: str | None, identities: set[str]) -> str:
    if not available or value is None:
        return "unknown"
    return "match" if value in identities else "no_match"


def run(args: argparse.Namespace) -> int:
    spectra = read_spectra(args.spectra)
    if not spectra:
        return 2
    study_id = spectra[0]["study_id"]
    spectrum_by_id = {row["spectrum_id"]: row for row in spectra}
    candidates = read_candidates(args.candidates, set(spectrum_by_id))
    candidate_by_key = {(row["spectrum_id"], row["candidate_id"]): row for row in candidates}
    predictions = {}
    eligibility = []
    acquisition_source = None
    seen_models = set()
    for run_dir in args.model_run:
        report = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
        model = report.get("model_name") or "sirius"
        if model not in MODELS or model in seen_models:
            raise ValueError(f"Expected one run per supported model; invalid duplicate: {model}")
        seen_models.add(model)
        prediction_path = run_dir / "predictions.parquet"
        if prediction_path.is_file():
            for row in pq.read_table(prediction_path).to_pylist():
                predictions[(row["spectrum_id"], row["candidate_id"], model)] = row
        eligibility_path = run_dir / "eligibility.parquet"
        if eligibility_path.is_file():
            eligibility.extend(pq.read_table(eligibility_path).to_pylist())
        quality_path = run_dir / "acquisition_quality.parquet"
        if model == "sirius" and quality_path.is_file():
            acquisition_source = quality_path
    evidence = []
    for spectrum_id, candidate_id in sorted(candidate_by_key):
        row = {"schema_version": MODEL_SCHEMA_VERSION, "study_id": study_id,
               "spectrum_id": spectrum_id, "candidate_id": candidate_id}
        models = []
        rank_scores = []
        for model in MODELS:
            prediction = predictions.get((spectrum_id, candidate_id, model))
            row[f"{model}_raw_score"] = prediction.get("raw_score") if prediction else None
            row[f"{model}_rank"] = prediction.get("rank") if prediction else None
            row[f"{model}_rank_score"] = prediction.get("rank_score") if prediction else None
            if prediction and prediction.get("status") == "success" and prediction.get("rank_score") is not None:
                models.append(model)
                rank_scores.append(float(prediction["rank_score"]))
        row["consensus_rank_score"] = sum(rank_scores) / len(rank_scores) if rank_scores else None
        row["contributing_model_count"] = len(models)
        row["contributing_models"] = models
        evidence.append(row)
    bundles = {}
    for bundle in args.checkpoint_bundle:
        manifest = json.loads((bundle / "model-manifest.json").read_text(encoding="utf-8"))
        model = str(manifest.get("model_name", "")).casefold()
        if model in bundles:
            raise ValueError(f"Duplicate checkpoint bundle for {model}.")
        bundles[model] = _identity_data(bundle, model)
    leakage = []
    spectrum_hashes = {key: _spectrum_hash(value) for key, value in spectrum_by_id.items()}
    for candidate in candidates:
        inchikey = candidate.get("inchikey")
        connectivity = inchikey.split("-")[0] if inchikey else None
        smiles = candidate.get("standardized_smiles") or candidate.get("canonical_smiles")
        for model in ("flare", "glacier", "iceberg"):
            data = bundles.get(model)
            available = bool(data and data["available"])
            leakage.append({
                "schema_version": MODEL_SCHEMA_VERSION, "study_id": study_id,
                "spectrum_id": candidate["spectrum_id"], "candidate_id": candidate["candidate_id"],
                "model_name": model,
                "exact_inchikey": _state(available, inchikey, data["inchikey"] if data else set()),
                "connectivity_block": _state(available, connectivity, data["connectivity"] if data else set()),
                "standardized_structure": _state(available, smiles, data["smiles"] if data else set()),
                "exact_spectrum_hash": _state(available, spectrum_hashes[candidate["spectrum_id"]],
                                              data["spectrum_hash"] if data else set()),
            })
    target = args.output_root.resolve() / study_id / "annotation-evidence" / args.run_id
    temporary, publish = atomic_directory(target)
    write_rows(temporary / "annotation_evidence.parquet", evidence, EVIDENCE_SCHEMA)
    write_rows(temporary / "leakage.parquet", leakage, LEAKAGE_SCHEMA)
    write_rows(temporary / "eligibility.parquet", eligibility, ELIGIBILITY_SCHEMA)
    if acquisition_source:
        shutil.copy2(acquisition_source, temporary / "acquisition_quality.parquet")
    write_json(temporary / "run_report.json", {
        "schema_version": MODEL_SCHEMA_VERSION, "status": "success", "study_id": study_id,
        "run_id": args.run_id, "models": sorted(seen_models),
        "counts": {"candidates": len(candidates), "evidence_rows": len(evidence),
                   "leakage_rows": len(leakage)}, "completed_utc": utc_now(),
    })
    publish()
    print(f"evidence: {len(evidence):,} candidate rows for {study_id}")
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
