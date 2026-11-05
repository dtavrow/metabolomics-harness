"""Select audited Workbench studies without exceeding transfer or disk budgets."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

import pandas as pd

from pipeline_common import deliverable_path, parse_byte_size, read_table, read_tsv, write_tsv_atomic


OUTPUT_COLUMNS = [
    "study_id", "selected", "selection_reason", "validation_statuses", "formats",
    "archive_count", "download_bytes", "uncompressed_bytes", "ms2_spectrum_count",
    "candidate_count", "file_scoped_candidate_count", "instrument_names",
    "instrument_types", "ion_modes", "collision_energy_values", "priority_rank",
]


def _joined(values: pd.Series) -> str:
    """Join unique nonempty manifest values without changing their first-seen order."""

    result = []
    for value in values.dropna().astype(str):
        value = value.strip()
        if value and value not in result:
            result.append(value)
    return ";".join(result)


def summarize_audit(
    audit: pd.DataFrame,
    candidates: pd.DataFrame | None,
    accepted_statuses: set[str],
) -> pd.DataFrame:
    """Collapse qualifying file-level audit evidence into study-level storage costs."""

    rows = audit[
        audit["validation_status"].astype(str).str.casefold().isin(accepted_statuses)
        & audit["archive_name"].notna()
    ].copy()
    if rows.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    candidate_counts: dict[str, int] = {}
    file_counts: dict[str, int] = {}
    if candidates is not None and not candidates.empty:
        candidate_counts = candidates.groupby("study_id")["metabolite"].nunique().to_dict()
        file_counts = (
            candidates[candidates["mapping_scope"].eq("file")]
            .groupby("study_id")["metabolite"].nunique().to_dict()
        )

    summaries = []
    for study_id, group in rows.groupby("study_id", sort=False):
        archives = group.drop_duplicates(["archive_name"])
        logical = group.drop_duplicates(["archive_name", "logical_input"])
        statuses = set(group["validation_status"].astype(str).str.casefold())
        status_rank = 0 if "confirmed" in statuses else 1
        summaries.append(
            {
                "study_id": study_id,
                "validation_statuses": ";".join(sorted(statuses)),
                "formats": _joined(group["format"]),
                "archive_count": int(archives.shape[0]),
                "download_bytes": int(pd.to_numeric(archives["archive_bytes"], errors="coerce").fillna(0).sum()),
                "uncompressed_bytes": int(pd.to_numeric(logical["uncompressed_bytes"], errors="coerce").fillna(0).sum()),
                "ms2_spectrum_count": int(pd.to_numeric(group["ms2_spectrum_count"], errors="coerce").fillna(0).sum()),
                "candidate_count": int(candidate_counts.get(study_id, 0)),
                "file_scoped_candidate_count": int(file_counts.get(study_id, 0)),
                "instrument_names": _joined(group.get("instrument_names", pd.Series(dtype=str))),
                "instrument_types": _joined(group.get("instrument_types", pd.Series(dtype=str))),
                "ion_modes": _joined(group.get("ion_modes", pd.Series(dtype=str))),
                "collision_energy_values": _joined(group.get("collision_energy_values", pd.Series(dtype=str))),
                "_status_rank": status_rank,
            }
        )
    return pd.DataFrame(summaries)


def select_under_budget(
    studies: pd.DataFrame,
    *,
    max_download_bytes: int,
    max_uncompressed_bytes: int,
    max_studies: int | None,
    require_candidates: bool,
) -> pd.DataFrame:
    """Greedily select high-evidence, well-annotated studies under both budgets."""

    if studies.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    ranked = studies.sort_values(
        ["_status_rank", "file_scoped_candidate_count", "candidate_count", "download_bytes", "study_id"],
        ascending=[True, False, False, True, True],
        kind="stable",
    ).reset_index(drop=True)
    selected_count = 0
    used_download = 0
    used_uncompressed = 0
    decisions = []
    for rank, (_, row) in enumerate(ranked.iterrows(), start=1):
        reason = "selected"
        selected = True
        if require_candidates and row["candidate_count"] == 0:
            selected, reason = False, "no_metabolite_candidates"
        elif max_studies is not None and selected_count >= max_studies:
            selected, reason = False, "max_studies_reached"
        elif used_download + row["download_bytes"] > max_download_bytes:
            selected, reason = False, "download_budget_exceeded"
        elif used_uncompressed + row["uncompressed_bytes"] > max_uncompressed_bytes:
            selected, reason = False, "uncompressed_budget_exceeded"
        if selected:
            selected_count += 1
            used_download += int(row["download_bytes"])
            used_uncompressed += int(row["uncompressed_bytes"])
        record = row.to_dict()
        record.update(selected=selected, selection_reason=reason, priority_rank=rank)
        decisions.append(record)
    return pd.DataFrame(decisions).drop(columns=["_status_rank"])[OUTPUT_COLUMNS]


def build_parser() -> argparse.ArgumentParser:
    """Create the manifest-oriented storage-selection CLI."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-tsv", type=deliverable_path, required=True)
    parser.add_argument("--candidates-tsv", type=deliverable_path)
    parser.add_argument("--output-tsv", type=deliverable_path, required=True)
    parser.add_argument("--statuses", nargs="+", default=("confirmed", "probable"))
    parser.add_argument("--max-download-bytes", type=parse_byte_size, default=parse_byte_size("5GB"))
    parser.add_argument("--max-uncompressed-bytes", type=parse_byte_size, default=parse_byte_size("10GB"))
    parser.add_argument("--max-studies", type=int)
    # A run spec selects by catalog predicate, which resolves to a study list;
    # without this the stage can only take the globally smallest studies, so a
    # targeted slice had to be assembled by hand.
    parser.add_argument("--studies", nargs="+",
                        help="Restrict selection to these study accessions.")
    # Other acquisition modes are excluded here rather than downstream, because
    # the cheapest way not to store them is never to fetch them.
    parser.add_argument("--acquisition-tsv", type=deliverable_path,
                        help="Per-file acquisition classification; rows whose acquisition_type "
                             "is not permitted are dropped before the budget is applied.")
    parser.add_argument("--acquisition-types", nargs="+", default=("product_ion",),
                        help="Acquisition modes to keep. Default: product-ion scans only.")
    parser.add_argument("--allow-unclassified-acquisition", action="store_true",
                        help="Keep files the classifier could not place. Off by default: an "
                             "unclassified file is as likely to be MRM as anything else.")
    parser.add_argument("--exclude-studies", nargs="+",
                        help="Drop these study accessions from selection.")
    parser.add_argument("--allow-without-candidates", action="store_true")
    return parser


def filter_by_acquisition(
    audit: pd.DataFrame,
    acquisition: pd.DataFrame,
    *,
    permitted: set[str],
    allow_unclassified: bool = False,
) -> pd.DataFrame:
    """Drop audit rows whose file is not one of the permitted acquisition modes.

    Matching is on the file's basename, because the audit records a path inside
    an archive while the classifier records the extracted path.  A file with no
    classification is dropped unless explicitly allowed: silence is not evidence
    that a file is a product-ion scan.
    """

    key = "source_file" if "source_file" in acquisition.columns else "logical_input"
    verdicts = (acquisition.assign(
        _basename=acquisition[key].astype(str).str.replace("\\", "/", regex=False)
        .str.rsplit("/", n=1).str[-1].str.casefold())
        .drop_duplicates(["study_id", "_basename"])
        .set_index(["study_id", "_basename"])["acquisition_type"])
    basenames = (audit["logical_input"].astype(str).str.replace("\\", "/", regex=False)
                 .str.rsplit("/", n=1).str[-1].str.casefold())
    resolved = pd.MultiIndex.from_arrays([audit["study_id"], basenames]).map(verdicts)
    keep = pd.Series(resolved, index=audit.index).isin(permitted)
    if allow_unclassified:
        keep = keep | pd.Series(resolved, index=audit.index).isna()
    dropped = int((~keep).sum())
    if dropped:
        print(f"Acquisition filter removed {dropped:,} of {len(audit):,} files "
              f"(keeping {sorted(permitted)}).")
    return audit[keep]


def main(argv: Sequence[str] | None = None) -> int:
    """Select studies and emit an auditable decision for every eligible study."""

    args = build_parser().parse_args(argv)
    # read_table, not read_tsv: the container ships the corpus index as Parquet
    # (0.42 MB for the same 20,764 rows the audit TSV carries in 1,103 MB), and
    # this flag has to accept either without a rename.
    audit = read_table(
        args.audit_tsv,
        required=("study_id", "archive_name", "format", "validation_status", "archive_bytes", "uncompressed_bytes"),
    )
    # The index already carries acquisition_type, so a separate projection file
    # is only needed when selecting from the raw audit.
    if args.acquisition_tsv is None and "acquisition_type" in audit.columns:
        permitted = set(args.acquisition_types)
        keep = audit["acquisition_type"].isin(permitted)
        if args.allow_unclassified_acquisition:
            keep = keep | audit["acquisition_type"].isna()
        dropped = int((~keep).sum())
        if dropped:
            print(f"Acquisition filter removed {dropped:,} of {len(audit):,} files "
                  f"(keeping {sorted(permitted)}).")
        audit = audit[keep]
        if audit.empty:
            raise SystemExit("No files remain after the acquisition filter.")
    candidates = None
    if args.candidates_tsv:
        candidates = read_tsv(args.candidates_tsv, required=("study_id", "metabolite", "mapping_scope"))
    # Filter before summarising so the budget is spent on the requested slice
    # rather than being consumed by studies that are then discarded.
    if args.studies:
        audit = audit[audit["study_id"].isin(set(args.studies))]
        if audit.empty:
            raise SystemExit(f"No audit rows for the requested studies: {sorted(set(args.studies))}")
    if args.exclude_studies:
        audit = audit[~audit["study_id"].isin(set(args.exclude_studies))]
    if args.acquisition_tsv:
        audit = filter_by_acquisition(
            audit, read_tsv(args.acquisition_tsv, required=("acquisition_type",)),
            permitted=set(args.acquisition_types),
            allow_unclassified=args.allow_unclassified_acquisition,
        )
        if audit.empty:
            raise SystemExit("No files remain after the acquisition filter.")
    studies = summarize_audit(audit, candidates, {value.casefold() for value in args.statuses})
    result = select_under_budget(
        studies,
        max_download_bytes=args.max_download_bytes,
        max_uncompressed_bytes=args.max_uncompressed_bytes,
        max_studies=args.max_studies,
        require_candidates=not args.allow_without_candidates,
    )
    output = write_tsv_atomic(result, args.output_tsv)
    chosen = result[result["selected"]] if not result.empty else result
    print(f"Selected {len(chosen):,}/{len(result):,} eligible studies.")
    print(f"Download bytes: {int(chosen['download_bytes'].sum()) if not chosen.empty else 0:,}")
    print(f"Uncompressed bytes: {int(chosen['uncompressed_bytes'].sum()) if not chosen.empty else 0:,}")
    print(f"Manifest: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
