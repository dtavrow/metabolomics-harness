"""Small shared utilities for manifest-driven pipeline commands."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
STUDY_RE = re.compile(r"^ST\d{6}$", re.IGNORECASE)


def deliverable_path(value: str | Path) -> Path:
    """Resolve relative paths from the canonical Deliverable directory."""

    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    parts = path.parts
    if parts and parts[0].casefold() == SCRIPT_DIR.name.casefold():
        parts = parts[1:]
    return SCRIPT_DIR.joinpath(*parts).resolve()


def read_tsv(path: str | Path, required: Iterable[str] = ()) -> pd.DataFrame:
    """Read a TSV and fail early when its stage contract is incomplete."""

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    table = pd.read_csv(path, sep="\t", low_memory=False)
    missing = set(required).difference(table.columns)
    if missing:
        raise ValueError(f"{path} is missing required columns: {sorted(missing)}")
    return table


#: Suffixes ``read_table`` recognises, mapped to their reader.  Parquet exists
#: here because the container ships the corpus index in that form: the same
#: 20,764 rows are 0.41 MB as Parquet against 1,103 MB as the audit TSV, and a
#: columnar read touches only the columns a filter names.
TABLE_READERS = {
    ".parquet": lambda path: pd.read_parquet(path),
    ".pq": lambda path: pd.read_parquet(path),
    ".tsv": lambda path: pd.read_csv(path, sep="\t", low_memory=False),
    ".txt": lambda path: pd.read_csv(path, sep="\t", low_memory=False),
    ".csv": lambda path: pd.read_csv(path, low_memory=False),
}


def read_table(path: str | Path, required: Iterable[str] = ()) -> pd.DataFrame:
    """Read a tabular stage input, dispatching on suffix, and check its contract.

    Stages take ``--*-tsv`` arguments for historical reasons; this accepts the
    Parquet corpus index at the same flags so a container does not need a
    1.1 GB text file to answer a selection query.
    """

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    reader = TABLE_READERS.get(path.suffix.casefold())
    if reader is None:
        raise ValueError(
            f"{path}: unsupported table format {path.suffix!r}; "
            f"expected one of {sorted(TABLE_READERS)}"
        )
    table = reader(path)
    missing = set(required).difference(table.columns)
    if missing:
        raise ValueError(f"{path} is missing required columns: {sorted(missing)}")
    return table


def write_tsv_atomic(table: pd.DataFrame, path: str | Path) -> Path:
    """Write a complete TSV through a temporary file for resumable stage outputs."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    table.to_csv(temporary, sep="\t", index=False)
    temporary.replace(path)
    return path.resolve()


def parse_byte_size(value: str | int | float) -> int:
    """Parse byte limits such as 5000000, 5GB, or 10GiB."""

    if isinstance(value, (int, float)):
        return int(value)
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|KIB|MIB|GIB|TIB)?\s*",
        str(value),
        re.IGNORECASE,
    )
    if not match:
        raise argparse.ArgumentTypeError(f"Invalid byte size {value!r}.")
    factors = {
        "B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12,
        "KIB": 1 << 10, "MIB": 1 << 20, "GIB": 1 << 30, "TIB": 1 << 40,
    }
    return int(float(match.group(1)) * factors[(match.group(2) or "B").upper()])


def parse_vector(value: Any) -> np.ndarray:
    """Parse MassSpecGym comma vectors and common bracketed/space vectors."""

    if isinstance(value, np.ndarray):
        return value.astype(np.float64, copy=True)
    text = str(value).strip().strip("[]")
    separator = "," if "," in text else " "
    vector = np.fromstring(text, sep=separator, dtype=np.float64)
    if vector.size == 0 and text:
        raise ValueError(f"Could not parse numeric vector {str(value)[:80]!r}.")
    return vector


def comma_vector(values: np.ndarray) -> str:
    """Serialize a numeric vector in the MassSpecGym-compatible representation."""

    return ",".join(format(float(value), ".15g") for value in values)


def as_bool(value: Any) -> bool:
    """Interpret common TSV boolean values without Python's truthy-string trap."""

    if isinstance(value, bool):
        return value
    if pd.isna(value):
        return False
    return str(value).strip().casefold() in {"1", "true", "yes", "y", "selected"}


def stable_id(*parts: Any, prefix: str = "spec") -> str:
    """Create a deterministic identifier from provenance fields."""

    payload = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def sha256_file(path: str | Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """Hash one artifact so downstream stages can verify provenance."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()

