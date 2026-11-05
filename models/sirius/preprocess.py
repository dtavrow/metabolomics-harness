"""Convert model-neutral spectra parquet into SIRIUS .ms feature files."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

from spectra import read_spectra


def feature_id(spectrum_id: str) -> str:
    return "sp_" + hashlib.sha256(spectrum_id.encode("utf-8")).hexdigest()[:24]


def write_ms(path: Path, row: dict, feature: str) -> None:
    charge = row.get("precursor_charge")
    adduct = row.get("declared_adduct")
    if not adduct:
        values = row.get("candidate_adducts") or []
        adduct = values[0] if len(values) == 1 else None
    lines = [f">compound {feature}", f">feature_id {feature}",
             f">parentmass {float(row['precursor_mz']):.10g}"]
    if row.get("declared_formula"):
        lines.append(f">formula {row['declared_formula']}")
    if adduct:
        lines.append(f">ionization {adduct}")
    elif charge in {-1, 1}:
        lines.append(f">charge {charge}")
    energy = row.get("collision_energy")
    lines.append(f">collision {float(energy):.10g}" if energy is not None else ">ms2")
    lines.extend(f"{float(mz):.10g} {float(intensity):.10g}"
                 for mz, intensity in zip(row["mz_array"], row["intensity_array"])
                 if math.isfinite(float(mz)) and math.isfinite(float(intensity)))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def reason(row: dict) -> str | None:
    if row.get("precursor_mz") is None:
        return "missing_precursor_mz"
    if not row.get("mz_array"):
        return "empty_peak_list"
    charge = row.get("precursor_charge")
    if charge is not None and abs(charge) != 1:
        return "sirius_requires_singly_charged_ions"
    return None


def prepare(spectra: list[dict], output_dir: Path) -> tuple[dict[str, str], dict[str, str | None]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    mapping = {}
    reasons = {}
    for row in spectra:
        feature = feature_id(row["spectrum_id"])
        mapping[feature] = row["spectrum_id"]
        skip = reason(row)
        reasons[row["spectrum_id"]] = skip
        if skip is None:
            write_ms(output_dir / f"{feature}.ms", row, feature)
    return mapping, reasons


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spectra", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    mapping, reasons = prepare(read_spectra(args.spectra), args.output_dir)
    (args.output_dir / "feature_mapping.json").write_text(
        json.dumps({"features": mapping, "ineligible": reasons}, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
