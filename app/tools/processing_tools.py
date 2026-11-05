"""Tools for the mass_spectrometry_payload_parser_agent.

Implements the parse step and the LOOP_CONTRACT delete-safety check: a raw
payload is only ever deleted after all four checks pass, and every outcome
(parsed, parse_failed, or deleted) is written to this harness's own ledger.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.app_utils.progress_bus import get_progress_bus
from app.disk_budget import check_disk_budget
from app.docker_environment import (
    DockerEnvironmentError,
    DockerPipelineEnvironment,
    InvalidStudyIdError,
)
from app.ledger import append_ledger_row, read_ledger_row

logger = logging.getLogger(__name__)

_environment = DockerPipelineEnvironment()


def parse_study_payload(
    study_id: str, file_format: str, candidates_sidecar: str | None = None,
) -> dict[str, Any]:
    """Parse one study's fetched payload into the shared spectra/candidates
    Parquet contract.

    Args:
        study_id: The Workbench accession, e.g. "ST004496".
        file_format: "mzml" or "mzxml" -- whichever format the fetched
            payload actually contains.
        candidates_sidecar: Optional host path to a pre-prepared candidates
            file (see models/README.md); without one, parsing still
            succeeds but produces zero candidates, which will block the
            analysis stage later.
    """

    logger.info("Processing of study %s: parsing %s payload...", study_id, file_format)
    get_progress_bus().publish(
        stage="processing", key=study_id, status="running",
        detail=f"parsing {file_format} payload...",
    )
    try:
        result = _environment.parse_study_payload(
            study_id, file_format=file_format,
            candidates_sidecar=candidates_sidecar,
        )
    except (InvalidStudyIdError, DockerEnvironmentError) as error:
        logger.info("Processing of study %s done: error", study_id)
        get_progress_bus().publish(
            stage="processing", key=study_id, status="error", detail=str(error),
        )
        return {"success": False, "status": "error", "error": str(error)}

    if result.returncode == 2:
        status = "no_spectra"
    elif result.succeeded:
        status = "parsed"
    else:
        status = "failed"
    logger.info("Processing of study %s done: %s", study_id, status)
    get_progress_bus().publish(
        stage="processing", key=study_id, status=status, detail=status,
    )

    append_ledger_row(
        study_id, "parse", state=status, file_format=file_format,
        returncode=result.returncode, elapsed_seconds=result.elapsed_seconds,
        stdout_tail=result.stdout_tail, stderr_tail=result.stderr_tail,
    )
    return {
        "success": status == "parsed",
        "status": status,
        "report": result.stdout_tail,
        "returncode": result.returncode,
        "stderr_tail": result.stderr_tail,
    }


def verify_parse_output(
    study_id: str, file_format: str, manifest_ms2_expected: int | None = None,
) -> dict[str, Any]:
    """Run LOOP_CONTRACT's 4-point delete-safety check.

    All four must pass before the raw payload may be deleted:
      1. the fetch stage's ledger record shows MWExtract exited 0.
      2. the raw payload's spectra/ directory has at least one file.
      3. processed/<format>/spectra.parquet exists and is non-empty.
      4. the parsed spectrum count is not below
         ``parse_count_floor_ratio * manifest_ms2_expected`` (a floor, not
         equality -- QC rejection means the parsed count is always <= the
         raw manifest count).

    Args:
        manifest_ms2_expected: Override for the expected MS2 count; if
            omitted, read from the fetch stage's own ledger record (the
            exact figure MWExtract reported when it fetched this study).
    """

    checks: dict[str, bool] = {}
    reasons: list[str] = []

    fetch_row = read_ledger_row(study_id, "fetch")
    checks["fetch_exit_zero"] = bool(fetch_row and fetch_row.get("returncode") == 0)
    if not checks["fetch_exit_zero"]:
        reasons.append("fetch stage did not record a clean exit (returncode 0).")

    if manifest_ms2_expected is None:
        manifest_ms2_expected = (fetch_row or {}).get("ms2_expected")
    if manifest_ms2_expected is None:
        reasons.append("no manifest_ms2_expected available from the fetch ledger.")

    spectra_dir = _environment.raw_study_dir(study_id) / "spectra"
    has_spectra_files = spectra_dir.is_dir() and any(spectra_dir.iterdir())
    checks["raw_spectra_present"] = has_spectra_files
    if not has_spectra_files:
        reasons.append(f"no files found under {spectra_dir}.")

    parsed_parquet = _environment.parsed_output_dir(study_id, file_format) / "spectra.parquet"
    non_empty = parsed_parquet.is_file() and parsed_parquet.stat().st_size > 0
    checks["parsed_parquet_non_empty"] = non_empty
    if not non_empty:
        reasons.append(f"{parsed_parquet} is missing or empty.")

    parsed_count = None
    if non_empty:
        report_path = parsed_parquet.parent / "parse_report.json"
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            parsed_count = report.get("counts", {}).get("ms2_spectra")

    floor = (
        _environment.config.parse_count_floor_ratio * manifest_ms2_expected
        if manifest_ms2_expected is not None else None
    )
    checks["parsed_count_above_floor"] = (
        parsed_count is not None and floor is not None and parsed_count >= floor
    )
    if not checks["parsed_count_above_floor"] and floor is not None:
        reasons.append(
            f"parsed spectrum count {parsed_count} is below the floor "
            f"{floor:.0f} ({_environment.config.parse_count_floor_ratio:.0%} of "
            f"manifest_ms2_expected={manifest_ms2_expected})."
        )

    all_passed = all(checks.values())
    return {
        "all_passed": all_passed,
        "checks": checks,
        "reasons": reasons,
        "parsed_count": parsed_count,
    }


def delete_raw_payload_if_verified(
    study_id: str, file_format: str, manifest_ms2_expected: int | None = None,
) -> dict[str, Any]:
    """Delete the raw payload only if verify_parse_output passes; otherwise
    keep it and mark the study parse_failed so a later retry can resume
    without re-downloading."""

    verification = verify_parse_output(study_id, file_format, manifest_ms2_expected)
    if not verification["all_passed"]:
        append_ledger_row(
            study_id, "delete", state="parse_failed",
            reasons=verification["reasons"], checks=verification["checks"],
        )
        return {"deleted": False, "status": "parse_failed", **verification}

    before = check_disk_budget()
    freed_bytes = _environment.delete_raw_study_payload(study_id)
    after = check_disk_budget()
    append_ledger_row(
        study_id, "delete", state="deleted", freed_bytes=freed_bytes,
        disk_free_bytes_before=before.disk_free_bytes,
        disk_free_bytes_after=after.disk_free_bytes,
    )
    return {
        "deleted": True, "status": "deleted", "freed_bytes": freed_bytes,
        **verification,
    }
