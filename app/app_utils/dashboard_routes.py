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

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.app_utils.progress_bus import progress_log_path

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
        from app.app_utils.progress_bus import get_progress_bus

        return get_progress_bus().read_all()

    return router
