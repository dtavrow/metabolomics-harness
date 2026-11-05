"""On-demand provisioning of the official MassSpecGym model weights."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
import urllib.request
import zipfile
import argparse
from pathlib import Path, PurePosixPath

from checkpoints import load_checkpoint_bundle
from contracts import MODEL_SCHEMA_VERSION

REGISTRY = {
    "flare": {
        "model_version": "MassSpecGym-FLARE-2026",
        "upstream_repository": "https://huggingface.co/spaces/HassounLab/FLARE",
        "upstream_commit": "991d5ce4060e8bcd2c8fccb61cc053660cca2949",
        "url": "https://huggingface.co/spaces/HassounLab/FLARE/resolve/main/pretrained_models/flare.ckpt",
        "archive_sha256": "78d0be3cc488b2cccaa39cdcdf4fe1abe05aa22a76007f5ba7095a72fa93b9e0",
        "archive_type": "file",
        "files": [{"path": "flare.ckpt", "sha256": "78d0be3cc488b2cccaa39cdcdf4fe1abe05aa22a76007f5ba7095a72fa93b9e0"}],
    },
    "glacier": {
        "model_version": "MassSpecGym-GLACIER-2026",
        "upstream_repository": "https://github.com/coleygroup/ms-pred",
        "upstream_commit": "708148c",
        "url": "https://www.dropbox.com/scl/fo/ta99j0mp1w7qzek5zvy3w/AFCLQNO8W2EP7ZDMOj9nxWI?rlkey=563zxtyvvhwfu1uyz6n10n2ui&dl=1",
        "archive_sha256": "7e0fa24a19163513fe7cbeea3bf51a48cdd33bf2dbe8ac7a0457cbbf28d1258b",
        "archive_type": "zip",
        "files": [{"path": "best.ckpt", "sha256": "5a47cecca707d3abd5a49c7dbac99d100aa2a586d5d4f848f1e8e35140d7db11"}],
    },
    "iceberg": {
        "model_version": "ICEBERG-2.1-MassSpecGym-msg_all",
        "upstream_repository": "https://github.com/coleygroup/ms-pred",
        "upstream_commit": "708148c",
        "url": "https://www.dropbox.com/scl/fo/kwm35ih8tlfnshfrcq8ot/AOeS4M0_v9MhqeEZys9sCRQ?rlkey=f1n6pbzx94g1k2el2wcbmee61&dl=1",
        "archive_sha256": "2b890a2bf2de1ee0e7d223b440536d8915d35768a09dd8fc5230cd6bac139c66",
        "archive_type": "zip",
        "files": [
            {"path": "gen/best.ckpt", "sha256": "77d78adf7b60e2fdf68b45db39346c7bde2a1a2dbf57c5e7fc2b05223d43f16a"},
            {"path": "gen/hparams.yaml", "sha256": "b4866c3d03007685623e335411f8ee73d3d6b6f30cef2c374d868dbbd39321ea"},
            {"path": "inten_contr/best.ckpt", "sha256": "d2785ff38beb52a1a22200832ed07b9817709470aa03d45bf6af75905bdaa7fa"},
            {"path": "inten_contr/hparams.yaml", "sha256": "7b730add1fe5f9cffaa85cb5902c374cbb840fd2f9b6e098de4b7b39b3477aec"},
        ],
    },
    "jestr": {
        "model_version": "JESTR-MassSpecGym-1741546103623",
        "upstream_repository": "https://github.com/HassounLab/JESTR1",
        "upstream_commit": "a5619c18a85a49171d60ead079684ae667cc0dd0",
        "urls": {
            "pretrained_mol_enc_model_1741546103623_best.pt":
                "https://raw.githubusercontent.com/HassounLab/JESTR1/a5619c18a85a49171d60ead079684ae667cc0dd0/data/MassSpecGym/pretrained_mol_enc_model_1741546103623_best.pt",
            "pretrained_spec_enc_model_1741546103623_best.pt":
                "https://raw.githubusercontent.com/HassounLab/JESTR1/a5619c18a85a49171d60ead079684ae667cc0dd0/data/MassSpecGym/pretrained_spec_enc_model_1741546103623_best.pt",
        },
        "archive_type": "files",
        "files": [
            {"path": "pretrained_mol_enc_model_1741546103623_best.pt", "sha256": "195375270ee530752f29cff2a0cfeb6b2567861b37458aa23a8c01d6ceb8dea4"},
            {"path": "pretrained_spec_enc_model_1741546103623_best.pt", "sha256": "b9f2ccc25ae7710d17d30bfa7cc5ca6e3065962fd0bf24d57af9527d804972fa"},
        ],
    },
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path, timeout: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "MWExtract-model-assets/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


def _safe_extract(archive: Path, destination: Path, expected: set[str]) -> None:
    with zipfile.ZipFile(archive) as zipped:
        actual = set()
        for info in zipped.infolist():
            if info.is_dir():
                continue
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts or len(member.parts) == 0:
                raise ValueError(f"Unsafe checkpoint archive path: {info.filename}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f"Checkpoint archive contains a symlink: {info.filename}")
            relative = member.as_posix()
            if relative not in expected:
                raise ValueError(f"Unexpected checkpoint archive member: {relative}")
            actual.add(relative)
            target = destination.joinpath(*member.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
        if actual != expected:
            raise ValueError(f"Checkpoint archive is missing: {sorted(expected - actual)}")


def _manifest(model: str, spec: dict) -> dict:
    return {
        "schema_version": "1",
        "model_name": model,
        "model_version": spec["model_version"],
        "upstream_repository": spec["upstream_repository"],
        "upstream_commit": spec["upstream_commit"],
        "files": spec["files"],
        "training_datasets": [{"name": "MassSpecGym", "variant": "msg_all" if model == "iceberg" else "official"}],
        "preprocessing_schema_version": MODEL_SCHEMA_VERSION,
        "inference_command": [
            "python", "/app/default_backend.py", "--input", "{input}", "--output", "{output}",
            "--checkpoint-bundle", "{checkpoint_bundle}", "--device", "{device}",
            "--batch-size", "{batch_size}",
        ],
        "score_direction": "higher",
        "asset_url": spec.get("url", spec.get("urls")),
        "asset_sha256": spec.get("archive_sha256"),
    }


def provision_default(model: str, cache_root: Path, timeout: int = 120) -> Path:
    """Download and verify a default bundle, or return its verified cache."""
    spec = REGISTRY[model]
    model_root = cache_root.resolve() / model
    model_root.mkdir(parents=True, exist_ok=True)
    bundle = model_root / "massspecgym"
    lock_path = model_root / ".massspecgym.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if bundle.is_dir():
            try:
                load_checkpoint_bundle(bundle, model)
                return bundle
            except (OSError, ValueError, json.JSONDecodeError):
                invalid = model_root / f"massspecgym.invalid.{os.getpid()}"
                if invalid.exists():
                    shutil.rmtree(invalid)
                bundle.replace(invalid)
        temporary = Path(tempfile.mkdtemp(prefix=".massspecgym-", dir=model_root))
        try:
            if spec["archive_type"] == "files":
                for item in spec["files"]:
                    target = temporary / item["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _download(spec["urls"][item["path"]], target, timeout)
                    if _sha256(target) != item["sha256"]:
                        raise ValueError(f"Downloaded {model} checkpoint checksum mismatch: {item['path']}")
            else:
                archive = temporary / "asset.download"
                _download(spec["url"], archive, timeout)
                if _sha256(archive) != spec["archive_sha256"]:
                    raise ValueError(f"Downloaded {model} checkpoint archive checksum mismatch.")
            if spec["archive_type"] == "zip":
                _safe_extract(archive, temporary, {item["path"] for item in spec["files"]})
                archive.unlink()
            elif spec["archive_type"] == "file":
                archive.replace(temporary / spec["files"][0]["path"])
            (temporary / "model-manifest.json").write_text(
                json.dumps(_manifest(model, spec), indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            load_checkpoint_bundle(temporary, model)
            temporary.chmod(0o755)
            temporary.replace(bundle)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Download a verified default model bundle at image build time.")
    parser.add_argument("--model", choices=sorted(REGISTRY), required=True)
    parser.add_argument("--destination", type=Path, default=Path("/opt/model-bundles"))
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    provision_default(args.model, args.destination, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
