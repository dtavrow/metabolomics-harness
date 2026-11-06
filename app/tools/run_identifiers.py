"""A tiny tool so the overseer never has to invent a run id by hand."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


def pipeline_version() -> str:
    """The harness's own version, read straight from pyproject.toml so
    there is exactly one place this number lives."""

    data = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))
    return data["project"]["version"]


def generate_run_id(study_id: str, version: str | None = None) -> str:
    """Deterministic run id scoped to one study at one pipeline version:
    <study>-<version with dots as dashes>, e.g. ``st005046-1-0-0``.

    Campaigns are aggregated by model version, not by invocation: rerunning
    the same study at the same version reuses this same run id on purpose,
    so results land in one place instead of accumulating under new ids
    every time a model is rerun.
    """

    slug = re.sub(r"[^A-Za-z0-9]+", "", study_id).lower()
    version_slug = re.sub(r"[^A-Za-z0-9]+", "-", version or pipeline_version())
    return f"{slug}-{version_slug}"
