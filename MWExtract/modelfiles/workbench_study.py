"""Study-level REST, mwTab, FTP planning, and selective mzML extraction.

Workbench's REST API supplies study/analysis metadata and mwTab raw filenames.
The corresponding mzML payloads remain inside FTP ZIP archives, so this module
plans against remote ZIP directories before any complete archive is downloaded.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from ftplib import FTP, all_errors, error_perm
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

import pandas as pd
import requests

from workbench_peek import (
    MW_FTP_HOST,
    MW_FTP_STUDIES_DIR,
    MW_REST_BASE,
    WorkbenchPeekConfig,
    peek_remote_zip_members,
)


STUDY_ID_PATTERN = re.compile(r"^ST\d{6}$", re.IGNORECASE)
AVAILABLE_STUDY_PATTERN = re.compile(
    r"(?im)^study_id\s*[\t:]\s*(ST\d{6})\s*$"
)


@dataclass(frozen=True)
class StudyRESTBundle:
    """Hold REST summary, analyses, parsed mwTab documents, and raw mzML names."""

    study_id: str
    summary: dict[str, Any]
    analyses: tuple[dict[str, Any], ...]
    mwtab_documents: tuple[Any, ...]
    mzml_filenames: tuple[str, ...]
    errors: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StudyMzMLPlan:
    """Describe remote archives and the mwTab-matched mzML members they contain."""

    study_id: str
    expected_mzml_filenames: tuple[str, ...]
    archive_candidates: pd.DataFrame
    inspection_status: pd.DataFrame
    mzml_members: pd.DataFrame
    selected_archives: pd.DataFrame
    selected_members: pd.DataFrame


@dataclass(frozen=True)
class StudyMzMLDownloadResult:
    """Bundle REST metadata, a download plan, archive transfers, and extracted files."""

    rest: StudyRESTBundle
    plan: StudyMzMLPlan
    archive_manifest: pd.DataFrame
    extracted_files: pd.DataFrame
    dry_run: bool


def mwtab_id_field(accession: str) -> str:
    """Return the REST path segment that keys an mwTab lookup by accession type.

    The Workbench REST API keys study accessions (``ST``) under ``study_id``
    and analysis accessions (``AN``) under ``analysis_id``.  This replicates
    ``mwtab.mwrest.generate_mwtab_urls`` for the one case this codebase uses,
    so that dependency can be dropped.
    """

    return "analysis_id" if str(accession).upper().startswith("AN") else "study_id"


def validate_study_id(study_id: str) -> str:
    """Validate and canonicalize a Workbench ``ST######`` accession."""

    normalized = study_id.strip().upper()
    if not STUDY_ID_PATTERN.fullmatch(normalized):
        raise ValueError("Study IDs must look like ST002558.")
    return normalized


def _open_ftp(email: str, timeout: float) -> FTP:
    """Open and initialize one anonymous binary-mode Workbench FTP connection."""

    ftp = FTP(MW_FTP_HOST, timeout=timeout)
    # See workbench_peek._open_ftp: active mode cannot work from a container.
    ftp.set_pasv(True)
    ftp.login(user="anonymous", passwd=email)
    ftp.cwd(MW_FTP_STUDIES_DIR)
    ftp.voidcmd("TYPE I")
    return ftp


def _close_ftp(ftp: FTP | None) -> None:
    """Close an FTP connection without masking the original pipeline error."""

    if ftp is None:
        return
    try:
        ftp.quit()
    except Exception:
        ftp.close()


@contextmanager
def ftp_session(email: str, timeout: float = 60.0) -> Iterator[FTP]:
    """Yield an anonymous binary FTP session in the Workbench Studies directory."""

    ftp = _open_ftp(email, timeout)
    try:
        yield ftp
    finally:
        _close_ftp(ftp)


def _request_text(
    session: requests.Session,
    url: str,
    *,
    timeout: float,
    max_retries: int,
    retry_backoff_seconds: Sequence[float],
) -> str:
    """GET one REST resource with retries that honor HTTP ``Retry-After``."""

    if max_retries and not retry_backoff_seconds:
        raise ValueError("retry_backoff_seconds cannot be empty when retries are enabled.")
    final_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            final_error = exc
            if attempt >= max_retries:
                break
            delay = retry_backoff_seconds[min(attempt, len(retry_backoff_seconds) - 1)]
            response = getattr(exc, "response", None)
            retry_after = response.headers.get("Retry-After") if response is not None else None
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        retry_time = parsedate_to_datetime(retry_after)
                        if retry_time.tzinfo is None:
                            retry_time = retry_time.replace(tzinfo=timezone.utc)
                        seconds = (retry_time - datetime.now(timezone.utc)).total_seconds()
                        delay = max(delay, seconds, 0.0)
                    except (TypeError, ValueError, OverflowError):
                        pass
            time.sleep(delay)
    assert final_error is not None
    raise final_error


def parse_concatenated_json(text: str) -> tuple[Any, ...]:
    """Parse one or more adjacent JSON documents returned by the mwTab endpoint."""

    decoder = json.JSONDecoder()
    documents: list[Any] = []
    position = 0
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            break
        document, position = decoder.raw_decode(text, position)
        documents.append(document)
    return tuple(documents)


def _mapping_records(payload: Any) -> tuple[dict[str, Any], ...]:
    """Normalize common Workbench list, record, and numbered-record JSON shapes."""

    if isinstance(payload, list):
        return tuple(item for item in payload if isinstance(item, dict))
    if isinstance(payload, dict):
        if not payload:
            return ()
        nested = tuple(item for item in payload.values() if isinstance(item, dict))
        if nested and len(nested) == len(payload):
            return nested
        return (payload,)
    return ()


def _json_strings(value: Any) -> Iterator[tuple[str | None, str]]:
    """Yield every string in nested JSON together with its immediate mapping key."""

    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                yield str(key), item
            else:
                yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                yield None, item
            else:
                yield from _json_strings(item)


def mwtab_mzml_filenames(documents: Sequence[Any]) -> tuple[str, ...]:
    """Collect unique mzML raw filenames reported anywhere in mwTab JSON."""

    names: dict[str, str] = {}
    for document in documents:
        for key, value in _json_strings(document):
            normalized_key = re.sub(r"[^a-z0-9]+", "", (key or "").lower())
            candidates = [part.strip() for part in re.split(r"[;,|]", value)]
            for candidate in candidates:
                normalized = candidate.replace("\\", "/")
                if normalized.lower().endswith(".mzml") and (
                    normalized_key in {"rawfilename", "rawfilenames"}
                    or "/" not in normalized
                    or normalized_key.endswith("filename")
                ):
                    basename = PurePosixPath(normalized).name
                    names.setdefault(basename.lower(), basename)
    return tuple(names[key] for key in sorted(names))


def _cached_request_text(
    session: requests.Session,
    url: str,
    *,
    cache_path: Path | None,
    refresh_cache: bool,
    timeout: float,
    max_retries: int,
    retry_backoff_seconds: Sequence[float],
) -> str:
    """Read a successful cached REST response or request and cache it atomically."""

    if cache_path is not None and cache_path.exists() and not refresh_cache:
        return cache_path.read_text(encoding="utf-8")
    text = _request_text(
        session,
        url,
        timeout=timeout,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_name(cache_path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(cache_path)
    return text


def fetch_available_study_ids(
    *,
    session: requests.Session | None = None,
    cache_dir: str | Path | None = None,
    refresh_cache: bool = False,
    timeout: float = 120.0,
    max_retries: int = 5,
    retry_backoff_seconds: tuple[float, ...] = (2.0, 5.0, 15.0, 30.0, 60.0),
) -> tuple[str, ...]:
    """Fetch every valid study accession through one Workbench bulk request."""

    client = session or requests.Session()
    owns_session = session is None
    client.headers.setdefault(
        "User-Agent", "mwextract-batch/0.2 (Metabolomics Workbench research client)"
    )
    cache_path = Path(cache_dir) / "available_studies.txt" if cache_dir else None
    url = f"{MW_REST_BASE}/study/study_id/ST/available/json"
    try:
        text = _cached_request_text(
            client,
            url,
            cache_path=cache_path,
            refresh_cache=refresh_cache,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )
    finally:
        if owns_session:
            client.close()
    study_ids = tuple(dict.fromkeys(AVAILABLE_STUDY_PATTERN.findall(text.upper())))
    if not study_ids:
        raise ValueError("The Workbench available-studies response contained no accessions.")
    return study_ids


def fetch_study_rest_bundle(
    study_id: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 60.0,
    max_retries: int = 5,
    retry_backoff_seconds: tuple[float, ...] = (2.0, 5.0, 15.0, 30.0, 60.0),
    request_delay_seconds: float = 1.0,
    resources_to_fetch: Sequence[str] = ("summary", "analysis", "mwtab"),
    cache_dir: str | Path | None = None,
    refresh_cache: bool = False,
) -> StudyRESTBundle:
    """Fetch selected REST resources, optionally reusing raw-response files."""

    study_id = validate_study_id(study_id)
    client = session or requests.Session()
    owns_session = session is None
    client.headers.setdefault(
        "User-Agent", "mwextract-batch/0.2 (Metabolomics Workbench research client)"
    )
    resources: dict[str, str] = {}
    errors: dict[str, str] = {}
    resource_urls = {
        "summary": f"{MW_REST_BASE}/study/study_id/{study_id}/summary",
        "analysis": f"{MW_REST_BASE}/study/study_id/{study_id}/analysis",
        # Built inline rather than via mwtab.mwrest.generate_mwtab_urls, which
        # produced exactly this string.  That one call cost the image three
        # packages -- docopt, jsonschema and setuptools_scm -- because mwtab
        # must be installed --no-deps (its pins conflict with the solved
        # environment) and then imports jsonschema anyway.  Dropping it removes
        # three dependencies from the only container that needs network egress.
        "mwtab": f"{MW_REST_BASE}/study/{mwtab_id_field(study_id)}/{study_id}/mwtab/json",
    }
    unknown_resources = set(resources_to_fetch).difference(resource_urls)
    if unknown_resources:
        raise ValueError(f"Unknown REST resources: {sorted(unknown_resources)}")
    items = tuple(dict.fromkeys(resources_to_fetch))
    try:
        for index, item in enumerate(items):
            url = resource_urls[item]
            cache_path = (
                Path(cache_dir) / study_id / f"{item}.txt"
                if cache_dir is not None
                else None
            )
            try:
                resources[item] = _cached_request_text(
                    client,
                    url,
                    cache_path=cache_path,
                    refresh_cache=refresh_cache,
                    timeout=timeout,
                    max_retries=max_retries,
                    retry_backoff_seconds=retry_backoff_seconds,
                )
            except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                errors[item] = repr(exc)
            if index < len(items) - 1:
                time.sleep(request_delay_seconds)
    finally:
        if owns_session:
            client.close()

    summary: dict[str, Any] = {}
    analyses: tuple[dict[str, Any], ...] = ()
    mwtab_documents: tuple[Any, ...] = ()
    try:
        summary_payload = json.loads(resources.get("summary", "{}"))
        summary = summary_payload if isinstance(summary_payload, dict) else {}
    except json.JSONDecodeError as exc:
        errors["summary_parse"] = repr(exc)
    try:
        analyses = _mapping_records(json.loads(resources.get("analysis", "{}")))
    except json.JSONDecodeError as exc:
        errors["analysis_parse"] = repr(exc)
    try:
        mwtab_documents = parse_concatenated_json(resources.get("mwtab", ""))
    except json.JSONDecodeError as exc:
        errors["mwtab_parse"] = repr(exc)

    return StudyRESTBundle(
        study_id=study_id,
        summary=summary,
        analyses=analyses,
        mwtab_documents=mwtab_documents,
        mzml_filenames=mwtab_mzml_filenames(mwtab_documents),
        errors=errors,
    )


def list_study_archive_candidates(
    study_id: str,
    *,
    email: str,
    timeout: float = 60.0,
) -> pd.DataFrame:
    """List study-prefixed ZIPs, including targeted raw-data archive suffixes."""

    study_id = validate_study_id(study_id)
    columns = ["study_id", "remote_name", "archive_name", "bytes"]
    rows: list[dict[str, Any]] = []
    with ftp_session(email, timeout) as ftp:
        try:
            remote_names = ftp.nlst(f"{study_id}*")
        except error_perm:
            remote_names = ftp.nlst()
        for remote_name in remote_names:
            archive_name = PurePosixPath(remote_name).name
            if not (
                archive_name.upper().startswith(study_id)
                and archive_name.lower().endswith(".zip")
            ):
                continue
            try:
                size = ftp.size(remote_name)
            except (OSError, error_perm):
                size = None
            rows.append(
                {
                    "study_id": study_id,
                    "remote_name": remote_name,
                    "archive_name": archive_name,
                    "bytes": size,
                }
            )
    return pd.DataFrame(rows, columns=columns).sort_values(
        ["bytes", "archive_name"], na_position="last", ignore_index=True
    )


def plan_study_mzml_download(
    study_id: str,
    expected_mzml_filenames: Sequence[str],
    *,
    email: str,
    timeout: float = 60.0,
    peek_config: WorkbenchPeekConfig = WorkbenchPeekConfig(),
) -> StudyMzMLPlan:
    """Match mwTab mzML names against remote ZIP central-directory listings."""

    study_id = validate_study_id(study_id)
    expected = {PurePosixPath(name.replace("\\", "/")).name.lower() for name in expected_mzml_filenames}
    archives = list_study_archive_candidates(study_id, email=email, timeout=timeout)
    member_rows: list[dict[str, Any]] = []
    status_rows: list[dict[str, Any]] = []

    with ftp_session(email, timeout) as ftp:
        for archive in archives.itertuples():
            try:
                remote_size = int(archive.bytes) if pd.notna(archive.bytes) else ftp.size(archive.remote_name)
                if remote_size is None:
                    raise ValueError("Archive size is unavailable.")
                members = peek_remote_zip_members(
                    ftp, archive.remote_name, int(remote_size), peek_config
                )
                mzml = members[
                    members["member"].str.lower().str.endswith(".mzml", na=False)
                ]
                for member in mzml.itertuples():
                    basename = PurePosixPath(member.member).name
                    member_rows.append(
                        {
                            "study_id": study_id,
                            "archive_name": archive.archive_name,
                            "remote_name": archive.remote_name,
                            "member": member.member,
                            "basename": basename,
                            "mwtab_match": basename.lower() in expected,
                            "uncompressed_bytes": int(member.uncompressed_bytes),
                            "compressed_bytes": int(member.compressed_bytes),
                        }
                    )
                status_rows.append(
                    {
                        "archive_name": archive.archive_name,
                        "peek_success": True,
                        "zip_member_count": len(members),
                        "mzml_member_count": len(mzml),
                        "peek_error": None,
                    }
                )
            except (OSError, error_perm, ValueError, zipfile.BadZipFile) as exc:
                status_rows.append(
                    {
                        "archive_name": archive.archive_name,
                        "peek_success": False,
                        "zip_member_count": 0,
                        "mzml_member_count": 0,
                        "peek_error": repr(exc),
                    }
                )

    member_columns = [
        "study_id", "archive_name", "remote_name", "member", "basename",
        "mwtab_match", "uncompressed_bytes", "compressed_bytes",
    ]
    members = pd.DataFrame(member_rows, columns=member_columns)
    selected_members = members[members["mwtab_match"]].copy()
    selected_names = set(selected_members["archive_name"])
    selected_archives = archives[archives["archive_name"].isin(selected_names)].copy()
    status = pd.DataFrame(
        status_rows,
        columns=[
            "archive_name", "peek_success", "zip_member_count",
            "mzml_member_count", "peek_error",
        ],
    )
    return StudyMzMLPlan(
        study_id=study_id,
        expected_mzml_filenames=tuple(sorted(expected)),
        archive_candidates=archives,
        inspection_status=status,
        mzml_members=members,
        selected_archives=selected_archives,
        selected_members=selected_members,
    )


def _first_corrupt_zip_member(path: Path) -> str | None:
    """Return the name of the first member that fails CRC validation, or
    the reason the file is not a readable ZIP at all; ``None`` means the
    whole archive checks out.

    Matching size alone does not prove a resumed download is byte-correct:
    if the local partial file's length and the server's REST offset for it
    ever disagree, the appended bytes silently splice onto the wrong place
    in the stream, producing a file of the expected size whose contents are
    corrupt. This is the only check that would catch that before it reaches
    extraction.
    """

    try:
        with zipfile.ZipFile(path) as archive:
            return archive.testzip()
    except zipfile.BadZipFile as exc:
        return str(exc)


def _sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """Hash a downloaded archive without loading it into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_selected_archives(
    archives: pd.DataFrame,
    output_dir: Path,
    *,
    email: str,
    max_total_archive_bytes: int,
    timeout: float,
    max_retries: int = 8,
    retry_backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0),
) -> pd.DataFrame:
    """Download planned archives atomically while enforcing a total byte limit.

    A stalled or reset FTP data connection (seen in practice as a
    ``TimeoutError`` from ``retrbinary`` on slow Workbench transfers) retries
    on a fresh connection rather than failing the whole batch outright; the
    ``.part`` file's existing bytes are kept across retries so a retry
    resumes instead of restarting.
    """

    columns = ["study_id", "archive_name", "local_path", "bytes", "sha256", "status"]
    if archives.empty:
        return pd.DataFrame(columns=columns)
    if archives["bytes"].isna().any():
        raise ValueError("Every selected archive must have a known size before download.")
    total = int(archives["bytes"].sum())
    if total > max_total_archive_bytes:
        raise ValueError(
            f"Selected archives require {total:,} bytes; limit is {max_total_archive_bytes:,}."
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    ftp: FTP | None = None
    try:
        for archive in archives.itertuples():
            destination = output_dir / archive.archive_name
            partial = destination.with_name(destination.name + ".part")
            expected_size = int(archive.bytes)
            if (
                destination.exists()
                and destination.stat().st_size == expected_size
                and _first_corrupt_zip_member(destination) is None
            ):
                status = "cached"
            else:
                if destination.exists():
                    # Size matched on a previous run but content did not;
                    # a stale "cached" hit must not keep serving corrupt
                    # bytes to every subsequent fetch attempt.
                    destination.unlink()
                for attempt in range(max_retries + 1):
                    try:
                        if ftp is None:
                            ftp = _open_ftp(email, timeout)
                        offset = partial.stat().st_size if partial.exists() else 0
                        if offset > expected_size:
                            partial.unlink()
                            offset = 0
                        mode = "ab" if offset else "wb"
                        with partial.open(mode) as handle:
                            ftp.retrbinary(
                                f"RETR {archive.remote_name}",
                                handle.write,
                                blocksize=1024 * 1024,
                                rest=offset if offset else None,
                            )
                        if partial.stat().st_size != expected_size:
                            raise OSError(f"Incomplete download for {archive.archive_name}.")
                        corrupt_member = _first_corrupt_zip_member(partial)
                        if corrupt_member is not None:
                            # A resumed transfer is the prime suspect (see
                            # _first_corrupt_zip_member); only a full
                            # re-download, not another resume on top of
                            # corrupt bytes, can recover.
                            partial.unlink()
                            raise OSError(
                                f"{archive.archive_name} failed CRC validation "
                                f"(bad member {corrupt_member!r}); "
                                "redownloading from scratch."
                            )
                        break
                    except all_errors as exc:
                        # A failed/aborted transfer can desynchronize FTP
                        # control state, so every retry starts with a newly
                        # authenticated connection.
                        _close_ftp(ftp)
                        ftp = None
                        if attempt >= max_retries:
                            raise
                        delay = retry_backoff_seconds[
                            min(attempt, len(retry_backoff_seconds) - 1)
                        ]
                        print(
                            f"Retrying {archive.archive_name} in {delay:g}s "
                            f"after {exc!r}."
                        )
                        time.sleep(delay)
                partial.replace(destination)
                status = "downloaded"
            rows.append(
                {
                    "study_id": archive.study_id,
                    "archive_name": archive.archive_name,
                    "local_path": str(destination.resolve()),
                    "bytes": destination.stat().st_size,
                    "sha256": _sha256(destination),
                    "status": status,
                }
            )
    finally:
        _close_ftp(ftp)
    return pd.DataFrame(rows, columns=columns)


def download_selected_archives(
    archives: pd.DataFrame,
    output_dir: str | Path,
    *,
    email: str,
    max_total_archive_bytes: int,
    timeout: float = 60.0,
    max_retries: int = 8,
    retry_backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0),
) -> pd.DataFrame:
    """Download a pre-inspected archive table for any supported raw format.

    The input must contain the ``study_id``, ``archive_name``, ``remote_name``,
    and ``bytes`` columns produced by :func:`list_study_archive_candidates`.
    Complete ZIP archives are transferred because Workbench does not expose
    individual compressed members as independent FTP files.
    """

    required = {"study_id", "archive_name", "remote_name", "bytes"}
    missing = required.difference(archives.columns)
    if missing:
        raise ValueError(f"Archive table is missing columns: {sorted(missing)}")
    return _download_selected_archives(
        archives,
        Path(output_dir),
        email=email,
        max_total_archive_bytes=max_total_archive_bytes,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        timeout=timeout,
    )


def _safe_zip_member(name: str) -> PurePosixPath:
    """Normalize a ZIP member and reject absolute or parent-traversal paths."""

    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"Unsafe ZIP member {name!r}.")
    return path


def extract_selected_members(
    members: pd.DataFrame,
    manifest: pd.DataFrame,
    output_dir: str | Path,
    *,
    max_total_uncompressed_bytes: int,
    discard_archives: bool = False,
) -> pd.DataFrame:
    """Safely extract an arbitrary member plan into one flat output directory.

    ``members`` must identify each selected member by study and archive.
    Every selected member is written as ``output_dir / basename``, except
    that members whose basename collides (seen in practice: polarity
    subfolders reusing the same filename) are qualified with their parent
    directory name instead, so one archive member can never silently
    overwrite another.

    With ``discard_archives``, each ZIP is deleted once all of its planned
    members have been extracted, so peak disk is one archive plus the extracted
    members rather than the whole corpus twice.  The archive remains
    re-downloadable from the Workbench, and deletion is skipped if anything
    raised, leaving the archive available for a retry.
    """

    columns = [
        "study_id", "archive_name", "logical_input", "format", "member",
        "local_path", "uncompressed_bytes", "status",
    ]
    required_members = {
        "study_id", "archive_name", "logical_input", "format", "member",
        "uncompressed_bytes",
    }
    required_manifest = {"archive_name", "local_path"}
    missing_members = required_members.difference(members.columns)
    missing_manifest = required_manifest.difference(manifest.columns)
    if missing_members:
        raise ValueError(f"Member plan is missing columns: {sorted(missing_members)}")
    if missing_manifest:
        raise ValueError(
            f"Archive manifest is missing columns: {sorted(missing_manifest)}"
        )
    if members.empty:
        return pd.DataFrame(columns=columns)

    unique_members = members.drop_duplicates(
        subset=["study_id", "archive_name", "member"]
    ).copy()

    # Flattening drops each member's archive subdirectory, which collides
    # when two different members share a basename -- seen in practice for
    # studies that split one acquisition into polarity subfolders (NEG/,
    # POS/) using otherwise identical filenames. Disambiguate those with
    # their immediate parent directory name before falling back to
    # rejecting a collision no amount of context can resolve.
    sources_by_basename: dict[str, list[tuple[str, str]]] = {}
    for selected in unique_members.itertuples():
        member = _safe_zip_member(selected.member).as_posix()
        source = (str(selected.archive_name), member)
        basename_key = PurePosixPath(member).name.casefold()
        sources_by_basename.setdefault(basename_key, []).append(source)

    destination_names: dict[tuple[str, str], str] = {}
    for sources in sources_by_basename.values():
        unique_sources = sorted(set(sources))
        if len(unique_sources) == 1:
            (archive_name, member), = unique_sources
            destination_names[archive_name, member] = PurePosixPath(member).name
            continue
        tagged: dict[str, tuple[str, str]] = {}
        for archive_name, member in unique_sources:
            basename = PurePosixPath(member).name
            parent = PurePosixPath(member).parent.name
            tag = re.sub(r"[^A-Za-z0-9._-]+", "_", parent) if parent else ""
            name = f"{tag}__{basename}" if tag else basename
            name_key = name.casefold()
            previous = tagged.get(name_key)
            if previous is not None and previous != (archive_name, member):
                raise ValueError(
                    "Selected archive members cannot be flattened because "
                    "their filenames collide even after disambiguating by "
                    f"parent directory: {previous[0]}:{previous[1]} and "
                    f"{archive_name}:{member}."
                )
            tagged[name_key] = (archive_name, member)
            destination_names[archive_name, member] = name

    total = int(unique_members["uncompressed_bytes"].sum())
    if total > max_total_uncompressed_bytes:
        raise ValueError(
            f"Selected raw files expand to {total:,} bytes; "
            f"limit is {max_total_uncompressed_bytes:,}."
        )

    manifest_paths = {
        row.archive_name: Path(row.local_path) for row in manifest.itertuples()
    }
    output_root = Path(output_dir).resolve()
    rows: list[dict[str, Any]] = []
    for archive_name, group in unique_members.groupby("archive_name"):
        if archive_name not in manifest_paths:
            raise ValueError(f"No downloaded archive is recorded for {archive_name}.")
        archive_path = manifest_paths[archive_name]
        with zipfile.ZipFile(archive_path) as archive:
            info_by_safe_name = {
                _safe_zip_member(info.filename).as_posix(): info
                for info in archive.infolist()
                if not info.is_dir()
            }
            for selected in group.itertuples():
                member = _safe_zip_member(selected.member).as_posix()
                if member not in info_by_safe_name:
                    raise KeyError(f"Planned member is absent from {archive_name}: {member}")
                info = info_by_safe_name[member]
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError(f"Symlink archive members are not extracted: {member}")

                destination_name = destination_names[str(archive_name), member]
                destination = (output_root / destination_name).resolve()
                if not destination.is_relative_to(output_root):
                    raise ValueError(f"ZIP member escapes the output directory: {member}")
                output_root.mkdir(parents=True, exist_ok=True)
                if destination.exists() and destination.stat().st_size == info.file_size:
                    status = "cached"
                else:
                    temporary = destination.with_name(destination.name + ".part")
                    with archive.open(info) as source, temporary.open("wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    if temporary.stat().st_size != info.file_size:
                        raise IOError(f"Incomplete extraction for {archive_name}:{member}")
                    temporary.replace(destination)
                    status = "extracted"
                rows.append(
                    {
                        "study_id": selected.study_id,
                        "archive_name": archive_name,
                        "logical_input": selected.logical_input,
                        "format": selected.format,
                        "member": member,
                        "local_path": str(destination),
                        "uncompressed_bytes": info.file_size,
                        "status": status,
                    }
                )
        # Outside the ``with``: the handle must be closed before unlink on
        # Windows.  Reached only when every member above extracted without
        # raising, so a failure leaves the archive in place for a retry.
        if discard_archives:
            try:
                archive_path.unlink()
            except OSError as error:
                print(f"warning: could not delete {archive_path.name}: {error}")
    return pd.DataFrame(rows, columns=columns)


def _extract_selected_mzml(
    plan: StudyMzMLPlan,
    manifest: pd.DataFrame,
    output_dir: Path,
    *,
    max_total_uncompressed_bytes: int,
) -> pd.DataFrame:
    """Extract only mwTab-matched mzML members while preserving archive-relative paths."""

    columns = [
        "study_id", "archive_name", "member", "local_path",
        "uncompressed_bytes", "status",
    ]
    total = int(plan.selected_members["uncompressed_bytes"].sum())
    if total > max_total_uncompressed_bytes:
        raise ValueError(
            f"Selected mzML files expand to {total:,} bytes; "
            f"limit is {max_total_uncompressed_bytes:,}."
        )
    manifest_paths = {
        row.archive_name: Path(row.local_path) for row in manifest.itertuples()
    }
    rows: list[dict[str, Any]] = []
    output_root = output_dir.resolve()
    for archive_name, group in plan.selected_members.groupby("archive_name"):
        archive_path = manifest_paths[archive_name]
        with zipfile.ZipFile(archive_path) as archive:
            info_by_safe_name = {
                _safe_zip_member(info.filename).as_posix(): info
                for info in archive.infolist()
                if not info.is_dir()
            }
            for selected in group.itertuples():
                member = _safe_zip_member(selected.member).as_posix()
                info = info_by_safe_name[member]
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError(f"Symlink mzML members are not extracted: {member}")
                relative = Path(archive_path.stem, *PurePosixPath(member).parts)
                destination = (output_root / relative).resolve()
                if not destination.is_relative_to(output_root):
                    raise ValueError(f"ZIP member escapes the output directory: {member}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() and destination.stat().st_size == info.file_size:
                    status = "cached"
                else:
                    temporary = destination.with_name(destination.name + ".part")
                    with archive.open(info) as source, temporary.open("wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    temporary.replace(destination)
                    status = "extracted"
                rows.append(
                    {
                        "study_id": plan.study_id,
                        "archive_name": archive_name,
                        "member": member,
                        "local_path": str(destination),
                        "uncompressed_bytes": info.file_size,
                        "status": status,
                    }
                )
    return pd.DataFrame(rows, columns=columns)


def download_study_mzml_files(
    study_id: str,
    output_dir: str | Path,
    *,
    email: str,
    dry_run: bool = True,
    max_total_archive_bytes: int = 5_000_000_000,
    max_total_uncompressed_bytes: int = 10_000_000_000,
    timeout: float = 60.0,
    rest_timeout: float = 60.0,
    peek_config: WorkbenchPeekConfig = WorkbenchPeekConfig(),
) -> StudyMzMLDownloadResult:
    """Use REST and mwTab to plan, then optionally download matching mzML files.

    Workbench stores mzML members inside ZIP archives, so ``dry_run=False`` must
    download each selected complete archive before extracting only the mwTab-matched
    mzML members. The two byte limits prevent an accidental oversized transfer.
    """

    study_id = validate_study_id(study_id)
    output_dir = Path(output_dir)
    rest = fetch_study_rest_bundle(study_id, timeout=rest_timeout)
    plan = plan_study_mzml_download(
        study_id,
        rest.mzml_filenames,
        email=email,
        timeout=timeout,
        peek_config=peek_config,
    )
    empty_manifest = pd.DataFrame(
        columns=["study_id", "archive_name", "local_path", "bytes", "sha256", "status"]
    )
    empty_extracted = pd.DataFrame(
        columns=[
            "study_id", "archive_name", "member", "local_path",
            "uncompressed_bytes", "status",
        ]
    )
    if dry_run or plan.selected_members.empty:
        return StudyMzMLDownloadResult(
            rest=rest,
            plan=plan,
            archive_manifest=empty_manifest,
            extracted_files=empty_extracted,
            dry_run=dry_run,
        )

    manifest = _download_selected_archives(
        plan.selected_archives,
        output_dir / "archives",
        email=email,
        max_total_archive_bytes=max_total_archive_bytes,
        timeout=timeout,
    )
    extracted = _extract_selected_mzml(
        plan,
        manifest,
        output_dir / "mzml",
        max_total_uncompressed_bytes=max_total_uncompressed_bytes,
    )
    return StudyMzMLDownloadResult(
        rest=rest,
        plan=plan,
        archive_manifest=manifest,
        extracted_files=extracted,
        dry_run=False,
    )
