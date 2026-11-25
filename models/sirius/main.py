"""Convert parsed spectra to SIRIUS input and normalize SIRIUS summaries."""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
from candidates import candidate_id, write_candidates
from contracts import ELIGIBILITY_SCHEMA, MODEL_SCHEMA_VERSION, PREDICTION_SCHEMA
from preprocess import feature_id as _feature_id
from preprocess import prepare as prepare_sirius
from rdkit import Chem, RDLogger
from runtime import atomic_directory, utc_now, write_json, write_rows
from spectra import qc_metrics, read_spectra

RDLogger.DisableLog("rdApp.*")

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
    result.add_argument(
        "--candidate-timeout-seconds", type=float, default=0.0,
        help=(
            "Per-spectrum wall-clock budget for candidate generation. 0 "
            "(default) disables this and runs every spectrum in one SIRIUS "
            "invocation, as before. A positive value processes spectra in "
            "chunks (amortizing SIRIUS's ~30s JVM/AOT-cache startup cost "
            "across many spectra instead of paying it per spectrum), and "
            "bisects any chunk that blows its aggregate budget "
            "(chunk_size * this value) until the specific slow spectra are "
            "isolated and skipped -- logged as eligibility reason "
            "'candidate_generation_timeout', never silently dropped."
        ),
    )
    return result


_CHUNK_SIZE = 200


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


def _sirius_command(
    sirius_command: str, input_dir: Path, project: Path, top_k: int,
    compound_timeout: int, databases: str, summaries: Path,
) -> list[str]:
    return [sirius_command, "--input", str(input_dir), "--project", str(project),
            "formulas", "--candidates", str(top_k), "--compound-timeout", str(compound_timeout),
            "fingerprints", "classes", "structures", "--database", databases,
            "write-summaries", "--output", str(summaries),
            "--feature-quality-summary", "--top-k-summary", str(top_k)]


def _check_sirius_result(completed: subprocess.CompletedProcess) -> None:
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


def _run_chunk(
    sirius_command: str, ms_paths: list[Path], work: Path, summaries_root: Path,
    top_k: int, compound_timeout: int, databases: str, chunk_index: int,
    timeout_seconds: float,
) -> bool:
    """Run one chunk of .ms features through SIRIUS under an overall
    wall-clock budget. Returns False only on a timeout; any other SIRIUS
    failure (login, crash) still raises, same as the unchunked path."""

    chunk_dir = work / f"chunk_input_{chunk_index}"
    chunk_dir.mkdir()
    for path in ms_paths:
        os.link(path, chunk_dir / path.name)
    project = work / f"chunk_project_{chunk_index}.sirius"
    chunk_summaries = summaries_root / f"chunk_{chunk_index}"
    chunk_summaries.mkdir(parents=True)
    command = _sirius_command(
        sirius_command, chunk_dir, project, top_k, compound_timeout, databases, chunk_summaries,
    )
    try:
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False
    _check_sirius_result(completed)
    return True


def _run_chunked(
    sirius_command: str, ms_paths: list[Path], work: Path, summaries_root: Path,
    top_k: int, compound_timeout: int, databases: str, candidate_timeout_seconds: float,
    counter: list[int], timed_out_features: list[str],
) -> None:
    """Bisect ms_paths until every chunk either completes within its
    proportional budget (chunk size * candidate_timeout_seconds) or is
    narrowed down to the single feature(s) that cannot -- isolating exactly
    the slow spectra without paying SIRIUS's JVM/AOT-cache startup cost once
    per spectrum for the common case where nothing is pathologically slow."""

    if not ms_paths:
        return
    index = counter[0]
    counter[0] += 1
    budget = len(ms_paths) * candidate_timeout_seconds
    ok = _run_chunk(
        sirius_command, ms_paths, work, summaries_root, top_k, compound_timeout,
        databases, index, budget,
    )
    if ok:
        return
    if len(ms_paths) == 1:
        timed_out_features.append(ms_paths[0].stem)
        return
    midpoint = len(ms_paths) // 2
    _run_chunked(
        sirius_command, ms_paths[:midpoint], work, summaries_root, top_k,
        compound_timeout, databases, candidate_timeout_seconds, counter, timed_out_features,
    )
    _run_chunked(
        sirius_command, ms_paths[midpoint:], work, summaries_root, top_k,
        compound_timeout, databases, candidate_timeout_seconds, counter, timed_out_features,
    )


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
    timed_out_features: list[str] = []
    if args.candidate_timeout_seconds > 0:
        ms_paths = sorted(input_dir.glob("*.ms"))
        counter = [0]
        for start in range(0, len(ms_paths), _CHUNK_SIZE):
            _run_chunked(
                args.sirius_command, ms_paths[start:start + _CHUNK_SIZE], temporary,
                summaries, args.top_k, args.compound_timeout, args.databases,
                args.candidate_timeout_seconds, counter, timed_out_features,
            )
    else:
        project = temporary / f"{study_id}_{args.run_id}.sirius"
        command = _sirius_command(
            args.sirius_command, input_dir, project, args.top_k, args.compound_timeout,
            args.databases, summaries,
        )
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        _check_sirius_result(completed)
    structure_rows = _summary_rows(summaries, "structure_identifications*.tsv")
    structure_rows += _summary_rows(summaries, "structure_identifications*.csv")
    if not structure_rows and not timed_out_features:
        raise RuntimeError(
            "SIRIUS produced no structure-identification summaries; verify login and database access."
        )
    candidate_rows = {}
    ranks = {}
    invalid_smiles_count = 0
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
        if smiles and Chem.MolFromSmiles(smiles) is None:
            # SIRIUS/CSI:FingerID occasionally reports a structure (usually
            # from its PubChem lookup) that isn't chemically valid, e.g. a
            # hypervalent atom -- RDKit refuses to parse it. Drop it here,
            # at the source, instead of letting each of flare/glacier/
            # iceberg/jestr discover it independently and fail differently.
            invalid_smiles_count += 1
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
    by_spectrum: dict[str, list[tuple[dict, dict]]] = {}
    for pair in candidate_rows.values():
        by_spectrum.setdefault(pair[1]["spectrum_id"], []).append(pair)
    # Independent backstop on top of --top-k: SIRIUS is asked for at most
    # top_k structures per feature, but a feature can carry several
    # molecular-formula hypotheses, each contributing its own top_k
    # structures -- the merged, deduplicated-by-candidate_id count per
    # spectrum can still exceed top_k. Enforce the same ceiling here,
    # structurally, rather than trusting SIRIUS's own output size.
    capped_spectra = 0
    capped_candidates_removed = 0
    normalized: list[dict] = []
    predictions: list[dict] = []
    for pairs in by_spectrum.values():
        pairs.sort(key=lambda item: (item[1]["rank"], -(item[1]["raw_score"] or float("-inf"))))
        if len(pairs) > args.top_k:
            capped_spectra += 1
            capped_candidates_removed += len(pairs) - args.top_k
            pairs = pairs[: args.top_k]
        count = len(pairs)
        for ordinal, (normalized_row, prediction_row) in enumerate(pairs, 1):
            prediction_row["rank"] = ordinal
            prediction_row["candidate_count"] = count
            prediction_row["rank_score"] = 1.0 if count == 1 else 1.0 - (ordinal - 1) / (count - 1)
            normalized.append(normalized_row)
            predictions.append(prediction_row)
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
    timed_out_spectrum_ids = sorted({
        mapping[feature] for feature in timed_out_features if feature in mapping
    })
    write_json(temporary / "run_report.json", {
        "schema_version": MODEL_SCHEMA_VERSION, "status": "success", "model_name": "sirius",
        "study_id": study_id, "run_id": args.run_id, "databases": args.databases,
        "counts": {
            "spectra": len(spectra), "candidates": len(normalized),
            "invalid_smiles_filtered": invalid_smiles_count,
            "candidates_per_spectrum_cap": args.top_k,
            "spectra_capped": capped_spectra,
            "candidates_removed_by_cap": capped_candidates_removed,
            "candidate_generation_timeout_seconds": args.candidate_timeout_seconds or None,
            "candidate_generation_timeouts": len(timed_out_spectrum_ids),
        },
        "candidate_generation_timeout_spectrum_ids": timed_out_spectrum_ids,
        "completed_utc": utc_now(),
    })
    publish()
    print(f"sirius: {len(normalized):,} candidates for {study_id}")
    if invalid_smiles_count:
        print(f"sirius: filtered {invalid_smiles_count:,} candidates with chemically invalid SMILES")
    if capped_spectra:
        print(
            f"sirius: capped {capped_spectra:,} spectra to the top {args.top_k} candidates "
            f"({capped_candidates_removed:,} candidates removed)"
        )
    if timed_out_spectrum_ids:
        print(
            f"sirius: skipped {len(timed_out_spectrum_ids):,} spectra that exceeded the "
            f"{args.candidate_timeout_seconds:g}s candidate-generation budget"
        )
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
