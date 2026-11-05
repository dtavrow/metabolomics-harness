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

"""End-to-end checks of the actual entry point Claude Code drives this
harness through: ``python -m app.cli <subcommand>``, run as a real
subprocess (not by importing app.cli's functions directly), so a broken
argparse wiring or import would fail here the same way it would for a real
run.

These stick to subcommands that need neither Docker nor a fetched study, so
they run anywhere without MWB_CONTACT_EMAIL or built images.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest


def run_cli(*args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_generate_run_id_is_stable_shaped() -> None:
    result = run_cli("generate-run-id", "ST004496")
    assert result["run_id"].startswith("st004496-")


def test_check_disk_budget_reports_structured_status() -> None:
    result = run_cli("check-disk-budget")
    assert "refused" in result
    assert "disk_free_bytes" in result


def test_check_model_ready_for_unbuilt_image_is_not_an_error() -> None:
    # Exit code 0 either way -- "not ready" is a normal JSON status, not a crash.
    result = run_cli("check-model-ready", "flare")
    assert result["ready"] in (True, False)
    if not result["ready"]:
        assert "reason" in result


def test_unknown_subcommand_exits_nonzero() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "app.cli", "not-a-real-subcommand"],
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode != 0


@pytest.mark.parametrize("model_name", ["not-a-real-model"])
def test_run_model_rejects_unknown_model_at_argparse_level(model_name: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "app.cli", "run-model", model_name, "ST004496", "run-1"],
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode != 0
    assert "invalid choice" in completed.stderr
