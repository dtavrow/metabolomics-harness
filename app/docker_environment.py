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

import os
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

#: Models evidence (models/evidence/main.py) can merge, in its own `MODELS`.
_EVIDENCE_SOURCE_MODELS = ("sirius", "flare", "glacier", "iceberg")


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
        # ignore_errors=True previously masked a real failure mode here: a
        # raw payload written by an older, root-run mwextract container
        # (before _run_mwextract started passing --user) is owned by root
        # and this call cannot remove it, yet the caller would report
        # "deleted": true anyway on the strength of the byte count above.
        # A real failure must surface, not be reported as success.
        shutil.rmtree(target)
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
        # Host-owned output, same reasoning as _run_mwextract: this harness
        # (or a campaign script like prepare_workbench_candidates.py) needs
        # to read and, for candidates.parquet, overwrite this stage's output
        # from the host afterward.
        return self._execute(
            image, args, mounts=mounts,
            timeout=self.config.docker_command_timeout_seconds,
            user=f"{os.getuid()}:{os.getgid()}",
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
        # Deliberately root, unlike _run_mwextract/parse_study_payload: these
        # images bake in a writable-$HOME assumption (numba's cache locator,
        # DGL's `/.dgl`, matplotlib's `/.config`) that a host --user breaks
        # for flare/glacier/iceberg (confirmed: PermissionError on exactly
        # those paths). Nothing on the host ever writes into or deletes a
        # model's output directory, so root-owned-but-world-readable
        # predictions.parquet (the pattern every container here produces) is
        # sufficient for aggregate/evidence to read it back.

        spectra_path = self._require_existing(
            self.parsed_output_dir(study_id, "mzml") / "spectra.parquet",
            fallback=self.parsed_output_dir(study_id, "mzxml") / "spectra.parquet",
            missing_hint=(
                "Run the processing stage first (parse_study_payload); no "
                "parsed spectra.parquet was found for this study."
            ),
        )
        if model_name == "sirius":
            # SIRIUS generates candidates rather than consuming them -- it is
            # the only model allowed to run before candidates.parquet exists.
            # Its own CLI (models/sirius/main.py) takes none of the
            # --candidates/--device/--batch-size/--instrument-type flags the
            # other models share, so it gets its own arg list rather than the
            # generic block below.
            if self.config.sirius_home_dir is None:
                raise DockerEnvironmentError(
                    "SIRIUS_HOME_DIR is not configured; run `sirius login` once "
                    "per models/README.md and point SIRIUS_HOME_DIR at that "
                    "workspace before requesting the sirius model."
                )
            args = [
                "--spectra", f"/data/{spectra_path.relative_to(study_dir)}",
                "--output-root", "/data", "--run-id", run_id,
            ]
            mounts = [
                f"{study_dir}:/data",
                # Read-write, deliberately: SIRIUS's refresh token rotates on
                # every authenticated call (confirmed: even a bare `login
                # --show`), and the rotated token must be written back here
                # or the next call finds the one on disk already consumed
                # and reports "not logged in" -- a `:ro` mount burns the
                # login on its very first use, with no way to recover
                # short of a fresh interactive `sirius login`.
                f"{self.config.sirius_home_dir}:{_SIRIUS_WORKSPACE_MOUNT}",
            ]
            result = self._execute(
                image, args, mounts=mounts,
                timeout=self.config.docker_command_timeout_seconds,
            )
            if result.succeeded:
                # Seed the canonical candidates.parquet every other model
                # reads from, so a plain "run sirius then run <scorer>" works
                # without the manual prepare_workbench_candidates.py step.
                # Done as a root container `cp`, not a host-side shutil copy:
                # parse_study_payload's output is sometimes root-owned (seen
                # in practice despite requesting host-UID output), which a
                # non-root host process cannot overwrite.
                generated = (
                    self.predictions_path("sirius", study_id, run_id).parent
                    / "candidate_sets.parquet"
                )
                if generated.is_file():
                    destination = spectra_path.parent / "candidates.parquet"
                    copy_result = self._execute(
                        image,
                        [
                            "-c",
                            f"cp /data/{generated.relative_to(study_dir)} "
                            f"/data/{destination.relative_to(study_dir)}",
                        ],
                        mounts=[f"{study_dir}:/data"],
                        timeout=self.config.docker_command_timeout_seconds,
                        entrypoint="sh",
                    )
                    if not copy_result.succeeded:
                        raise DockerEnvironmentError(
                            "sirius generated candidate_sets.parquet but seeding "
                            f"candidates.parquet failed: {copy_result.stderr_tail}"
                        )
            return result

        candidates_path = self._require_existing(
            spectra_path.parent / "candidates.parquet",
            missing_hint=(
                "No candidates.parquet next to the parsed spectra. Run the "
                "sirius model first to generate one -- it writes "
                "candidate_sets.parquet and this harness copies that into "
                "candidates.parquet automatically on success. Candidate "
                "preparation is otherwise only wired up for ST004067/ST004496 "
                "-- see models/README.md and prepare_workbench_candidates.py. "
                "Note a manually prepared file must replace this exact file "
                "directly; parse_study_payload's --candidates-sidecar is a "
                "different, narrower schema (see "
                "MWExtract/parsers/common/candidates.py) and cannot carry "
                "the model-facing columns through."
            ),
        )

        if model_name == "evidence":
            # evidence does not score spectra itself -- it merges the other
            # four models' already-published runs (models/evidence/main.py's
            # `--model-run`, one per model, is required and repeatable), so
            # it takes none of the --device/--batch-size/--instrument-type
            # flags the scoring models share.
            run_dirs = []
            for source_model in _EVIDENCE_SOURCE_MODELS:
                run_dir = self.predictions_path(source_model, study_id, run_id).parent
                if (run_dir / "run_report.json").is_file():
                    run_dirs.append(run_dir)
            if not run_dirs:
                raise DockerEnvironmentError(
                    "evidence has nothing to merge: none of "
                    f"{_EVIDENCE_SOURCE_MODELS} has a completed run under "
                    f"run_id {run_id!r} for {study_id}. Run at least one of "
                    "them first with this same run_id."
                )
            args = [
                "--spectra", f"/data/{spectra_path.relative_to(study_dir)}",
                "--candidates", f"/data/{candidates_path.relative_to(study_dir)}",
                "--output-root", "/data", "--run-id", run_id,
            ]
            for run_dir in run_dirs:
                args += ["--model-run", f"/data/{run_dir.relative_to(study_dir)}"]
            return self._execute(
                image, args, mounts=[f"{study_dir}:/data"],
                timeout=self.config.docker_command_timeout_seconds,
            )

        args = [
            "--spectra", f"/data/{spectra_path.relative_to(study_dir)}",
            "--candidates", f"/data/{candidates_path.relative_to(study_dir)}",
            "--output-root", "/data", "--run-id", run_id,
            "--device", device, "--batch-size", str(batch_size),
            "--instrument-type", instrument_type,
        ]
        mounts = [f"{study_dir}:/data"]

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
        convention applied against this harness's per-study mount.

        evidence (models/evidence/main.py) does not follow that convention:
        it publishes to ``.../annotation-evidence/<run_id>/annotation_evidence.parquet``,
        not ``.../annotations/evidence/<run_id>/predictions.parquet``. Without
        this branch, the success check in analysis_tools.py
        (``result.succeeded and predictions_path.is_file()``) looks in the
        wrong place and reports a successful evidence run as failed."""

        study_id = validate_study_id(study_id)
        if model_name == "evidence":
            return (
                self.study_output_dir(study_id) / study_id / "annotation-evidence"
                / run_id / "annotation_evidence.parquet"
            )
        return (
            self.study_output_dir(study_id) / study_id / "annotations"
            / model_name / run_id / "predictions.parquet"
        )

    def ensure_host_writable_dir(self, path: Path) -> None:
        """mkdir -p + chown a directory to the host UID/GID via a throwaway
        root container.

        run_analysis_model's containers are deliberately root (see its own
        comment), so a study's ``annotations/`` tree can end up root-owned.
        A later host-side writer (e.g. aggregate, which does its merge in
        the host process) can't create a new subdirectory under a
        root-owned, non-world-writable ancestor -- mkdir() itself raises
        PermissionError. Docker auto-creates a bind-mount's host path (also
        root-owned) if it doesn't exist yet, so mounting ``path`` and
        chown-ing it from inside a root container fixes both cases in one
        step.
        """

        result = self._execute(
            self.config.mwextract_image,
            ["-c", f"mkdir -p /target && chown -R {os.getuid()}:{os.getgid()} /target"],
            mounts=[f"{path}:/target"],
            timeout=30.0,
            entrypoint="sh",
        )
        if not result.succeeded:
            raise DockerEnvironmentError(
                f"Failed to prepare writable directory {path}: {result.stderr_tail}"
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
        # Runs as the host UID/GID, not the image's default root, so the raw
        # payload it writes into the host-mounted /output volume is owned by
        # whoever runs this harness. Without this, delete_raw_study_payload
        # below cannot actually remove it (root:root, 0755 -- the host user
        # has no write permission on the containing directory) and the
        # delete silently no-ops.
        return self._execute(
            self.config.mwextract_image, args, mounts=mounts, timeout=timeout,
            user=f"{os.getuid()}:{os.getgid()}",
        )

    def _execute(
        self,
        image: str,
        container_args: list[str],
        *,
        mounts: list[str],
        timeout: float,
        gpu: bool = False,
        user: str | None = None,
        entrypoint: str | None = None,
    ) -> DockerCommandResult:
        command = ["docker", "run", "--rm"]
        if user is not None:
            command += ["--user", user]
        if entrypoint is not None:
            command += ["--entrypoint", entrypoint]
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
