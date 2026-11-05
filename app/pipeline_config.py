"""Environment-driven configuration shared by every pipeline tool and agent.

Centralizing these values means a threshold (concurrency cap, disk budget,
image tag) appears in exactly one place. Everything here is read once from
the process environment; nothing is hardcoded from memory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: Repository root -- two levels up from this file (app/pipeline_config.py).
#: MWExtract/ and models/ (the Docker build contexts) and pipeline_runs/
#: (this harness's own run ledger and scratch workspace) all live here.
REPO_ROOT = Path(__file__).resolve().parents[1]

AGENT_RUNS_ROOT = REPO_ROOT / "pipeline_runs" / "agent_runs"

#: Model names this harness can run, mapped to their default image tag, as
#: published in ``models/README.md``. Overridable per-model via
#: ``MWANNOTATE_<MODEL>_IMAGE`` so a locally retagged build still works.
DEFAULT_MODEL_IMAGES: dict[str, str] = {
    "flare": "mwannotate-flare:cpu",
    "glacier": "mwannotate-glacier:cpu",
    "iceberg": "mwannotate-iceberg:cpu",
    "jestr": "mwannotate-jestr:cpu",
    "sirius": "mwannotate-sirius:6.5.4",
    "evidence": "mwannotation-evidence:1",
}


def parse_byte_size(text: str) -> float:
    """Accept sizes as plain bytes or with a KB/MB/GB/TB suffix."""

    cleaned = str(text).strip().upper().replace("IB", "B")
    multipliers = {"KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "B": 1.0}
    for suffix, factor in multipliers.items():
        if cleaned.endswith(suffix):
            return float(cleaned[: -len(suffix)] or 0) * factor
    return float(cleaned)


def _optional_byte_size(value: str | None) -> float | None:
    if value is None or value.strip().lower() in {"none", "0", ""}:
        return None
    return parse_byte_size(value)


def _model_images_from_env() -> dict[str, str]:
    images = dict(DEFAULT_MODEL_IMAGES)
    for model_name in DEFAULT_MODEL_IMAGES:
        env_key = f"MWANNOTATE_{model_name.upper()}_IMAGE"
        override = os.environ.get(env_key)
        if override:
            images[model_name] = override
    return images


@dataclass(frozen=True)
class PipelineConfig:
    """Resolved configuration for one process lifetime."""

    repo_root: Path
    agent_runs_root: Path
    mwb_contact_email: str | None
    sirius_home_dir: Path | None
    analysis_max_concurrency: int
    disk_budget_bytes: float | None
    disk_reserve_bytes: float
    docker_command_timeout_seconds: float
    mwextract_image: str
    mwparse_mzml_image: str
    mwparse_mzxml_image: str
    parse_count_floor_ratio: float
    model_images: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> PipelineConfig:
        sirius_home = os.environ.get("SIRIUS_HOME_DIR")
        return cls(
            repo_root=REPO_ROOT,
            agent_runs_root=AGENT_RUNS_ROOT,
            mwb_contact_email=os.environ.get("MWB_CONTACT_EMAIL") or None,
            sirius_home_dir=Path(sirius_home) if sirius_home else None,
            analysis_max_concurrency=int(
                os.environ.get("ANALYSIS_MAX_CONCURRENCY", "2")
            ),
            disk_budget_bytes=_optional_byte_size(
                os.environ.get("DISK_BUDGET_BYTES", "100GB")
            ),
            disk_reserve_bytes=parse_byte_size(
                os.environ.get("DISK_RESERVE_BYTES", "50GB")
            ),
            docker_command_timeout_seconds=float(
                os.environ.get("DOCKER_COMMAND_TIMEOUT_SECONDS", str(4 * 3600))
            ),
            mwextract_image=os.environ.get(
                "MWEXTRACT_IMAGE", "mwextract:2026-09-26"
            ),
            mwparse_mzml_image=os.environ.get(
                "MWPARSE_MZML_IMAGE", "mwparse-mzml:latest"
            ),
            mwparse_mzxml_image=os.environ.get(
                "MWPARSE_MZXML_IMAGE", "mwparse-mzxml:latest"
            ),
            # LOOP_CONTRACT.md: the parser's spectrum count is post-QC, so it
            # is always <= the index's raw ms2_expected count by some
            # rejected fraction. It does not pin an exact tolerance ("compare
            # against a floor, not for equality"), so this ratio is this
            # harness's own explicit, overridable choice.
            parse_count_floor_ratio=float(
                os.environ.get("PARSE_COUNT_FLOOR_RATIO", "0.5")
            ),
            model_images=_model_images_from_env(),
        )


_CONFIG: PipelineConfig | None = None


def get_config() -> PipelineConfig:
    """Return the process-wide config, built from the environment on first use."""

    global _CONFIG
    if _CONFIG is None:
        _CONFIG = PipelineConfig.from_env()
    return _CONFIG
