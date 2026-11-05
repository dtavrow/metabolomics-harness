"""Durable, cross-process log of pipeline stage progress for the optional
dashboard's SSE feed.

Each pipeline stage (extraction, processing, scoring, aggregation) now runs
as its own ``python -m app.cli ...`` subprocess, invoked by Claude Code's
Bash tool -- there is no long-lived server process for an in-memory bus to
live in. So this is a plain append-only JSONL file (same atomic-write
spirit as app/ledger.py) that any tool process can append one line to, and
that the dashboard server (a separate, optional process) tails from the
end for its SSE stream.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from app.pipeline_config import get_config


class ProgressEvent(TypedDict):
    ts: str
    stage: str
    key: str
    status: str
    detail: str


def progress_log_path() -> Path:
    return get_config().agent_runs_root.parent / "progress.jsonl"


class ProgressBus:
    def publish(self, *, stage: str, key: str, status: str, detail: str) -> None:
        event: ProgressEvent = {
            "ts": datetime.now(UTC).isoformat(),
            "stage": stage,
            "key": key,
            "status": status,
            "detail": detail,
        }
        path = progress_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # One write() call of one line: on a local filesystem this is atomic
        # with respect to other appenders, which is all the guarantee a
        # best-effort progress feed (as opposed to the ledger) needs.
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

    def read_all(self) -> list[ProgressEvent]:
        path = progress_log_path()
        if not path.is_file():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]


_bus = ProgressBus()


def get_progress_bus() -> ProgressBus:
    return _bus
