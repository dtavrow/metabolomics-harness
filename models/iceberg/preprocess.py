"""Convert stable annotation rows into ICEBERG's ms-pred labels table."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import pyarrow.parquet as pq


def prepare(path: Path, rows: list[dict]) -> dict[str, dict]:
    fields = ["spec", "smiles", "ionization", "precursor", "collision_energies", "instrument"]
    mapping = {}
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        for index, row in enumerate(rows):
            name = f"row_{index:09d}"
            mapping[name] = row
            writer.writerow({
                "spec": name, "smiles": row["canonical_smiles"],
                "ionization": row["adduct"], "precursor": row["precursor_mz"],
                "collision_energies": repr([float(row["collision_energy"])]),
                "instrument": row["instrument_type"],
            })
    return mapping


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    prepare(args.output, pq.read_table(args.input).to_pylist())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
