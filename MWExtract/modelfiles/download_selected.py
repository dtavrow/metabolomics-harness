"""Download and extract only audited logical files from selected Workbench studies."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

import pandas as pd

from pipeline_common import (as_bool, deliverable_path, parse_byte_size, read_table, read_tsv,
                             write_tsv_atomic)
from workbench_batch import inspect_study_formats
from workbench_peek import WorkbenchPeekConfig
from workbench_study import download_selected_archives, extract_selected_members


def build_download_plan(
    selection: pd.DataFrame,
    audit: pd.DataFrame,
    *,
    email: str,
    statuses: set[str],
    timeout: float,
    peek_config: WorkbenchPeekConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Re-peek selected archives and reconstruct complete file/bundle member plans."""

    selected_ids = selection.loc[selection["selected"].map(as_bool), "study_id"].astype(str)
    archive_frames = []
    member_frames = []
    failures = []
    for number, study_id in enumerate(selected_ids, start=1):
        evidence = audit[
            audit["study_id"].eq(study_id)
            & audit["validation_status"].astype(str).str.casefold().isin(statuses)
            & audit["logical_input"].notna()
        ].copy()
        formats = tuple(evidence["format"].dropna().astype(str).str.lower().unique())
        if not formats:
            failures.append({"study_id": study_id, "stage": "plan", "error": "No qualifying audited logical inputs."})
            continue
        try:
            _, _, members, archives = inspect_study_formats(
                study_id,
                formats,
                email=email,
                timeout=timeout,
                peek_config=peek_config,
            )
            allowed = set(zip(evidence["archive_name"].astype(str), evidence["logical_input"].astype(str)))
            if not members.empty:
                mask = [
                    (str(row.archive_name), str(row.logical_input)) in allowed
                    for row in members.itertuples()
                ]
                members = members.loc[mask].copy()
            if members.empty:
                failures.append({"study_id": study_id, "stage": "plan", "error": "Audited files were not found during the fresh ZIP peek."})
                continue
            wanted_archives = set(members["archive_name"])
            archive_frames.append(archives[archives["archive_name"].isin(wanted_archives)].copy())
            member_frames.append(members)
            print(f"[{number}/{len(selected_ids)}] {study_id}: {len(members):,} ZIP members planned.")
        except Exception as exc:
            failures.append({"study_id": study_id, "stage": "plan", "error": repr(exc)})

    archives = pd.concat(archive_frames, ignore_index=True).drop_duplicates("archive_name") if archive_frames else pd.DataFrame()
    members = pd.concat(member_frames, ignore_index=True) if member_frames else pd.DataFrame()
    return archives, members, pd.DataFrame(failures, columns=["study_id", "stage", "error"])


def build_parser() -> argparse.ArgumentParser:
    """Define selected-study, FTP, storage-limit, and artifact arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection-tsv", type=deliverable_path, required=True)
    parser.add_argument("--audit-tsv", type=deliverable_path, required=True)
    parser.add_argument("--output-dir", type=deliverable_path, required=True)
    parser.add_argument(
        "--spectra-dir", type=deliverable_path,
        help="Payload destination. Defaults to <output-dir>/extracted for compatibility.",
    )
    parser.add_argument("--email", default=os.environ.get("MWB_EMAIL"))
    parser.add_argument("--statuses", nargs="+", default=("confirmed", "probable"))
    parser.add_argument("--max-download-bytes", type=parse_byte_size, default=parse_byte_size("5GB"))
    parser.add_argument("--max-uncompressed-bytes", type=parse_byte_size, default=parse_byte_size("10GB"))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--request-delay-seconds", type=float, default=0.5)
    # Observed in practice: the Workbench FTP data connection stalls mid-transfer
    # on multi-GB archives often enough that 3 attempts routinely exhausts
    # before the transfer completes, even though each retry does make
    # progress (it resumes from the partial file, see
    # workbench_study._download_selected_archives). More attempts with a
    # longer tail give a flaky link enough chances to actually finish.
    parser.add_argument("--max-retries", type=int, default=8)
    parser.add_argument(
        "--retry-backoff-seconds", type=float, nargs="+",
        default=(1.0, 2.0, 4.0, 8.0, 15.0, 30.0),
    )
    parser.add_argument("--dry-run", action="store_true")
    # Archives are deleted once their members are extracted, because they are
    # always re-downloadable from the Workbench and keeping both copies more
    # than doubles peak disk (589 GB of archives expand to 1,275 GB).
    parser.add_argument("--keep-archives", dest="discard_archives", action="store_false",
                        help="Retain downloaded ZIPs after extraction instead of deleting them.")
    parser.set_defaults(discard_archives=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Plan, optionally transfer, extract, and write machine-readable manifests."""

    args = build_parser().parse_args(argv)
    if not args.email:
        raise SystemExit("Provide --email or set MWB_EMAIL for anonymous FTP login.")
    selection = read_tsv(args.selection_tsv, required=("study_id", "selected"))
    # read_table so the Parquet corpus index works here too; select_storage
    # already accepts it and the two stages must take the same file.
    audit = read_table(
        args.audit_tsv,
        required=("study_id", "archive_name", "logical_input", "format", "validation_status"),
    )
    output_dir = args.output_dir
    manifest_dir = output_dir / "manifests"
    peek_config = WorkbenchPeekConfig(
        request_delay_seconds=args.request_delay_seconds,
        max_retries=args.max_retries,
        retry_backoff_seconds=tuple(args.retry_backoff_seconds),
    )
    archives, members, failures = build_download_plan(
        selection,
        audit,
        email=args.email,
        statuses={value.casefold() for value in args.statuses},
        timeout=args.timeout,
        peek_config=peek_config,
    )
    write_tsv_atomic(archives, manifest_dir / "archive_plan.tsv")
    write_tsv_atomic(members, manifest_dir / "member_plan.tsv")
    if args.dry_run or archives.empty or members.empty:
        write_tsv_atomic(failures, manifest_dir / "download_failures.tsv")
        print(f"Dry plan: {len(archives):,} archives and {len(members):,} members.")
        return 1 if failures.shape[0] and archives.empty else 0

    try:
        downloads = download_selected_archives(
            archives,
            output_dir / "archives",
            email=args.email,
            max_total_archive_bytes=args.max_download_bytes,
            timeout=args.timeout,
            max_retries=args.max_retries,
            retry_backoff_seconds=tuple(args.retry_backoff_seconds),
        )
        extracted = extract_selected_members(
            members,
            downloads,
            args.spectra_dir or output_dir / "extracted",
            max_total_uncompressed_bytes=args.max_uncompressed_bytes,
            discard_archives=args.discard_archives,
        )
    except Exception as exc:
        failures = pd.concat(
            [failures, pd.DataFrame([{"study_id": "", "stage": "download_or_extract", "error": repr(exc)}])],
            ignore_index=True,
        )
        write_tsv_atomic(failures, manifest_dir / "download_failures.tsv")
        raise

    # Carry study-level metadata forward so converter decisions remain explainable.
    metadata_columns = [
        column for column in ("study_id", "instrument_names", "instrument_types", "ion_modes", "collision_energy_values")
        if column in selection.columns
    ]
    if metadata_columns:
        extracted = extracted.merge(selection[metadata_columns].drop_duplicates("study_id"), on="study_id", how="left")
    write_tsv_atomic(downloads, manifest_dir / "downloads.tsv")
    extracted_path = write_tsv_atomic(extracted, manifest_dir / "extracted_files.tsv")
    write_tsv_atomic(failures, manifest_dir / "download_failures.tsv")
    print(f"Downloaded {len(downloads):,} archives; extracted {len(extracted):,} members.")
    print(f"Manifest: {extracted_path}")
    return 1 if not failures.empty else 0


if __name__ == "__main__":
    raise SystemExit(main())
