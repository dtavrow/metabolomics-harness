"""Unit tests for the two-pool analysis concurrency cap."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app import concurrency
from app.pipeline_config import PipelineConfig


def _config(tmp_path: Path, **overrides: object) -> PipelineConfig:
    base = {
        "repo_root": tmp_path,
        "agent_runs_root": tmp_path / "agent_runs",
        "mwb_contact_email": None,
        "sirius_home_dir": None,
        "analysis_max_concurrency": 1,
        "disk_budget_bytes": None,
        "disk_reserve_bytes": 0,
        "study_footprint_budget_bytes": None,
        "docker_command_timeout_seconds": 60.0,
        "mwextract_image": "x",
        "mwparse_mzml_image": "x",
        "mwparse_mzxml_image": "x",
        "parse_count_floor_ratio": 0.5,
        "model_images": {},
    }
    base.update(overrides)
    return PipelineConfig(**base)


def test_sirius_pool_is_always_exactly_one_slot_regardless_of_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, analysis_max_concurrency=5)
    monkeypatch.setattr(concurrency, "get_config", lambda: config)

    second_entered = threading.Event()

    def hold_a_sirius_slot() -> None:
        with concurrency.analysis_slot("sirius"):
            time.sleep(0.3)

    thread = threading.Thread(target=hold_a_sirius_slot)
    thread.start()
    time.sleep(0.05)  # let the first slot be claimed

    def try_second_slot() -> None:
        with concurrency.analysis_slot("sirius"):
            second_entered.set()

    second = threading.Thread(target=try_second_slot)
    second.start()
    # The second sirius run must still be queued behind the first.
    assert not second_entered.wait(timeout=0.1)

    thread.join()
    second.join()
    assert second_entered.is_set()


def test_sirius_and_other_model_pools_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One sirius run and one other-model run must be able to hold their
    slots at the same time -- they're separate pools, not one shared one."""

    config = _config(tmp_path, analysis_max_concurrency=1)
    monkeypatch.setattr(concurrency, "get_config", lambda: config)

    sirius_running = threading.Event()
    other_running = threading.Event()
    both_overlapped = threading.Event()

    def run_sirius() -> None:
        with concurrency.analysis_slot("sirius"):
            sirius_running.set()
            if other_running.wait(timeout=1):
                both_overlapped.set()

    def run_other() -> None:
        sirius_running.wait(timeout=1)
        with concurrency.analysis_slot("flare"):
            other_running.set()
            time.sleep(0.1)

    t1 = threading.Thread(target=run_sirius)
    t2 = threading.Thread(target=run_other)
    t1.start()
    t2.start()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert both_overlapped.is_set()


def test_two_other_models_do_not_run_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, analysis_max_concurrency=1)
    monkeypatch.setattr(concurrency, "get_config", lambda: config)

    second_entered = threading.Event()

    def hold_other_slot() -> None:
        with concurrency.analysis_slot("flare"):
            time.sleep(0.3)

    thread = threading.Thread(target=hold_other_slot)
    thread.start()
    time.sleep(0.05)

    def try_second_other() -> None:
        with concurrency.analysis_slot("glacier"):
            second_entered.set()

    second = threading.Thread(target=try_second_other)
    second.start()
    assert not second_entered.wait(timeout=0.1)

    thread.join()
    second.join()
    assert second_entered.is_set()
