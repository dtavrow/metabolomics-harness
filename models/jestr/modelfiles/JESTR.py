"""Run JESTR cosine-similarity retrieval for MGF spectra.

The command accepts one or more MGF files and one or more candidate molecule
files. Candidate files are combined into one de-duplicated SMILES list, and
each spectrum is scored against every valid candidate.
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path

import dgl
import numpy as np
import pandas as pd
import torch
import yaml
from pyteomics import mgf
from rdkit import Chem
from torch.utils.data import DataLoader

from dataset import mol_to_graph
from train_contr import train_contr
from utils import DatasetBuilder, Spectra_data, collate_spectra_data


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = Path(os.environ.get("INPUT_DIR", "/input"))
DEFAULT_OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/output"))
CANDIDATE_SUFFIXES = {".csv", ".json", ".smi", ".smiles", ".tsv", ".txt"}
SMILES_COLUMN_NAMES = {
    "canonical_smiles",
    "candidate",
    "molecule",
    "smile",
    "smiles",
}


def init_datadir(dataset_builder):
    """Initialize only the DatasetBuilder fields needed for inference."""
    dataset_builder.data_dict = []
    dataset_builder.mol_dict = []
    dataset_builder.pair_dict = []
    dataset_builder.split_dict = []
    dataset_builder.fp_dict = []
    dataset_builder.in_to_id_dict = []
    dataset_builder.in_to_id_dict_wneg = []
    dataset_builder.data_dir = "data/NPLIB1/"


def normalized_spectrum(mz_values, intensities):
    """Return JESTR's training representation: (intensity, mz) peak rows."""
    mz_values = np.asarray(mz_values, dtype=np.float32)
    intensities = np.asarray(intensities, dtype=np.float32)
    if mz_values.size == 0 or intensities.size == 0:
        raise ValueError("spectrum has no peaks")
    maximum = float(np.max(intensities))
    if not np.isfinite(maximum) or maximum <= 0:
        raise ValueError("spectrum has no positive finite intensities")

    scaled_intensities = intensities / maximum * 999.0
    peaks = np.column_stack((scaled_intensities, mz_values))
    return peaks[np.isfinite(peaks).all(axis=1) & (peaks[:, 1] <= 1000)]


def _strings_from_json(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings_from_json(item)
    elif isinstance(value, dict):
        matching_keys = [key for key in value if key.lower() in SMILES_COLUMN_NAMES]
        if matching_keys:
            for key in matching_keys:
                yield from _strings_from_json(value[key])
        else:
            for item in value.values():
                yield from _strings_from_json(item)


def read_candidate_file(path):
    """Read SMILES from JSON, delimited tables, or one-SMILES-per-line files."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        with path.open(encoding="utf-8") as handle:
            return list(_strings_from_json(json.load(handle)))

    if suffix in {".csv", ".tsv"}:
        delimiter = "," if suffix == ".csv" else "\t"
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle, delimiter=delimiter))
        if not rows:
            return []
        normalized_header = [cell.strip().lower() for cell in rows[0]]
        matching_columns = [
            index for index, name in enumerate(normalized_header)
            if name in SMILES_COLUMN_NAMES
        ]
        column = matching_columns[0] if matching_columns else 0
        data_rows = rows[1:] if matching_columns else rows
        return [row[column].strip() for row in data_rows if len(row) > column]

    candidates = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                candidates.append(stripped.split()[0])
    return candidates


def load_candidates(paths):
    """Combine candidate files and remove blank and duplicate SMILES."""
    candidates = []
    seen = set()
    for path in paths:
        for candidate in read_candidate_file(path):
            candidate = candidate.strip()
            if candidate and candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


def spectrum_id(spectrum, index):
    params = spectrum.get("params", {})
    for key in ("title", "scans", "scan", "spectrumid", "id"):
        value = params.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return str(index)


def discover_inputs(input_dir):
    if not input_dir.is_dir():
        return [], []
    files = sorted(path for path in input_dir.iterdir() if path.is_file())
    mgf_paths = [path for path in files if path.suffix.lower() == ".mgf"]
    candidate_paths = [path for path in files if path.suffix.lower() in CANDIDATE_SUFFIXES]
    return mgf_paths, candidate_paths


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Score every spectrum against a combined candidate SMILES list."
    )
    parser.add_argument("--mgf", nargs="+", type=Path, help="one or more MGF files")
    parser.add_argument(
        "--candidates",
        nargs="+",
        type=Path,
        help="one or more candidate files (CSV, TSV, JSON, SMI, or TXT)",
    )
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument(
        "--output",
        type=Path,
        help="output CSV path or directory (default: OUTPUT_DIR or /output)",
    )
    parser.add_argument(
        "--output-filename",
        help="output CSV filename, usable with either the default or a custom directory",
    )
    parser.add_argument("--params", type=Path, default=SCRIPT_DIR / "params.yaml")
    parser.add_argument(
        "--mol-model",
        type=Path,
        help="molecule encoder checkpoint; overrides pretrained_mol_enc_model",
    )
    parser.add_argument(
        "--spec-model",
        type=Path,
        help="spectrum encoder checkpoint; overrides pretrained_spec_enc_model",
    )
    args = parser.parse_args(argv)

    discovered_mgf, discovered_candidates = discover_inputs(args.input_dir)
    args.mgf = args.mgf or discovered_mgf
    args.candidates = args.candidates or discovered_candidates
    if not args.mgf:
        parser.error("no MGF files supplied with --mgf or found in the input directory")
    if not args.candidates:
        parser.error(
            "no candidate files supplied with --candidates or found in the input directory"
        )
    for path in [*args.mgf, *args.candidates, args.params]:
        if not path.is_file():
            parser.error(f"file not found: {path}")
    return args


def default_output_filename(mgf_paths):
    if len(mgf_paths) == 1:
        return f"jestr_{mgf_paths[0].stem}_output.csv"
    return "jestr_combined_output.csv"


def output_path(path, filename, mgf_paths):
    default_name = default_output_filename(mgf_paths)
    if filename:
        if Path(filename).name != filename:
            raise ValueError("--output-filename must be a filename, not a path")
        if not filename.lower().endswith(".csv"):
            filename = f"{filename}.csv"

    if path is None:
        return DEFAULT_OUTPUT_DIR / (filename or default_name)
    if path.exists() and path.is_dir():
        return path / (filename or default_name)
    if not path.suffix:
        return path / (filename or default_name)
    if filename:
        return path.parent / filename
    return path


def build_candidate_graphs(candidates, dataset_builder, params, device):
    valid_candidates = []
    invalid_candidates = []
    for candidate in candidates:
        molecule = Chem.MolFromSmiles(candidate)
        if molecule is None or not mol_to_graph(
            molecule, candidate, dataset_builder.molgraph_dict, params, device
        ):
            invalid_candidates.append(candidate)
            continue
        valid_candidates.append(candidate)
    return valid_candidates, invalid_candidates


def encode_candidates(candidates, molgraph_dict, model, batch_size, device):
    embeddings = []
    with torch.no_grad():
        for start in range(0, len(candidates), batch_size):
            names = candidates[start:start + batch_size]
            graph = dgl.batch([molgraph_dict[name] for name in names]).to(device)
            embeddings.append(model(graph, graph.ndata["h"]).cpu())
    return torch.cat(embeddings)


def load_spectra(paths):
    spectra = []
    failures = []
    for path in paths:
        with mgf.read(str(path), use_index=False) as reader:
            for index, spectrum in enumerate(reader):
                identifier = spectrum_id(spectrum, index)
                try:
                    peaks = normalized_spectrum(
                        spectrum["m/z array"], spectrum["intensity array"]
                    )
                    if not len(peaks):
                        raise ValueError("spectrum has no peaks at or below 1000 m/z")
                    spectra.append((path.name, identifier, peaks))
                except (KeyError, TypeError, ValueError) as error:
                    failures.append((path.name, identifier, str(error)))
    return spectra, failures


def encode_spectra(spectra, dummy_candidate, molgraph_dict, params, model, device):
    zero_fingerprint = [0.0] * 4096
    data = [
        (dummy_candidate, peaks, zero_fingerprint, 1)
        for _, _, peaks in spectra
    ]
    loader = DataLoader(
        Spectra_data(data),
        batch_size=params["batch_size_val_final"],
        shuffle=False,
        collate_fn=collate_spectra_data(molgraph_dict, params),
    )
    embeddings = []
    with torch.no_grad():
        for graph, mz, intensity, pad, fingerprint, labels, lengths, names in loader:
            embeddings.append(
                model(
                    mz.to(device, dtype=torch.float32),
                    intensity.to(device, dtype=torch.float32),
                    pad.to(device),
                    lengths,
                ).cpu()
            )
    return torch.cat(embeddings)


def result_frame(spectra, candidates, candidate_embeddings, spectrum_embeddings):
    candidate_embeddings = torch.nn.functional.normalize(candidate_embeddings, dim=1)
    spectrum_embeddings = torch.nn.functional.normalize(spectrum_embeddings, dim=1)
    scores = spectrum_embeddings @ candidate_embeddings.T
    best_indices = torch.argmax(scores, dim=1)

    records = []
    for row_index, (filename, identifier, _) in enumerate(spectra):
        best_index = int(best_indices[row_index])
        record = {
            "mgf filename": filename,
            "spectra id in mgf file": identifier,
            "best match": candidates[best_index],
            "best score": float(scores[row_index, best_index]),
        }
        record.update(
            (candidate, float(score))
            for candidate, score in zip(candidates, scores[row_index])
        )
        records.append(record)
    return pd.DataFrame.from_records(records)


def main(argv=None):
    args = parse_args(argv)
    destination = output_path(args.output, args.output_filename, args.mgf)
    with args.params.open(encoding="utf-8") as handle:
        params = yaml.safe_load(handle)
    params["load_dicts"] = False
    params["inter"] = False
    if args.mol_model:
        params["pretrained_mol_enc_model"] = str(args.mol_model.resolve())
    if args.spec_model:
        params["pretrained_spec_enc_model"] = str(args.spec_model.resolve())

    for model_key in ("pretrained_mol_enc_model", "pretrained_spec_enc_model"):
        model_path = Path(params[model_key])
        if not model_path.is_absolute():
            model_path = SCRIPT_DIR / model_path
        if not model_path.is_file():
            raise FileNotFoundError(
                f"{model_key} not found: {model_path}. Supply its path explicitly."
            )
        params[model_key] = str(model_path)

    candidates = load_candidates(args.candidates)
    if not candidates:
        raise ValueError("candidate files did not contain any SMILES")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    dataset_builder = DatasetBuilder(params["exp"])
    init_datadir(dataset_builder)
    dataset_builder.molgraph_dict = {}
    # Keep the graph cache in host memory and move only inference batches to
    # the accelerator. Candidate collections can be much larger than VRAM.
    valid_candidates, invalid_candidates = build_candidate_graphs(
        candidates, dataset_builder, params, torch.device("cpu")
    )
    if invalid_candidates:
        print(
            f"Skipped {len(invalid_candidates)} candidate(s) that could not be encoded.",
            file=sys.stderr,
        )
    if not valid_candidates:
        raise ValueError("none of the candidate SMILES could be encoded")

    spectra, failures = load_spectra(args.mgf)
    for filename, identifier, reason in failures:
        print(f"Skipped {filename} spectrum {identifier}: {reason}", file=sys.stderr)
    if not spectra:
        raise ValueError("no usable spectra were found")

    log_path = destination.with_suffix(".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        mol_model, spec_model, _ = train_contr(
            dataset_builder,
            dataset_builder.molgraph_dict,
            params,
            log,
            device,
            str(SCRIPT_DIR),
            True,
        )
    mol_model.eval()
    spec_model.eval()

    candidate_embeddings = encode_candidates(
        valid_candidates,
        dataset_builder.molgraph_dict,
        mol_model,
        params["batch_size_val_final"],
        device,
    )
    spectrum_embeddings = encode_spectra(
        spectra,
        valid_candidates[0],
        dataset_builder.molgraph_dict,
        params,
        spec_model,
        device,
    )
    results = result_frame(
        spectra, valid_candidates, candidate_embeddings, spectrum_embeddings
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(destination, index=False)
    print(f"Wrote {len(results)} spectrum result(s) to {destination}")


if __name__ == "__main__":
    main()
