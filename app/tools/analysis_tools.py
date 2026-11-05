"""Shared by every scoring-model CLI subcommand (flare/glacier/iceberg/jestr/
sirius/evidence).

The concurrency cap lives here, at the point where a container is actually
about to run: every model run is its own CLI process, so the cap is a
cross-process file lock (app/concurrency.py), not an in-process primitive.
"""

from __future__ import annotations

import logging
from typing import Any

from app.app_utils.progress_bus import get_progress_bus
from app.concurrency import analysis_slot
from app.docker_environment import (
    DockerEnvironmentError,
    DockerPipelineEnvironment,
    InvalidStudyIdError,
)
from app.ledger import append_ledger_row

logger = logging.getLogger(__name__)

_environment = DockerPipelineEnvironment()

#: model_name -> human description, for CLI help text and validation.
WORKER_SPECS: dict[str, str] = {
    "flare": "Scores candidates for one study with the FLARE fragmentation model.",
    "glacier": "Scores candidates for one study with the GLACIER fragmentation model.",
    "iceberg": "Scores candidates for one study with the ICEBERG fragmentation model.",
    "jestr": (
        "Scores candidates for one study with the JESTR spectrum-candidate "
        "matching model."
    ),
    "sirius": "Scores candidates for one study with SIRIUS structure discovery.",
    "evidence": "Scores candidates for one study with the MS/MS evidence model.",
}


def check_model_ready(model_name: str) -> dict[str, Any]:
    """Fail-fast prerequisite check: is this model's Docker image built, and
    (for SIRIUS) is a login workspace configured?

    Call this before run_analysis_model so a missing prerequisite surfaces
    as a clear, actionable message instead of an opaque Docker error.
    """

    try:
        image = _environment.analysis_image_tag(model_name)
    except DockerEnvironmentError as error:
        return {"ready": False, "reason": str(error)}

    if not _environment.image_exists(image):
        return {
            "ready": False,
            "reason": (
                f"Docker image {image!r} is not built locally. See "
                "models/README.md for the exact `docker build` command for "
                f"{model_name}."
            ),
        }
    if model_name == "sirius" and _environment.config.sirius_home_dir is None:
        return {
            "ready": False,
            "reason": (
                "SIRIUS_HOME_DIR is not configured. Run `sirius login` once "
                "per models/README.md, then set SIRIUS_HOME_DIR in .env."
            ),
        }
    return {"ready": True, "reason": None}


def run_analysis_model(
    model_name: str,
    study_id: str,
    run_id: str,
    device: str = "cpu",
    batch_size: int = 32,
    instrument_type: str = "Orbitrap",
) -> dict[str, Any]:
    """Run one scoring model against one study's parsed spectra/candidates.

    Args:
        model_name: One of flare, glacier, iceberg, jestr, sirius, evidence.
        study_id: The Workbench accession this study's parsed output belongs to.
        run_id: A label for this run, used in the output path; reuse it
            across models in the same campaign so results are comparable.
        device: "cpu" or "cuda".
        batch_size: Backend inference batch size.
        instrument_type: Recorded in the run report; defaults to Orbitrap,
            matching the MassSpecGym checkpoint inputs.
    """

    if model_name not in WORKER_SPECS:
        raise ValueError(
            f"Unknown analysis model {model_name!r}; expected one of "
            f"{sorted(WORKER_SPECS)}."
        )

    readiness = check_model_ready(model_name)
    if not readiness["ready"]:
        logger.info(
            "Evaluation of study %s on %s done: not ready", study_id, model_name,
        )
        get_progress_bus().publish(
            stage="scoring", key=f"{study_id}:{model_name}", status="not_ready",
            detail=readiness["reason"],
        )
        return {"success": False, "status": "not_ready", "error": readiness["reason"]}

    logger.info("Evaluation of study %s on %s: starting...", study_id, model_name)
    get_progress_bus().publish(
        stage="scoring", key=f"{study_id}:{model_name}", status="running",
        detail="starting...",
    )
    with analysis_slot():
        try:
            result = _environment.run_analysis_model(
                model_name, study_id, run_id=run_id, device=device,
                batch_size=batch_size, instrument_type=instrument_type,
            )
        except (InvalidStudyIdError, DockerEnvironmentError) as error:
            logger.info("Evaluation of study %s on %s done: error", study_id, model_name)
            get_progress_bus().publish(
                stage="scoring", key=f"{study_id}:{model_name}", status="error",
                detail=str(error),
            )
            return {"success": False, "status": "error", "error": str(error)}

    predictions_path = _environment.predictions_path(model_name, study_id, run_id)
    success = result.succeeded and predictions_path.is_file()
    logger.info(
        "Evaluation of study %s on %s done: %s",
        study_id, model_name, "succeeded" if success else "failed",
    )
    get_progress_bus().publish(
        stage="scoring", key=f"{study_id}:{model_name}",
        status="succeeded" if success else "failed",
        detail="succeeded" if success else "failed",
    )
    append_ledger_row(
        study_id, f"analysis_{model_name}",
        state="succeeded" if success else "failed",
        run_id=run_id, returncode=result.returncode,
        elapsed_seconds=result.elapsed_seconds,
        stdout_tail=result.stdout_tail, stderr_tail=result.stderr_tail,
    )
    return {
        "success": success,
        "status": "succeeded" if success else "failed",
        "model_name": model_name,
        "predictions_path": str(predictions_path),
        "report": result.stdout_tail,
        "returncode": result.returncode,
        "stderr_tail": result.stderr_tail,
    }
