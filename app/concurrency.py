"""Cross-process concurrency cap for the analysis stage.

Each model run is now its own ``python -m app.cli run-model ...`` process
(Claude Code's Bash tool invokes the CLI directly), so an in-process
``asyncio.Semaphore`` can no longer bound how many ``docker run`` calls
happen at once across those processes. This does the same job with one
lock file per slot: a process holds slot N for as long as it's running a
container, so at most ``analysis_max_concurrency`` containers ever run at
the same time, regardless of how many separate CLI invocations are racing
for a slot.
"""

from __future__ import annotations

import contextlib
import fcntl
import time
from collections.abc import Iterator
from pathlib import Path

from app.pipeline_config import get_config

_POLL_SECONDS = 1.0


@contextlib.contextmanager
def analysis_slot() -> Iterator[None]:
    """Block until one of ``analysis_max_concurrency`` slots is free, then
    hold it for the duration of the ``with`` block."""

    config = get_config()
    lock_dir = config.agent_runs_root.parent / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    slot_paths = [lock_dir / f"analysis-slot-{i}.lock" for i in range(config.analysis_max_concurrency)]

    while True:
        for slot_path in slot_paths:
            handle = Path(slot_path).open("a")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            return
        time.sleep(_POLL_SECONDS)
