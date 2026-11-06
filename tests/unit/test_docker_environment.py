"""Unit tests for DockerPipelineEnvironment's command construction.

These assert on the argv docker commands build, not on LLM behavior: real
docker is never invoked here (subprocess.run is monkeypatched out).
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from app import docker_environment
from app.docker_environment import (
    DockerEnvironmentError,
    DockerPipelineEnvironment,
    InvalidStudyIdError,
    validate_study_id,
)
from app.pipeline_config import PipelineConfig


@pytest.fixture
def config(tmp_path: Path) -> PipelineConfig:
    return PipelineConfig(
        repo_root=tmp_path,
        agent_runs_root=tmp_path / "agent_runs",
        mwb_contact_email="researcher@example.edu",
        sirius_home_dir=None,
        analysis_max_concurrency=2,
        disk_budget_bytes=100_000_000_000,
        disk_reserve_bytes=50_000_000_000,
        docker_command_timeout_seconds=60.0,
        mwextract_image="mwextract:test",
        mwparse_mzml_image="mwparse-mzml:test",
        mwparse_mzxml_image="mwparse-mzxml:test",
        parse_count_floor_ratio=0.5,
        model_images={"flare": "mwannotate-flare:test", "sirius": "mwannotate-sirius:test"},
    )


@pytest.fixture
def environment(config: PipelineConfig) -> DockerPipelineEnvironment:
    return DockerPipelineEnvironment(config=config)


class _FakeCompleted:
    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = "ok"
        self.stderr = ""


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        calls.append(command)
        return _FakeCompleted()

    monkeypatch.setattr(docker_environment.subprocess, "run", fake_run)
    return calls


def test_validate_study_id_accepts_canonical_accession() -> None:
    assert validate_study_id("st004496") == "ST004496"


def test_validate_study_id_rejects_path_traversal() -> None:
    with pytest.raises(InvalidStudyIdError):
        validate_study_id("../../etc/passwd")


def test_fetch_requires_contact_email(config: PipelineConfig) -> None:
    config_without_email = dataclasses.replace(config, mwb_contact_email=None)
    environment = DockerPipelineEnvironment(config=config_without_email)
    with pytest.raises(DockerEnvironmentError):
        environment.fetch_workbench_study("ST004496")


def test_fetch_never_puts_email_literally_in_argv(
    environment: DockerPipelineEnvironment, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_run(monkeypatch)
    environment.fetch_workbench_study("ST004496", polarity=["negative"])
    assert len(calls) == 1
    command_text = " ".join(calls[0])
    assert "researcher@example.edu" not in command_text
    assert "--email-file" in calls[0]
    assert "--polarity" in calls[0] and "negative" in calls[0]


def test_list_studies_builds_expected_flags(
    environment: DockerPipelineEnvironment, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_run(monkeypatch)
    environment.list_workbench_studies(
        polarity=["negative"], instrument_types=["Orbitrap"], min_ms2_spectra=10,
    )
    command = calls[0]
    assert command[:3] == ["docker", "run", "--rm"]
    assert "--list" in command
    assert "--min-ms2-spectra" in command and "10" in command


def test_run_analysis_model_rejects_unknown_model(
    environment: DockerPipelineEnvironment,
) -> None:
    with pytest.raises(DockerEnvironmentError):
        environment.analysis_image_tag("not-a-real-model")


def test_run_analysis_model_requires_sirius_home_dir(
    environment: DockerPipelineEnvironment,
) -> None:
    (environment.study_output_dir("ST004496") / "processed" / "mzml").mkdir(parents=True)
    spectra = environment.parsed_output_dir("ST004496", "mzml") / "spectra.parquet"
    spectra.write_bytes(b"not-empty")
    (spectra.parent / "candidates.parquet").write_bytes(b"not-empty")

    with pytest.raises(DockerEnvironmentError, match="SIRIUS_HOME_DIR"):
        environment.run_analysis_model("sirius", "ST004496", run_id="r1")


def test_run_analysis_model_requires_parsed_spectra_first(
    environment: DockerPipelineEnvironment,
) -> None:
    with pytest.raises(DockerEnvironmentError, match="spectra.parquet"):
        environment.run_analysis_model("flare", "ST004496", run_id="r1")


def test_run_analysis_model_sirius_does_not_require_candidates_first(
    config: PipelineConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = dataclasses.replace(config, sirius_home_dir=tmp_path / "sirius-home")
    environment = DockerPipelineEnvironment(config=config)
    (environment.study_output_dir("ST004496") / "processed" / "mzml").mkdir(parents=True)
    spectra = environment.parsed_output_dir("ST004496", "mzml") / "spectra.parquet"
    spectra.write_bytes(b"not-empty")

    calls = _capture_run(monkeypatch)
    environment.run_analysis_model("sirius", "ST004496", run_id="r1")

    assert len(calls) == 1
    command = calls[0]
    assert "--candidates" not in command
    assert "--device" not in command
    assert "--spectra" in command and "--output-root" in command


def test_run_analysis_model_sirius_seeds_candidates_parquet_via_root_copy(
    config: PipelineConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The copy must happen inside a root container, not a host-side shutil
    # copy: parse_study_payload's output is sometimes root-owned in
    # practice, which a non-root host process cannot overwrite.
    config = dataclasses.replace(config, sirius_home_dir=tmp_path / "sirius-home")
    environment = DockerPipelineEnvironment(config=config)
    (environment.study_output_dir("ST004496") / "processed" / "mzml").mkdir(parents=True)
    spectra = environment.parsed_output_dir("ST004496", "mzml") / "spectra.parquet"
    spectra.write_bytes(b"not-empty")

    generated = environment.predictions_path("sirius", "ST004496", "r1").parent
    generated.mkdir(parents=True)
    (generated / "candidate_sets.parquet").write_bytes(b"sirius-candidates")

    calls = _capture_run(monkeypatch)
    environment.run_analysis_model("sirius", "ST004496", run_id="r1")

    assert len(calls) == 2
    copy_command = calls[1]
    assert "--entrypoint" in copy_command
    assert "sh" in copy_command
    copy_script = copy_command[-1]
    assert "candidate_sets.parquet" in copy_script
    assert copy_script.strip().endswith("candidates.parquet")


def test_run_analysis_model_sirius_raises_if_candidates_copy_fails(
    config: PipelineConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = dataclasses.replace(config, sirius_home_dir=tmp_path / "sirius-home")
    environment = DockerPipelineEnvironment(config=config)
    (environment.study_output_dir("ST004496") / "processed" / "mzml").mkdir(parents=True)
    spectra = environment.parsed_output_dir("ST004496", "mzml") / "spectra.parquet"
    spectra.write_bytes(b"not-empty")

    generated = environment.predictions_path("sirius", "ST004496", "r1").parent
    generated.mkdir(parents=True)
    (generated / "candidate_sets.parquet").write_bytes(b"sirius-candidates")

    calls: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        calls.append(command)
        completed = _FakeCompleted()
        if len(calls) == 2:
            completed.returncode = 1
            completed.stderr = "cp: permission denied"
        return completed

    monkeypatch.setattr(docker_environment.subprocess, "run", fake_run)

    with pytest.raises(DockerEnvironmentError, match="seeding candidates.parquet failed"):
        environment.run_analysis_model("sirius", "ST004496", run_id="r1")


def test_ensure_host_writable_dir_builds_mkdir_chown_command(
    environment: DockerPipelineEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_run(monkeypatch)
    target = tmp_path / "annotations" / "campaign_comparison"

    environment.ensure_host_writable_dir(target)

    assert len(calls) == 1
    command = calls[0]
    assert "--entrypoint" in command
    assert "sh" in command
    assert f"{target}:/target" in command
    script = command[-1]
    assert "mkdir -p /target" in script
    assert "chown -R" in script and "/target" in script


def test_ensure_host_writable_dir_raises_on_failure(
    environment: DockerPipelineEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        completed = _FakeCompleted()
        completed.returncode = 1
        completed.stderr = "chown: permission denied"
        return completed

    monkeypatch.setattr(docker_environment.subprocess, "run", fake_run)

    with pytest.raises(DockerEnvironmentError, match="Failed to prepare writable directory"):
        environment.ensure_host_writable_dir(tmp_path / "annotations" / "campaign_comparison")


def test_run_analysis_model_flare_still_requires_candidates_first(
    environment: DockerPipelineEnvironment,
) -> None:
    (environment.study_output_dir("ST004496") / "processed" / "mzml").mkdir(parents=True)
    spectra = environment.parsed_output_dir("ST004496", "mzml") / "spectra.parquet"
    spectra.write_bytes(b"not-empty")

    with pytest.raises(DockerEnvironmentError, match="candidates.parquet"):
        environment.run_analysis_model("flare", "ST004496", run_id="r1")


def test_raw_and_processed_roots_are_distinct(
    environment: DockerPipelineEnvironment,
) -> None:
    assert environment.raw_studies_root != environment.processed_studies_root
    assert environment.raw_study_dir("ST004496") != environment.study_output_dir("ST004496")


def test_timeout_is_reported_without_raising(
    environment: DockerPipelineEnvironment, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(docker_environment.subprocess, "run", fake_run)
    result = environment.list_workbench_studies()
    assert result.timed_out is True
    assert result.succeeded is False
