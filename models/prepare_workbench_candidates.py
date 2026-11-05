"""Build spectrum-scoped model candidates from Workbench study metadata.

This campaign utility supports the two audited studies in this workspace. It
matches deposited features to parsed MS2 spectra by precursor m/z and retention
time, resolves accurate-mass candidates to structures, and writes the stable
candidate parquet contract consumed by every annotation model.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


SCHEMA_VERSION = "1.0.0"
CANDIDATE_SCHEMA = pa.schema([
    pa.field("schema_version", pa.string(), nullable=False),
    pa.field("study_id", pa.string(), nullable=False),
    pa.field("spectrum_id", pa.string(), nullable=False),
    pa.field("candidate_id", pa.string(), nullable=False),
    pa.field("canonical_smiles", pa.string()),
    pa.field("standardized_smiles", pa.string()),
    pa.field("inchikey", pa.string()),
    pa.field("molecular_formula", pa.string()),
    pa.field("adduct", pa.string()),
    pa.field("charge", pa.int16()),
    pa.field("candidate_source", pa.string(), nullable=False),
    pa.field("source_database", pa.string()),
    pa.field("source_rank", pa.int32()),
    pa.field("is_reference", pa.bool_(), nullable=False),
    pa.field("formula_source", pa.string()),
    pa.field("adduct_source", pa.string()),
])


def _candidate_id(inchikey: str | None, smiles: str, formula: str | None) -> str:
    identity = inchikey or smiles or formula
    return "candidate:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _features(path: Path, polarity: str) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        header = next(csv.reader(stream, delimiter="\t"))
    result = []
    for value in header[2:]:
        try:
            mz_text, rt_text = value.rsplit("_", 1)
            result.append({"feature_id": value, "mz": float(mz_text),
                           "rt_minutes": float(rt_text), "polarity": polarity})
        except ValueError:
            continue
    return result


def _spectrum_rt_minutes(study_id: str, row: dict) -> float | None:
    value = row.get("retention_time_seconds")
    if value is None:
        return None
    # ST004067's converter wrote ISO `PT...S`, but the numeric 0-20 range and
    # the deposited feature table both describe the 20 minute LC method.
    return float(value) if study_id == "ST004067" else float(value) / 60.0


def _match(spectra: list[dict], features: list[dict], ppm: float, rt_window: float) -> list[dict]:
    by_polarity = defaultdict(list)
    for feature in features:
        by_polarity[feature["polarity"]].append(feature)
    matches = []
    for spectrum in spectra:
        charge = spectrum.get("precursor_charge")
        if charge is not None and abs(int(charge)) != 1:
            continue
        observed = float(spectrum["precursor_mz"])
        rt = _spectrum_rt_minutes(spectrum["study_id"], spectrum)
        if rt is None:
            continue
        for feature in by_polarity[str(spectrum.get("polarity") or "").lower()]:
            error_ppm = abs(observed - feature["mz"]) / feature["mz"] * 1e6
            rt_error = abs(rt - feature["rt_minutes"])
            if error_ppm <= ppm and rt_error <= rt_window:
                matches.append({
                    "spectrum_id": spectrum["spectrum_id"], "feature_id": feature["feature_id"],
                    "polarity": feature["polarity"], "precursor_mz": observed,
                    "feature_mz": feature["mz"], "mass_error_ppm": error_ppm,
                    "spectrum_rt_minutes": rt, "feature_rt_minutes": feature["rt_minutes"],
                    "rt_error_minutes": rt_error,
                })
    return matches


def _request_text(url: str, attempts: int = 4) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "MWExtract-candidate-preparation/1.0"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            # Retrying permanent client errors makes large PubChem name batches
            # needlessly slow. Rate limits and server errors can be transient.
            if 400 <= error.code < 500 and error.code != 429:
                raise RuntimeError(f"Request failed: {url}: {error}") from error
            if attempt + 1 == attempts:
                raise RuntimeError(f"Request failed: {url}: {error}") from error
            time.sleep(1.5 * (attempt + 1))
        except OSError as error:
            if attempt + 1 == attempts:
                raise RuntimeError(f"Request failed: {url}: {error}") from error
            time.sleep(1.5 * (attempt + 1))
    raise AssertionError("unreachable")


def _mass_candidates(feature: dict, cache: dict, ppm: float) -> list[dict]:
    key = feature["feature_id"] + "|" + feature["polarity"]
    if key not in cache:
        ion = "M%2BH" if feature["polarity"] == "positive" else "M-H"
        url = ("https://www.metabolomicsworkbench.org/rest/moverz/MB/"
               f"{feature['mz']:.8f}/{ion}/0.01/txt")
        text = _request_text(url)
        rows = list(csv.DictReader(text.splitlines(), delimiter="\t"))
        cache[key] = [{
            "name": row.get("Name"), "formula": row.get("Formula"),
            "matched_mz": row.get("Matched m/z"), "delta": row.get("Delta"),
            "ion": row.get("Ion"), "category": row.get("Category"),
        } for row in rows if row.get("Name") and row.get("Formula")]
    # The Workbench endpoint accepts an absolute-Da window. Enforce the
    # campaign's tighter ppm contract on both new and cached responses.
    result = []
    for row in cache[key]:
        try:
            error_ppm = abs(float(row["matched_mz"]) - feature["mz"]) / feature["mz"] * 1e6
        except (KeyError, TypeError, ValueError):
            continue
        if error_ppm <= ppm:
            result.append(row)
    return result


def _pubchem(name: str, cache: dict) -> dict | None:
    if name in cache:
        return cache[name]
    encoded = urllib.parse.quote(name, safe="")
    url = ("https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/" + encoded +
           "/property/CanonicalSMILES,IsomericSMILES,InChIKey,MolecularFormula/JSON")
    try:
        payload = json.loads(_request_text(url, attempts=3))
        source = payload["PropertyTable"]["Properties"][0]
        smiles = source.get("SMILES") or source.get("ConnectivitySMILES")
        result = {
            "pubchem_cid": source.get("CID"), "canonical_smiles": smiles,
            "inchikey": source.get("InChIKey"), "molecular_formula": source.get("MolecularFormula"),
        } if smiles else None
    except (KeyError, IndexError, json.JSONDecodeError, RuntimeError):
        result = None
    cache[name] = result
    return result


def _st004067_candidates(matches: list[dict], features: list[dict], cache_path: Path,
                         ppm: float) -> tuple[list[dict], list[dict]]:
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    mass_cache = cache.setdefault("mass_candidates", {})
    pubchem_cache = cache.setdefault("pubchem", {})
    feature_lookup = {row["feature_id"]: row for row in features}
    needed = sorted({row["feature_id"] for row in matches})
    provenance = []
    by_feature = {}
    for index, feature_id in enumerate(needed, 1):
        feature = feature_lookup[feature_id]
        resolved = []
        for rank, candidate in enumerate(_mass_candidates(feature, mass_cache, ppm), 1):
            structure = _pubchem(candidate["name"], pubchem_cache)
            provenance.append({"feature_id": feature_id, "source_rank": rank, **candidate,
                               **(structure or {}), "resolved": structure is not None})
            if structure:
                resolved.append((rank, candidate, structure))
        by_feature[feature_id] = resolved
        cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Resolved feature {index}/{len(needed)}: {feature_id} ({len(resolved)} structures)",
              flush=True)
    cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rows = []
    for match in matches:
        adduct = "[M+H]+" if match["polarity"] == "positive" else "[M-H]-"
        for rank, source, structure in by_feature[match["feature_id"]]:
            rows.append({
                "schema_version": SCHEMA_VERSION, "study_id": "ST004067",
                "spectrum_id": match["spectrum_id"],
                "candidate_id": _candidate_id(structure["inchikey"], structure["canonical_smiles"],
                                                structure["molecular_formula"]),
                "canonical_smiles": structure["canonical_smiles"],
                "standardized_smiles": structure["canonical_smiles"],
                "inchikey": structure["inchikey"],
                "molecular_formula": structure["molecular_formula"], "adduct": adduct,
                "charge": 1 if match["polarity"] == "positive" else -1,
                "candidate_source": "workbench_accurate_mass", "source_database": "MW-MetRef+PubChem",
                "source_rank": rank, "is_reference": False,
                "formula_source": "PubChem", "adduct_source": "analysis_ion_mode",
            })
    return rows, provenance


def _st004496_features(mwtab_path: Path) -> tuple[list[dict], dict[str, dict]]:
    documents = json.loads(mwtab_path.read_text(encoding="utf-8"))
    document = documents[0] if isinstance(documents, list) else documents
    metabolites = document["MS_METABOLITE_DATA"]["Metabolites"]
    features, lookup = [], {}
    for index, row in enumerate(metabolites):
        feature_id = f"submitted:{index:04d}"
        normalized = {str(key).casefold(): value for key, value in row.items()}
        try:
            mz = float(normalized["quantified m/z"])
            rt = float(normalized["retention index"])
        except (KeyError, TypeError, ValueError):
            continue
        features.append({"feature_id": feature_id, "mz": mz, "rt_minutes": rt,
                         "polarity": "negative"})
        lookup[feature_id] = normalized
    return features, lookup


def _st004496_candidates(matches: list[dict], lookup: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    rows, provenance = [], []
    for match in matches:
        source = lookup[match["feature_id"]]
        smiles = str(source.get("smiles") or "").strip()
        if not smiles:
            continue
        formula = "".join(str(source.get("formula") or "").split()) or None
        inchikey = str(source.get("inchi key") or "").strip() or None
        provenance.append({"feature_id": match["feature_id"], "name": source.get("metabolite"),
                           "canonical_smiles": smiles, "inchikey": inchikey,
                           "molecular_formula": formula, "resolved": True})
        rows.append({
            "schema_version": SCHEMA_VERSION, "study_id": "ST004496",
            "spectrum_id": match["spectrum_id"],
            "candidate_id": _candidate_id(inchikey, smiles, formula),
            "canonical_smiles": smiles, "standardized_smiles": smiles,
            "inchikey": inchikey, "molecular_formula": formula, "adduct": "[M-H]-", "charge": -1,
            "candidate_source": "workbench_submitted_metabolite",
            "source_database": "Metabolomics Workbench", "source_rank": 1,
            "is_reference": True, "formula_source": "study_submission",
            "adduct_source": "study_method_[M-H]-",
        })
    return rows, provenance


def _write_tsv(path: Path, rows: list[dict]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", choices=("ST004067", "ST004496"), required=True)
    parser.add_argument("--spectra", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, nargs="+", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ppm", type=float, default=10.0)
    parser.add_argument("--rt-window-minutes", type=float)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    spectra = pq.read_table(args.spectra).to_pylist()
    if args.study == "ST004067":
        features = _features(args.metadata[0], "positive") + _features(args.metadata[1], "negative")
        rt_window = args.rt_window_minutes if args.rt_window_minutes is not None else 0.10
        matches = _match(spectra, features, args.ppm, rt_window)
        candidates, provenance = _st004067_candidates(
            matches, features, args.output_dir / "resolution_cache.json", args.ppm
        )
        rt_note = "source mzXML PT...S numeric values treated as minutes based on deposited 20 minute LC table"
    else:
        features, lookup = _st004496_features(args.metadata[0])
        rt_window = args.rt_window_minutes if args.rt_window_minutes is not None else 0.50
        matches = _match(spectra, features, args.ppm, rt_window)
        candidates, provenance = _st004496_candidates(matches, lookup)
        rt_note = "parsed seconds converted to minutes"
    unique = {(row["spectrum_id"], row["candidate_id"]): row for row in candidates}
    candidates = list(unique.values())
    pq.write_table(pa.Table.from_pylist(candidates, schema=CANDIDATE_SCHEMA),
                   args.output_dir / "candidates.parquet", compression="zstd")
    _write_tsv(args.output_dir / "spectrum_feature_matches.tsv", matches)
    _write_tsv(args.output_dir / "candidate_provenance.tsv", provenance)
    report = {
        "schema_version": SCHEMA_VERSION, "study_id": args.study,
        "spectra": len(spectra), "deposited_features": len(features),
        "matched_spectra": len({row["spectrum_id"] for row in matches}),
        "feature_matches": len(matches), "candidate_rows": len(candidates),
        "candidate_spectra": len({row["spectrum_id"] for row in candidates}),
        "ppm_tolerance": args.ppm, "rt_window_minutes": rt_window,
        "retention_time_interpretation": rt_note,
    }
    (args.output_dir / "candidate_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
