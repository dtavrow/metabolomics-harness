"""Shared safe I/O and subprocess helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_rows(path: Path, rows: list[dict], schema: pa.Schema) -> None:
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, compression="zstd")


def atomic_directory(target: Path) -> tuple[Path, callable]:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    # mkdtemp intentionally creates mode 0700. Published Docker bind-mount
    # outputs must remain inspectable by the host user.
    temporary.chmod(0o755)

    def publish() -> None:
        backup = target.with_name(target.name + ".previous")
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            target.replace(backup)
        try:
            temporary.replace(target)
        except Exception:
            if backup.exists() and not target.exists():
                backup.replace(target)
            raise
        if backup.exists():
            shutil.rmtree(backup)

    return temporary, publish


def run_command(command: list[str], cwd: Path | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False)
    if completed.returncode:
        raise RuntimeError(f"Backend exited with code {completed.returncode}.")


def expand_command(command: list[str], values: dict[str, str]) -> list[str]:
    if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
        raise ValueError("inference_command must be a non-empty string list.")
    return [part.format_map(values) for part in command]
