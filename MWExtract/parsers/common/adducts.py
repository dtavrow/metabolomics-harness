"""Versioned, explicitly non-identifying adduct candidate inference."""

from __future__ import annotations

import json
from pathlib import Path


def load_adduct_config(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("version"), str) or not payload["version"].strip():
        raise ValueError("Adduct config requires a nonempty string version.")
    mappings = payload.get("mappings")
    if not isinstance(mappings, dict):
        raise ValueError("Adduct config requires a mappings object.")
    for key, values in mappings.items():
        if not isinstance(key, str) or not isinstance(values, list) or not all(
            isinstance(value, str) and value for value in values
        ):
            raise ValueError(f"Invalid adduct mapping {key!r}.")
    return payload


def normalize_charge(raw, polarity: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        charge = int(float(raw))
    except (TypeError, ValueError):
        return None
    if polarity == "negative" and charge > 0:
        charge = -charge
    if polarity == "positive" and charge < 0:
        charge = -charge
    return charge or None


def candidate_adducts(
    declared: str | None,
    polarity: str | None,
    charge: int | None,
    config: dict,
) -> tuple[list[str], str]:
    if declared:
        return [declared], "declared"
    if polarity not in {"positive", "negative"} or charge is None:
        return [], "missing"
    values = config["mappings"].get(f"{polarity}:{abs(charge)}", [])
    return list(values), "inferred" if values else "missing"
