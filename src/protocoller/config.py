"""Explicit local paths and offline runtime defaults."""

import os
from dataclasses import dataclass
from pathlib import Path
import tomllib


def offline_environment() -> None:
    # Set before importing any model libraries, even if the shell enables telemetry.
    os.environ.update({
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "PYANNOTE_METRICS_ENABLED": "0",
        "DO_NOT_TRACK": "1",
    })


def data_directory() -> Path:
    return Path(os.environ.get("PROTOCOLLER_DATA_DIR", "~/.protocoller")).expanduser()


@dataclass(frozen=True)
class Config:
    transcription_model: Path
    diarization_model: Path
    minutes_model: Path | None
    language: str = "auto"
    context_size: int = 4096
    max_tokens: int = 1024
    threads: int = 4

    @classmethod
    def load(cls, path: Path, require_minutes: bool = True) -> "Config":
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
        models = raw.get("models", {})
        runtime = raw.get("runtime", {})

        def local_path(key: str) -> Path:
            value = models.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"Missing [models].{key} in {path}")
            candidate = Path(value).expanduser()
            return (path.parent / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

        language = runtime.get("language", "auto")
        if language not in {"auto", "de", "en"}:
            raise ValueError("language must be auto, de, or en")
        values = {key: runtime.get(key, default) for key, default in
                  [("context_size", 4096), ("max_tokens", 1024), ("threads", 4)]}
        if any(type(value) is not int or value <= 0 for value in values.values()):
            raise ValueError("Runtime sizes and threads must be positive integers")
        if values["max_tokens"] >= values["context_size"]:
            raise ValueError("max_tokens must be smaller than context_size")
        return cls(local_path("transcription"), local_path("diarization"),
                   local_path("minutes") if require_minutes or models.get("minutes") else None,
                   language, **values)

    def validate_models(self, include_minutes: bool = True) -> None:
        from protocoller.models import verify_model

        models = [("transcription", self.transcription_model), ("diarization", self.diarization_model)]
        if include_minutes:
            if self.minutes_model is None:
                raise ValueError("Missing [models].minutes in configuration")
            models.append(("minutes", self.minutes_model))
        for kind, path in models:
            verify_model(path, kind)
