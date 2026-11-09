"""SSE endpoint an optional dashboard can subscribe to for live pipeline
progress.

Every pipeline stage runs as its own CLI subprocess (see app/cli.py), so
there is no in-process event stream to hook into -- this just tails the
JSONL file app/app_utils/progress_bus.py appends to and re-emits each new
line as an SSE frame. Running this server at all is optional: the harness
itself works entirely through `claude` + app/cli.py with no server.

Note: Cloudflare's anonymous Quick Tunnels (trycloudflare.com) buffer the
entire response body until the connection closes regardless of what's sent
here -- a documented Quick Tunnel limitation, not something fixable at the
app layer. This endpoint only streams correctly behind a named tunnel (or
no tunnel at all).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict

import pyarrow.parquet as pq
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.app_utils.progress_bus import get_progress_bus, progress_log_path
from app.docker_environment import DockerPipelineEnvironment, InvalidStudyIdError
from app.pipeline_config import get_config
from app.winners_db import list_winners

_HEARTBEAT_SECONDS = 15
_POLL_SECONDS = 0.5


def build_dashboard_router() -> APIRouter:
    router = APIRouter()

    @router.get("/dashboard/events")
    async def dashboard_events(request: Request) -> StreamingResponse:
        async def event_stream():
            yield ": connected\n\n"
            path = progress_log_path()
            offset = path.stat().st_size if path.is_file() else 0
            elapsed_since_heartbeat = 0.0
            while True:
                if await request.is_disconnected():
                    break
                if path.is_file() and path.stat().st_size > offset:
                    with path.open("rb") as stream:
                        stream.seek(offset)
                        new_bytes = stream.read()
                        offset = stream.tell()
                    for line in new_bytes.decode("utf-8").splitlines():
                        if line.strip():
                            yield f"data: {line}\n\n"
                    elapsed_since_heartbeat = 0.0
                    continue
                await asyncio.sleep(_POLL_SECONDS)
                elapsed_since_heartbeat += _POLL_SECONDS
                if elapsed_since_heartbeat >= _HEARTBEAT_SECONDS:
                    yield ": heartbeat\n\n"
                    elapsed_since_heartbeat = 0.0

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/dashboard/history")
    async def dashboard_history() -> list[dict]:
        return get_progress_bus().read_all()

    @router.post("/dashboard/dismiss/{study_id}")
    async def dashboard_dismiss(study_id: str) -> dict:
        """Record that a study's block should stop showing in the Active
        grid. This is just another progress-bus event (stage="dismiss") --
        every tab (this one and any other) already replays progress.jsonl
        on load and subscribes to it live, so no separate storage or stream
        is needed for this to be visible everywhere."""

        get_progress_bus().publish(
            stage="dismiss", key=study_id, status="dismissed", detail="",
        )
        return {"study_id": study_id, "dismissed": True}

    @router.get("/dashboard/study-stats/{study_id}")
    async def dashboard_study_stats(study_id: str) -> dict:
        """Spectrum/candidate counts for the Finished tab. Not available
        anywhere else (not in progress.jsonl, not in the ledger) -- read
        straight from the durable parsed-output Parquet files' metadata
        (row counts only, no full scan)."""

        environment = DockerPipelineEnvironment()
        try:
            processed_root = environment.study_output_dir(study_id) / "processed"
        except InvalidStudyIdError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        spectrum_count = None
        candidate_count = None
        for spectra_path in processed_root.glob("*/spectra.parquet"):
            spectrum_count = pq.ParquetFile(spectra_path).metadata.num_rows
            candidates_path = spectra_path.parent / "candidates.parquet"
            if candidates_path.is_file():
                candidate_count = pq.ParquetFile(candidates_path).metadata.num_rows
            break

        avg_candidates_per_spectrum = (
            candidate_count / spectrum_count
            if spectrum_count and candidate_count is not None
            else None
        )
        return {
            "study_id": study_id,
            "spectrum_count": spectrum_count,
            "candidate_count": candidate_count,
            "avg_candidates_per_spectrum": avg_candidates_per_spectrum,
        }

    @router.get("/dashboard/instrument-polarity-counts")
    async def dashboard_instrument_polarity_counts() -> dict:
        """Spectra extracted so far, cross-tabbed by instrument type and
        polarity, for the Active tab's heatmap. Summed from every study's
        fetch-stage ledger row (`instrument_polarity_counts`, recorded at
        fetch time -- see app/tools/extraction_tools.py) rather than the raw
        payload, which is routinely deleted long before a study is done."""

        totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        ledger_root = get_config().agent_runs_root / "ledger"
        for fetch_row_path in ledger_root.glob("*/fetch.json"):
            row = json.loads(fetch_row_path.read_text(encoding="utf-8"))
            for instrument, polarities in (row.get("instrument_polarity_counts") or {}).items():
                for polarity, count in polarities.items():
                    totals[instrument][polarity] += count

        return {instrument: dict(polarities) for instrument, polarities in totals.items()}

    @router.get("/dashboard/winners")
    async def dashboard_winners() -> list[dict]:
        """Spectra where 3 or more scoring models independently agree on the
        same candidate with a normalized score (rank_score) of at least
        0.8 -- see app/winners_db.py, populated incrementally by aggregate."""

        return list_winners()

    return router
