from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "models" / "common"))


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class PreprocessingTests(unittest.TestCase):
    def test_jestr_reproduces_training_peak_contract(self):
        jestr = module("jestr_preprocess", ROOT / "models/jestr/preprocess.py")
        mz, intensity = jestr.normalized_peaks([10, 20, 1001], [2, 4, 100])
        self.assertEqual(mz, [10.0, 20.0])
        self.assertEqual(intensity, [499.5, 999.0])

    def test_mspred_preprocessors_preserve_model_fields(self):
        row = {
            "canonical_smiles": "CCO", "adduct": "[M+H]+", "precursor_mz": 47.0,
            "collision_energy": 20.0, "instrument_type": "Orbitrap",
        }
        for model_name in ("glacier", "iceberg"):
            native = module(
                f"{model_name}_preprocess", ROOT / f"models/{model_name}/preprocess.py"
            )
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "labels.tsv"
                mapping = native.prepare(output, [row])
                self.assertEqual(mapping["row_000000000"], row)
                text = output.read_text(encoding="utf-8")
                self.assertIn("collision_energies", text)
                self.assertIn("Orbitrap", text)

    def test_flare_groups_candidates_by_spectrum_and_formula(self):
        flare = module("flare_preprocess", ROOT / "models/flare/preprocess.py")
        base = {
            "spectrum_id": "s1", "molecular_formula": "C2H6O", "mz_array": [10.0],
            "intensity_array": [1.0], "precursor_mz": 47.0, "adduct": "[M+H]+",
            "instrument_type": "Orbitrap", "collision_energy": 20.0,
        }
        rows = [{**base, "canonical_smiles": "CCO"}, {**base, "canonical_smiles": "OCC"}]
        with tempfile.TemporaryDirectory() as directory:
            _, candidates, mapping = flare.prepare(Path(directory), rows)
            self.assertEqual(len(mapping), 1)
            self.assertEqual(json.loads(candidates.read_text())["C2H6O"], ["CCO", "OCC"])

    def test_sirius_writes_stable_native_feature(self):
        sirius = module("sirius_preprocess", ROOT / "models/sirius/preprocess.py")
        row = {
            "spectrum_id": "s1", "precursor_mz": 101.0, "precursor_charge": 1,
            "declared_adduct": "[M+H]+", "candidate_adducts": ["[M+H]+"],
            "declared_formula": "C4H4O3", "collision_energy": 20.0,
            "mz_array": [25.0, 50.0], "intensity_array": [2.0, 3.0],
        }
        with tempfile.TemporaryDirectory() as directory:
            mapping, reasons = sirius.prepare([row], Path(directory))
            self.assertIsNone(reasons["s1"])
            feature = next(iter(mapping))
            text = (Path(directory) / f"{feature}.ms").read_text(encoding="utf-8")
            self.assertIn(">ionization [M+H]+", text)
            self.assertIn("25 2", text)


if __name__ == "__main__":
    unittest.main()
