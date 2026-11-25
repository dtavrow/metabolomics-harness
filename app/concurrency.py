"""Cross-process concurrency cap for the analysis stage.

Each model run is now its own ``python -m app.cli run-model ...`` process
(Claude Code's Bash tool invokes the CLI directly), so an in-process
``asyncio.Semaphore`` can no longer bound how many ``docker run`` calls
happen at once across those processes. This does the same job with one
lock file per slot: a process holds a slot for as long as it's running a
container, so at most that pool's slot count ever runs at the same time,
regardless of how many separate CLI invocations are racing for a slot.

Two independent pools, not one shared one:

- ``sirius`` -- always exactly one slot, not configurable. SIRIUS_HOME_DIR
  is a single shared login workspace that concurrent sirius runs would race
  on, so this is "only ever run one sirius container at a time, full stop"
  (per CLAUDE.md), enforced here rather than left to agent discipline.
- ``other`` -- every other model (flare/glacier/iceberg/jestr/evidence),
  sized by ``ANALYSIS_MAX_CONCURRENCY`` (default 1).

A sirius run and an other-model run can proceed at the same time (one slot
from each pool); two sirius runs, or two other-model runs, cannot.
"""

from __future__ import annotations

import contextlib
import fcntl
import time
from collections.abc import Iterator
from pathlib import Path

from app.pipeline_config import get_config

_POLL_SECONDS = 1.0

#: Fixed, not read from config -- see module docstring.
_SIRIUS_POOL_SIZE = 1


def _pool_slot_paths(pool_name: str, pool_size: int) -> list[Path]:
    config = get_config()
    lock_dir = config.agent_runs_root.parent / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return [
        lock_dir / f"analysis-slot-{pool_name}-{i}.lock" for i in range(pool_size)
    ]


@contextlib.contextmanager
def analysis_slot(model_name: str) -> Iterator[None]:
    """Block until a slot in ``model_name``'s pool is free, then hold it for
    the duration of the ``with`` block."""

    config = get_config()
    if model_name == "sirius":
        pool_name, pool_size = "sirius", _SIRIUS_POOL_SIZE
    else:
        pool_name, pool_size = "other", max(1, config.analysis_max_concurrency)
    slot_paths = _pool_slot_paths(pool_name, pool_size)

    while True:
        for slot_path in slot_paths:
            handle = slot_path.open("a")
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
