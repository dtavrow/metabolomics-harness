"""Format-specific translation from Pyteomics records to the shared schema."""

from __future__ import annotations

import re
from typing import Any

import numpy as np

from adducts import candidate_adducts, normalize_charge


def _number(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _unit(value) -> str | None:
    unit = getattr(value, "unit_info", None)
    return _text(unit)


def _polarity(record: dict) -> str | None:
    if "positive scan" in record or record.get("polarity") == "+":
        return "positive"
    if "negative scan" in record or record.get("polarity") == "-":
        return "negative"
    value = str(record.get("polarity", "")).casefold()
    return value if value in {"positive", "negative"} else None


def _formula(mapping: dict) -> str | None:
    for key in ("molecular formula", "chemical formula", "formula", "precursor formula"):
        if key in mapping:
            return _text(mapping[key])
    return None


def _adduct(mapping: dict) -> str | None:
    for key in ("adduct ion", "adduct", "precursor type", "ion type"):
        if key in mapping:
            return _text(mapping[key])
    return None


def _activation(activation: dict) -> str | None:
    ignored = {"collision energy", "normalized collision energy", "collision energy ramp start",
               "collision energy ramp end"}
    for key, value in activation.items():
        if key not in ignored and (value is True or value == ""):
            return str(key)
    return _text(activation.get("activationMethod") or activation.get("method"))


def _collision(activation: dict) -> tuple[float | None, str | None, str | None]:
    for key in ("collision energy", "normalized collision energy", "collisionEnergy"):
        if key in activation:
            value = activation[key]
            number = _number(value)
            unit = _unit(value)
            if key == "normalized collision energy" and not unit:
                unit = "percent"
            return number, unit, _text(value)
    return None, None, None


def _precursor(
    selected: dict,
    isolation: dict,
    activation: dict,
    polarity: str | None,
    adduct_config: dict,
) -> dict:
    raw_charge = selected.get("charge state", selected.get("possible charge state", selected.get("precursorCharge")))
    charge = normalize_charge(raw_charge, polarity)
    declared = _adduct(selected)
    adducts, adduct_source = candidate_adducts(declared, polarity, charge, adduct_config)
    energy, energy_unit, energy_raw = _collision(activation)
    return {
        "mz": _number(selected.get("selected ion m/z", selected.get("precursorMz"))),
        "charge": charge,
        "charge_raw": _text(raw_charge),
        "intensity": _number(selected.get("peak intensity", selected.get("precursorIntensity"))),
        "isolation_target_mz": _number(isolation.get("isolation window target m/z")),
        "isolation_lower_offset": _number(isolation.get("isolation window lower offset")),
        "isolation_upper_offset": _number(isolation.get("isolation window upper offset")),
        "activation_method": _activation(activation),
        "collision_energy": energy,
        "collision_energy_unit": energy_unit,
        "collision_energy_raw": energy_raw,
        "declared_adduct": declared,
        "declared_formula": _formula(selected),
        "candidate_adducts": adducts,
        "adduct_source": adduct_source,
    }


def _row_common(record: dict, polarity: str | None, precursors: list[dict]) -> dict:
    mz = np.asarray(record.get("m/z array", []), dtype=np.float64)
    intensity = np.asarray(record.get("intensity array", []), dtype=np.float64)
    if mz.ndim != 1 or intensity.ndim != 1 or len(mz) != len(intensity):
        raise ValueError("m/z and intensity arrays must be one-dimensional and equal length.")
    primary = precursors[0] if precursors else {}
    return {
        "polarity": polarity,
        "precursors": precursors,
        "precursor_mz": primary.get("mz"),
        "precursor_charge": primary.get("charge"),
        "collision_energy": primary.get("collision_energy"),
        "collision_energy_unit": primary.get("collision_energy_unit"),
        "activation_method": primary.get("activation_method"),
        "declared_adduct": primary.get("declared_adduct"),
        "declared_formula": primary.get("declared_formula"),
        "candidate_adducts": primary.get("candidate_adducts", []),
        "mz_array": mz.tolist(),
        "intensity_array": intensity.tolist(),
        "peak_count": len(mz),
    }


def mzml_record(record: dict, adduct_config: dict) -> dict | None:
    if int(record.get("ms level", 0)) != 2:
        return None
    polarity = _polarity(record)
    precursors = []
    for item in record.get("precursorList", {}).get("precursor", []):
        ions = item.get("selectedIonList", {}).get("selectedIon", []) or [{}]
        isolation = item.get("isolationWindow", {})
        activation = item.get("activation", {})
        precursors.extend(_precursor(ion, isolation, activation, polarity, adduct_config) for ion in ions)
    scan = next(iter(record.get("scanList", {}).get("scan", [])), {})
    retention = scan.get("scan start time")
    retention_seconds = _number(retention)
    unit = (_unit(retention) or "").casefold()
    if retention_seconds is not None and ("minute" in unit or unit == "min"):
        retention_seconds *= 60
    native_id = str(record.get("id", record.get("index", "")))
    scan_match = re.search(r"(?:scan|spectrum)=(\d+)", native_id)
    row = _row_common(record, polarity, precursors)
    row.update(
        native_id=native_id,
        scan_number=int(scan_match.group(1)) if scan_match else None,
        ms_level=2,
        retention_time_seconds=retention_seconds,
        centroid_status=("centroid" if "centroid spectrum" in record else
                         "profile" if "profile spectrum" in record else None),
    )
    return row


def mzxml_record(record: dict, adduct_config: dict) -> dict | None:
    if int(record.get("msLevel", 0)) != 2:
        return None
    polarity = _polarity(record)
    raw_precursors: Any = record.get("precursorMz", [])
    if isinstance(raw_precursors, dict):
        raw_precursors = [raw_precursors]
    elif not isinstance(raw_precursors, list):
        raw_precursors = [{"precursorMz": raw_precursors}]
    precursors = []
    for selected in raw_precursors:
        if not isinstance(selected, dict):
            selected = {"precursorMz": selected}
        activation = {
            "activationMethod": selected.get("activationMethod") or record.get("activationMethod"),
            "collisionEnergy": selected.get("collisionEnergy") or record.get("collisionEnergy"),
        }
        precursors.append(_precursor(selected, {}, activation, polarity, adduct_config))
    retention = record.get("retentionTime")
    row = _row_common(record, polarity, precursors)
    row.update(
        native_id=str(record.get("num", record.get("id", ""))),
        scan_number=int(record["num"]) if str(record.get("num", "")).isdigit() else None,
        ms_level=2,
        retention_time_seconds=_number(retention),
        centroid_status=("centroid" if str(record.get("centroided", "")).casefold() in {"1", "true"}
                         else "profile" if str(record.get("centroided", "")).casefold() in {"0", "false"}
                         else None),
    )
    return row
