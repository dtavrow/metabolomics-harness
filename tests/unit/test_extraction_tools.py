# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for app/tools/extraction_tools.py."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from app.tools.extraction_tools import _instrument_polarity_counts


def _write_catalog(path: Path, rows: list[dict]) -> None:
    table = pa.Table.from_pylist(
        rows,
        schema=pa.schema([
            pa.field("instrument_type", pa.string()),
            pa.field("polarity", pa.string()),
            pa.field("ms2_spectrum_count", pa.int64()),
        ]),
    )
    pq.write_table(table, path)


def test_skips_rows_with_null_instrument_or_polarity(tmp_path: Path) -> None:
    # Some real studies (e.g. ST004718) have rows where the source metadata
    # never recorded a polarity -- these used to surface as a `None` dict
    # key, which crashes json.dump(..., sort_keys=True) downstream.
    catalog_path = tmp_path / "study.parquet"
    _write_catalog(catalog_path, [
        {"instrument_type": "Orbitrap", "polarity": "positive", "ms2_spectrum_count": 10},
        {"instrument_type": "Orbitrap", "polarity": "negative", "ms2_spectrum_count": 5},
        {"instrument_type": "Orbitrap", "polarity": None, "ms2_spectrum_count": 7},
        {"instrument_type": None, "polarity": "positive", "ms2_spectrum_count": 3},
    ])

    result = _instrument_polarity_counts(catalog_path, None, None, None)

    assert result == {"Orbitrap": {"positive": 10, "negative": 5}}
    # Must not raise: real production code path sorts dict keys.
    json.dumps(result, sort_keys=True)


def test_missing_catalog_file_returns_empty(tmp_path: Path) -> None:
    assert _instrument_polarity_counts(tmp_path / "missing.parquet", None, None, None) == {}
