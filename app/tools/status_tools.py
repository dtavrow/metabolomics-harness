"""Render each study's ledger as a human-readable markdown status file.

The ledger (app/ledger.py) is the source of truth -- one JSON file per
(study, stage) outcome. This module never stores anything of its own; every
markdown file here is fully regenerated from the ledger each time, so there
is nothing to keep in sync and nothing that can drift stale.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.docker_environment import InvalidStudyIdError, validate_study_id
from app.pipeline_config import get_config

#: Canonical read order: the four always-single-valued stages first, then
#: however many analysis_<model> stages exist (sorted for a stable order),
#: then the campaign-level aggregate.
_STAGE_ORDER = ("fetch", "parse", "delete", "aggregate")

#: Fields long enough that inlining them would bury the stage's actual
#: outcome; shown last, fenced, and clipped.
_LONG_FIELDS = ("stdout_tail", "stderr_tail", "report", "reasons")
_LONG_FIELD_CLIP = 500


def status_root() -> Path:
    root = get_config().agent_runs_root / "status"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ledger_dir(study_id: str) -> Path:
    return get_config().agent_runs_root / "ledger" / study_id


def _sort_key(stage: str) -> tuple[int, str]:
    if stage in _STAGE_ORDER:
        return (_STAGE_ORDER.index(stage), stage)
    return (len(_STAGE_ORDER), stage)


def _format_value(value: Any) -> str:
    if isinstance(value, dict | list):
        return f"`{json.dumps(value, sort_keys=True)}`"
    if isinstance(value, str):
        return f"`{value}`"
    return f"`{value!r}`"


def _render_stage(stage: str, row: dict[str, Any]) -> str:
    state = row.get("state", "unknown")
    lines = [f"## {stage}", "", f"- **state**: {state}"]
    short_fields = {
        key: value for key, value in row.items()
        if key not in {"study_id", "stage", "state", *_LONG_FIELDS}
    }
    for key, value in sorted(short_fields.items()):
        lines.append(f"- **{key}**: {_format_value(value)}")
    for key in _LONG_FIELDS:
        value = row.get(key)
        if not value:
            continue
        text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        clipped = text if len(text) <= _LONG_FIELD_CLIP else text[:_LONG_FIELD_CLIP] + " …(clipped)"
        lines += [f"- **{key}**:", "", "```", clipped, "```"]
    lines.append("")
    return "\n".join(lines)


def render_study_status_markdown(study_id: str) -> str | None:
    """Build one study's markdown status from its ledger rows, or ``None``
    if this study has no ledger entries at all (nothing has run yet)."""

    ledger_dir = _ledger_dir(study_id)
    if not ledger_dir.is_dir():
        return None
    rows: dict[str, dict[str, Any]] = {}
    for path in ledger_dir.glob("*.json"):
        rows[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    if not rows:
        return None

    recorded = sorted((row.get("recorded_utc", "") for row in rows.values()), reverse=True)
    sections = [_render_stage(stage, rows[stage]) for stage in sorted(rows, key=_sort_key)]
    header = [
        f"# {study_id}", "",
        f"Last activity: {recorded[0] if recorded else 'unknown'}",
        "",
        "Auto-generated from the ledger (`pipeline_runs/agent_runs/ledger/"
        f"{study_id}/`) -- do not hand-edit, it is overwritten on every "
        "regeneration.",
        "",
        "",
    ]
    return "\n".join(header) + "\n".join(sections)


def write_study_status_markdown(study_id: str) -> dict[str, Any]:
    """Write (or refresh) one study's status markdown. Returns a result dict
    matching this harness's standard CLI contract."""

    try:
        study_id = validate_study_id(study_id)
    except InvalidStudyIdError as error:
        return {"success": False, "status": "invalid_study_id", "error": str(error)}

    markdown = render_study_status_markdown(study_id)
    if markdown is None:
        return {"success": False, "status": "no_ledger_entries", "study_id": study_id}

    path = status_root() / f"{study_id}.md"
    path.write_text(markdown, encoding="utf-8")
    return {
        "success": True, "status": "written", "study_id": study_id,
        "markdown_path": str(path),
    }


def write_all_study_status_markdown() -> dict[str, Any]:
    """Regenerate every study's status markdown, plus one index file
    listing all of them with their last-activity stage at a glance."""

    ledger_root = get_config().agent_runs_root / "ledger"
    study_ids = sorted(
        entry.name for entry in ledger_root.iterdir() if entry.is_dir()
    ) if ledger_root.is_dir() else []

    written: list[str] = []
    index_rows: list[tuple[str, str, str]] = []
    for study_id in study_ids:
        result = write_study_status_markdown(study_id)
        if not result["success"]:
            continue
        written.append(result["markdown_path"])
        rows = {
            path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in _ledger_dir(study_id).glob("*.json")
        }
        latest_stage = max(rows, key=lambda stage: rows[stage].get("recorded_utc", ""))
        index_rows.append((study_id, latest_stage, rows[latest_stage].get("state", "unknown")))

    index_lines = [
        "# Study status index", "",
        "Auto-generated -- do not hand-edit, it is overwritten on every "
        "regeneration. One row per study; open `<study>.md` for full detail.",
        "",
        "| study | last stage | state |",
        "| --- | --- | --- |",
    ]
    for study_id, stage, state in index_rows:
        index_lines.append(f"| [{study_id}]({study_id}.md) | {stage} | {state} |")
    index_path = status_root() / "INDEX.md"
    index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    return {
        "success": True, "status": "written", "studies_written": len(written),
        "index_path": str(index_path), "markdown_paths": written,
    }
