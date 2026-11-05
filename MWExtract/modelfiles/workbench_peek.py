"""Remote, payload-free inventory helpers for Metabolomics Workbench ZIP archives.

The public orchestration function keeps its historical name for notebook
compatibility, but it never downloads complete archives. It reads the FTP
directory once and uses FTP REST offsets to retrieve only ZIP directory bytes.
"""

from __future__ import annotations

import json
import re
import struct
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from ftplib import FTP, all_errors, error_perm
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping

import numpy as np
import pandas as pd
import requests

from pipeline_formats import BUNDLE_SUFFIXES, FORMAT_COLUMNS, FORMAT_SUFFIXES


MW_FTP_HOST = "www.metabolomicsworkbench.org"
MW_FTP_STUDIES_DIR = "/Studies"
MW_REST_BASE = "https://www.metabolomicsworkbench.org/rest"
STUDY_RE = re.compile(r"(ST\d{6})", re.IGNORECASE)
EXACT_STUDY_ARCHIVE_RE = re.compile(r"^(ST\d{6})\.zip$", re.IGNORECASE)

EOCD = b"PK\x05\x06"
ZIP64_EOCD = b"PK\x06\x06"
ZIP64_LOCATOR = b"PK\x06\x07"
CENTRAL_MEMBER = b"PK\x01\x02"


@dataclass(frozen=True)
class WorkbenchPeekConfig:
    """Set limits for remote ZIP metadata reads; ``None`` means every archive."""

    tail_bytes: int = 131_072
    max_central_directory_bytes: int = 50_000_000
    max_archives: int | None = None
    progress_every: int = 100
    request_delay_seconds: float = 0.2
    max_retries: int = 3
    retry_backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0)
    checkpoint_every: int = 50
    checkpoint_path: str | Path | None = None


@dataclass(frozen=True)
class WorkbenchPeekResult:
    """Hold raw inventories and the final study-indexed selection table."""

    archive_inventory: pd.DataFrame
    archive_peek_status: pd.DataFrame
    member_inventory: pd.DataFrame
    analysis_metadata: pd.DataFrame
    study_dataframe: pd.DataFrame


def _open_ftp(email: str, timeout: float) -> FTP:
    """Open and initialize one anonymous binary-mode Workbench FTP connection."""

    ftp = FTP(MW_FTP_HOST, timeout=timeout)
    # Passive mode explicitly, though it is also ftplib's default.  In ACTIVE
    # mode the server opens the data connection back to the client, which
    # cannot work from inside a container (or behind any NAT): the address the
    # client advertises is its private bridge address and the server's connect
    # attempt is dropped.  Stating it here means a future edit cannot silently
    # turn it off, and a reader debugging a container hang sees the answer.
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
def _ftp_session(email: str, timeout: float) -> Iterator[FTP]:
    """Yield an initialized FTP connection and close it reliably on exit."""

    ftp = _open_ftp(email, timeout)
    try:
        yield ftp
    finally:
        _close_ftp(ftp)


def _validate_peek_config(config: WorkbenchPeekConfig) -> None:
    """Reject unsafe or internally inconsistent retry and checkpoint settings."""

    if config.request_delay_seconds < 0:
        raise ValueError("request_delay_seconds cannot be negative.")
    if config.max_retries < 0:
        raise ValueError("max_retries cannot be negative.")
    if config.max_retries and not config.retry_backoff_seconds:
        raise ValueError("retry_backoff_seconds cannot be empty when retries are enabled.")
    if any(delay < 0 for delay in config.retry_backoff_seconds):
        raise ValueError("retry_backoff_seconds cannot contain negative values.")
    if config.checkpoint_every <= 0:
        raise ValueError("checkpoint_every must be a positive integer.")


def _backoff_delay(delays: tuple[float, ...], retry_number: int) -> float:
    """Return the configured delay for a one-based retry, reusing the final value."""

    return delays[min(retry_number - 1, len(delays) - 1)]


def _directory_entries(ftp: FTP) -> list[tuple[str, int | None]]:
    """List the current FTP directory once, preferring MLSD-provided file sizes."""

    rows: list[tuple[str, int | None]] = []
    try:
        for name, facts in ftp.mlsd(facts=["type", "size"]):
            if facts.get("type") == "file":
                rows.append((name, int(facts["size"]) if facts.get("size") else None))
        return rows
    except (AttributeError, OSError, error_perm):
        pass
    for name in ftp.nlst():
        try:
            size = ftp.size(name)
        except (OSError, error_perm):
            size = None
        rows.append((name, size))
    return rows


def inventory_workbench_studies(*, email: str, timeout: float = 60.0) -> pd.DataFrame:
    """List ``/Studies`` once and retain only exact ``ST######.zip`` basenames."""

    rows: list[dict[str, Any]] = []
    with _ftp_session(email, timeout) as ftp:
        for remote_name, size in _directory_entries(ftp):
            archive_name = PurePosixPath(remote_name).name
            match = EXACT_STUDY_ARCHIVE_RE.fullmatch(archive_name)
            if match is None:
                # Filtering happens before selection and progress accounting, so an
                # auxiliary or specially named ZIP is never counted as inspected.
                continue
            rows.append(
                {
                    "study_id": match.group(1).upper(),
                    "remote_name": remote_name,
                    "archive_name": archive_name,
                    "bytes": size,
                }
            )
    columns = ["study_id", "remote_name", "archive_name", "bytes"]
    # Fixed-width ST accessions sort chronologically by their numeric component;
    # descending order makes bounded sweeps start with the newest assigned studies.
    return pd.DataFrame(rows, columns=columns).sort_values(
        ["study_id", "archive_name"],
        ascending=[False, False],
        kind="stable",
        ignore_index=True,
    )


def _study_archive_summary(archives: pd.DataFrame) -> pd.DataFrame:
    """Count archives and sum only known remote sizes for each study."""

    if archives.empty:
        return pd.DataFrame(columns=["archive_count", "known_total_bytes"]).rename_axis("study_id")
    return archives.groupby("study_id").agg(
        archive_count=("archive_name", "size"),
        known_total_bytes=("bytes", lambda values: int(values.dropna().sum())),
    )


def _fetch_suffix(ftp: FTP, name: str, start: int, maximum: int) -> bytes:
    """Retrieve bytes from ``start`` to EOF and abort if the server exceeds the bound."""

    chunks: list[bytes] = []
    received = 0

    def collect(chunk: bytes) -> None:
        """Collect one metadata chunk while enforcing the byte ceiling."""

        nonlocal received
        received += len(chunk)
        if received > maximum:
            raise ValueError(f"Remote metadata read exceeded {maximum:,} bytes.")
        chunks.append(chunk)

    # REST is the key to peeking: the transfer starts near EOF, not at byte zero.
    ftp.retrbinary(f"RETR {name}", collect, blocksize=262_144, rest=start)
    return b"".join(chunks)


def _zip64_location(
    ftp: FTP,
    name: str,
    tail: bytes,
    tail_start: int,
    eocd_position: int,
    maximum: int,
) -> tuple[int, int, int]:
    """Resolve entry count, directory size, and offset from ZIP64 end records."""

    locator_position = eocd_position - 20
    if locator_position < 0 or tail[locator_position:locator_position + 4] != ZIP64_LOCATOR:
        raise zipfile.BadZipFile("ZIP64 locator is missing.")
    _, locator_disk, zip64_offset, disk_count = struct.unpack_from(
        "<4sLQL", tail, locator_position
    )
    if locator_disk or disk_count != 1:
        raise zipfile.BadZipFile("Multi-disk ZIP files are unsupported.")
    if tail_start <= zip64_offset and zip64_offset + 56 <= tail_start + len(tail):
        start = zip64_offset - tail_start
        record = tail[start:start + 56]
    else:
        record = _fetch_suffix(ftp, name, zip64_offset, maximum)[:56]
    if len(record) < 56 or record[:4] != ZIP64_EOCD:
        raise zipfile.BadZipFile("ZIP64 end record is invalid.")
    values = struct.unpack_from("<4sQ2H2L4Q", record)
    disk_number, directory_disk = values[4], values[5]
    if disk_number or directory_disk:
        raise zipfile.BadZipFile("Multi-disk ZIP files are unsupported.")
    return int(values[7]), int(values[8]), int(values[9])


def _directory_location(
    ftp: FTP,
    name: str,
    remote_size: int,
    config: WorkbenchPeekConfig,
) -> tuple[int, int, int, bytes, int]:
    """Find a remote ZIP central directory by reading only the archive tail."""

    tail_size = min(remote_size, config.tail_bytes)
    tail_start = remote_size - tail_size
    tail = _fetch_suffix(ftp, name, tail_start, tail_size)
    position = tail.rfind(EOCD)
    if position < 0 or position + 22 > len(tail):
        raise zipfile.BadZipFile("ZIP end record was not found.")
    values = struct.unpack_from("<4s4H2LH", tail, position)
    disk, directory_disk, disk_entries, entries = values[1:5]
    directory_size, directory_offset, comment_size = values[5:]
    if position + 22 + comment_size > len(tail):
        raise zipfile.BadZipFile("ZIP end record is truncated.")
    if disk or directory_disk or disk_entries != entries:
        raise zipfile.BadZipFile("Multi-disk ZIP files are unsupported.")
    if entries == 0xFFFF or directory_size == 0xFFFFFFFF or directory_offset == 0xFFFFFFFF:
        entries, directory_size, directory_offset = _zip64_location(
            ftp, name, tail, tail_start, position, config.max_central_directory_bytes
        )
    if directory_size > config.max_central_directory_bytes:
        raise ValueError(f"ZIP directory exceeds {config.max_central_directory_bytes:,} bytes.")
    if directory_offset + directory_size > remote_size:
        raise zipfile.BadZipFile("ZIP directory extends beyond EOF.")
    return int(entries), int(directory_size), int(directory_offset), tail, tail_start


def _zip64_sizes(extra: bytes, expanded: int, compressed: int) -> tuple[int, int]:
    """Replace 32-bit member-size sentinels using the ZIP64 extra field."""

    position = 0
    while position + 4 <= len(extra):
        field_id, field_size = struct.unpack_from("<HH", extra, position)
        position += 4
        field = extra[position:position + field_size]
        position += field_size
        if field_id != 1:
            continue
        value_position = 0
        if expanded == 0xFFFFFFFF:
            expanded = struct.unpack_from("<Q", field, value_position)[0]
            value_position += 8
        if compressed == 0xFFFFFFFF:
            compressed = struct.unpack_from("<Q", field, value_position)[0]
        break
    if expanded == 0xFFFFFFFF or compressed == 0xFFFFFFFF:
        raise zipfile.BadZipFile("ZIP64 member sizes are missing.")
    return int(expanded), int(compressed)


def _safe_member(name: str) -> str:
    """Normalize a ZIP member and reject absolute or parent-traversal paths."""

    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"Unsafe archive member {name!r}.")
    return path.as_posix()


def _parse_directory(data: bytes, expected: int) -> pd.DataFrame:
    """Parse member names and sizes from central-directory records."""

    rows: list[dict[str, Any]] = []
    position = 0
    while position + 46 <= len(data) and data[position:position + 4] == CENTRAL_MEMBER:
        flags = struct.unpack_from("<H", data, position + 8)[0]
        compressed = struct.unpack_from("<L", data, position + 20)[0]
        expanded = struct.unpack_from("<L", data, position + 24)[0]
        name_size, extra_size, comment_size = struct.unpack_from("<3H", data, position + 28)
        external_attr = struct.unpack_from("<L", data, position + 38)[0]
        end = position + 46 + name_size + extra_size + comment_size
        if end > len(data):
            raise zipfile.BadZipFile("Central-directory member is truncated.")
        raw_name = data[position + 46:position + 46 + name_size]
        encoding = "utf-8" if flags & 0x800 else "cp437"
        name = raw_name.decode(encoding, errors="replace")
        extra_start = position + 46 + name_size
        expanded, compressed = _zip64_sizes(
            data[extra_start:extra_start + extra_size], expanded, compressed
        )
        rows.append(
            {
                "member": _safe_member(name),
                "uncompressed_bytes": expanded,
                "compressed_bytes": compressed,
                "is_directory": name.endswith("/"),
                "external_attr": external_attr,
            }
        )
        position = end
    if len(rows) != expected:
        raise zipfile.BadZipFile(f"Expected {expected:,} members; parsed {len(rows):,}.")
    return pd.DataFrame(rows)


def peek_remote_zip_members(
    ftp: FTP,
    remote_name: str,
    remote_size: int,
    config: WorkbenchPeekConfig = WorkbenchPeekConfig(),
) -> pd.DataFrame:
    """Read a remote ZIP member listing without retrieving its data payloads."""

    entries, size, offset, tail, tail_start = _directory_location(
        ftp, remote_name, remote_size, config
    )
    if tail_start <= offset and offset + size <= tail_start + len(tail):
        start = offset - tail_start
        directory = tail[start:start + size]
    else:
        bytes_to_eof = remote_size - offset
        allowed = config.max_central_directory_bytes + config.tail_bytes
        if bytes_to_eof > allowed:
            raise ValueError("Directory-to-EOF range exceeds the metadata-read limit.")
        directory = _fetch_suffix(ftp, remote_name, offset, bytes_to_eof)[:size]
    if len(directory) != size:
        raise zipfile.BadZipFile("Central directory is truncated.")
    return _parse_directory(directory, entries)


def _format_name(path_name: str) -> str | None:
    """Map a case-insensitive member suffix to its output format column."""

    lower = path_name.lower()
    for format_name, suffixes in FORMAT_SUFFIXES:
        if lower.endswith(suffixes):
            return format_name
    return None


def logical_data_inventory(
    members: pd.DataFrame, *, study_id: str, archive_name: str
) -> pd.DataFrame:
    """Collapse member rows into standalone files and complete .raw/.d bundles."""

    columns = [
        "study_id", "archive_name", "logical_input", "format",
        "uncompressed_bytes", "compressed_bytes", "member_count",
    ]
    if members.empty:
        return pd.DataFrame(columns=columns)
    paths = {row.member: PurePosixPath(row.member) for row in members.itertuples()}
    names = tuple(paths)
    bundle_roots: set[str] = set()
    for row in members.itertuples():
        path = paths[row.member]
        for index, part in enumerate(path.parts):
            if not part.lower().endswith(BUNDLE_SUFFIXES):
                continue
            root = PurePosixPath(*path.parts[:index + 1]).as_posix()
            if row.is_directory or any(name.startswith(root + "/") for name in names):
                bundle_roots.add(root)
    ordered_roots = sorted(bundle_roots, key=len, reverse=True)
    rows: list[dict[str, Any]] = []
    for row in members.itertuples():
        if row.is_directory:
            continue
        member = paths[row.member].as_posix()
        root = next((value for value in ordered_roots if member.startswith(value + "/")), None)
        logical_input = root or member
        format_name = _format_name(PurePosixPath(logical_input).name)
        if format_name is None:
            continue
        rows.append(
            {
                "study_id": study_id,
                "archive_name": archive_name,
                "logical_input": logical_input,
                "format": format_name,
                "uncompressed_bytes": int(row.uncompressed_bytes),
                "compressed_bytes": int(row.compressed_bytes),
                "member_count": 1,
            }
        )
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows).groupby(
        ["study_id", "archive_name", "logical_input", "format"], as_index=False
    ).agg(
        uncompressed_bytes=("uncompressed_bytes", "sum"),
        compressed_bytes=("compressed_bytes", "sum"),
        member_count=("member_count", "sum"),
    )


def _normalized_key(value: str) -> str:
    """Normalize Workbench metadata keys for resilient matching."""

    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _record_value(record: Mapping[str, Any], *names: str) -> Any:
    """Return the first nonempty value matching one of several metadata names."""

    normalized = {_normalized_key(str(key)): value for key, value in record.items()}
    for name in names:
        value = normalized.get(_normalized_key(name))
        if value not in (None, "", "NA", "N/A"):
            return value
    return None


def _analysis_records(payload: Any) -> list[dict[str, Any]]:
    """Find analysis-like mappings in heterogeneous REST JSON shapes."""

    records: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        """Walk nested lists and mappings, retaining analysis-level mappings."""

        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            keys = {_normalized_key(str(key)) for key in value}
            markers = {
                "studyid", "analysisid", "instrumenttype", "msinstrumenttype",
                "instrumentname", "msinstrumentname", "ionmode", "msionmode",
            }
            if keys & markers:
                records.append(value)
            else:
                for item in value.values():
                    visit(item)

    visit(payload)
    return records


def _study_id(record: Mapping[str, Any]) -> str | None:
    """Find an ST accession in a metadata record."""

    candidates = [_record_value(record, "study_id", "study id"), *record.values()]
    for value in candidates:
        match = STUDY_RE.search(str(value))
        if match:
            return match.group(1).upper()
    return None


def _collision_energies(record: Mapping[str, Any]) -> list[str]:
    """Collect explicitly labeled collision energies when Workbench exposes them."""

    values = [
        str(value).strip()
        for key, value in record.items()
        if ("collisionenergy" in _normalized_key(str(key)) or _normalized_key(str(key)) in {"ce", "nce"})
        and value not in (None, "", "NA", "N/A")
    ]
    text = " | ".join(str(value) for value in record.values())
    pattern = re.compile(
        r"\b(?:collision\s*energy|NCE|CE)\b\s*[:=]?\s*(\d+(?:\.\d+)?(?:\s*(?:eV|%))?)",
        re.IGNORECASE,
    )
    values.extend(match.group(1).strip() for match in pattern.finditer(text))
    return sorted(set(values))


def fetch_workbench_analysis_metadata(
    *,
    study_ids: Iterable[str] | None = None,
    prefix_length: int = 5,
    session: requests.Session | None = None,
    timeout: float = 120.0,
    request_delay_seconds: float = 0.2,
    max_retries: int = 3,
    retry_backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0),
) -> pd.DataFrame:
    """Fetch accession-prefix metadata with polite pacing and bounded retries."""

    client = session or requests.Session()
    if prefix_length < 3 or prefix_length > 8:
        raise ValueError("prefix_length must be between 3 and 8 characters.")
    if request_delay_seconds < 0 or max_retries < 0:
        raise ValueError("REST delay and retry counts cannot be negative.")
    if max_retries and not retry_backoff_seconds:
        raise ValueError("REST retry_backoff_seconds cannot be empty.")
    if any(delay < 0 for delay in retry_backoff_seconds):
        raise ValueError("REST retry backoff values cannot be negative.")
    prefixes = (
        sorted({str(study_id).upper()[:prefix_length] for study_id in study_ids})
        if study_ids is not None
        else ["ST"]
    )
    rows: list[dict[str, Any]] = []
    for prefix_number, prefix in enumerate(prefixes, start=1):
        payload: Any = None
        final_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                response = client.get(
                    f"{MW_REST_BASE}/study/study_id/{prefix}/analysis",
                    timeout=timeout,
                )
                response.raise_for_status()
                payload = response.json()
                final_error = None
                break
            except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                final_error = exc
                if attempt >= max_retries:
                    break
                delay = _backoff_delay(retry_backoff_seconds, attempt + 1)
                print(
                    f"Retrying Workbench metadata prefix {prefix} in {delay:g}s "
                    f"after {exc!r}."
                )
                time.sleep(delay)
        if final_error is not None:
            print(f"Workbench metadata prefix {prefix} was unavailable: {final_error!r}")
            if prefix_number < len(prefixes):
                time.sleep(request_delay_seconds)
            continue
        for record in _analysis_records(payload):
            study_id = _study_id(record)
            if study_id is None:
                continue
            ion_mode = str(
                _record_value(record, "ion_mode", "MS ion_mode", "ion mode") or ""
            ).lower()
            rows.append(
                {
                    "study_id": study_id,
                    "analysis_id": _record_value(record, "analysis_id", "analysis id"),
                    "instrument_type": _record_value(
                        record, "instrument_type", "MS instrument_type"
                    ),
                    "instrument_name": _record_value(
                        record, "instrument_name", "MS instrument_name"
                    ),
                    "collision_energies": _collision_energies(record),
                    "positive_present": (
                        "positive" in ion_mode or ion_mode.strip() in {"+", "pos"}
                    ),
                    "negative_present": (
                        "negative" in ion_mode or ion_mode.strip() in {"-", "neg"}
                    ),
                }
            )
        if prefix_number < len(prefixes):
            time.sleep(request_delay_seconds)
    columns = [
        "study_id", "analysis_id", "instrument_type", "instrument_name",
        "collision_energies", "positive_present", "negative_present",
    ]
    return pd.DataFrame(rows, columns=columns)


def _unique(values: Iterable[Any]) -> list[str]:
    """Return sorted unique nonmissing metadata strings."""

    return sorted(
        {str(value).strip() for value in values if value is not None and not pd.isna(value) and str(value).strip()}
    )


def _metadata_by_study(metadata: pd.DataFrame) -> pd.DataFrame:
    """Aggregate instrument, collision-energy, and polarity fields by study."""

    columns = [
        "study_id", "instrument_types", "instrument_names", "collision_energies",
        "positive_present", "negative_present",
    ]
    if metadata.empty:
        return pd.DataFrame(columns=columns).set_index("study_id")
    rows: list[dict[str, Any]] = []
    for study_id, group in metadata.groupby("study_id", sort=True):
        energies = sorted(
            {item for values in group.collision_energies for item in (values if isinstance(values, list) else [])}
        )
        rows.append(
            {
                "study_id": study_id,
                "instrument_types": _unique(group.instrument_type) or np.nan,
                "instrument_names": _unique(group.instrument_name) or np.nan,
                "collision_energies": energies or np.nan,
                "positive_present": bool(group.positive_present.any()),
                "negative_present": bool(group.negative_present.any()),
            }
        )
    return pd.DataFrame(rows, columns=columns).set_index("study_id")


def build_workbench_study_dataframe(
    archives: pd.DataFrame,
    status: pd.DataFrame,
    logical_inputs: pd.DataFrame,
    metadata: pd.DataFrame,
) -> pd.DataFrame:
    """Compose the study-indexed DataFrame used for pre-download filtering."""

    table = _study_archive_summary(archives)
    for column in FORMAT_COLUMNS:
        table[column] = pd.Series(np.nan, index=table.index, dtype=object)
    for (study_id, format_name), group in logical_inputs.groupby(["study_id", "format"], sort=True):
        table.at[study_id, format_name] = [
            {
                "name": row.logical_input,
                "archive": row.archive_name,
                "uncompressed_bytes": int(row.uncompressed_bytes),
                "compressed_bytes": int(row.compressed_bytes),
            }
            for row in group.sort_values(["archive_name", "logical_input"]).itertuples()
        ]
    if status.empty:
        table["archives_peeked"] = 0
        table["peek_complete"] = False
        table["peek_errors"] = np.nan
    else:
        success = status[status.peek_success].groupby("study_id").archive_name.nunique()
        errors = status[status.peek_error.notna()].groupby("study_id").peek_error.apply(list)
        table["archives_peeked"] = success.reindex(table.index, fill_value=0).astype(int)
        table["peek_complete"] = table.archives_peeked.eq(table.archive_count)
        table["peek_errors"] = errors.reindex(table.index)
    table = table.join(_metadata_by_study(metadata), how="left")
    table["positive_present"] = table.positive_present.astype("boolean")
    table["negative_present"] = table.negative_present.astype("boolean")
    # Keep the exported table aligned with the newest-to-oldest sweep order.
    return table.sort_index(ascending=False)


def _json_ready(value: Any) -> Any:
    """Convert pandas and NumPy scalars into values accepted by ``json.dumps``."""

    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    if value is pd.NA or (isinstance(value, float) and np.isnan(value)):
        return None
    return value


def _load_checkpoint(path: str | Path | None) -> dict[str, dict[str, Any]]:
    """Load prior archive results keyed by remote name, or return an empty mapping."""

    if path is None or not Path(path).exists():
        return {}
    checkpoint_path = Path(path)
    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read checkpoint {checkpoint_path}: {exc}") from exc
    if payload.get("version") != 1 or not isinstance(payload.get("archives"), list):
        raise ValueError(f"Unsupported checkpoint structure in {checkpoint_path}.")
    return {
        str(record["remote_name"]): record
        for record in payload["archives"]
        if isinstance(record, dict) and record.get("remote_name")
    }


def _write_checkpoint(
    path: str | Path | None,
    records: Mapping[str, Mapping[str, Any]],
) -> None:
    """Atomically write the current archive results to a small JSON metadata file."""

    if path is None:
        return
    checkpoint_path = Path(path)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = checkpoint_path.with_name(checkpoint_path.name + ".tmp")
    payload = {
        "version": 1,
        "saved_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "archives": list(records.values()),
    }
    temporary.write_text(
        json.dumps(_json_ready(payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(checkpoint_path)


def _checkpoint_matches(record: Mapping[str, Any], row: Any) -> bool:
    """Return true when a successful checkpoint record still describes this archive."""

    current_size = int(row.bytes) if pd.notna(row.bytes) else None
    return (
        bool(record.get("status", {}).get("peek_success"))
        and record.get("study_id") == row.study_id
        and record.get("archive_name") == row.archive_name
        and record.get("bytes") == current_size
    )


def _checkpoint_record(
    row: Any,
    status: Mapping[str, Any],
    logical_inputs: pd.DataFrame,
) -> dict[str, Any]:
    """Package one archive status and its logical inputs for resumable processing."""

    return {
        "study_id": row.study_id,
        "remote_name": row.remote_name,
        "archive_name": row.archive_name,
        "bytes": int(row.bytes) if pd.notna(row.bytes) else None,
        "status": dict(status),
        "logical_inputs": logical_inputs.to_dict("records"),
    }


def inventory_download_and_inspect_workbench(
    *,
    email: str,
    config: WorkbenchPeekConfig = WorkbenchPeekConfig(),
    timeout: float = 60.0,
    metadata_timeout: float = 120.0,
) -> WorkbenchPeekResult:
    """Peek at remote ZIP directories and build a filterable, study-indexed table.

    Despite the retained legacy name, this function performs no complete archive
    downloads and creates no local archive directory. Only ZIP tail/central-directory
    metadata are transferred. A tiny ZIP can fit inside the tail window, but its data
    members are never extracted.
    """

    _validate_peek_config(config)
    archives: pd.DataFrame | None = None
    for attempt in range(config.max_retries + 1):
        try:
            archives = inventory_workbench_studies(email=email, timeout=timeout)
            break
        except all_errors as exc:
            if attempt >= config.max_retries:
                raise
            delay = _backoff_delay(config.retry_backoff_seconds, attempt + 1)
            print(f"Retrying the /Studies listing in {delay:g}s after {exc!r}.")
            time.sleep(delay)
    if archives is None:
        raise RuntimeError("The Workbench archive inventory did not return a table.")
    selected = archives
    if config.max_archives is not None:
        selected = selected.head(config.max_archives)
    frames: list[pd.DataFrame] = []
    statuses: list[dict[str, Any]] = []
    checkpoint_records = _load_checkpoint(config.checkpoint_path)
    ftp: FTP | None = None
    new_since_checkpoint = 0
    resumed = 0
    peek_errors = all_errors + (ValueError, struct.error, zipfile.BadZipFile)
    try:
        for number, row in enumerate(selected.itertuples(), start=1):
            saved = checkpoint_records.get(row.remote_name)
            if saved is not None and _checkpoint_matches(saved, row):
                saved_status = dict(saved["status"])
                statuses.append(saved_status)
                saved_inputs = pd.DataFrame(saved.get("logical_inputs", []))
                if not saved_inputs.empty:
                    frames.append(saved_inputs)
                resumed += 1
            else:
                status_row: dict[str, Any] | None = None
                logical = pd.DataFrame()
                for attempt in range(config.max_retries + 1):
                    try:
                        if ftp is None:
                            ftp = _open_ftp(email, timeout)
                        remote_size = (
                            int(row.bytes)
                            if pd.notna(row.bytes)
                            else ftp.size(row.remote_name)
                        )
                        if remote_size is None:
                            raise ValueError("Archive size is unavailable.")
                        members = peek_remote_zip_members(
                            ftp, row.remote_name, int(remote_size), config
                        )
                        logical = logical_data_inventory(
                            members,
                            study_id=row.study_id,
                            archive_name=row.archive_name,
                        )
                        status_row = {
                            "study_id": row.study_id,
                            "archive_name": row.archive_name,
                            "peek_success": True,
                            "zip_member_count": len(members),
                            "logical_input_count": len(logical),
                            "peek_error": None,
                        }
                        break
                    except peek_errors as exc:
                        # A failed/aborted transfer can desynchronize FTP control state,
                        # so every retry starts with a newly authenticated connection.
                        _close_ftp(ftp)
                        ftp = None
                        if attempt >= config.max_retries:
                            status_row = {
                                "study_id": row.study_id,
                                "archive_name": row.archive_name,
                                "peek_success": False,
                                "zip_member_count": 0,
                                "logical_input_count": 0,
                                "peek_error": repr(exc),
                            }
                            break
                        delay = _backoff_delay(
                            config.retry_backoff_seconds, attempt + 1
                        )
                        print(
                            f"Retrying {row.archive_name} in {delay:g}s "
                            f"after {exc!r}."
                        )
                        time.sleep(delay)

                if status_row is None:
                    raise RuntimeError(f"No status was produced for {row.archive_name}.")
                statuses.append(status_row)
                if not logical.empty:
                    frames.append(logical)
                checkpoint_records[row.remote_name] = _checkpoint_record(
                    row, status_row, logical
                )
                new_since_checkpoint += 1
                if (
                    config.checkpoint_path is not None
                    and new_since_checkpoint >= config.checkpoint_every
                ):
                    _write_checkpoint(config.checkpoint_path, checkpoint_records)
                    new_since_checkpoint = 0
                if number < len(selected):
                    time.sleep(config.request_delay_seconds)

            if config.progress_every and (
                number % config.progress_every == 0 or number == len(selected)
            ):
                print(
                    f"Processed {number:,}/{len(selected):,} archives "
                    f"({resumed:,} resumed)."
                )
    finally:
        _close_ftp(ftp)
        _write_checkpoint(config.checkpoint_path, checkpoint_records)
    logical_inputs = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=[
            "study_id", "archive_name", "logical_input", "format",
            "uncompressed_bytes", "compressed_bytes", "member_count",
        ]
    )
    status = pd.DataFrame(
        statuses,
        columns=[
            "study_id", "archive_name", "peek_success", "zip_member_count",
            "logical_input_count", "peek_error",
        ],
    )
    try:
        metadata = fetch_workbench_analysis_metadata(
            study_ids=archives.study_id.unique(),
            timeout=metadata_timeout,
            request_delay_seconds=config.request_delay_seconds,
            max_retries=config.max_retries,
            retry_backoff_seconds=config.retry_backoff_seconds,
        )
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        print(f"Workbench analysis metadata was unavailable: {exc!r}")
        metadata = pd.DataFrame(
            columns=[
                "study_id", "analysis_id", "instrument_type", "instrument_name",
                "collision_energies", "positive_present", "negative_present",
            ]
        )
    study_dataframe = build_workbench_study_dataframe(
        archives, status, logical_inputs, metadata
    )
    return WorkbenchPeekResult(
        archive_inventory=archives,
        archive_peek_status=status,
        member_inventory=logical_inputs,
        analysis_metadata=metadata,
        study_dataframe=study_dataframe,
    )
