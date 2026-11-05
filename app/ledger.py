"""Atomic, append-only run ledger for this harness's own pipeline_runs/agent_runs tree.

One JSON file per (study, stage) outcome, written the same way
``download_worker.py`` already writes its job records: through a temp file and
``os.replace`` so a concurrent reader never sees a partial write.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.pipeline_config import get_config


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_json_atomic(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def ledger_path(study_id: str, stage: str) -> Path:
    config = get_config()
    return config.agent_runs_root / "ledger" / study_id / f"{stage}.json"


def append_ledger_row(study_id: str, stage: str, **fields: Any) -> Path:
    """Record one stage outcome for one study.

    Overwrites any previous record for the same ``(study_id, stage)`` pair --
    a stage either has not run, is running, or has its latest outcome; there
    is no need to keep history of earlier attempts for this harness's v1 scope.
    """

    path = ledger_path(study_id, stage)
    payload = {
        "study_id": study_id,
        "stage": stage,
        "recorded_utc": utc_now(),
        **fields,
    }
    write_json_atomic(payload, path)
    return path


def read_ledger_row(study_id: str, stage: str) -> dict[str, Any] | None:
    path = ledger_path(study_id, stage)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
