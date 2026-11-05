"""Run the official MassSpecGym JESTR encoders against stable annotation rows."""

from __future__ import annotations

import argparse
import tempfile
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import torch
import yaml

from JESTR import (
    build_candidate_graphs, encode_candidates, encode_spectra, init_datadir,
)
from preprocess import prepare_rows
from train_contr import train_contr
from utils import DatasetBuilder


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--input", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--checkpoint-bundle", type=Path, required=True)
    result.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    result.add_argument("--batch-size", type=int, default=32)
    return result


def run(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    rows = prepare_rows(pq.read_table(args.input).to_pylist())
    if not rows:
        pq.write_table(pa.table({"spectrum_id": [], "candidate_id": [],
                                 "collision_energy": [], "raw_score": []}), args.output)
        return 0
    if args.device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is unavailable")
    device = torch.device(args.device)
    params = yaml.safe_load(Path("/opt/jestr/params.yaml").read_text(encoding="utf-8"))
    params.update({
        "load_dicts": False, "inter": False,
        "batch_size_val_final": args.batch_size,
        "pretrained_mol_enc_model": str(args.checkpoint_bundle / "pretrained_mol_enc_model_1741546103623_best.pt"),
        "pretrained_spec_enc_model": str(args.checkpoint_bundle / "pretrained_spec_enc_model_1741546103623_best.pt"),
    })
    builder = DatasetBuilder(params["exp"])
    init_datadir(builder)
    builder.molgraph_dict = {}
    smiles = list(OrderedDict.fromkeys(row["canonical_smiles"] for row in rows))
    valid, invalid = build_candidate_graphs(smiles, builder, params, torch.device("cpu"))
    if invalid:
        raise ValueError(f"JESTR could not encode {len(invalid)} candidate SMILES")
    spectra_by_id = OrderedDict()
    for row in rows:
        spectra_by_id.setdefault(row["spectrum_id"], (
            "stable-contract", row["spectrum_id"],
            np.column_stack((row["intensity_array"], row["mz_array"])),
        ))
    with tempfile.TemporaryDirectory(prefix="jestr-") as directory:
        with (Path(directory) / "inference.log").open("w", encoding="utf-8") as log:
            mol_model, spec_model, _ = train_contr(
                builder, builder.molgraph_dict, params, log, device, "/opt/jestr", True,
            )
    mol_model.eval()
    spec_model.eval()
    molecule_embeddings = torch.nn.functional.normalize(encode_candidates(
        valid, builder.molgraph_dict, mol_model, args.batch_size, device,
    ), dim=1)
    spectrum_embeddings = torch.nn.functional.normalize(encode_spectra(
        list(spectra_by_id.values()), valid[0], builder.molgraph_dict, params, spec_model, device,
    ), dim=1)
    smiles_index = {value: index for index, value in enumerate(valid)}
    spectrum_index = {value: index for index, value in enumerate(spectra_by_id)}
    output = [{
        "spectrum_id": row["spectrum_id"], "candidate_id": row["candidate_id"],
        "collision_energy": row["collision_energy"],
        "raw_score": float(spectrum_embeddings[spectrum_index[row["spectrum_id"]]] @
                           molecule_embeddings[smiles_index[row["canonical_smiles"]]]),
    } for row in rows]
    schema = pa.schema([
        pa.field("spectrum_id", pa.string(), nullable=False),
        pa.field("candidate_id", pa.string(), nullable=False),
        pa.field("collision_energy", pa.float64(), nullable=False),
        pa.field("raw_score", pa.float64(), nullable=False),
    ])
    pq.write_table(pa.Table.from_pylist(output, schema=schema), args.output, compression="zstd")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
