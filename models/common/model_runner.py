"""Common runtime for FLARE, GLACIER, and ICEBERG backend adapters."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from candidates import read_candidates
from checkpoints import load_checkpoint_bundle
from contracts import (
    CE_SEARCH_SCHEMA, ELIGIBILITY_SCHEMA, MODEL_SCHEMA_VERSION, PREDICTION_SCHEMA,
)
from runtime import atomic_directory, expand_command, run_command, utc_now, write_json, write_rows
from spectra import collision_energies, read_spectra

BACKEND_INPUT_SCHEMA = pa.schema([
    pa.field("study_id", pa.string(), nullable=False),
    pa.field("spectrum_id", pa.string(), nullable=False),
    pa.field("candidate_id", pa.string(), nullable=False),
    pa.field("precursor_mz", pa.float64(), nullable=False),
    pa.field("charge", pa.int16()),
    pa.field("adduct", pa.string(), nullable=False),
    pa.field("molecular_formula", pa.string()),
    pa.field("canonical_smiles", pa.string(), nullable=False),
    pa.field("instrument_type", pa.string(), nullable=False),
    pa.field("collision_energy", pa.float64(), nullable=False),
    pa.field("collision_energy_source", pa.string(), nullable=False),
    pa.field("mz_array", pa.list_(pa.float64()), nullable=False),
    pa.field("intensity_array", pa.list_(pa.float64()), nullable=False),
])

BACKEND_REQUIRED = {"spectrum_id", "candidate_id", "collision_energy", "raw_score"}


def parser(model: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=f"Run {model} annotation against candidate structures.")
    result.add_argument("--spectra", type=Path, required=True)
    result.add_argument("--candidates", type=Path, required=True)
    weights = result.add_mutually_exclusive_group()
    weights.add_argument("--checkpoint-bundle", type=Path)
    weights.add_argument("--weights", choices=("massspecgym",))
    result.add_argument("--model-bundles", type=Path, default=Path("/opt/model-bundles"))
    result.add_argument("--output-root", type=Path, required=True)
    result.add_argument("--run-id", required=True)
    result.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    result.add_argument("--batch-size", type=int, default=32)
    result.add_argument("--instrument-type", default="Orbitrap")
    result.add_argument("--collision-energy-grid", default="10,20,30,40,50")
    return result


def _grid(value: str) -> list[float]:
    values = [float(item.strip()) for item in value.split(",") if item.strip()]
    if not values or any(item <= 0 for item in values):
        raise ValueError("Collision-energy grid must contain positive values.")
    return values


def _reason(model: str, spectrum: dict, candidate: dict, adduct: str | None) -> str | None:
    if spectrum.get("precursor_mz") is None:
        return "missing_precursor_mz"
    if not spectrum.get("mz_array"):
        return "empty_peak_list"
    if not candidate.get("canonical_smiles"):
        return "missing_candidate_smiles"
    if "." in candidate["canonical_smiles"]:
        return "multi_component_candidate"
    if not adduct:
        return "missing_adduct"
    if model == "flare" and not candidate.get("molecular_formula"):
        return "missing_molecular_formula"
    if model == "jestr":
        usable = [
            float(intensity) for mz, intensity in zip(
                spectrum.get("mz_array") or [], spectrum.get("intensity_array") or []
            ) if float(mz) <= 1000 and float(intensity) > 0
        ]
        if not usable:
            return "no_positive_peaks_at_or_below_1000_mz"
    return None


def _backend_rows(path: Path, allowed: set[tuple[str, str, float]]) -> list[dict]:
    if not path.is_file():
        raise ValueError("Backend did not create backend_predictions.parquet.")
    table = pq.read_table(path)
    missing = sorted(BACKEND_REQUIRED - set(table.column_names))
    if missing:
        raise ValueError(f"Backend predictions are missing: {', '.join(missing)}.")
    rows = table.to_pylist()
    seen = set()
    for row in rows:
        key = (str(row["spectrum_id"]), str(row["candidate_id"]), float(row["collision_energy"]))
        if key not in allowed:
            raise ValueError(f"Backend returned an unknown input key: {key}")
        if key in seen:
            raise ValueError(f"Backend returned a duplicate input key: {key}")
        seen.add(key)
        row["collision_energy"] = key[2]
        row["raw_score"] = float(row["raw_score"])
    if seen != allowed:
        raise ValueError(f"Backend omitted {len(allowed - seen)} requested predictions.")
    return rows


def run(model: str, args: argparse.Namespace) -> int:
    if not args.instrument_type.strip():
        raise ValueError("Instrument type must not be empty.")
    started = utc_now()
    spectra = read_spectra(args.spectra)
    if not spectra:
        return 2
    study_id = spectra[0]["study_id"]
    by_id = {row["spectrum_id"]: row for row in spectra}
    candidates = read_candidates(args.candidates, set(by_id))
    checkpoint_bundle = args.checkpoint_bundle
    if checkpoint_bundle is None:
        checkpoint_bundle = args.model_bundles / model / "massspecgym"
    if not checkpoint_bundle.is_dir():
        raise ValueError(
            f"Default {model} weights are not installed at {checkpoint_bundle}. "
            "Rebuild the image or provide --checkpoint-bundle."
        )
    manifest, fingerprint = load_checkpoint_bundle(checkpoint_bundle, model)
    if manifest["preprocessing_schema_version"] != MODEL_SCHEMA_VERSION:
        raise ValueError("Checkpoint preprocessing schema is incompatible with this image.")
    grid = _grid(args.collision_energy_grid)
    target = args.output_root.resolve() / study_id / "annotations" / model / args.run_id
    temporary, publish = atomic_directory(target)
    eligibility = []
    prepared = []
    candidate_spectra = defaultdict(int)
    for candidate in candidates:
        spectrum = by_id[candidate["spectrum_id"]]
        adduct = candidate.get("adduct") or spectrum.get("declared_adduct")
        if not adduct:
            inferred = spectrum.get("candidate_adducts") or []
            adduct = inferred[0] if len(inferred) == 1 else None
        reason = _reason(model, spectrum, candidate, adduct)
        eligibility.append({
            "schema_version": MODEL_SCHEMA_VERSION, "study_id": study_id,
            "spectrum_id": spectrum["spectrum_id"],
            "candidate_id": candidate["candidate_id"], "model_name": model,
            "status": "eligible" if reason is None else "skipped", "reason": reason,
        })
        if reason:
            continue
        energies, source = collision_energies(spectrum, grid)
        for energy in energies:
            prepared.append({
                "study_id": study_id, "spectrum_id": spectrum["spectrum_id"],
                "candidate_id": candidate["candidate_id"],
                "precursor_mz": spectrum["precursor_mz"],
                "charge": candidate.get("charge") or spectrum.get("precursor_charge"),
                "adduct": adduct, "molecular_formula": candidate.get("molecular_formula"),
                "canonical_smiles": candidate["canonical_smiles"],
                "instrument_type": args.instrument_type,
                "collision_energy": energy, "collision_energy_source": source,
                "mz_array": spectrum["mz_array"], "intensity_array": spectrum["intensity_array"],
            })
        candidate_spectra[spectrum["spectrum_id"]] += 1
    write_rows(temporary / "eligibility.parquet", eligibility, ELIGIBILITY_SCHEMA)
    if not prepared:
        write_rows(temporary / "predictions.parquet", [], PREDICTION_SCHEMA)
        write_rows(temporary / "collision_energy_search.parquet", [], CE_SEARCH_SCHEMA)
        write_json(temporary / "run_report.json", {
            "schema_version": MODEL_SCHEMA_VERSION, "status": "no_eligible_spectra",
            "model_name": model, "study_id": study_id, "run_id": args.run_id,
            "started_utc": started, "completed_utc": utc_now(),
        })
        publish()
        return 2
    backend_input = temporary / "backend_input.parquet"
    write_rows(backend_input, prepared, BACKEND_INPUT_SCHEMA)
    backend_output = temporary / "backend_predictions.parquet"
    values = {
        "input": str(backend_input), "output": str(backend_output),
        "checkpoint_bundle": str(checkpoint_bundle.resolve()),
        "device": args.device, "batch_size": str(args.batch_size),
    }
    run_command(expand_command(manifest["inference_command"], values))
    allowed = {(r["spectrum_id"], r["candidate_id"], r["collision_energy"]) for r in prepared}
    backend = _backend_rows(backend_output, allowed)
    source_by_key = {
        (r["spectrum_id"], r["candidate_id"], r["collision_energy"]): r["collision_energy_source"]
        for r in prepared
    }
    grouped = defaultdict(list)
    for row in backend:
        grouped[(row["spectrum_id"], row["candidate_id"])].append(row)
    higher = manifest["score_direction"] == "higher"
    selected = {}
    ce_rows = []
    for key, rows in grouped.items():
        chosen = sorted(rows, key=lambda r: r["raw_score"], reverse=higher)[0]
        selected[key] = chosen
        for row in rows:
            ce_rows.append({
                "schema_version": MODEL_SCHEMA_VERSION, "study_id": study_id,
                "spectrum_id": row["spectrum_id"], "candidate_id": row["candidate_id"],
                "model_name": model, "run_id": args.run_id,
                "collision_energy": row["collision_energy"], "raw_score": row["raw_score"],
                "selected": row is chosen,
            })
    predictions = []
    by_spectrum = defaultdict(list)
    for key, row in selected.items():
        by_spectrum[key[0]].append(row)
    candidate_lookup = {(r["spectrum_id"], r["candidate_id"]): r for r in candidates}
    for spectrum_id, rows in by_spectrum.items():
        ordered = sorted(rows, key=lambda r: r["raw_score"], reverse=higher)
        count = len(ordered)
        for rank, row in enumerate(ordered, 1):
            candidate = candidate_lookup[(spectrum_id, row["candidate_id"])]
            source = source_by_key[(spectrum_id, row["candidate_id"], row["collision_energy"])]
            predictions.append({
                "schema_version": MODEL_SCHEMA_VERSION, "study_id": study_id,
                "spectrum_id": spectrum_id, "candidate_id": row["candidate_id"],
                "model_name": model, "run_id": args.run_id,
                "checkpoint_fingerprint": fingerprint, "status": "success",
                "eligibility_reason": None, "raw_score": row["raw_score"], "rank": rank,
                "candidate_count": count,
                "rank_score": 1.0 if count == 1 else 1.0 - (rank - 1) / (count - 1),
                "adduct": candidate.get("adduct"),
                "collision_energy": row["collision_energy"], "collision_energy_source": source,
            })
    write_rows(temporary / "predictions.parquet", predictions, PREDICTION_SCHEMA)
    write_rows(temporary / "collision_energy_search.parquet", ce_rows, CE_SEARCH_SCHEMA)
    write_json(temporary / "run_report.json", {
        "schema_version": MODEL_SCHEMA_VERSION, "status": "success", "model_name": model,
        "model_version": manifest["model_version"], "study_id": study_id, "run_id": args.run_id,
        "device": args.device, "checkpoint_fingerprint": fingerprint,
        "instrument_type": args.instrument_type,
        "weight_source": "checkpoint_bundle" if args.checkpoint_bundle else "massspecgym",
        "counts": {"spectra": len(spectra), "candidates": len(candidates),
                   "backend_requests": len(prepared), "predictions": len(predictions)},
        "started_utc": started, "completed_utc": utc_now(),
    })
    publish()
    print(f"{model}: {len(predictions):,} candidate predictions for {study_id}")
    print(f"Output: {target}")
    return 0


def main(model: str) -> int:
    try:
        return run(model, parser(model).parse_args())
    except Exception as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
