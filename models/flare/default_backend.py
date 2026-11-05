"""Native adapter from the stable Parquet contract to FLARE retrieval."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from preprocess import prepare


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
    rows = pq.read_table(args.input).to_pylist()
    with tempfile.TemporaryDirectory(prefix="mwannotate-flare-") as directory:
        work = Path(directory)
        tsv, candidates, query_by_key = prepare(work, rows)
        subformulae = work / "subformulae"
        assign = [
            "python", "/opt/flare/flare/subformula_assign/assign_subformulae.py",
            "--spec-files", str(tsv), "--labels-file", str(tsv),
            "--output-dir", str(subformulae), "--max-formulae", "60", "--num-workers", "1",
        ]
        if subprocess.run(assign, cwd="/opt/flare/flare/subformula_assign", check=False).returncode:
            raise RuntimeError("FLARE subformula assignment failed.")

        params = yaml.safe_load(Path("/opt/flare/run_scripts/msgym_main/params.yaml").read_text())
        result_path = work / "experiment" / "flare_results.pkl"
        params.update({
            "accelerator": "gpu" if args.device == "cuda" else "cpu",
            "devices": [0] if args.device == "cuda" else 1,
            "batch_size": args.batch_size, "num_workers": 0,
            "dataset_pth": str(tsv), "candidates_pth": None,
            "formula_to_smiles_pth": str(candidates),
            "subformula_dir_pth": str(subformulae), "split_pth": None,
            "checkpoint_pth": str(args.checkpoint_bundle / "flare.ckpt"),
            "df_test_path": str(result_path), "external_test": True,
            "experiment_dir": str(work / "experiment"),
        })
        params_path = work / "params.yaml"
        params_path.write_text(yaml.safe_dump(params), encoding="utf-8")
        command = [
            "python", "/opt/flare/flare/test.py", "--param_pth", str(params_path),
            "--checkpoint_pth", str(args.checkpoint_bundle / "flare.ckpt"),
            "--exp_dir", str(work / "experiment"), "--candidates_pth", str(candidates),
            "--df_test_pth", result_path.name, "--external_test",
        ]
        if subprocess.run(command, cwd="/opt/flare/flare", check=False).returncode:
            raise RuntimeError("FLARE inference failed.")
        results = pd.read_pickle(result_path)
        score_lookup = {}
        for result in results.to_dict("records"):
            score_column = "scores" if "scores" in result else "scores_fine"
            for smiles, score in zip(result["candidates"], result[score_column]):
                score_lookup[(result["identifier"], smiles)] = float(score)
        output = []
        for row in rows:
            query = query_by_key[(row["spectrum_id"], row["molecular_formula"])]
            key = (query, row["canonical_smiles"])
            if key not in score_lookup:
                raise ValueError(f"FLARE omitted candidate {row['candidate_id']}.")
            output.append({
                "spectrum_id": row["spectrum_id"], "candidate_id": row["candidate_id"],
                "collision_energy": float(row["collision_energy"]), "raw_score": score_lookup[key],
            })
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
