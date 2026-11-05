from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "models" / "common"
sys.path.insert(0, str(COMMON))

from candidates import write_candidates
from checkpoints import load_checkpoint_bundle
from contracts import MODEL_SCHEMA_VERSION
from default_weights import REGISTRY, _safe_extract, provision_default
from model_runner import run as run_model
from model_runner import parser as model_parser
from model_runner import _reason
from runtime import atomic_directory


SPECTRUM_SCHEMA = pa.schema([
    pa.field("study_id", pa.string()), pa.field("spectrum_id", pa.string()),
    pa.field("source_file", pa.string()), pa.field("precursor_mz", pa.float64()),
    pa.field("precursor_charge", pa.int16()), pa.field("collision_energy", pa.float64()),
    pa.field("collision_energy_unit", pa.string()), pa.field("declared_adduct", pa.string()),
    pa.field("candidate_adducts", pa.list_(pa.string())), pa.field("declared_formula", pa.string()),
    pa.field("mz_array", pa.list_(pa.float64())), pa.field("intensity_array", pa.list_(pa.float64())),
    pa.field("peak_count", pa.int32()), pa.field("centroid_status", pa.string()),
])


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ModelContractTests(unittest.TestCase):
    def test_multi_component_candidates_are_explicitly_skipped(self):
        spectrum = {"precursor_mz": 101.0, "mz_array": [50.0], "intensity_array": [1.0]}
        candidate = {"canonical_smiles": "CCO.[Na+]", "molecular_formula": "C2H6O"}
        self.assertEqual(
            _reason("flare", spectrum, candidate, "[M+H]+"),
            "multi_component_candidate",
        )

    def test_baked_default_weights_need_no_runtime_flag(self):
        parser = model_parser("flare")
        required = ["--spectra", "s", "--candidates", "c", "--output-root", "o", "--run-id", "r"]
        defaulted = parser.parse_args(required)
        self.assertIsNone(defaulted.checkpoint_bundle)
        selected = parser.parse_args(required + ["--weights", "massspecgym"])
        self.assertEqual(selected.weights, "massspecgym")
        with self.assertRaises(SystemExit):
            parser.parse_args(required + ["--weights", "massspecgym", "--checkpoint-bundle", "b"])

    def test_default_weight_is_downloaded_verified_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ckpt"
            source.write_bytes(b"official test weight")
            original = REGISTRY["flare"]
            REGISTRY["flare"] = {
                **original, "url": source.as_uri(), "archive_sha256": digest(source),
                "files": [{"path": "flare.ckpt", "sha256": digest(source)}],
            }
            try:
                bundle = provision_default("flare", root / "cache")
                self.assertEqual((bundle / "flare.ckpt").read_bytes(), source.read_bytes())
                source.unlink()
                self.assertEqual(provision_default("flare", root / "cache"), bundle)
            finally:
                REGISTRY["flare"] = original

    def test_default_weight_archive_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("../checkpoint.ckpt", b"unsafe")
            with self.assertRaisesRegex(ValueError, "Unsafe checkpoint archive path"):
                _safe_extract(archive, root / "output", {"../checkpoint.ckpt"})

    def test_atomic_output_directory_is_host_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "output"
            temporary, publish = atomic_directory(target)
            (temporary / "result.txt").write_text("ok", encoding="utf-8")
            publish()
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            self.assertEqual((target / "result.txt").read_text(encoding="utf-8"), "ok")

    def _bundle(self, root: Path, model: str, backend: Path) -> Path:
        bundle = root / f"{model}-bundle"
        bundle.mkdir()
        weights = bundle / "weights.ckpt"
        weights.write_bytes(b"test weights")
        manifest = {
            "schema_version": "1", "model_name": model, "model_version": "test",
            "upstream_repository": "https://example.invalid/model", "upstream_commit": "abc123",
            "files": [{"path": "weights.ckpt", "sha256": digest(weights)}],
            "training_datasets": [{"name": "fixture", "version": "1", "split": "train"}],
            "preprocessing_schema_version": MODEL_SCHEMA_VERSION,
            "score_direction": "higher",
            "inference_command": [sys.executable, str(backend), "{input}", "{output}"],
        }
        (bundle / "model-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return bundle

    def test_checkpoint_checksum_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = root / "unused.py"
            backend.write_text("", encoding="utf-8")
            bundle = self._bundle(root, "glacier", backend)
            _, fingerprint = load_checkpoint_bundle(bundle, "glacier")
            self.assertEqual(len(fingerprint), 64)
            (bundle / "weights.ckpt").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                load_checkpoint_bundle(bundle, "glacier")

    def test_checkpoint_file_entry_requires_path_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = root / "unused.py"
            backend.write_text("", encoding="utf-8")
            bundle = self._bundle(root, "flare", backend)
            manifest_path = bundle / "model-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"] = [{"unrelated": "value"}]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "needs path and sha256"):
                load_checkpoint_bundle(bundle, "flare")

    def test_missing_collision_energy_grid_is_fully_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = root / "backend.py"
            backend.write_text(
                "import sys, pyarrow.parquet as pq\n"
                "table=pq.read_table(sys.argv[1])\n"
                "rows=[]\n"
                "for r in table.to_pylist():\n"
                " rows.append({'spectrum_id':r['spectrum_id'],'candidate_id':r['candidate_id'],"
                "'collision_energy':r['collision_energy'],'raw_score':-abs(r['collision_energy']-20)})\n"
                "pq.write_table(__import__('pyarrow').Table.from_pylist(rows),sys.argv[2])\n",
                encoding="utf-8",
            )
            bundle = self._bundle(root, "glacier", backend)
            spectra_path = root / "spectra.parquet"
            pq.write_table(pa.Table.from_pylist([{
                "study_id": "ST000001", "spectrum_id": "spectrum:1", "source_file": "a.mzML",
                "precursor_mz": 101.0, "precursor_charge": 1, "collision_energy": None,
                "collision_energy_unit": None, "declared_adduct": "[M+H]+",
                "candidate_adducts": ["[M+H]+"], "declared_formula": None,
                "mz_array": [20.0, 50.0], "intensity_array": [1.0, 2.0], "peak_count": 2,
                "centroid_status": "centroid",
            }], schema=SPECTRUM_SCHEMA), spectra_path)
            candidates_path = root / "candidates.parquet"
            write_candidates(candidates_path, [{
                "study_id": "ST000001", "spectrum_id": "spectrum:1", "candidate_id": "candidate:1",
                "canonical_smiles": "CCO", "standardized_smiles": "CCO", "inchikey": None,
                "molecular_formula": "C2H6O", "adduct": "[M+H]+", "charge": 1,
                "candidate_source": "fixture", "source_database": None, "source_rank": 1,
                "is_reference": True, "formula_source": "fixture", "adduct_source": "fixture",
            }])
            code = run_model("glacier", Namespace(
                spectra=spectra_path, candidates=candidates_path, checkpoint_bundle=bundle,
                output_root=root / "out", run_id="grid", device="cpu", batch_size=2,
                collision_energy_grid="10,20,30", download_timeout=120,
                instrument_type="Orbitrap", weights=None, model_cache=root / "cache",
                model_bundles=root / "unused",
            ))
            self.assertEqual(code, 0)
            output = root / "out/ST000001/annotations/glacier/grid"
            eligibility = pq.read_table(output / "eligibility.parquet").to_pylist()[0]
            self.assertEqual(eligibility["candidate_id"], "candidate:1")
            search = pq.read_table(output / "collision_energy_search.parquet").to_pylist()
            self.assertEqual([row["collision_energy"] for row in search], [10.0, 20.0, 30.0])
            self.assertEqual([row["selected"] for row in search], [False, True, False])
            prediction = pq.read_table(output / "predictions.parquet").to_pylist()[0]
            self.assertEqual(prediction["collision_energy"], 20.0)
            self.assertEqual(prediction["collision_energy_source"], "PREDICTED_GRID_SEARCH")


if __name__ == "__main__":
    unittest.main()
