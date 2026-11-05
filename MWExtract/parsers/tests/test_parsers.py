from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import sys

COMMON_DIR = Path(__file__).resolve().parents[1] / "common"
if COMMON_DIR.is_dir():
    sys.path.insert(0, str(COMMON_DIR))
else:
    # Inside either image, shared modules and tests both live below /app.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters import mzml_record, mzxml_record
from adducts import candidate_adducts, load_adduct_config, normalize_charge
from candidates import read_sidecar


class UnitFloat(float):
    unit_info = "minute"


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config_dir = COMMON_DIR if COMMON_DIR.is_dir() else Path(__file__).resolve().parents[1]
        cls.config = load_adduct_config(config_dir / "adducts.v1.json")

    def test_mzml_multiple_precursors_and_lossless_arrays(self):
        record = {
            "id": "controllerType=0 scan=42",
            "ms level": 2,
            "negative scan": True,
            "centroid spectrum": True,
            "scanList": {"scan": [{"scan start time": UnitFloat(1.5)}]},
            "precursorList": {"precursor": [{
                "isolationWindow": {
                    "isolation window target m/z": 300.0,
                    "isolation window lower offset": 1.0,
                    "isolation window upper offset": 1.0,
                },
                "selectedIonList": {"selectedIon": [
                    {"selected ion m/z": 299.1, "charge state": 1},
                    {"selected ion m/z": 300.2, "charge state": 2},
                ]},
                "activation": {"collision energy": 25.0, "collision-induced dissociation": True},
            }]},
            "m/z array": np.array([50.2, 75.4]),
            "intensity array": np.array([10.0, 5.0]),
        }
        row = mzml_record(record, self.config)
        self.assertEqual(row["scan_number"], 42)
        self.assertEqual(row["retention_time_seconds"], 90.0)
        self.assertEqual(row["mz_array"], [50.2, 75.4])
        self.assertEqual(len(row["precursors"]), 2)
        self.assertEqual(row["precursor_charge"], -1)
        self.assertEqual(row["candidate_adducts"][0], "[M-H]-")
        self.assertEqual(row["collision_energy"], 25.0)

    def test_mzxml_metadata(self):
        record = {
            "num": "7", "msLevel": 2, "polarity": "+", "retentionTime": 12.5,
            "centroided": "1", "collisionEnergy": "35",
            "precursorMz": [{"precursorMz": 121.1, "precursorCharge": 1,
                              "activationMethod": "CID"}],
            "m/z array": np.array([40.0]), "intensity array": np.array([99.0]),
        }
        row = mzxml_record(record, self.config)
        self.assertEqual(row["native_id"], "7")
        self.assertEqual(row["activation_method"], "CID")
        self.assertEqual(row["collision_energy"], 35.0)
        self.assertIn("[M+H]+", row["candidate_adducts"])

    def test_non_ms2_is_ignored(self):
        self.assertIsNone(mzml_record({"ms level": 1}, self.config))
        self.assertIsNone(mzxml_record({"msLevel": 3}, self.config))

    def test_array_length_mismatch_fails(self):
        with self.assertRaisesRegex(ValueError, "equal length"):
            mzxml_record({"msLevel": 2, "m/z array": [1, 2], "intensity array": [1]}, self.config)

    def test_adduct_rules_are_explicit(self):
        self.assertEqual(normalize_charge(1, "negative"), -1)
        values, source = candidate_adducts(None, "positive", 1, self.config)
        self.assertEqual(source, "inferred")
        self.assertIn("[M+Na]+", values)
        self.assertEqual(candidate_adducts(None, None, 1, self.config), ([], "missing"))
        self.assertEqual(candidate_adducts("[M+Li]+", "positive", 1, self.config),
                         (["[M+Li]+"], "declared"))

    def test_candidates_use_file_scope_then_study_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.parquet"
            pd.DataFrame([
                {"study_id": "ST000001", "source_file": None,
                 "molecular_formula": "C6H12O6", "compound_name": "study candidate"},
                {"study_id": "ST000001", "source_file": "a.mzML",
                 "molecular_formula": "C5H10O5", "compound_name": "file candidate"},
            ]).to_parquet(path, index=False)
            table = read_sidecar(path, "ST000001", ["a.mzML", "b.mzML"]).to_pandas()
            a = table[table.source_file.eq("a.mzML")]
            b = table[table.source_file.eq("b.mzML")]
            self.assertEqual(a.compound_name.tolist(), ["file candidate"])
            self.assertEqual(a.source_scope.tolist(), ["file"])
            self.assertEqual(b.compound_name.tolist(), ["study candidate"])
            self.assertEqual(b.source_scope.tolist(), ["study"])

    def test_custom_adduct_config_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adducts.json"
            path.write_text(json.dumps({"version": "test", "mappings": {"positive:1": ["x"]}}))
            self.assertEqual(load_adduct_config(path)["version"], "test")


if __name__ == "__main__":
    unittest.main()
