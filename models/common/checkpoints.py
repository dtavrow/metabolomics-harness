"""Validation for runtime-mounted, auditable checkpoint bundles."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

REQUIRED = {
    "schema_version", "model_name", "model_version", "upstream_repository",
    "upstream_commit", "files", "training_datasets", "preprocessing_schema_version",
    "inference_command", "score_direction",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_checkpoint_bundle(bundle: Path, expected_model: str) -> tuple[dict, str]:
    manifest_path = bundle / "model-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"Checkpoint bundle is missing {manifest_path.name}.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    missing = sorted(REQUIRED - set(manifest))
    if missing:
        raise ValueError(f"Checkpoint manifest is missing: {', '.join(missing)}.")
    if manifest["model_name"].casefold() != expected_model.casefold():
        raise ValueError(
            f"Checkpoint is for {manifest['model_name']}, not {expected_model}."
        )
    if manifest["score_direction"] not in {"higher", "lower"}:
        raise ValueError("score_direction must be higher or lower.")
    files = manifest["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("Checkpoint manifest files must be a non-empty list.")
    for item in files:
        if not isinstance(item, dict) or not {"path", "sha256"} <= set(item):
            raise ValueError("Every checkpoint file needs path and sha256.")
        path = (bundle / item["path"]).resolve()
        if bundle.resolve() not in path.parents:
            raise ValueError(f"Checkpoint path escapes bundle: {item['path']}")
        if not path.is_file():
            raise ValueError(f"Checkpoint file not found: {item['path']}")
        actual = sha256(path)
        if actual.casefold() != str(item["sha256"]).casefold():
            raise ValueError(f"Checkpoint checksum mismatch: {item['path']}")
    fingerprint = sha256(manifest_path)
    return manifest, fingerprint
