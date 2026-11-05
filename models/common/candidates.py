"""Candidate-set validation and SIRIUS result normalization helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from contracts import CANDIDATE_SCHEMA, MODEL_SCHEMA_VERSION


def candidate_id(inchikey: str | None, smiles: str | None, formula: str | None) -> str:
    identity = inchikey or smiles or formula
    if not identity:
        raise ValueError("Candidate needs an InChIKey, SMILES, or formula.")
    return "candidate:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def read_candidates(path: Path, spectrum_ids: set[str]) -> list[dict]:
    table = pq.read_table(path)
    required = {field.name for field in CANDIDATE_SCHEMA}
    missing = sorted(required - set(table.column_names))
    if missing:
        raise ValueError(f"Candidate Parquet is missing: {', '.join(missing)}.")
    rows = table.select([field.name for field in CANDIDATE_SCHEMA]).to_pylist()
    unknown = sorted({row["spectrum_id"] for row in rows} - spectrum_ids)
    if unknown:
        raise ValueError(f"Candidates reference unknown spectrum_id: {unknown[0]}")
    return rows


def write_candidates(path: Path, rows: list[dict]) -> None:
    for row in rows:
        row.setdefault("schema_version", MODEL_SCHEMA_VERSION)
        row.setdefault("standardized_smiles", row.get("canonical_smiles"))
        row.setdefault("is_reference", False)
    pq.write_table(pa.Table.from_pylist(rows, schema=CANDIDATE_SCHEMA), path, compression="zstd")

