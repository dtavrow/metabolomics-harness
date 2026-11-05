"""The one place every `docker run` invocation in this harness goes through.

No tool function calls ``subprocess`` directly. Every method here builds its
command from a fixed, typed argument list -- never a raw shell string -- and
every host path derived from model-supplied input is validated before it is
mounted into a container. This mirrors the allowlist discipline
``download_worker.py`` already enforces for the existing job-queue bridge,
and the ``Environment`` abstraction pattern studied from the
``long-horizon-harness`` recipe (tools call one interface, never the host).

Path layout (verified against MWExtract.py and the parser/model_runner
source, not assumed): both MWExtract and the parser append ``<study_id>/...``
onto whatever output root they are given. This harness uses two separate
roots, not one:

- ``raw/`` -- MWExtract's payload (``spectra/`` + ``<STUDY_ID>.parquet``).
  Ephemeral: deleted once a parse is verified.
- ``processed/`` -- the parser's durable output
  (``<STUDY_ID>/processed/<format>/{spectra,candidates}.parquet``) and, after
  analysis, ``<STUDY_ID>/annotations/<model>/<run_id>/predictions.parquet``.

Keeping them apart means LOOP_CONTRACT's "one recursive delete" of the raw
payload can never also delete the processed data the analysis stage reads.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.pipeline_config import PipelineConfig, get_config

STUDY_ID_PATTERN = re.compile(r"^ST\d{6}$", re.IGNORECASE)

#: Container mount point for the anonymous-FTP contact address. Mounted as a
#: file, never passed as a CLI argument or written to any log or ledger.
_EMAIL_SECRET_MOUNT = "/run/secrets/mwb_email"

#: Pinned mount point for the SIRIUS login workspace, matching the version
#: pinned in models/README.md.
_SIRIUS_WORKSPACE_MOUNT = "/root/.sirius-6.5"


class InvalidStudyIdError(ValueError):
    """Raised when a study id does not match the corpus's accession format."""


class DockerEnvironmentError(RuntimeError):
    """Raised for configuration or missing-prerequisite problems caught
    before a container ever runs."""


@dataclass(frozen=True)
class DockerCommandResult:
    command: list[str]
    returncode: int
    stdout_tail: str
    stderr_tail: str
    elapsed_seconds: float
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def validate_study_id(study_id: str) -> str:
    """Reject anything that is not a bare Workbench accession.

    This is the harness's path-traversal guard: every per-study host
    directory is built from this value, so constraining its shape to
    ``ST######`` makes a directory-escaping study id structurally
    impossible, rather than relying on after-the-fact path checks.
    """

    candidate = str(study_id).strip().upper()
    if not STUDY_ID_PATTERN.match(candidate):
        raise InvalidStudyIdError(
            f"{study_id!r} is not a Workbench study accession (expected STxxxxxx)."
        )
    return candidate


class DockerPipelineEnvironment:
    """Typed, allowlisted wrapper around every pipeline-stage container."""

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.config = config or get_config()

    # -- shared paths -------------------------------------------------------

    @property
    def raw_studies_root(self) -> Path:
        """Mount root for MWExtract's raw payload -- deliberately separate
        from ``processed_studies_root`` so deleting a raw payload after a
        verified parse (LOOP_CONTRACT's "one recursive delete of a path
        predictable before the run starts") can never also delete the
        durable parsed output the analysis stage depends on."""

        root = self.config.agent_runs_root / "raw"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @property
    def processed_studies_root(self) -> Path:
        """Mount root for parsed spectra/candidates -- durable, kept after
        the raw payload is deleted."""

        root = self.config.agent_runs_root / "processed"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def raw_study_dir(self, study_id: str) -> Path:
        """MWExtract's own output for one study: ``spectra/`` plus the
        catalog-slice ``<STUDY_ID>.parquet``."""

        return self.raw_studies_root / validate_study_id(study_id)

    def study_output_dir(self, study_id: str) -> Path:
        """Everything durable this harness has produced for one study."""

        return self.processed_studies_root / validate_study_id(study_id)

    def parsed_output_dir(self, study_id: str, file_format: str) -> Path:
        return self.study_output_dir(study_id) / "processed" / file_format

    def delete_raw_study_payload(self, study_id: str) -> int:
        """Recursively delete one study's raw payload directory.

        Returns the number of bytes freed. Callers must have already run
        the LOOP_CONTRACT delete-safety check; this method does not
        re-verify anything, by design -- it is the one place the actual
        irreversible removal happens.
        """

        target = self.raw_study_dir(study_id)
        freed = sum(
            entry.stat().st_size for entry in target.rglob("*") if entry.is_file()
        ) if target.exists() else 0
        shutil.rmtree(target, ignore_errors=True)
        return freed

    # -- extraction -----------------------------------------------------

    def list_workbench_studies(
        self,
        *,
        polarity: Sequence[str] | None = None,
        instrument_types: Sequence[str] | None = None,
        acquisition_types: Sequence[str] | None = None,
        min_ms2_spectra: int | None = None,
    ) -> DockerCommandResult:
        args = ["--list"]
        args += _repeated_flag("--polarity", polarity)
        args += _repeated_flag("--instrument-types", instrument_types)
        args += _repeated_flag("--acquisition-types", acquisition_types)
        if min_ms2_spectra is not None:
            args += ["--min-ms2-spectra", str(min_ms2_spectra)]
        return self._run_mwextract(args, mounts=[], timeout=120.0)

    def estimate_study_cost(
        self,
        study_id: str,
        *,
        polarity: Sequence[str] | None = None,
        instrument_types: Sequence[str] | None = None,
    ) -> DockerCommandResult:
        study_id = validate_study_id(study_id)
        args = ["--study", study_id, "--dry-run", "--output", "/output"]
        args += _repeated_flag("--polarity", polarity)
        args += _repeated_flag("--instrument-types", instrument_types)
        mounts = [f"{self.raw_studies_root}:/output"]
        return self._run_mwextract(args, mounts=mounts, timeout=120.0)

    def fetch_workbench_study(
        self,
        study_id: str,
        *,
        polarity: Sequence[str] | None = None,
        instrument_types: Sequence[str] | None = None,
        acquisition_types: Sequence[str] | None = None,
        min_ms2_spectra: int | None = None,
        max_download_bytes: int | None = None,
        max_uncompressed_bytes: int | None = None,
    ) -> DockerCommandResult:
        if not self.config.mwb_contact_email:
            raise DockerEnvironmentError(
                "MWB_CONTACT_EMAIL is not configured; set it in .env before "
                "fetching a study (the Workbench FTP uses it as the anonymous "
                "login password)."
            )
        study_id = validate_study_id(study_id)
        args = ["--study", study_id, "--output", "/output",
                "--email-file", _EMAIL_SECRET_MOUNT]
        args += _repeated_flag("--polarity", polarity)
        args += _repeated_flag("--instrument-types", instrument_types)
        args += _repeated_flag("--acquisition-types", acquisition_types)
        if min_ms2_spectra is not None:
            args += ["--min-ms2-spectra", str(min_ms2_spectra)]
        if max_download_bytes is not None:
            args += ["--max-download-bytes", str(max_download_bytes)]
        if max_uncompressed_bytes is not None:
            args += ["--max-uncompressed-bytes", str(max_uncompressed_bytes)]

        with tempfile.TemporaryDirectory(prefix="mwb-email-") as secret_dir:
            secret_path = Path(secret_dir) / "mwb_email"
            secret_path.write_text(self.config.mwb_contact_email, encoding="utf-8")
            secret_path.chmod(0o400)
            mounts = [
                f"{self.raw_studies_root}:/output",
                f"{secret_path}:{_EMAIL_SECRET_MOUNT}:ro",
            ]
            return self._run_mwextract(
                args, mounts=mounts,
                timeout=self.config.docker_command_timeout_seconds,
            )

    # -- processing -------------------------------------------------------

    def parse_study_payload(
        self, study_id: str, *, file_format: str, candidates_sidecar: Path | None = None,
    ) -> DockerCommandResult:
        if file_format not in {"mzml", "mzxml"}:
            raise DockerEnvironmentError(f"Unsupported parse format: {file_format!r}")
        study_id = validate_study_id(study_id)
        image = (
            self.config.mwparse_mzml_image
            if file_format == "mzml"
            else self.config.mwparse_mzxml_image
        )
        args = ["--study-dir", f"/raw/{study_id}", "--output-root", "/data"]
        mounts = [
            f"{self.raw_studies_root}:/raw:ro",
            f"{self.processed_studies_root}:/data",
        ]
        if candidates_sidecar is not None:
            args += ["--candidates", "/candidates-sidecar"]
            mounts.append(f"{candidates_sidecar}:/candidates-sidecar:ro")
        return self._execute(
            image, args, mounts=mounts,
            timeout=self.config.docker_command_timeout_seconds,
        )

    # -- analysis ---------------------------------------------------------

    def analysis_image_tag(self, model_name: str) -> str:
        try:
            return self.config.model_images[model_name]
        except KeyError as error:
            raise DockerEnvironmentError(
                f"Unknown analysis model {model_name!r}; expected one of "
                f"{sorted(self.config.model_images)}."
            ) from error

    def image_exists(self, image_tag: str) -> bool:
        result = subprocess.run(
            ["docker", "image", "inspect", image_tag],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0

    def run_analysis_model(
        self,
        model_name: str,
        study_id: str,
        *,
        run_id: str,
        device: str = "cpu",
        batch_size: int = 32,
        instrument_type: str = "Orbitrap",
        checkpoint_bundle: Path | None = None,
    ) -> DockerCommandResult:
        study_id = validate_study_id(study_id)
        image = self.analysis_image_tag(model_name)
        study_dir = self.study_output_dir(study_id)

        spectra_path = self._require_existing(
            self.parsed_output_dir(study_id, "mzml") / "spectra.parquet",
            fallback=self.parsed_output_dir(study_id, "mzxml") / "spectra.parquet",
            missing_hint=(
                "Run the processing stage first (parse_study_payload); no "
                "parsed spectra.parquet was found for this study."
            ),
        )
        candidates_path = self._require_existing(
            spectra_path.parent / "candidates.parquet",
            missing_hint=(
                "No candidates.parquet next to the parsed spectra. Candidate "
                "preparation (resolving structures/formulas via PubChem) is "
                "not automated by this harness yet -- see "
                "models/README.md and prepare_workbench_candidates.py, and "
                "pass its output as parse_study_payload's candidates_sidecar."
            ),
        )

        args = [
            "--spectra", f"/data/{spectra_path.relative_to(study_dir)}",
            "--candidates", f"/data/{candidates_path.relative_to(study_dir)}",
            "--output-root", "/data", "--run-id", run_id,
            "--device", device, "--batch-size", str(batch_size),
            "--instrument-type", instrument_type,
        ]
        mounts = [f"{study_dir}:/data"]

        if model_name == "sirius":
            if self.config.sirius_home_dir is None:
                raise DockerEnvironmentError(
                    "SIRIUS_HOME_DIR is not configured; run `sirius login` once "
                    "per models/README.md and point SIRIUS_HOME_DIR at that "
                    "workspace before requesting the sirius model."
                )
            mounts.append(f"{self.config.sirius_home_dir}:{_SIRIUS_WORKSPACE_MOUNT}:ro")

        if checkpoint_bundle is not None:
            args += ["--checkpoint-bundle", "/checkpoint"]
            mounts.append(f"{checkpoint_bundle}:/checkpoint:ro")

        return self._execute(
            image, args, mounts=mounts, gpu=(device == "cuda"),
            timeout=self.config.docker_command_timeout_seconds,
        )

    def predictions_path(self, model_name: str, study_id: str, run_id: str) -> Path:
        """Where ``run_analysis_model`` leaves its output, per
        model_runner.py's own ``output_root/study_id/annotations/model/run_id``
        convention applied against this harness's per-study mount."""

        study_id = validate_study_id(study_id)
        return (
            self.study_output_dir(study_id) / study_id / "annotations"
            / model_name / run_id / "predictions.parquet"
        )

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _require_existing(
        path: Path, *, fallback: Path | None = None, missing_hint: str | None = None,
    ) -> Path:
        if path.is_file():
            return path
        if fallback is not None and fallback.is_file():
            return fallback
        detail = f" {missing_hint}" if missing_hint else ""
        raise DockerEnvironmentError(f"Expected input file not found: {path}.{detail}")

    def _run_mwextract(
        self, args: list[str], *, mounts: list[str], timeout: float,
    ) -> DockerCommandResult:
        return self._execute(self.config.mwextract_image, args, mounts=mounts, timeout=timeout)

    def _execute(
        self,
        image: str,
        container_args: list[str],
        *,
        mounts: list[str],
        timeout: float,
        gpu: bool = False,
    ) -> DockerCommandResult:
        command = ["docker", "run", "--rm"]
        if gpu:
            command.append("--gpus=all")
        for mount in mounts:
            command += ["-v", mount]
        command.append(image)
        command += container_args

        started = time.monotonic()
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout,
            )
            return DockerCommandResult(
                command=command,
                returncode=completed.returncode,
                stdout_tail=_tail(completed.stdout),
                stderr_tail=_tail(completed.stderr),
                elapsed_seconds=round(time.monotonic() - started, 1),
            )
        except subprocess.TimeoutExpired as error:
            return DockerCommandResult(
                command=command,
                returncode=-1,
                stdout_tail=_tail(error.stdout.decode() if error.stdout else ""),
                stderr_tail=_tail(error.stderr.decode() if error.stderr else ""),
                elapsed_seconds=round(time.monotonic() - started, 1),
                timed_out=True,
            )


def _repeated_flag(flag: str, values: Sequence[str] | None) -> list[str]:
    if not values:
        return []
    return [flag, *[str(value) for value in values]]
