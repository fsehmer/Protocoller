"""Explicit installation with immutable revisions and integrity manifests."""

import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

from protocoller.io import write_json

KINDS = ("transcription", "diarization", "minutes")


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def bundle_root(path: Path) -> Path:
    return path.parent if path.is_file() else path


def check_layout(path: Path, kind: str) -> None:
    if kind == "transcription":
        required = ("model.bin", "config.json", "tokenizer.json")
    elif kind == "diarization":
        required = ("config.yaml",)
        if not any(path.rglob("*.bin")) and not any(path.rglob("*.safetensors")):
            raise ValueError("Diarization bundle needs local weights, not only a remote config")
    elif kind == "minutes":
        if not path.is_file() or path.suffix.lower() != ".gguf":
            raise ValueError("Minutes model must be a local GGUF file")
        with path.open("rb") as stream:
            if stream.read(4) != b"GGUF":
                raise ValueError("Invalid GGUF file header")
        return
    else:
        raise ValueError(f"Unknown model kind: {kind}")
    if not path.is_dir() or any(not (path / name).is_file() for name in required):
        raise ValueError(f"Incomplete {kind} model directory; required: {', '.join(required)}")


def verify_model(path: Path, kind: str, full: bool = False) -> dict:
    if not path.exists():
        raise ValueError(f"Missing {kind} model: {path}; run models import or models download first")
    check_layout(path, kind)
    root = bundle_root(path)
    manifest_path = root / "protocoller-model.json"
    if not manifest_path.is_file():
        raise ValueError(f"Unregistered model: {path}; register it with models import")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("kind") != kind or manifest.get("schema_version") != 1:
        raise ValueError(f"Invalid model manifest: {manifest_path}")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Model manifest has no files")
    expected_files = {file.relative_to(root).as_posix() for file in root.rglob("*")
                      if file.is_file() and file != manifest_path}
    if expected_files != set(files):
        raise ValueError("Model bundle files changed; import the original bundle again")
    for name, expected in files.items():
        file = (root / name).resolve()
        if not file.is_relative_to(root.resolve()) or not file.is_file():
            raise ValueError(f"Invalid model file: {name}")
        if file.stat().st_size != expected["size"] or (full and digest(file) != expected["sha256"]):
            raise ValueError(f"Model file failed integrity check: {name}")
    return manifest


def import_model(source: Path, destination: Path, kind: str, provenance: dict | None = None) -> Path:
    source = source.resolve()
    destination = destination.resolve()
    check_layout(source, kind)
    if source.is_dir() and destination.is_relative_to(source):
        raise ValueError("Model destination must be outside the source directory")
    if destination.exists():
        raise ValueError(f"Destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".model-import-") as temp:
        stage = Path(temp) / "bundle"
        if source.is_file():
            stage.mkdir()
            shutil.copyfile(source, stage / source.name)
            model_path = stage / source.name
        else:
            shutil.copytree(source, stage, ignore=shutil.ignore_patterns(".git", ".cache", "protocoller-model.json"))
            model_path = stage
        check_layout(model_path, kind)
        files = {file.relative_to(stage).as_posix(): {"size": file.stat().st_size, "sha256": digest(file)}
                 for file in sorted(stage.rglob("*")) if file.is_file()}
        write_json(stage / "protocoller-model.json", {
            "schema_version": 1, "kind": kind, "provenance": provenance or {"source": "offline import"},
            "files": files,
        })
        stage.rename(destination)
    return destination / source.name if source.is_file() else destination


def download_model(repo: str, revision: str, destination: Path, kind: str,
                   filename: str | None = None) -> Path:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise ValueError("Use an immutable 40-character model commit SHA, not main or a tag")
    if kind == "minutes" and (not filename or not filename.lower().endswith(".gguf")):
        raise ValueError("Minutes download requires --filename with the chosen GGUF file")
    if destination.exists():
        raise ValueError(f"Destination already exists: {destination}")
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise ValueError("Install the setup extra: uv sync --extra setup") from error
    # Only this explicitly invoked command enables online model retrieval.
    import os
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    with tempfile.TemporaryDirectory(prefix="protocoller-download-") as temp:
        snapshot = Path(snapshot_download(repo_id=repo, revision=revision, local_dir=temp,
                                         allow_patterns=[filename] if filename else None))
        source = snapshot / filename if filename else snapshot
        return import_model(source, destination, kind, {"repo": repo, "revision": revision,
                                                        "filename": filename})
