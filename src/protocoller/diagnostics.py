"""Read-only diagnostics: never imports models or starts recording."""

import importlib.metadata
import os
from pathlib import Path
import platform
import shutil

from protocoller.config import Config


def diagnose(config_path: Path | None = None, transcript_only: bool = False) -> dict:
    result = {"python": platform.python_version(), "platform": platform.platform(),
              "architecture": platform.machine(), "logical_cpus": os.cpu_count(),
              "capture_supported": platform.system() == "Darwin" and
              int((platform.mac_ver()[0] or "0").split(".")[0]) >= 15,
              "swiftc": shutil.which("swiftc"), "languages": ["de", "en"],
              "packages": {}, "models_ready": False}
    for package in ("faster-whisper", "pyannote.audio", "llama-cpp-python", "av", "numpy", "huggingface-hub"):
        try:
            result["packages"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result["packages"][package] = None
    try:
        result["physical_memory_bytes"] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError):
        result["physical_memory_bytes"] = None
    if config_path is not None:
        try:
            Config.load(config_path, require_minutes=not transcript_only).validate_models(
                include_minutes=not transcript_only)
            result["models_ready"] = True
        except (ValueError, OSError) as error:
            result["model_error"] = str(error)
    return result
