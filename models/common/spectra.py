"""Read and validate the parser's stable spectrum contract."""

from __future__ import annotations

import math
from pathlib import Path

import pyarrow.parquet as pq

REQUIRED_COLUMNS = {
    "study_id", "spectrum_id", "source_file", "precursor_mz", "precursor_charge",
    "collision_energy", "collision_energy_unit", "declared_adduct", "candidate_adducts",
    "declared_formula", "mz_array", "intensity_array", "peak_count",
}


def read_spectra(path: Path) -> list[dict]:
    table = pq.read_table(path)
    missing = sorted(REQUIRED_COLUMNS - set(table.column_names))
    if missing:
        raise ValueError(f"Spectrum Parquet is missing: {', '.join(missing)}.")
    rows = table.to_pylist()
    studies = {row["study_id"] for row in rows}
    if len(studies) > 1:
        raise ValueError("Spectrum Parquet must contain one study.")
    for row in rows:
        mzs = row["mz_array"] or []
        intensities = row["intensity_array"] or []
        if len(mzs) != len(intensities):
            raise ValueError(f"Mismatched peak arrays for {row['spectrum_id']}.")
    return rows


def qc_metrics(row: dict) -> dict:
    pairs = [
        (float(mz), float(intensity))
        for mz, intensity in zip(row.get("mz_array") or [], row.get("intensity_array") or [])
        if math.isfinite(float(mz)) and math.isfinite(float(intensity)) and mz > 0 and intensity >= 0
    ]
    intensities = [item[1] for item in pairs]
    total = sum(intensities)
    probabilities = [value / total for value in intensities if total > 0 and value > 0]
    entropy = -sum(value * math.log(value) for value in probabilities)
    return {
        "peak_count": len(pairs),
        "total_ion_current": total,
        "base_peak_intensity": max(intensities, default=0.0),
        "minimum_mz": min((item[0] for item in pairs), default=None),
        "maximum_mz": max((item[0] for item in pairs), default=None),
        "spectral_entropy": entropy,
        "invalid_peak_count": len(row.get("mz_array") or []) - len(pairs),
        "precursor_present": row.get("precursor_mz") is not None,
        "centroid_status": row.get("centroid_status"),
    }


def collision_energies(row: dict, grid: list[float]) -> tuple[list[float], str]:
    value = row.get("collision_energy")
    unit = (row.get("collision_energy_unit") or "").strip().casefold()
    usable = not unit or unit in {"ev", "electronvolt", "electronvolts"}
    if value is not None and math.isfinite(float(value)) and usable:
        return [float(value)], "OBSERVED"
    return grid, "PREDICTED_GRID_SEARCH"

