from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "prepare_workbench_candidates", ROOT / "models/prepare_workbench_candidates.py"
)
CAMPAIGN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAMPAIGN)


class WorkbenchCandidateTests(unittest.TestCase):
    def test_cached_mass_candidates_are_filtered_by_ppm(self):
        feature = {"feature_id": "100.0_1.0", "polarity": "positive", "mz": 100.0}
        cache = {"100.0_1.0|positive": [
            {"name": "near", "formula": "C", "matched_mz": "100.0005"},
            {"name": "far", "formula": "C", "matched_mz": "100.0020"},
        ]}
        rows = CAMPAIGN._mass_candidates(feature, cache, ppm=10.0)
        self.assertEqual([row["name"] for row in rows], ["near"])

    def test_study_specific_retention_time_conversion(self):
        self.assertEqual(
            CAMPAIGN._spectrum_rt_minutes("ST004067", {"retention_time_seconds": 12.5}),
            12.5,
        )
        self.assertEqual(
            CAMPAIGN._spectrum_rt_minutes("ST004496", {"retention_time_seconds": 120.0}),
            2.0,
        )


if __name__ == "__main__":
    unittest.main()
