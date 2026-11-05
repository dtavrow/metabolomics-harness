"""Single format registry shared by pipeline discovery and processing stages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class FormatSpec:
    """Describe how one canonical spectral format is stored and processed."""

    name: str
    suffixes: tuple[str, ...]
    storage: str = "file"          # ``file`` or ``file_or_directory``.
    audit: str | None = None       # xml, mgf, msp, cdf, or None.
    ingestion: str = "convert"     # direct or convert.
    parser: str | None = None       # mzml, mzxml, mgf, or msp.


FORMAT_REGISTRY = (
    FormatSpec("mzdata", (".mzdata.xml", ".mzdata"), audit="xml"),
    FormatSpec("wiff", (".wiff.scan", ".wiff2", ".wiff")),
    FormatSpec("mzxml", (".mzxml",), audit="xml", ingestion="direct", parser="mzxml"),
    FormatSpec("mzml", (".mzml",), audit="xml", ingestion="direct", parser="mzml"),
    FormatSpec("mgf", (".mgf",), audit="mgf", ingestion="direct", parser="mgf"),
    FormatSpec("msp", (".msp",), audit="msp", ingestion="direct", parser="msp"),
    FormatSpec("cdf", (".netcdf", ".cdf"), audit="cdf"),
    FormatSpec("mz5", (".mz5",)),
    FormatSpec("ms2", (".cms2", ".bms2", ".ms2")),
    FormatSpec("raw", (".raw",), storage="file_or_directory"),
    FormatSpec("d", (".d",), storage="file_or_directory"),
    FormatSpec("lcd", (".lcd",)),
    FormatSpec("bruker", (".baf", ".tdf", ".tsf", ".yep", ".fid")),
    FormatSpec("t2d", (".t2d",)),
)

FORMAT_BY_NAME = {spec.name: spec for spec in FORMAT_REGISTRY}
FORMAT_SUFFIXES = tuple((spec.name, spec.suffixes) for spec in FORMAT_REGISTRY)
FORMAT_COLUMNS = tuple(spec.name for spec in FORMAT_REGISTRY)
BUNDLE_SUFFIXES = tuple(
    suffix
    for spec in FORMAT_REGISTRY
    if spec.storage == "file_or_directory"
    for suffix in spec.suffixes
)
SORTED_SUFFIXES = tuple(
    sorted(
        ((suffix, spec) for spec in FORMAT_REGISTRY for suffix in spec.suffixes),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)


def get_format(name: str) -> FormatSpec:
    """Return a format definition by its case-insensitive canonical name."""

    key = str(name).strip().lower()
    if key not in FORMAT_BY_NAME:
        raise ValueError(f"Unknown format {name!r}; expected one of {FORMAT_COLUMNS}.")
    return FORMAT_BY_NAME[key]


def detect_format(path: str | Path, *, is_directory: bool | None = None) -> FormatSpec | None:
    """Identify a format from a path without confusing files and vendor bundles."""

    value = Path(path)
    if is_directory is None:
        is_directory = value.is_dir()
    name = value.name.lower()
    for suffix, spec in SORTED_SUFFIXES:
        if not name.endswith(suffix):
            continue
        if is_directory and spec.storage != "file_or_directory":
            return None
        return spec
    return None


def formats_with(*, audit: str | None = None, ingestion: str | None = None) -> set[str]:
    """Return canonical names matching one or more processing capabilities."""

    selected: Iterable[FormatSpec] = FORMAT_REGISTRY
    if audit is not None:
        selected = (spec for spec in selected if spec.audit == audit)
    if ingestion is not None:
        selected = (spec for spec in selected if spec.ingestion == ingestion)
    return {spec.name for spec in selected}

