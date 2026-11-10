"""Extraction-stage operations, invoked by app/cli.py.

Each function is a thin, typed translation from one CLI subcommand to one
``DockerPipelineEnvironment`` method -- no function here calls
``docker``/``subprocess`` itself.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import asdict
from typing import Any

import pyarrow.parquet as pq

from app.app_utils.progress_bus import get_progress_bus
from app.disk_budget import check_disk_budget
from app.docker_environment import (
    DockerEnvironmentError,
    DockerPipelineEnvironment,
    InvalidStudyIdError,
)
from app.ledger import append_ledger_row

logger = logging.getLogger(__name__)

_environment = DockerPipelineEnvironment()

#: MWExtract prints "<study>: N files, M MS2 spectra, ..." on a successful
#: fetch (see MWExtract.py's main()). Reading the expected count from this
#: report, once, at fetch time keeps the LOOP_CONTRACT delete-safety check
#: from having to re-derive it from the catalog slice later.
_MS2_COUNT_PATTERN = re.compile(r"([\d,]+)\s+MS2 spectra")


def _extract_ms2_expected(report_text: str) -> int | None:
    match = _MS2_COUNT_PATTERN.search(report_text)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def _instrument_polarity_counts(
    catalog_slice_path,
    polarity: list[str] | None,
    instrument_types: list[str] | None,
    min_ms2_spectra: int | None,
) -> dict[str, dict[str, int]]:
    """Spectra-extracted counts by (instrument_type, polarity) for one
    study's fetch, for the dashboard's heatmap.

    MWExtract writes ``<STUDY_ID>.parquet`` as every file the corpus index
    has for this study -- it is NOT narrowed to the files this particular
    fetch actually selected (that filtered set only ever exists in a
    temporary in-container workspace and never reaches the host; see
    MWExtract/modelfiles/MWExtract.py's ``in_study`` vs ``kept``). Re-apply
    the same polarity/instrument-type/min-ms2 filters this fetch call itself
    used, so the counts match what was actually fetched whenever filters
    were given (the common case per CLAUDE.md's workflow is no filters at
    all, where this is exact by construction either way).
    """

    if not catalog_slice_path.is_file():
        return {}
    rows = pq.read_table(
        catalog_slice_path,
        columns=["instrument_type", "polarity", "ms2_spectrum_count"],
    ).to_pylist()
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        # A null instrument_type/polarity means the source metadata didn't
        # record one -- skip rather than emit a None dict key, which would
        # later crash json.dump(..., sort_keys=True) comparing None to str.
        if row["instrument_type"] is None or row["polarity"] is None:
            continue
        if polarity and row["polarity"] not in polarity:
            continue
        if instrument_types and row["instrument_type"] not in instrument_types:
            continue
        if min_ms2_spectra and (row["ms2_spectrum_count"] or 0) < min_ms2_spectra:
            continue
        counts[row["instrument_type"]][row["polarity"]] += row["ms2_spectrum_count"] or 0
    return {instrument: dict(polarities) for instrument, polarities in counts.items()}


def list_workbench_studies(
    polarity: list[str] | None = None,
    instrument_types: list[str] | None = None,
    acquisition_types: list[str] | None = None,
    min_ms2_spectra: int | None = None,
) -> dict[str, Any]:
    """List studies matching catalog filters -- index only, no network, no download.

    Args:
        polarity: Restrict to these polarities, e.g. ["negative"].
        instrument_types: Restrict to these instrument classes, e.g. ["Orbitrap"].
        acquisition_types: Restrict to these acquisition types; defaults to
            MWExtract's own default of product-ion scans if omitted.
        min_ms2_spectra: Drop studies with fewer MS2 spectra than this.

    Returns:
        A dict with ``success``, ``accessions`` (the raw work-list text, one
        accession per line with a commented summary), and the raw command result.
    """

    result = _environment.list_workbench_studies(
        polarity=polarity,
        instrument_types=instrument_types,
        acquisition_types=acquisition_types,
        min_ms2_spectra=min_ms2_spectra,
    )
    return {
        "success": result.succeeded,
        "accessions": result.stdout_tail,
        "returncode": result.returncode,
        "stderr_tail": result.stderr_tail,
    }


def estimate_study_cost(
    study_id: str,
    polarity: list[str] | None = None,
    instrument_types: list[str] | None = None,
) -> dict[str, Any]:
    """Report one study's download/extracted size without fetching it.

    Args:
        study_id: The Workbench accession, e.g. "ST004496".
        polarity: Restrict the cost estimate to these polarities.
        instrument_types: Restrict the cost estimate to these instrument classes.
    """

    try:
        result = _environment.estimate_study_cost(
            study_id, polarity=polarity, instrument_types=instrument_types,
        )
    except InvalidStudyIdError as error:
        return {"success": False, "status": "invalid_study_id", "error": str(error)}
    return {
        "success": result.succeeded,
        "status": "estimated" if result.succeeded else "failed",
        "report": result.stdout_tail,
        "returncode": result.returncode,
        "stderr_tail": result.stderr_tail,
    }


def check_disk_budget_before_fetch(required_bytes: float = 0.0) -> dict[str, Any]:
    """Check whether fetching ``required_bytes`` more fits the configured
    disk budget and free-disk reserve. Call this before fetch_workbench_study."""

    status = check_disk_budget(required_bytes)
    return asdict(status)


def fetch_workbench_study(
    study_id: str,
    polarity: list[str] | None = None,
    instrument_types: list[str] | None = None,
    acquisition_types: list[str] | None = None,
    min_ms2_spectra: int | None = None,
    max_download_bytes: int | None = None,
    max_uncompressed_bytes: int | None = None,
) -> dict[str, Any]:
    """Fetch exactly one study's qualifying files over FTP and extract them.

    Per LOOP_CONTRACT.md, MWExtract's exit code 2 means the filter chain
    matched no files in this study -- a non-error outcome, not a failure.
    Only run the processing stage afterward when this reports
    ``status: "fetched"`` -- every other outcome means there is nothing on
    disk for the parser to act on.

    Args:
        study_id: The Workbench accession, e.g. "ST004496".
        polarity: Restrict to these polarities.
        instrument_types: Restrict to these instrument classes.
        acquisition_types: Restrict to these acquisition types.
        min_ms2_spectra: Drop files with fewer MS2 spectra than this.
        max_download_bytes: Per-study download ceiling.
        max_uncompressed_bytes: Per-study extracted-size ceiling; also used
            as the amount checked against the disk budget before fetching.
    """

    budget_status = check_disk_budget(required_bytes=max_uncompressed_bytes or 0.0)
    if budget_status.refused:
        append_ledger_row(
            study_id, "fetch", state="refused", reason=budget_status.reason,
        )
        return {
            "success": False, "status": "refused_disk_budget",
            "reason": budget_status.reason,
        }

    logger.info("Extraction of study %s: fetching...", study_id)
    get_progress_bus().publish(
        stage="extraction", key=study_id, status="running", detail="fetching...",
    )
    try:
        result = _environment.fetch_workbench_study(
            study_id,
            polarity=polarity,
            instrument_types=instrument_types,
            acquisition_types=acquisition_types,
            min_ms2_spectra=min_ms2_spectra,
            max_download_bytes=max_download_bytes,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
    except (InvalidStudyIdError, DockerEnvironmentError) as error:
        logger.info("Extraction of study %s done: error", study_id)
        get_progress_bus().publish(
            stage="extraction", key=study_id, status="error", detail=str(error),
        )
        return {"success": False, "status": "error", "error": str(error)}

    if result.returncode == 2:
        status = "no_matching_files"
    elif result.succeeded:
        status = "fetched"
    else:
        status = "failed"

    logger.info("Extraction of study %s done: %s", study_id, status)
    get_progress_bus().publish(
        stage="extraction", key=study_id, status=status, detail=status,
    )

    ms2_expected = _extract_ms2_expected(result.stdout_tail)
    instrument_polarity_counts = (
        _instrument_polarity_counts(
            _environment.raw_study_dir(study_id) / f"{study_id}.parquet",
            polarity, instrument_types, min_ms2_spectra,
        )
        if status == "fetched" else {}
    )
    append_ledger_row(
        study_id, "fetch", state=status, returncode=result.returncode,
        elapsed_seconds=result.elapsed_seconds, stdout_tail=result.stdout_tail,
        stderr_tail=result.stderr_tail, ms2_expected=ms2_expected,
        instrument_polarity_counts=instrument_polarity_counts,
    )
    return {
        "success": status == "fetched",
        "status": status,
        "report": result.stdout_tail,
        "ms2_expected": ms2_expected,
        "returncode": result.returncode,
        "stderr_tail": result.stderr_tail,
    }
