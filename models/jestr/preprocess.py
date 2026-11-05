"""Convert stable annotation rows to JESTR's training-time peak representation."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def normalized_peaks(mz_values, intensities) -> tuple[list[float], list[float]]:
    mz = np.asarray(mz_values, dtype=np.float32)
    intensity = np.asarray(intensities, dtype=np.float32)
    valid = np.isfinite(mz) & np.isfinite(intensity) & (mz <= 1000)
    mz, intensity = mz[valid], intensity[valid]
    if not len(mz):
        raise ValueError("spectrum has no finite peaks at or below 1000 m/z")
    maximum = float(np.max(intensity))
    if not np.isfinite(maximum) or maximum <= 0:
        raise ValueError("spectrum has no positive finite intensities")
    return mz.tolist(), (intensity / maximum * 999.0).tolist()


def prepare_rows(rows: list[dict]) -> list[dict]:
    prepared = []
    cache = {}
    for row in rows:
        spectrum_id = row["spectrum_id"]
        if spectrum_id not in cache:
            cache[spectrum_id] = normalized_peaks(row["mz_array"], row["intensity_array"])
        mz, intensity = cache[spectrum_id]
        prepared.append({
            "spectrum_id": spectrum_id, "candidate_id": row["candidate_id"],
            "canonical_smiles": row["canonical_smiles"],
            "collision_energy": float(row["collision_energy"]),
            "mz_array": mz, "intensity_array": intensity,
        })
    return prepared


SCHEMA = pa.schema([
    pa.field("spectrum_id", pa.string(), nullable=False),
    pa.field("candidate_id", pa.string(), nullable=False),
    pa.field("canonical_smiles", pa.string(), nullable=False),
    pa.field("collision_energy", pa.float64(), nullable=False),
    pa.field("mz_array", pa.list_(pa.float64()), nullable=False),
    pa.field("intensity_array", pa.list_(pa.float64()), nullable=False),
])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = prepare_rows(pq.read_table(args.input).to_pylist())
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), args.output, compression="zstd")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
