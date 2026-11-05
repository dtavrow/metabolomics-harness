"""Fetch the MS/MS payload of ONE Metabolomics Workbench study.

The image handles exactly one study per invocation. That is not a mode, it is
the contract: the orchestrating agent loops, and between iterations it parses
the payload and deletes it. The reason is disk. The product-ion corpus is
1,134 GB uncompressed across 298 studies, but the largest single study is
49 GB and the median is 1.9 GB -- so a one-study-at-a-time loop turns a figure
that does not fit on the machine into one that comfortably does.

It does two things and nothing else:

    --list      enumerate the studies matching a filter, as a work list for
                the agent's loop. Reads only the baked index: no network, no
                credentials, no output files.
    --study ID  select that study's qualifying files, fetch its archive over
                FTP, extract only those files, delete the archive.

Parsing is a separate image, because the parser has no business sharing a
rebuild cycle with the only component that needs network egress.

Output for a study lands in a single directory named for it, so the agent's
cleanup step is one recursive delete of a path it can predict before the run
starts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parent
if str(MODEL_DIR) not in sys.path:
    sys.path.insert(0, str(MODEL_DIR))

#: Baked at build time.  A snapshot of the Workbench as of the audit date in
#: the image label -- studies deposited later are invisible to it, which is
#: intentional: a run must be reproducible against a fixed corpus.
DEFAULT_INDEX = MODEL_DIR / "workbench_msms_index.parquet"
DEFAULT_OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/output"))

#: Formats a parser reads without a vendor conversion step.  The conversion
#: path shells out to a ProteoWizard *container*, which cannot run from inside
#: this one without the host docker socket -- deliberately not requested.  The
#: excluded remainder is 35 mzdata files of 20,764.
DIRECT_FORMATS = ("mzml", "mzxml", "mgf", "msp")

#: Per-study ceilings, sized so the largest study in the corpus (49.0 GB
#: uncompressed, 19.7 GB compressed) passes.  These bound ONE study; the agent
#: bounds the campaign by deciding how many studies to loop over.
DEFAULT_MAX_DOWNLOAD = "25GB"
DEFAULT_MAX_UNCOMPRESSED = "60GB"

#: Keys a spec file may set.  An explicit allowlist rather than a passthrough,
#: so a spec cannot inject an argument the entrypoint never meant to expose.
SPEC_KEYS = frozenset({
    "study", "files", "files_file", "statuses", "formats",
    "acquisition_types", "acquisition_confidence", "allow_unclassified_acquisition",
    "polarity", "instrument_types", "ionization",
    "require_collision_energy", "min_ms2_spectra",
    "max_download_bytes", "max_uncompressed_bytes",
    "dry_run", "email", "email_file",
})

#: Index columns a value filter may target, mapped to the flag driving them.
VALUE_FILTERS = (
    ("format", "formats"),
    ("validation_status", "statuses"),
    ("acquisition_type", "acquisition_types"),
    ("acquisition_confidence", "acquisition_confidence"),
    ("polarity", "polarity"),
    ("instrument_type", "instrument_types"),
    ("ionization", "ionization"),
)


def build_parser() -> argparse.ArgumentParser:
    """Define the interface: one study, or a work list of them."""

    parser = argparse.ArgumentParser(
        prog="MWExtract", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--study", help="The single study accession to fetch, e.g. ST004496.")
    parser.add_argument("--list", action="store_true", dest="list_only",
                        help="Print the accessions matching the filters, one per line, with a "
                             "commented summary. Index only: no network, no output files. "
                             "This is the agent's work list.")
    parser.add_argument("--spec", type=Path,
                        help="JSON run spec. Command-line flags override its values.")
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX,
                        help="Corpus index. Defaults to the one baked into the image.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="Parent directory. The study's files land in <output>/<study>/.")

    files = parser.add_argument_group("which files within the study")
    files.add_argument("--files", nargs="+",
                       help="Restrict to these file names, matched against the bare name or "
                            "the path inside the archive.")
    files.add_argument("--files-file", type=Path, help="File of file names, one per line.")
    files.add_argument("--formats", nargs="+", default=DIRECT_FORMATS,
                       help="Default: the formats a parser reads directly. Adding mzdata "
                            "selects files this image cannot convert.")
    files.add_argument("--statuses", nargs="+", default=("confirmed",),
                       help="Audit validation statuses to accept. Default: confirmed only.")
    files.add_argument("--acquisition-types", nargs="+", default=("product_ion",),
                       help="Acquisition modes to keep. Default: product-ion scans only.")
    files.add_argument("--acquisition-confidence", nargs="+",
                       choices=("declared", "instrument_prior", "unknown"),
                       help="How the acquisition mode was established. 'declared' (11,286 "
                            "files corpus-wide) means the study metadata named it; "
                            "'instrument_prior' (8,798) means it was inferred from the "
                            "instrument class alone.")
    files.add_argument("--allow-unclassified-acquisition", action="store_true",
                       help="Keep files the classifier could not place. Off by default: an "
                            "unclassified file is as likely to be MRM as anything else.")
    files.add_argument("--polarity", nargs="+", choices=("positive", "negative"),
                       help="Ion mode. 33.8%% of files corpus-wide have none recorded and are "
                            "dropped by this filter.")
    files.add_argument("--instrument-types", nargs="+", help="Instrument class, e.g. Orbitrap.")
    files.add_argument("--ionization", nargs="+",
                       help="Ionization source, matched as a case-insensitive SUBSTRING: the "
                            "field is free text and submitters write ESI as 'ESI', 'ES+', "
                            "'H-ESI' and 'Electrospray ionization (ESI)'.")
    files.add_argument("--require-collision-energy", action="store_true",
                       help="Keep only files with a recorded collision energy -- 10.7%% of the "
                            "corpus. The field is usually absent from submitter metadata, "
                            "not from the file.")
    files.add_argument("--min-ms2-spectra", type=int,
                       help="Drop files with fewer than this many MS2 spectra.")

    budget = parser.add_argument_group("per-study ceilings")
    budget.add_argument("--max-download-bytes", default=DEFAULT_MAX_DOWNLOAD,
                        help=f"Default {DEFAULT_MAX_DOWNLOAD}, which clears the largest study "
                             "in the corpus (19.7 GB).")
    budget.add_argument("--max-uncompressed-bytes", default=DEFAULT_MAX_UNCOMPRESSED,
                        help=f"Default {DEFAULT_MAX_UNCOMPRESSED}, which clears the largest "
                             "study in the corpus (49.0 GB).")

    run = parser.add_argument_group("execution")
    run.add_argument("--email", default=os.environ.get("MWB_EMAIL"),
                     help="Contact address for the Workbench FTP, which logs in as anonymous "
                          "and takes an email as the password. Prefer --email-file: a value "
                          "passed here is visible in the host process list and docker inspect.")
    run.add_argument("--email-file", type=Path,
                     default=os.environ.get("MWB_EMAIL_FILE") or None,
                     help="Read the contact address from a file, e.g. a mounted secret at "
                          "/run/secrets/mwb_email. Takes precedence over --email.")
    run.add_argument("--dry-run", action="store_true",
                     help="Select and report without fetching. Needs no network.")
    return parser


def apply_spec(args: argparse.Namespace, argv: list[str]) -> argparse.Namespace:
    """Overlay a JSON spec, letting explicit command-line flags win.

    The spec is the reproducible record, but an operator overriding one value
    on the command line should not have to edit and re-save it to do so.
    """

    if args.spec is None:
        return args
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    unknown = set(spec).difference(SPEC_KEYS)
    if unknown:
        raise SystemExit(f"Unsupported spec keys: {sorted(unknown)}")
    given = {token.lstrip("-").replace("-", "_") for token in argv if token.startswith("--")}
    for key, value in spec.items():
        if key not in given:
            setattr(args, key, value)
    return args


def read_lines(path: Path | None) -> list[str]:
    """Read one value per line, ignoring blanks and ``#`` comments."""

    if path is None:
        return []
    path = Path(path)
    if not path.is_file():
        raise SystemExit(
            f"List file not found: {path}\n"
            "If this path came from a run spec it is a CONTAINER path -- mount the "
            "directory holding it, e.g. -v /local/input:/input:ro")
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        token = line.split("#", 1)[0].strip()
        if token:
            values.append(token)
    return values


def resolve_email(args: argparse.Namespace) -> str | None:
    """Prefer a file-held address over one passed on the command line.

    A value in ``--email`` is readable by anything that can see the host
    process list or run ``docker inspect``; a mounted file is not.  The file
    wins so that mounting one overrides an address inherited from the
    environment.
    """

    if args.email_file:
        path = Path(args.email_file)
        if not path.is_file():
            raise SystemExit(f"--email-file not found: {path}")
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise SystemExit(f"--email-file is empty: {path}")
        return value
    return args.email


def filter_files(table, args: argparse.Namespace, *, report: bool = True):
    """Narrow the index to the qualifying files, reporting each cut.

    Every filter prints what it removed.  An over-narrow chain is the most
    common confusing outcome -- ``--polarity`` alone discards a third of the
    corpus, because polarity is often simply unrecorded -- and a silent
    narrowing leaves the operator guessing which predicate was responsible.
    """

    total = len(table)
    cuts: list[str] = []

    def cut(mask, label: str):
        nonlocal table
        before = len(table)
        table = table[mask]
        removed = before - len(table)
        if removed:
            cuts.append(f"  {label:34s} -{removed:,} -> {len(table):,}")
        if table.empty:
            print(f"No files remain. The filter chain emptied at: {label}")
            for line in cuts:
                print(line)
            raise SystemExit(2)

    names = {value.casefold() for value in (args.files or [])} | {
        value.casefold() for value in read_lines(args.files_file)}
    if names:
        # Match either the bare file name or its path inside the archive, so a
        # caller can paste names from a manifest without knowing which it has.
        base = table["basename"].astype(str).str.casefold()
        full = table["logical_input"].astype(str).str.casefold()
        cut(base.isin(names) | full.isin(names), f"--files ({len(names)})")

    for column, attribute in VALUE_FILTERS:
        wanted = getattr(args, attribute, None)
        if not wanted or column not in table.columns:
            continue
        allowed = {str(value).casefold() for value in wanted}
        if column == "ionization":
            # Substring, not equality: free text, same source written many ways.
            pattern = "|".join(re.escape(value) for value in allowed)
            mask = table[column].astype(str).str.casefold().str.contains(pattern, na=False)
        else:
            mask = table[column].astype(str).str.casefold().isin(allowed)
        if column == "acquisition_type" and args.allow_unclassified_acquisition:
            mask = mask | table[column].isna()
        cut(mask, f"--{attribute.replace('_', '-')}")

    if args.require_collision_energy:
        cut(table["collision_energy"].notna(), "--require-collision-energy")
    if args.min_ms2_spectra:
        cut(table["ms2_spectrum_count"].fillna(0) >= args.min_ms2_spectra,
            f"--min-ms2-spectra {args.min_ms2_spectra}")

    if report and cuts:
        print(f"Filter chain, from {total:,} files:")
        for line in cuts:
            print(line)
    return table


def study_cost(table) -> tuple[float, float, int]:
    """Return (download GB, uncompressed GB, MS2 spectra) for one study's files.

    Download size deduplicates archives first: ``archive_bytes`` is the size
    of the ZIP, repeated on every member row, so a per-file sum over-counts
    by the number of members.
    """

    archives = table.drop_duplicates(["study_id", "archive_name"])
    return (float(archives["archive_bytes"].fillna(0).sum()) / 1e9,
            float(table["uncompressed_bytes"].fillna(0).sum()) / 1e9,
            int(table["ms2_spectrum_count"].fillna(0).sum()))


def emit_work_list(table, args: argparse.Namespace) -> int:
    """Print the matching accessions, one per line, for the agent to loop over.

    Bare accessions on stdout with everything else commented, so the caller can
    pipe it straight into a loop without parsing anything.
    """

    kept = filter_files(table, args, report=False)
    grouped = kept.groupby("study_id")
    download, uncompressed, spectra = study_cost(kept)
    print(f"# {grouped.ngroups} studies, {len(kept):,} files, {spectra:,} MS2 spectra")
    print(f"# {download:,.1f} GB to download, {uncompressed:,.1f} GB extracted, "
          f"if every study below is fetched")
    largest = max(
        float(group["uncompressed_bytes"].fillna(0).sum()) / 1e9
        for _, group in grouped)
    print(f"# largest single study: {largest:,.1f} GB extracted -- peak disk for the loop")
    print("# accession  files  ms2_spectra  extracted_gb")
    for study_id, group in grouped:
        size = float(group["uncompressed_bytes"].fillna(0).sum()) / 1e9
        print(f"{study_id}\t{len(group)}\t{int(group['ms2_spectrum_count'].fillna(0).sum())}"
              f"\t{size:.2f}")
    return 0


def run_stage(name: str, arguments: list) -> None:
    """Invoke one pipeline stage in-process and fail loudly if it does."""

    import importlib

    module = importlib.import_module(name)
    print(f"\n=== {name} ===", flush=True)
    code = module.main([str(value) for value in arguments])
    if code:
        raise SystemExit(f"{name} exited {code}")


def main(argv: list[str] | None = None) -> int:
    """Enumerate studies, or fetch exactly one."""

    argv = list(sys.argv[1:] if argv is None else argv)
    args = apply_spec(build_parser().parse_args(argv), argv)

    index = Path(args.index)
    if not index.is_file():
        raise SystemExit(f"Corpus index not found: {index}")

    import pandas as pd

    table = pd.read_parquet(index)

    if args.list_only:
        return emit_work_list(table, args)
    if not args.study:
        raise SystemExit(
            "--study is required. This image fetches exactly ONE study per run; the "
            "orchestrating agent loops over them and deletes each payload after parsing. "
            "Use --list to get the work list.")

    study = str(args.study).strip()
    in_study = table[table["study_id"].eq(study)]
    if in_study.empty:
        raise SystemExit(
            f"{study} is not in the corpus index. Either it has no payload-verified MS/MS, "
            f"or it was deposited after the index snapshot. --list shows what is available.")

    kept = filter_files(in_study, args)
    download, uncompressed, spectra = study_cost(kept)
    print(f"{study}: {len(kept):,} files, {spectra:,} MS2 spectra, "
          f"{download:,.2f} GB download -> {uncompressed:,.2f} GB extracted", flush=True)

    # The public deliverable contains only the study index slice and spectra.
    # Selection, transfer, and extraction manifests are operational state and
    # stay in a temporary workspace that disappears after this invocation.
    output = Path(args.output) / study
    output.mkdir(parents=True, exist_ok=True)
    study_metadata = output / f"{study}.parquet"
    in_study.to_parquet(study_metadata, index=False)

    workspace_handle = tempfile.TemporaryDirectory(prefix=f"mwextract-{study}-")
    workspace = Path(workspace_handle.name)
    (workspace / "manifests").mkdir(parents=True, exist_ok=True)
    scoped = workspace / "index_selected.parquet"
    kept.to_parquet(scoped, index=False)

    selection = workspace / "manifests" / "selection.tsv"
    run_stage("select_storage", [
        "--audit-tsv", scoped, "--output-tsv", selection,
        "--studies", study,
        "--statuses", *args.statuses,
        "--acquisition-types", *args.acquisition_types,
        "--max-download-bytes", args.max_download_bytes,
        "--max-uncompressed-bytes", args.max_uncompressed_bytes,
        # The index is a file inventory, not a candidate list; candidate
        # filtering belongs to a later image, so never gate on it here.
        "--allow-without-candidates",
    ])

    chosen = pd.read_csv(selection, sep="\t")
    accepted = chosen["selected"].astype(str).str.lower().isin(("true", "1"))
    if not accepted.any():
        reason = chosen["selection_reason"].iloc[0] if len(chosen) else "unknown"
        raise SystemExit(
            f"{study} was not selected: {reason}. It needs {download:,.2f} GB download and "
            f"{uncompressed:,.2f} GB extracted against ceilings of {args.max_download_bytes} "
            f"and {args.max_uncompressed_bytes}. Raise them, or narrow the file filters.")

    if args.dry_run:
        workspace_handle.cleanup()
        print(f"\nDry run: study metadata written to {study_metadata}. Nothing fetched.", flush=True)
        return 0

    email = resolve_email(args)
    if not email:
        raise SystemExit(
            "No contact address. The Workbench FTP logs in as anonymous and takes an email "
            "as the password. Supply it with --email-file (preferred), --env-file, "
            "-e MWB_EMAIL, or --email. Use --dry-run or --list to work without fetching.")

    download_args = [
        "--selection-tsv", selection, "--audit-tsv", scoped,
        "--output-dir", workspace, "--spectra-dir", output / "spectra", "--email", email,
        "--statuses", *args.statuses,
        "--max-download-bytes", args.max_download_bytes,
        "--max-uncompressed-bytes", args.max_uncompressed_bytes,
    ]
    run_stage("download_selected", download_args)

    extracted = workspace / "manifests" / "extracted_files.tsv"
    if not extracted.is_file():
        raise SystemExit(f"No extraction manifest at {extracted}: nothing was extracted.")
    manifest = pd.read_csv(extracted, sep="\t")
    ok = manifest["status"].astype(str).eq("extracted").sum()
    # The agent deletes this directory after parsing, so the run must state
    # plainly what it delivered -- a count the parse step can be checked
    # against, rather than a bare exit code.
    print(f"\n{study}: extracted {ok:,} of {len(manifest):,} planned files to "
          f"{output / 'spectra'}", flush=True)
    print(f"  metadata  {study_metadata}")
    print(f"  delete    {output}", flush=True)
    workspace_handle.cleanup()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
