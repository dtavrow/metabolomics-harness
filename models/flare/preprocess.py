"""Convert the stable annotation parquet into FLARE's TSV and candidate JSON."""

from __future__ import annotations

import argparse
import csv
import json
from collections import OrderedDict
from pathlib import Path

import pyarrow.parquet as pq


def prepare(work: Path, rows: list[dict]) -> tuple[Path, Path, dict]:
    groups = OrderedDict()
    for row in rows:
        key = (row["spectrum_id"], row["molecular_formula"])
        groups.setdefault(key, []).append(row)
    query_by_key = {key: f"query_{index:09d}" for index, key in enumerate(groups)}
    tsv = work / "queries.tsv"
    columns = [
        "identifier", "mzs", "intensities", "smiles", "inchikey", "formula",
        "precursor_formula", "parent_mass", "precursor_mz", "adduct",
        "instrument_type", "collision_energy", "fold", "simulation_challenge",
    ]
    candidates = OrderedDict()
    with tsv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, delimiter="\t")
        writer.writeheader()
        for key, grouped in groups.items():
            first = grouped[0]
            query = query_by_key[key]
            formula = first["molecular_formula"]
            candidates.setdefault(formula, [])
            candidates[formula].extend(
                smiles for smiles in dict.fromkeys(row["canonical_smiles"] for row in grouped)
                if smiles not in candidates[formula]
            )
            writer.writerow({
                "identifier": query,
                "mzs": ",".join(str(value) for value in first["mz_array"]),
                "intensities": ",".join(str(value) for value in first["intensity_array"]),
                "smiles": first["canonical_smiles"], "inchikey": "", "formula": formula,
                "precursor_formula": formula, "parent_mass": first["precursor_mz"],
                "precursor_mz": first["precursor_mz"], "adduct": first["adduct"],
                "instrument_type": first["instrument_type"],
                "collision_energy": first["collision_energy"], "fold": "test",
                "simulation_challenge": False,
            })
    candidate_path = work / "candidates.json"
    candidate_path.write_text(json.dumps(candidates), encoding="utf-8")
    return tsv, candidate_path, query_by_key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prepare(args.output_dir, pq.read_table(args.input).to_pylist())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
