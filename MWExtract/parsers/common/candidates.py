"""Validate and resolve optional file- and study-scoped candidate sidecars."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from schema import CANDIDATE_SCHEMA, SCHEMA_VERSION

COLUMNS = [
    "molecular_formula", "compound_name", "inchikey", "canonical_smiles",
    "hmdb_id", "pubchem_cid", "source",
]


def _clean(value):
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _candidate_id(row: dict) -> str:
    identity = "|".join(str(row.get(key) or "") for key in COLUMNS[:-1])
    return "candidate:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def read_sidecar(path: Path | None, study_id: str, source_files: list[str]) -> pa.Table:
    if path is None:
        return pa.Table.from_pylist([], schema=CANDIDATE_SCHEMA)
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.casefold() in {".parquet", ".pq"}:
        frame = pd.read_parquet(path)
    elif path.suffix.casefold() in {".tsv", ".txt"}:
        frame = pd.read_csv(path, sep="\t", dtype=str)
    elif path.suffix.casefold() == ".csv":
        frame = pd.read_csv(path, dtype=str)
    else:
        raise ValueError("Candidate sidecar must be Parquet, TSV, or CSV.")
    if "study_id" not in frame.columns:
        raise ValueError("Candidate sidecar is missing study_id.")
    frame = frame[frame["study_id"].astype(str).eq(study_id)].copy()
    for column in ["source_file", *COLUMNS]:
        if column not in frame.columns:
            frame[column] = None
    rows = []
    for source_file in source_files:
        matches = frame[frame["source_file"].map(_clean).map(
            lambda value: Path(value).name.casefold() == source_file.casefold() if value else False
        )]
        chosen = matches if not matches.empty else frame[frame["source_file"].map(_clean).isna()]
        scope = "file" if not matches.empty else "study"
        for _, source in chosen.iterrows():
            row = {column: _clean(source[column]) for column in COLUMNS}
            if not any(row[column] for column in COLUMNS[:-1]):
                raise ValueError("Every candidate needs a formula, name, or stable identifier.")
            row.update(
                schema_version=SCHEMA_VERSION,
                study_id=study_id,
                source_file=source_file,
                source_scope=scope,
            )
            row["candidate_id"] = _candidate_id(row)
            rows.append(row)
    unique = {(row["source_file"], row["candidate_id"]): row for row in rows}
    return pa.Table.from_pylist(list(unique.values()), schema=CANDIDATE_SCHEMA)


def write_candidates(table: pa.Table, path: Path) -> None:
    pq.write_table(table, path, compression="zstd")
