"""Batch metadata collection and raw-file extraction for Metabolomics Workbench.

The command line accepts explicit study accessions, an inclusive range, or a study
list. Standard inspection writes three concise reports: study metadata, failures,
and extracted files. Complete ZIP downloads happen only when ``--download`` is
supplied and both byte budgets permit the transfer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import zipfile
from ftplib import all_errors
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

import pandas as pd
import requests

from msms_evidence import (
    LIKELIHOOD_ORDER,
    MSMS_REPORT_COLUMNS,
    extract_msms_evidence,
    study_likelihoods,
)
from workbench_peek import (
    FORMAT_COLUMNS,
    FORMAT_SUFFIXES,
    WorkbenchPeekConfig,
    logical_data_inventory,
    peek_remote_zip_members,
)
from workbench_study import (
    download_selected_archives,
    extract_selected_members,
    fetch_available_study_ids,
    fetch_study_rest_bundle,
    ftp_session,
    list_study_archive_candidates,
)


STUDY_TOKEN_RE = re.compile(r"^(?:ST)?(\d{1,6})$", re.IGNORECASE)
PEEK_ERRORS = all_errors + (ValueError, zipfile.BadZipFile)

STUDY_METADATA_COLUMNS = [
    "study_id",
    "study_title",
    "analysis_type",
    "species",
    "number_of_samples",
]
INSPECTION_COLUMNS = [
    "study_id",
    "archive_name",
    "remote_name",
    "archive_bytes",
    "peek_success",
    "zip_member_count",
    "logical_input_count",
    "selected_logical_input_count",
    "peek_error",
]
LOGICAL_PLAN_COLUMNS = [
    "study_id",
    "archive_name",
    "logical_input",
    "format",
    "uncompressed_bytes",
    "compressed_bytes",
    "member_count",
]
MEMBER_PLAN_COLUMNS = [
    "study_id",
    "archive_name",
    "remote_name",
    "logical_input",
    "format",
    "member",
    "uncompressed_bytes",
    "compressed_bytes",
]
ARCHIVE_COLUMNS = ["study_id", "remote_name", "archive_name", "bytes"]
FAILURE_COLUMNS = ["study_id", "file_name", "reason"]
ARCHIVE_MANIFEST_COLUMNS = [
    "study_id", "archive_name", "local_path", "bytes", "sha256", "status",
]
EXTRACTED_FILE_COLUMNS = [
    "study_id", "extracted_member_path", "format", "size_bytes",
]
LEGACY_STANDARD_REPORT_NAMES = (
    "analysis_metadata.tsv",
    "archive_inspection.tsv",
    "archive_manifest.tsv",
    "archive_member_plan.tsv",
    "msms_first_pass.tsv",
    "raw_file_plan.tsv",
)
FIRST_PASS_MARKER_NAME = ".workbench_first_pass_only"
RESERVED_FIRST_PASS_DIRECTORY_NAMES = {
    "msms_all_first_pass",
    "msms_all_fist_pass",
}


def first_pass_root(path: Path) -> Path | None:
    """Return the protected first-pass ancestor containing ``path``, if any."""

    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name.casefold() in RESERVED_FIRST_PASS_DIRECTORY_NAMES:
            return candidate
        if (candidate / FIRST_PASS_MARKER_NAME).is_file():
            return candidate
        if (candidate / "reports" / "msms_first_pass.tsv").is_file():
            return candidate
    return None


def protect_first_pass_from_standard_write(path: Path, option_name: str) -> None:
    """Reject a standard-mode write target inside a first-pass output tree."""

    protected_root = first_pass_root(path)
    if protected_root is not None:
        raise ValueError(
            f"Standard inspection cannot use {option_name}={path} because it is "
            f"inside protected first-pass output {protected_root}. Choose a separate directory."
        )


def mark_first_pass_output(output_dir: Path) -> None:
    """Mark an output tree so later standard runs cannot write anywhere inside it."""

    output_dir.mkdir(parents=True, exist_ok=True)
    marker = output_dir / FIRST_PASS_MARKER_NAME
    temporary = marker.with_name(marker.name + ".tmp")
    temporary.write_text(
        "Reserved for workbench_batch.py --first-pass-only output.\n",
        encoding="utf-8",
    )
    temporary.replace(marker)


def normalize_study_id(value: str | int) -> str:
    """Convert a number or ``ST`` accession into canonical ``ST######`` form."""

    text = str(value).strip()
    match = STUDY_TOKEN_RE.fullmatch(text)
    if not match:
        raise argparse.ArgumentTypeError(
            f"Invalid study {value!r}; use a number or an ST accession."
        )
    number = int(match.group(1))
    if number < 1:
        raise argparse.ArgumentTypeError("Study numbers must be at least 1.")
    return f"ST{number:06d}"


def resolve_study_ids(
    studies: Sequence[str] | None,
    study_range: Sequence[str] | None,
    studies_file: Path | None = None,
) -> tuple[str, ...]:
    """Expand one CLI study source and remove duplicates without reordering it."""

    if studies:
        tokens = [piece for item in studies for piece in item.split(",") if piece]
        values = [normalize_study_id(token) for token in tokens]
    elif studies_file is not None:
        if not studies_file.is_file():
            raise argparse.ArgumentTypeError(
                f"Study-list file does not exist: {studies_file}"
            )
        tokens: list[str] = []
        for line in studies_file.read_text(encoding="utf-8-sig").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.lower() in {"study_id", "study"}:
                continue
            tokens.extend(piece for piece in re.split(r"[\s,]+", stripped) if piece)
        values = [normalize_study_id(token) for token in tokens]
        if not values:
            raise argparse.ArgumentTypeError(
                f"Study-list file contains no accessions: {studies_file}"
            )
    elif study_range:
        start = int(normalize_study_id(study_range[0])[2:])
        stop = int(normalize_study_id(study_range[1])[2:])
        step = 1 if stop >= start else -1
        values = [f"ST{number:06d}" for number in range(start, stop + step, step)]
    else:
        raise argparse.ArgumentTypeError(
            "Provide --studies, --studies-file, or --study-range."
        )
    return tuple(dict.fromkeys(values))


def normalize_formats(values: Sequence[str]) -> tuple[str, ...]:
    """Resolve case-insensitive format names, dotted suffixes, and comma lists."""

    aliases: dict[str, str] = {name.lower(): name for name in FORMAT_COLUMNS}
    for name, suffixes in FORMAT_SUFFIXES:
        for suffix in suffixes:
            aliases[suffix.lower().lstrip(".")] = name

    requested: list[str] = []
    for value in values:
        for piece in value.split(","):
            token = piece.strip().lower().lstrip(".")
            if not token:
                continue
            if token not in aliases:
                choices = ", ".join(FORMAT_COLUMNS)
                raise argparse.ArgumentTypeError(
                    f"Unknown format {piece!r}. Supported format groups: {choices}."
                )
            requested.append(aliases[token])
    if not requested:
        raise argparse.ArgumentTypeError("At least one format is required.")
    return tuple(dict.fromkeys(requested))


def parse_byte_size(value: str) -> int:
    """Parse byte limits such as ``5000000``, ``5GB``, or ``10GiB``."""

    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|KIB|MIB|GIB|TIB)?\s*",
        value,
        re.IGNORECASE,
    )
    if not match:
        raise argparse.ArgumentTypeError(f"Invalid byte size {value!r}.")
    number = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    factors = {
        "B": 1,
        "KB": 1_000,
        "MB": 1_000_000,
        "GB": 1_000_000_000,
        "TB": 1_000_000_000_000,
        "KIB": 1 << 10,
        "MIB": 1 << 20,
        "GIB": 1 << 30,
        "TIB": 1 << 40,
    }
    return int(number * factors[unit])


def study_metadata_row(study_id: str, bundle: Any) -> dict[str, Any]:
    """Select the five most useful fields from a study's REST summary."""

    summary = bundle.summary or {}
    return {
        "study_id": study_id,
        "study_title": summary.get("study_title"),
        "analysis_type": summary.get("analysis_type"),
        "species": summary.get("species"),
        "number_of_samples": summary.get("number_of_samples"),
    }


def failure_row(study_id: str, file_name: str, reason: Any) -> dict[str, str]:
    """Create one human-readable failure record with the requested three fields."""

    return {
        "study_id": study_id,
        "file_name": file_name,
        "reason": str(reason),
    }


def extracted_file_report(extracted: pd.DataFrame) -> pd.DataFrame:
    """Reduce the internal extraction manifest to the four public report fields."""

    if extracted.empty:
        return pd.DataFrame(columns=EXTRACTED_FILE_COLUMNS)
    return pd.DataFrame(
        {
            "study_id": extracted["study_id"],
            "extracted_member_path": extracted["local_path"],
            "format": extracted["format"],
            "size_bytes": extracted["uncompressed_bytes"],
        },
        columns=EXTRACTED_FILE_COLUMNS,
    )


def _member_rows_for_selection(
    members: pd.DataFrame,
    logical: pd.DataFrame,
    *,
    remote_name: str,
) -> list[dict[str, Any]]:
    """Expand selected logical files or bundles into concrete ZIP member rows."""

    rows: list[dict[str, Any]] = []
    file_members = members[~members["is_directory"]]
    for selected in logical.itertuples():
        root = PurePosixPath(selected.logical_input).as_posix()
        for member in file_members.itertuples():
            member_name = PurePosixPath(member.member).as_posix()
            if member_name != root and not member_name.startswith(root.rstrip("/") + "/"):
                continue
            rows.append(
                {
                    "study_id": selected.study_id,
                    "archive_name": selected.archive_name,
                    "remote_name": remote_name,
                    "logical_input": root,
                    "format": selected.format,
                    "member": member_name,
                    "uncompressed_bytes": int(member.uncompressed_bytes),
                    "compressed_bytes": int(member.compressed_bytes),
                }
            )
    return rows


def inspect_study_formats(
    study_id: str,
    formats: Sequence[str],
    *,
    email: str,
    timeout: float,
    peek_config: WorkbenchPeekConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Peek at one study's archives and plan members for the requested formats."""

    archives = list_study_archive_candidates(
        study_id,
        email=email,
        timeout=timeout,
    )
    statuses: list[dict[str, Any]] = []
    logical_frames: list[pd.DataFrame] = []
    member_rows: list[dict[str, Any]] = []

    for archive_number, archive in enumerate(archives.itertuples(), start=1):
        members: pd.DataFrame | None = None
        error: Exception | None = None
        for attempt in range(peek_config.max_retries + 1):
            try:
                with ftp_session(email, timeout) as ftp:
                    remote_size = (
                        int(archive.bytes)
                        if pd.notna(archive.bytes)
                        else ftp.size(archive.remote_name)
                    )
                    if remote_size is None:
                        raise ValueError("Archive size is unavailable.")
                    members = peek_remote_zip_members(
                        ftp,
                        archive.remote_name,
                        int(remote_size),
                        peek_config,
                    )
                error = None
                break
            except PEEK_ERRORS as exc:
                error = exc
                if attempt >= peek_config.max_retries:
                    break
                delays = peek_config.retry_backoff_seconds
                if not delays:
                    raise ValueError("Retry backoff values cannot be empty.") from exc
                delay = delays[min(attempt, len(delays) - 1)]
                print(
                    f"Retrying {archive.archive_name} in {delay:g}s after {exc!r}.",
                    file=sys.stderr,
                )
                time.sleep(delay)

        if members is None:
            statuses.append(
                {
                    "study_id": study_id,
                    "archive_name": archive.archive_name,
                    "remote_name": archive.remote_name,
                    "archive_bytes": archive.bytes,
                    "peek_success": False,
                    "zip_member_count": 0,
                    "logical_input_count": 0,
                    "selected_logical_input_count": 0,
                    "peek_error": repr(error),
                }
            )
        else:
            logical = logical_data_inventory(
                members,
                study_id=study_id,
                archive_name=archive.archive_name,
            )
            selected = logical[logical["format"].isin(formats)].copy()
            if not selected.empty:
                logical_frames.append(selected)
                member_rows.extend(
                    _member_rows_for_selection(
                        members,
                        selected,
                        remote_name=archive.remote_name,
                    )
                )
            statuses.append(
                {
                    "study_id": study_id,
                    "archive_name": archive.archive_name,
                    "remote_name": archive.remote_name,
                    "archive_bytes": archive.bytes,
                    "peek_success": True,
                    "zip_member_count": len(members),
                    "logical_input_count": len(logical),
                    "selected_logical_input_count": len(selected),
                    "peek_error": None,
                }
            )

        if archive_number < len(archives):
            time.sleep(peek_config.request_delay_seconds)

    logical_plan = (
        pd.concat(logical_frames, ignore_index=True)
        if logical_frames
        else pd.DataFrame(columns=LOGICAL_PLAN_COLUMNS)
    )
    member_plan = pd.DataFrame(member_rows, columns=MEMBER_PLAN_COLUMNS)
    selected_names = set(logical_plan["archive_name"])
    selected_archives = archives[archives["archive_name"].isin(selected_names)].copy()
    return (
        pd.DataFrame(statuses, columns=INSPECTION_COLUMNS),
        logical_plan,
        member_plan,
        selected_archives,
    )


def _frame(rows: Iterable[dict[str, Any]], leading_columns: Sequence[str]) -> pd.DataFrame:
    """Build a stable report frame while retaining dynamic REST metadata columns."""

    frame = pd.DataFrame(list(rows))
    if frame.empty:
        return pd.DataFrame(columns=list(leading_columns))
    ordered = [column for column in leading_columns if column in frame.columns]
    trailing = sorted(column for column in frame.columns if column not in ordered)
    return frame[ordered + trailing]


def write_tsv(frame: pd.DataFrame, path: Path) -> None:
    """Write one report atomically so interrupted runs do not leave partial TSVs."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    frame.to_csv(temporary, sep="\t", index=False)
    temporary.replace(path)


def write_reports(output_dir: Path, reports: dict[str, pd.DataFrame]) -> None:
    """Atomically write the requested public report tables."""

    report_dir = output_dir / "reports"
    for name, frame in reports.items():
        write_tsv(frame, report_dir / f"{name}.tsv")


def standard_reports(
    study_rows: Iterable[dict[str, Any]],
    failures: Iterable[dict[str, Any]],
    extracted: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame]:
    """Build the three concise reports emitted by standard inspection."""

    return {
        "study_metadata": _frame(study_rows, STUDY_METADATA_COLUMNS)[
            STUDY_METADATA_COLUMNS
        ],
        "failures": pd.DataFrame(failures, columns=FAILURE_COLUMNS),
        "extracted_files": extracted_file_report(
            pd.DataFrame() if extracted is None else extracted
        ),
    }


def remove_legacy_standard_reports(output_dir: Path) -> None:
    """Remove only report files emitted by the former verbose standard workflow."""

    report_dir = output_dir / "reports"
    for name in LEGACY_STANDARD_REPORT_NAMES:
        path = report_dir / name
        if path.is_file():
            path.unlink()


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser and document storage-safe defaults."""

    parser = argparse.ArgumentParser(
        description=(
            "Collect Workbench study metadata and plan or extract selected raw formats."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--studies",
        nargs="+",
        help="Study accessions/numbers, separated by spaces or commas.",
    )
    source.add_argument(
        "--studies-file",
        type=Path,
        help="Text file containing one ST accession per line.",
    )
    source.add_argument(
        "--study-range",
        nargs=2,
        metavar=("START", "END"),
        help="Inclusive numeric or accession range; descending ranges are allowed.",
    )
    source.add_argument(
        "--all-studies",
        action="store_true",
        help="Use one bulk REST request to resolve every currently available study.",
    )
    parser.add_argument(
        "--formats",
        nargs="+",
        help=(
            f"Desired format groups: {', '.join(FORMAT_COLUMNS)}. "
            "Not needed with --first-pass-only."
        ),
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("MWB_EMAIL"),
        help="Anonymous FTP password/email. Defaults to the MWB_EMAIL environment variable.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Destination for reports, archives, and extracted raw files.",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download selected complete ZIPs and extract requested members.",
    )
    parser.add_argument(
        "--first-pass-only",
        action="store_true",
        help=(
            "Generate msms_first_pass.tsv from one cached mwTab request per study; "
            "skip all FTP listing, archive inspection, and downloads."
        ),
    )
    parser.add_argument(
        "--max-studies",
        type=int,
        default=100,
        help=(
            "Process at most the first N studies from a list, file, or range "
            "instead of failing when the input is longer (default: 100)."
        ),
    )
    parser.add_argument(
        "--max-total-archive-bytes",
        type=parse_byte_size,
        default=parse_byte_size("5GB"),
        help="Combined compressed transfer cap, e.g. 5GB (default: 5GB).",
    )
    parser.add_argument(
        "--max-total-uncompressed-bytes",
        type=parse_byte_size,
        default=parse_byte_size("10GB"),
        help="Combined extracted-data cap, e.g. 10GB (default: 10GB).",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--request-delay-seconds", type=float, default=1.0)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        nargs="+",
        default=(2.0, 5.0, 15.0, 30.0, 60.0),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="REST response cache (default: OUTPUT_DIR/cache/rest).",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Ignore and replace cached REST responses.",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=25,
        help="Write resumable first-pass reports every N attempted studies (default: 25).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="Pause after each N first-pass studies (default: 200).",
    )
    parser.add_argument(
        "--batch-pause-seconds",
        type=float,
        default=15.0,
        help="Pause between first-pass batches (default: 15 seconds).",
    )
    parser.add_argument(
        "--restart-first-pass",
        action="store_true",
        help="Rebuild the first-pass report instead of resuming completed studies.",
    )
    parser.add_argument(
        "--download-msms-likelihoods",
        nargs="+",
        choices=tuple(
            sorted(LIKELIHOOD_ORDER, key=LIKELIHOOD_ORDER.get, reverse=True)
        ),
        help=(
            "During --download, retain only studies having at least one analysis "
            "in an allowed likelihood category. Inspection still covers all studies."
        ),
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at the first study error instead of recording it and continuing.",
    )
    return parser


def _read_report(path: Path, columns: Sequence[str]) -> pd.DataFrame:
    """Read a resumable TSV report or return an empty frame with its schema."""

    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=list(columns))
    frame = pd.read_csv(path, sep="\t", dtype={"study_id": str})
    for column in columns:
        if column not in frame.columns:
            frame[column] = None
    return frame[list(columns)]


def _read_failure_report(path: Path) -> pd.DataFrame:
    """Read failures and migrate the former stage/error schema when necessary."""

    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=FAILURE_COLUMNS)
    frame = pd.read_csv(path, sep="\t", dtype={"study_id": str})
    if "file_name" not in frame.columns:
        frame["file_name"] = frame.get("stage")
    if "reason" not in frame.columns:
        frame["reason"] = frame.get("error")
    for column in FAILURE_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    return frame[FAILURE_COLUMNS]


def _checkpoint_first_pass(
    output_dir: Path,
    evidence: pd.DataFrame,
    failures: pd.DataFrame,
) -> None:
    """Atomically save the combined evidence report and current failure table."""

    report_dir = output_dir / "reports"
    evidence = evidence.drop_duplicates(
        subset=["study_id", "analysis_id"], keep="last"
    ).sort_values(["study_id", "analysis_id"], na_position="last", ignore_index=True)
    failures = failures.drop_duplicates(
        subset=["study_id", "file_name", "reason"], keep="last"
    ).sort_values(["study_id", "file_name"], ignore_index=True)
    write_tsv(evidence, report_dir / "msms_first_pass.tsv")
    write_tsv(failures, report_dir / "failures.tsv")


def run_first_pass_only(
    args: argparse.Namespace,
    study_ids: Sequence[str],
    *,
    session: requests.Session,
    output_dir: Path,
    cache_dir: Path,
) -> int:
    """Run the cached, REST-only, resumable MS/MS metadata classifier."""

    report_path = output_dir / "reports" / "msms_first_pass.tsv"
    failure_path = output_dir / "reports" / "failures.tsv"
    if args.restart_first_pass:
        evidence = pd.DataFrame(columns=MSMS_REPORT_COLUMNS)
        failures = pd.DataFrame(columns=FAILURE_COLUMNS)
    else:
        evidence = _read_report(report_path, MSMS_REPORT_COLUMNS)
        failures = _read_failure_report(failure_path)

    completed = (
        set()
        if args.refresh_cache
        else set(evidence["study_id"].dropna().astype(str))
    )
    pending = [study_id for study_id in study_ids if study_id not in completed]
    print(
        f"REST-only first pass: {len(study_ids)} requested, "
        f"{len(completed.intersection(study_ids))} resumed, {len(pending)} pending."
    )
    if not pending:
        _checkpoint_first_pass(output_dir, evidence, failures)
        print(f"First pass is already complete: {report_path}")
        return 0

    new_frames: list[pd.DataFrame] = []
    failure_rows = failures.to_dict(orient="records")
    for number, study_id in enumerate(pending, start=1):
        print(f"[{number}/{len(pending)}] mwTab {study_id}")
        try:
            bundle = fetch_study_rest_bundle(
                study_id,
                session=session,
                timeout=args.timeout,
                max_retries=args.max_retries,
                retry_backoff_seconds=tuple(args.retry_backoff_seconds),
                request_delay_seconds=args.request_delay_seconds,
                resources_to_fetch=("mwtab",),
                cache_dir=cache_dir,
                refresh_cache=args.refresh_cache,
            )
            if bundle.errors:
                raise RuntimeError(json.dumps(dict(bundle.errors), sort_keys=True))
            new_frames.append(
                extract_msms_evidence(
                    study_id,
                    bundle.mwtab_documents,
                    bundle.analyses,
                )
            )
            failure_rows = [
                row for row in failure_rows if row.get("study_id") != study_id
            ]
        except Exception as exc:
            failure_rows = [
                row for row in failure_rows if row.get("study_id") != study_id
            ]
            failure_rows.append(failure_row(study_id, "mwtab.txt", repr(exc)))
            print(f"{study_id} failed: {exc!r}", file=sys.stderr)
            if args.fail_fast:
                raise

        checkpoint_due = number % args.checkpoint_every == 0 or number == len(pending)
        if checkpoint_due:
            frames = [evidence, *new_frames]
            evidence = pd.concat(frames, ignore_index=True)
            new_frames.clear()
            failures = pd.DataFrame(failure_rows, columns=FAILURE_COLUMNS)
            _checkpoint_first_pass(output_dir, evidence, failures)
            print(
                f"Checkpoint: {evidence['study_id'].nunique()} studies written to "
                f"{report_path}."
            )

        if number < len(pending):
            time.sleep(args.request_delay_seconds)
        if number % args.batch_size == 0 and number < len(pending):
            print(
                f"Completed batch of {args.batch_size}; pausing "
                f"{args.batch_pause_seconds:g}s."
            )
            time.sleep(args.batch_pause_seconds)

    final_failures = pd.DataFrame(failure_rows, columns=FAILURE_COLUMNS)
    print(
        f"First pass finished with {evidence['study_id'].nunique()} completed studies "
        f"and {len(final_failures)} failures."
    )
    return 1 if not final_failures.empty else 0


def run(args: argparse.Namespace) -> int:
    """Execute metadata collection, planning, and optional bounded extraction."""

    if args.first_pass_only and args.download:
        raise ValueError("--first-pass-only cannot be combined with --download.")
    if args.all_studies and not args.first_pass_only:
        raise ValueError("--all-studies is restricted to the REST-only first pass.")
    if not args.first_pass_only and not args.email:
        raise ValueError("Provide --email or set the MWB_EMAIL environment variable.")
    if not args.first_pass_only and not args.formats:
        raise ValueError("--formats is required unless --first-pass-only is used.")
    if args.max_studies < 1:
        raise ValueError("--max-studies must be at least 1.")
    if (
        args.request_delay_seconds < 0
        or args.batch_pause_seconds < 0
        or args.max_retries < 0
    ):
        raise ValueError("Request delay and retry count cannot be negative.")
    if args.checkpoint_every < 1 or args.batch_size < 1:
        raise ValueError("Checkpoint frequency and batch size must be at least 1.")
    if args.max_retries and not args.retry_backoff_seconds:
        raise ValueError("Retry backoff values cannot be empty.")
    if any(value < 0 for value in args.retry_backoff_seconds):
        raise ValueError("Retry backoff values cannot be negative.")

    output_dir = args.output_dir.resolve()
    cache_dir = (
        args.cache_dir.resolve()
        if args.cache_dir is not None
        else output_dir / "cache" / "rest"
    )
    if args.first_pass_only:
        mark_first_pass_output(output_dir)
    else:
        protect_first_pass_from_standard_write(output_dir, "--output-dir")
        protect_first_pass_from_standard_write(cache_dir, "--cache-dir")

    client = requests.Session()
    client.headers.update(
        {"User-Agent": "mwextract-batch/0.2 (Metabolomics Workbench research client)"}
    )
    if args.all_studies:
        study_ids = fetch_available_study_ids(
            session=client,
            cache_dir=cache_dir,
            refresh_cache=args.refresh_cache,
            timeout=args.timeout,
            max_retries=args.max_retries,
            retry_backoff_seconds=tuple(args.retry_backoff_seconds),
        )
    else:
        study_ids = resolve_study_ids(
            args.studies,
            args.study_range,
            args.studies_file,
        )
    if not args.all_studies and len(study_ids) > args.max_studies:
        requested_count = len(study_ids)
        study_ids = study_ids[: args.max_studies]
        print(
            f"Input contains {requested_count} studies; processing the first "
            f"{len(study_ids)} because --max-studies={args.max_studies}."
        )
    if args.first_pass_only:
        try:
            return run_first_pass_only(
                args,
                study_ids,
                session=client,
                output_dir=output_dir,
                cache_dir=cache_dir,
            )
        finally:
            client.close()

    formats = normalize_formats(args.formats)
    remove_legacy_standard_reports(output_dir)
    peek_config = WorkbenchPeekConfig(
        request_delay_seconds=args.request_delay_seconds,
        max_retries=args.max_retries,
        retry_backoff_seconds=tuple(args.retry_backoff_seconds),
    )
    study_rows: list[dict[str, Any]] = []
    msms_frames: list[pd.DataFrame] = []
    logical_frames: list[pd.DataFrame] = []
    member_frames: list[pd.DataFrame] = []
    archive_frames: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []

    for index, study_id in enumerate(study_ids, start=1):
        print(f"[{index}/{len(study_ids)}] Inspecting {study_id}")
        try:
            bundle = fetch_study_rest_bundle(
                study_id,
                session=client,
                timeout=args.timeout,
                max_retries=args.max_retries,
                retry_backoff_seconds=tuple(args.retry_backoff_seconds),
                request_delay_seconds=args.request_delay_seconds,
                cache_dir=cache_dir,
                refresh_cache=args.refresh_cache,
            )
        except Exception as exc:
            failures.append(failure_row(study_id, "REST metadata", repr(exc)))
            print(f"{study_id} metadata failed: {exc!r}", file=sys.stderr)
            write_reports(output_dir, standard_reports(study_rows, failures))
            if args.fail_fast:
                raise
            if index < len(study_ids):
                time.sleep(args.request_delay_seconds)
            continue

        study_rows.append(study_metadata_row(study_id, bundle))
        for resource_name, reason in bundle.errors.items():
            failures.append(
                failure_row(study_id, f"{resource_name}.txt", reason)
            )

        try:
            msms_frames.append(
                extract_msms_evidence(
                    study_id,
                    bundle.mwtab_documents,
                    bundle.analyses,
                )
            )
        except Exception as exc:
            failures.append(failure_row(study_id, "mwtab.txt", repr(exc)))
            print(f"{study_id} MS/MS classification failed: {exc!r}", file=sys.stderr)
            if args.fail_fast:
                raise

        try:
            inspection, logical, members, archives = inspect_study_formats(
                study_id,
                formats,
                email=args.email,
                timeout=args.timeout,
                peek_config=peek_config,
            )
            logical_frames.append(logical)
            member_frames.append(members)
            archive_frames.append(archives)
            for status in inspection.itertuples(index=False):
                if not bool(status.peek_success):
                    failures.append(
                        failure_row(study_id, status.archive_name, status.peek_error)
                    )
        except Exception as exc:
            failures.append(failure_row(study_id, "archive listing", repr(exc)))
            print(f"{study_id} archive inspection failed: {exc!r}", file=sys.stderr)
            if args.fail_fast:
                raise

        write_reports(output_dir, standard_reports(study_rows, failures))
        if index < len(study_ids):
            time.sleep(args.request_delay_seconds)

    logical_plan = (
        pd.concat(logical_frames, ignore_index=True)
        if logical_frames
        else pd.DataFrame(columns=LOGICAL_PLAN_COLUMNS)
    )
    selected_archives = (
        pd.concat(archive_frames, ignore_index=True)
        if archive_frames
        else pd.DataFrame(columns=ARCHIVE_COLUMNS)
    ).drop_duplicates(subset=["remote_name"])
    member_plan = (
        pd.concat(member_frames, ignore_index=True)
        if member_frames
        else pd.DataFrame(columns=MEMBER_PLAN_COLUMNS)
    ).drop_duplicates(
        subset=["study_id", "archive_name", "member"]
    )
    msms_evidence = (
        pd.concat(msms_frames, ignore_index=True)
        if msms_frames
        else pd.DataFrame(columns=MSMS_REPORT_COLUMNS)
    )
    planned_archive_bytes = (
        int(selected_archives["bytes"].sum())
        if not selected_archives.empty and not selected_archives["bytes"].isna().any()
        else None
    )
    planned_uncompressed_bytes = int(member_plan["uncompressed_bytes"].sum())
    print(
        "Plan:",
        f"{len(logical_plan)} logical inputs,",
        f"{len(selected_archives)} complete archives,",
        f"{planned_archive_bytes if planned_archive_bytes is not None else 'unknown'} transfer bytes,",
        f"{planned_uncompressed_bytes} extracted bytes.",
    )

    archive_manifest = pd.DataFrame(columns=ARCHIVE_MANIFEST_COLUMNS)
    extracted_files = pd.DataFrame()
    download_archives = selected_archives
    download_members = member_plan
    if args.download_msms_likelihoods:
        allowed = set(args.download_msms_likelihoods)
        per_study = study_likelihoods(msms_evidence)
        eligible_studies = {
            study_id for study_id, likelihood in per_study.items()
            if likelihood in allowed
        }
        download_archives = selected_archives[
            selected_archives["study_id"].isin(eligible_studies)
        ].copy()
        download_members = member_plan[
            member_plan["study_id"].isin(eligible_studies)
        ].copy()
        print(
            "MS/MS download filter:",
            f"allowed={sorted(allowed)},",
            f"eligible_studies={len(eligible_studies)},",
            f"selected_archives={len(download_archives)}.",
        )

    if args.download and not download_archives.empty:
        archive_manifest = download_selected_archives(
            download_archives,
            output_dir / "archives",
            email=args.email,
            max_total_archive_bytes=args.max_total_archive_bytes,
            timeout=args.timeout,
        )
        extracted_files = extract_selected_members(
            download_members,
            archive_manifest,
            output_dir / "raw_files",
            max_total_uncompressed_bytes=args.max_total_uncompressed_bytes,
        )
    elif not args.download:
        print("Planning only; add --download to extract the selected files.")

    write_reports(
        output_dir,
        standard_reports(study_rows, failures, extracted_files),
    )
    client.close()
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse command-line input and return a process exit status."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (ValueError, argparse.ArgumentTypeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
