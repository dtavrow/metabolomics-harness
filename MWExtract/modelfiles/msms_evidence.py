"""First-pass MS/MS evidence extraction from Workbench REST and mwTab metadata.

The detector is deliberately conservative: it records exact metadata evidence and
assigns a likelihood, but it does not claim scan-level proof without reading the
spectral payload. One output row represents one Workbench analysis.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import pandas as pd


MSMS_REPORT_COLUMNS = [
    "study_id",
    "msms_likelihood",
    "analysis_id",
    "instrument_name",
    "instrument_type",
    "ion_mode",
    "explicit_terms",
    "acquisition_modes",
    "fragmentation_methods",
    "tandem_methods",
    "excluded_mode_terms",
    "collision_energy_present",
    "collision_energy_values",
    "collision_energy_text",
    "precursor_types",
    "precursor_mz_values",
    "evidence_hits",
]

LIKELIHOOD_ORDER = {"unknown": 0, "low": 1, "medium": 2, "high": 3}

# Large result tables can repeat thousands of ordinary feature m/z values. The
# first pass searches descriptive metadata sections instead, where a precursor
# must be explicitly identified as such to count.
SEARCHED_SECTIONS = {
    "METABOLOMICS WORKBENCH",
    "PROJECT",
    "STUDY",
    "ANALYSIS",
    "MS",
    "CHROMATOGRAPHY",
    "SAMPLEPREP",
    "COLLECTION",
    "TREATMENT",
}


def _term_patterns(items: Sequence[tuple[str, str]]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Compile named, case-insensitive evidence patterns once at import time."""

    return tuple((name, re.compile(pattern, re.IGNORECASE)) for name, pattern in items)


EXPLICIT_PATTERNS = _term_patterns(
    (
        ("MS/MS", r"(?<![A-Za-z0-9])MS\s*/\s*MS(?![A-Za-z0-9])"),
        ("MS2", r"(?<![A-Za-z0-9])MS(?:\^?2|²)(?![A-Za-z0-9])"),
        ("MSn (>=3)", r"(?<![A-Za-z0-9])MS(?:\^?[3-9]\d*|[³⁴⁵⁶⁷⁸⁹])(?![A-Za-z0-9])"),
        ("MSn", r"(?<![A-Za-z0-9])MS\^?n(?![A-Za-z0-9])"),
        ("tandem mass spectrometry", r"\btandem\s+mass\s+spectrometr(?:y|ic)\b"),
        ("product ion", r"\bproduct[- ]ion(?:s)?(?:\s+(?:scan|spectrum|spectra))?\b"),
        ("daughter ion", r"\bdaughter[- ]ion(?:s)?(?:\s+(?:scan|spectrum|spectra))?\b"),
        ("fragment ion spectrum", r"\bfragment[- ]ion\s+(?:scan|spectrum|spectra)\b"),
    )
)

ACQUISITION_PATTERNS = _term_patterns(
    (
        ("DDA", r"\b(?:DDA|data[- ]dependent acquisition)\b"),
        ("IDA", r"\b(?:IDA|information[- ]dependent(?: acquisition| manner)?)\b"),
        ("DIA", r"\b(?:DIA|data[- ]independent acquisition)\b"),
        ("SWATH", r"\bSWATH(?:[- ]MS)?\b"),
        ("MSE", r"(?<![A-Za-z0-9])MS(?:E|ᴱ)(?![A-Za-z0-9])"),
        ("AIF", r"\b(?:AIF|all[- ]ion fragmentation)\b"),
        ("auto MS/MS", r"\bauto(?:matic)?\s+MS\s*/\s*MS\b"),
        # Split because the two are opposite cases: diaPASEF is data-independent
        # and chimeric, while plain PASEF rides on DDA and is usable.
        ("diaPASEF", r"\bdiaPASEF\b"),
        ("PASEF", r"(?<!dia)\bPASEF\b"),
    )
)

FRAGMENTATION_PATTERNS = _term_patterns(
    (
        ("CID", r"\b(?:CID|collision[- ]induced dissociation)\b"),
        ("HCD", r"\b(?:HCD|higher[- ]energy collisional dissociation)\b"),
        ("ETD", r"\b(?:ETD|electron[- ]transfer dissociation)\b"),
        ("ECD", r"\b(?:ECD|electron[- ]capture dissociation)\b"),
        ("PQD", r"\b(?:PQD|pulsed[- ]Q dissociation)\b"),
        ("UVPD", r"\b(?:UVPD|ultraviolet photodissociation)\b"),
        ("SID", r"\b(?:SID|surface[- ]induced dissociation)\b"),
        ("IRMPD", r"\b(?:IRMPD|infrared multiphoton dissociation)\b"),
    )
)

TANDEM_PATTERNS = _term_patterns(
    (
        ("MRM", r"\b(?:MRM|multiple reaction monitoring)\b"),
        ("SRM", r"\b(?:SRM|selected reaction monitoring)\b"),
        ("PRM", r"\b(?:PRM|parallel reaction monitoring)\b"),
        ("product ion scan", r"\bproduct[- ]ion\s+scan(?:ning|s)?\b"),
        ("precursor ion scan", r"\bprecursor[- ]ion\s+scan(?:ning|s)?\b"),
        ("neutral loss scan", r"\bneutral[- ]loss\s+scan(?:ning|s)?\b"),
        ("Q1/Q3 transition", r"\bQ1\s*(?:/|and|to|[-–])\s*Q3\b"),
        ("precursor/product transition", r"\bprecursor\s*(?:/|to|[-–])\s*product\b"),
    )
)

#: Acquisition and tandem terms naming a mode that does NOT yield a product-ion
#: spectrum of its stated precursor.  They are still detected and reported --
#: the acquisition classifier needs them, and they are the basis of the
#: exclusion predicate -- but they must not by themselves make a study look like
#: a source of spectrum-molecule pairs.  Crediting them is how MRM studies
#: entered the corpus as "high likelihood" in the first place.
#:
#: PRM is deliberately absent: parallel reaction monitoring is targeted, but the
#: readout on a Q-Orbitrap is a full product-ion spectrum, so it is usable.
EXCLUDED_MODE_TERMS = frozenset({
    "DIA", "SWATH", "MSE", "AIF", "diaPASEF",
    "MRM", "SRM",
    "precursor ion scan", "neutral loss scan",
    "Q1/Q3 transition", "precursor/product transition",
})


def product_ion_compatible(terms: Sequence[str]) -> list[str]:
    """Keep only terms consistent with a product-ion spectrum."""

    return [term for term in terms if term not in EXCLUDED_MODE_TERMS]


def excluded_mode_terms(*term_groups: Sequence[str]) -> list[str]:
    """Collect the detected terms that name a non-product-ion mode."""

    found = {term for group in term_groups for term in group if term in EXCLUDED_MODE_TERMS}
    return sorted(found)


COLLISION_CONTEXT_PATTERN = re.compile(
    r"\b(?:collision\s+energ(?:y|ies)|normalized\s+collision\s+energy|NCE)\b"
    # Keep decimal points inside formulas; a semicolon/newline still bounds the
    # excerpt, while the character cap prevents an entire comments block.
    r"[^;\n]{0,250}",
    re.IGNORECASE,
)
COLLISION_VALUE_PATTERN = re.compile(
    r"(?:\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?\s*(?:eV|V|%)|"
    r"NCE\s*[:=]?\s*\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
PRECURSOR_MZ_PATTERN = re.compile(
    r"\b(?:precursor(?:[- ]ion)?\s+(?:m\s*/\s*z|mz)|Q1)\b"
    r"\s*(?:[:=]|of|was|is|at)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _normalized_key(value: str) -> str:
    """Normalize a metadata key for resilient structured-field matching."""

    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _metadata_strings(document: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """Yield scalar field paths and values from descriptive mwTab sections."""

    def visit(value: Any, path: tuple[str, ...]) -> Iterator[tuple[str, str]]:
        """Recursively traverse nested descriptive metadata values."""

        if isinstance(value, Mapping):
            for key, item in value.items():
                yield from visit(item, (*path, str(key)))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from visit(item, (*path, str(index)))
        elif value not in (None, ""):
            yield ".".join(path), str(value)

    for section, value in document.items():
        if str(section).upper() in SEARCHED_SECTIONS:
            yield from visit(value, (str(section),))


def _mapping_value(mapping: Any, *names: str) -> Any:
    """Return the first nonempty mapping value matching normalized names."""

    if not isinstance(mapping, Mapping):
        return None
    normalized = {_normalized_key(str(key)): value for key, value in mapping.items()}
    for name in names:
        value = normalized.get(_normalized_key(name))
        if value not in (None, ""):
            return value
    return None


def _document_analysis_id(document: Mapping[str, Any]) -> str | None:
    """Read an analysis accession from a parsed mwTab document."""

    header = document.get("METABOLOMICS WORKBENCH", {})
    value = _mapping_value(header, "ANALYSIS_ID")
    return str(value) if value else None


def _add_hit(
    hits: list[dict[str, str]],
    *,
    category: str,
    normalized_term: str,
    source_field: str,
    matched_text: str,
    source_value: str,
) -> None:
    """Append one evidence hit while suppressing exact duplicates."""

    hit = {
        "category": category,
        "normalized_term": normalized_term,
        "source_field": source_field,
        "matched_text": matched_text,
        "source_value": source_value,
    }
    if hit not in hits:
        hits.append(hit)


def _pattern_hits(
    fields: Sequence[tuple[str, str]],
    patterns: Sequence[tuple[str, re.Pattern[str]]],
    category: str,
    hits: list[dict[str, str]],
) -> list[str]:
    """Find and record all normalized terms for one evidence category."""

    terms: list[str] = []
    for source_field, source_value in fields:
        for normalized_term, pattern in patterns:
            for match in pattern.finditer(source_value):
                if normalized_term not in terms:
                    terms.append(normalized_term)
                _add_hit(
                    hits,
                    category=category,
                    normalized_term=normalized_term,
                    source_field=source_field,
                    matched_text=match.group(0),
                    source_value=source_value,
                )
    return terms


def _structured_fragmentation_methods(
    fields: Sequence[tuple[str, str]], hits: list[dict[str, str]]
) -> list[str]:
    """Collect nonempty structured fragmentation and activation fields."""

    methods: list[str] = []
    accepted = {"fragmentationmethod", "activationparameter", "activationtime"}
    for source_field, source_value in fields:
        key = _normalized_key(source_field.rsplit(".", 1)[-1])
        if key not in accepted:
            continue
        value = source_value.strip()
        if value and value not in methods:
            methods.append(value)
        _add_hit(
            hits,
            category="fragmentation_method",
            normalized_term=value,
            source_field=source_field,
            matched_text=value,
            source_value=source_value,
        )
    return methods


def _collision_energy(
    fields: Sequence[tuple[str, str]], hits: list[dict[str, str]]
) -> tuple[list[str], list[str]]:
    """Collect structured and contextual collision-energy text and values."""

    texts: list[str] = []
    values: list[str] = []
    for source_field, source_value in fields:
        key = _normalized_key(source_field.rsplit(".", 1)[-1])
        snippets: list[str] = []
        if key == "collisionenergy":
            snippets.append(source_value.strip())
        snippets.extend(match.group(0).strip() for match in COLLISION_CONTEXT_PATTERN.finditer(source_value))
        for snippet in snippets:
            if snippet and snippet not in texts:
                texts.append(snippet)
            for value_match in COLLISION_VALUE_PATTERN.finditer(snippet):
                value = value_match.group(0).strip()
                if value not in values:
                    values.append(value)
            _add_hit(
                hits,
                category="collision_energy",
                normalized_term="collision energy",
                source_field=source_field,
                matched_text=snippet,
                source_value=source_value,
            )
    return texts, values


def _precursors(
    fields: Sequence[tuple[str, str]], hits: list[dict[str, str]]
) -> tuple[list[str], list[str]]:
    """Collect explicitly labeled precursor types and precursor/Q1 m/z values."""

    precursor_types: list[str] = []
    precursor_mz_values: list[str] = []
    for source_field, source_value in fields:
        key = _normalized_key(source_field.rsplit(".", 1)[-1])
        if key == "precursortype":
            value = source_value.strip()
            if value and value not in precursor_types:
                precursor_types.append(value)
            _add_hit(
                hits,
                category="precursor",
                normalized_term="precursor type",
                source_field=source_field,
                matched_text=value,
                source_value=source_value,
            )
        for match in PRECURSOR_MZ_PATTERN.finditer(source_value):
            value = match.group(1)
            if value not in precursor_mz_values:
                precursor_mz_values.append(value)
            _add_hit(
                hits,
                category="precursor",
                normalized_term="precursor m/z",
                source_field=source_field,
                matched_text=match.group(0),
                source_value=source_value,
            )
    return precursor_types, precursor_mz_values


def classify_msms_likelihood(
    *,
    explicit_terms: Sequence[str],
    acquisition_modes: Sequence[str],
    fragmentation_methods: Sequence[str],
    tandem_methods: Sequence[str],
    collision_energy_text: Sequence[str],
    precursor_types: Sequence[str],
    precursor_mz_values: Sequence[str],
) -> str:
    """Assign a transparent high/medium/low/unknown metadata likelihood.

    Only evidence compatible with a product-ion spectrum counts toward "high".
    A study declaring nothing but MRM is evidence of tandem MS, but not of the
    kind that yields a spectrum of a single molecule, so it must not be
    promoted on that basis.
    """

    usable_acquisition = product_ion_compatible(acquisition_modes)
    usable_tandem = product_ion_compatible(tandem_methods)
    if explicit_terms or usable_acquisition or usable_tandem:
        return "high"
    precursor_present = bool(precursor_types or precursor_mz_values)
    if fragmentation_methods or (collision_energy_text and precursor_present):
        return "medium"
    if collision_energy_text or precursor_present:
        return "low"
    return "unknown"


def _json_list(values: Sequence[Any]) -> str:
    """Serialize list-valued report cells consistently for TSV output."""

    return json.dumps(list(values), ensure_ascii=False, sort_keys=True)


def _evidence_row(
    study_id: str,
    analysis_id: str | None,
    document: Mapping[str, Any],
    fallback_analysis: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one combined summary-and-evidence row for a Workbench analysis."""

    fields = list(_metadata_strings(document))
    if fallback_analysis:
        fields.extend((f"REST_ANALYSIS.{key}", str(value)) for key, value in fallback_analysis.items() if value not in (None, ""))

    hits: list[dict[str, str]] = []
    explicit = _pattern_hits(fields, EXPLICIT_PATTERNS, "explicit_term", hits)
    acquisition = _pattern_hits(fields, ACQUISITION_PATTERNS, "acquisition_mode", hits)
    fragmentation = _pattern_hits(fields, FRAGMENTATION_PATTERNS, "fragmentation_method", hits)
    for value in _structured_fragmentation_methods(fields, hits):
        if value not in fragmentation:
            fragmentation.append(value)
    tandem = _pattern_hits(fields, TANDEM_PATTERNS, "tandem_method", hits)
    collision_text, collision_values = _collision_energy(fields, hits)
    precursor_types, precursor_mz = _precursors(fields, hits)
    likelihood = classify_msms_likelihood(
        explicit_terms=explicit,
        acquisition_modes=acquisition,
        fragmentation_methods=fragmentation,
        tandem_methods=tandem,
        collision_energy_text=collision_text,
        precursor_types=precursor_types,
        precursor_mz_values=precursor_mz,
    )
    # Recorded so selection can exclude these modes before any download, and so
    # a study promoted on other evidence still carries the warning.
    excluded_modes = excluded_mode_terms(acquisition, tandem)

    ms = document.get("MS", {})
    instrument_name = _mapping_value(ms, "INSTRUMENT_NAME") or _mapping_value(
        fallback_analysis or {}, "ms_instrument_name"
    )
    instrument_type = _mapping_value(ms, "INSTRUMENT_TYPE") or _mapping_value(
        fallback_analysis or {}, "ms_instrument_type"
    )
    ion_mode = _mapping_value(ms, "ION_MODE") or _mapping_value(
        fallback_analysis or {}, "ion_mode"
    )
    return {
        "study_id": study_id,
        "msms_likelihood": likelihood,
        "analysis_id": analysis_id,
        "instrument_name": instrument_name,
        "instrument_type": instrument_type,
        "ion_mode": ion_mode,
        "explicit_terms": _json_list(explicit),
        "acquisition_modes": _json_list(acquisition),
        "fragmentation_methods": _json_list(fragmentation),
        "tandem_methods": _json_list(tandem),
        "excluded_mode_terms": _json_list(excluded_modes),
        "collision_energy_present": bool(collision_text),
        "collision_energy_values": _json_list(collision_values),
        "collision_energy_text": _json_list(collision_text),
        "precursor_types": _json_list(precursor_types),
        "precursor_mz_values": _json_list(precursor_mz),
        "evidence_hits": _json_list(hits),
    }


def extract_msms_evidence(
    study_id: str,
    mwtab_documents: Sequence[Any],
    analyses: Sequence[Mapping[str, Any]] = (),
) -> pd.DataFrame:
    """Return one combined, filterable MS/MS evidence row per analysis."""

    analysis_by_id = {
        str(value): record
        for record in analyses
        if (value := _mapping_value(record, "analysis_id")) is not None
    }
    rows: list[dict[str, Any]] = []
    seen_analysis_ids: set[str] = set()
    for index, value in enumerate(mwtab_documents, start=1):
        if not isinstance(value, Mapping):
            continue
        analysis_id = _document_analysis_id(value) or f"document_{index}"
        fallback = analysis_by_id.get(analysis_id)
        rows.append(_evidence_row(study_id, analysis_id, value, fallback))
        seen_analysis_ids.add(analysis_id)

    # REST analysis metadata provides a fallback when mwTab is absent or fails
    # to parse, ensuring the report still has a filterable row for each analysis.
    for index, analysis in enumerate(analyses, start=1):
        analysis_id_value = _mapping_value(analysis, "analysis_id")
        analysis_id = str(analysis_id_value or f"analysis_{index}")
        if analysis_id in seen_analysis_ids:
            continue
        document = {"ANALYSIS": dict(analysis)}
        rows.append(_evidence_row(study_id, analysis_id, document, analysis))

    if not rows:
        rows.append(_evidence_row(study_id, None, {}, None))
    return pd.DataFrame(rows, columns=MSMS_REPORT_COLUMNS)


def study_likelihoods(evidence: pd.DataFrame) -> dict[str, str]:
    """Reduce analysis likelihoods to each study's strongest evidence level."""

    if evidence.empty:
        return {}
    result: dict[str, str] = {}
    for study_id, group in evidence.groupby("study_id"):
        result[str(study_id)] = max(
            group["msms_likelihood"], key=lambda value: LIKELIHOOD_ORDER[str(value)]
        )
    return result
