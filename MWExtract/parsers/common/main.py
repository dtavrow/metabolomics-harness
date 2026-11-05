"""One-study, atomic mzML or mzXML to model-neutral Parquet conversion."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from pyteomics import mzml, mzxml

from adapters import mzml_record, mzxml_record
from adducts import load_adduct_config
from candidates import read_sidecar, write_candidates
from schema import SCHEMA_VERSION, SPECTRA_SCHEMA

FORMAT = os.environ.get("MWPARSER_FORMAT", "").casefold()
BATCH_SIZE = 500


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--study-dir", type=Path, required=True)
    result.add_argument("--output-root", type=Path, required=True)
    result.add_argument("--candidates", type=Path)
    result.add_argument("--adduct-config", type=Path,
                        default=Path("/app/adducts.v1.json"))
    return result


def _study(study_dir: Path) -> tuple[str, Path]:
    files = sorted(study_dir.glob("ST*.parquet"))
    if len(files) != 1:
        raise ValueError(f"Expected exactly one ST*.parquet in {study_dir}; found {len(files)}.")
    table = pq.read_table(files[0], columns=["study_id"])
    values = set(table.column("study_id").to_pylist())
    if len(values) != 1:
        raise ValueError("Study Parquet must contain exactly one study_id.")
    study_id = str(next(iter(values)))
    if files[0].stem != study_id:
        raise ValueError(f"Study Parquet filename must be {study_id}.parquet.")
    return study_id, files[0]


def _input_files(study_dir: Path) -> list[Path]:
    suffix = ".mzml" if FORMAT == "mzml" else ".mzxml"
    spectra = study_dir / "spectra"
    if not spectra.is_dir():
        raise ValueError(f"Spectra directory not found: {spectra}")
    return sorted((path for path in spectra.iterdir() if path.is_file() and path.suffix.casefold() == suffix),
                  key=lambda path: path.name.casefold())


def _spectrum_id(study_id: str, source_file: str, native_id: str) -> str:
    value = f"{study_id}|{source_file}|{native_id}"
    return "spectrum:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _publish(temporary: Path, target: Path) -> None:
    backup = target.with_name(target.name + ".previous")
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        target.replace(backup)
    try:
        temporary.replace(target)
    except Exception:
        if backup.exists() and not target.exists():
            backup.replace(target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def run(args: argparse.Namespace) -> int:
    if FORMAT not in {"mzml", "mzxml"}:
        raise ValueError("MWPARSER_FORMAT must be mzml or mzxml.")
    study_dir = args.study_dir.resolve()
    study_id, metadata_path = _study(study_dir)
    files = _input_files(study_dir)
    output_base = args.output_root.resolve() / study_id / "processed"
    target = output_base / FORMAT
    failure = output_base / f"{FORMAT}.failure.json"
    config = load_adduct_config(args.adduct_config)
    started = datetime.now(timezone.utc)
    temporary = Path(tempfile.mkdtemp(prefix=f".{FORMAT}-", dir=output_base.mkdir(parents=True, exist_ok=True) or output_base))
    counts = {"input_files": len(files), "ms2_spectra": 0, "spectra_without_precursor": 0,
              "spectra_without_collision_energy": 0, "spectra_without_adduct": 0}
    writer = None
    batch = []
    try:
        spectra_path = temporary / "spectra.parquet"
        writer = pq.ParquetWriter(spectra_path, SPECTRA_SCHEMA, compression="zstd")
        reader_type = mzml.MzML if FORMAT == "mzml" else mzxml.MzXML
        convert = mzml_record if FORMAT == "mzml" else mzxml_record
        for source in files:
            with reader_type(str(source), read_schema=False, iterative=True, use_index=False,
                             decode_binary=True) as records:
                for record in records:
                    row = convert(record, config)
                    if row is None:
                        continue
                    row.update(
                        schema_version=SCHEMA_VERSION,
                        study_id=study_id,
                        source_file=source.name,
                        source_format=FORMAT,
                    )
                    row["spectrum_id"] = _spectrum_id(study_id, source.name, row["native_id"])
                    counts["ms2_spectra"] += 1
                    counts["spectra_without_precursor"] += not bool(row["precursors"])
                    counts["spectra_without_collision_energy"] += row["collision_energy"] is None
                    counts["spectra_without_adduct"] += not bool(row["candidate_adducts"])
                    batch.append(row)
                    if len(batch) >= BATCH_SIZE:
                        writer.write_table(pa.Table.from_pylist(batch, schema=SPECTRA_SCHEMA))
                        batch.clear()
        if batch:
            writer.write_table(pa.Table.from_pylist(batch, schema=SPECTRA_SCHEMA))
        writer.close()
        writer = None
        candidates = read_sidecar(args.candidates, study_id, [path.name for path in files])
        write_candidates(candidates, temporary / "candidates.parquet")
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "success" if files and counts["ms2_spectra"] else "no_spectra",
            "study_id": study_id,
            "format": FORMAT,
            "study_metadata": metadata_path.name,
            "adduct_config_version": config["version"],
            "candidate_sidecar_supplied": args.candidates is not None,
            "candidate_rows": candidates.num_rows,
            "counts": counts,
            "started_utc": started.isoformat(),
            "completed_utc": datetime.now(timezone.utc).isoformat(),
            "versions": {
                "python": sys.version.split()[0],
                "pyteomics": importlib.metadata.version("pyteomics"),
                "pyarrow": importlib.metadata.version("pyarrow"),
            },
        }
        _write_json(temporary / "parse_report.json", report)
        _publish(temporary, target)
        failure.unlink(missing_ok=True)
        print(f"{study_id}: {counts['ms2_spectra']:,} MS2 spectra from {len(files):,} {FORMAT} files")
        print(f"Output: {target}")
        return 0 if files and counts["ms2_spectra"] else 2
    except Exception as error:
        if writer is not None:
            writer.close()
        shutil.rmtree(temporary, ignore_errors=True)
        _write_json(failure, {
            "schema_version": SCHEMA_VERSION,
            "status": "failed",
            "study_id": study_id,
            "format": FORMAT,
            "error_type": type(error).__name__,
            "error": str(error),
            "failed_utc": datetime.now(timezone.utc).isoformat(),
        })
        raise


def main() -> int:
    try:
        return run(parser().parse_args())
    except Exception as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
