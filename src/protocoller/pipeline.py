"""Offline feasibility pipeline with separate processes for each model."""

import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

from protocoller.audio.normalize import mix_tracks, normalize_source
from protocoller.config import Config, offline_environment
from protocoller.io import write_json, write_text
from protocoller.minutes.local import render, validate_minutes
from protocoller.models import verify_model
from protocoller.schemas import Turn, Word, reconcile


def run_stage(stage: str, config_path: Path, input_path: Path, output_path: Path,
              speakers: int | None) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    print(f"Processing: {stage}", file=sys.stderr)
    subprocess.run([sys.executable, "-m", "protocoller.worker", stage, str(config_path.resolve()),
                    str(input_path.resolve()), str(output_path.resolve()), str(speakers) if speakers else ""],
                   env=env, check=True)
    return json.loads(output_path.read_text())


def process(input_path: Path, output: Path, config_path: Path, source: str = "auto",
            speakers: int | None = None) -> dict:
    offline_environment()
    if speakers is not None and speakers < 1:
        raise ValueError("Expected speaker count must be positive")
    config = Config.load(config_path)
    config.validate_models()
    if not input_path.exists():
        raise ValueError(f"Audio input does not exist: {input_path}")
    output.mkdir(parents=True, exist_ok=False)
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(dir=output, prefix=".processing-") as temp:
        work = Path(temp)
        audio = work / "normalized.wav"
        sources = [source]
        if input_path.is_dir() and source in {"auto", "both"}:
            from protocoller.audio.recording import read_manifest
            available = read_manifest(input_path)["tracks"]
            sources = ["system", "microphone"] if source == "both" else [
                name for name in ("system", "microphone") if available.get(name, {}).get("has_audio")]
            if not sources:
                raise ValueError("Capture has no recorded audio sources")
        if input_path.is_dir() and len(sources) > 1:
            tracks = []
            for name in sources:
                normalized = work / f"{name}.wav"
                normalize_source(input_path, name, normalized)
                tracks.append(normalized)
            duration = mix_tracks(tracks, audio)
        else:
            duration = normalize_source(input_path, sources[0], audio)
        if duration <= 0:
            raise ValueError("Audio is empty")
        speech = run_stage("transcribe", config_path, audio, work / "speech.json", speakers)
        speakers_result = run_stage("diarize", config_path, audio, work / "speakers.json", speakers)
        words = [Word(**item) for item in speech["words"]]
        turns = [Turn(**item) for item in speakers_result["turns"]]
        segments = reconcile(words, turns)
        transcript = {"schema_version": 1, "language": speech["language"],
                      "duration_seconds": duration, "segments": [segment.to_dict() for segment in segments],
                      "speaker_names": {speaker: speaker for speaker in sorted({turn.speaker for turn in turns})}}
        write_json(output / "transcript.json", transcript)
        write_json(output / "raw-transcription.json", speech)
        write_json(output / "raw-diarization.json", speakers_result)
        lines = ["# Transcript", "", "Speaker IDs are meeting-local; ambiguous assignments require review.", ""]
        for segment in segments:
            flag = " [review speaker]" if segment.ambiguous else ""
            lines.extend([f'<a id="{segment.id}"></a>',
                          f"[{segment.start:.2f}–{segment.end:.2f}] **{segment.speaker or 'Unknown'}**{flag}: {segment.text}", ""])
        write_text(output / "transcript.md", "\n".join(lines))
        summary = run_stage("minutes", config_path, output / "transcript.json", work / "minutes.json", speakers)
        minutes = validate_minutes(summary["minutes"], transcript)
        write_json(output / "minutes.json", {"schema_version": 1, "status": "draft", **minutes})
        write_text(output / "minutes.md", render(minutes))
    elapsed = time.perf_counter() - start
    versions = {}
    for package in ("faster-whisper", "pyannote.audio", "llama-cpp-python", "av"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not installed"
    report = {"schema_version": 1, "platform": platform.platform(), "processor": platform.machine(),
              "python": platform.python_version(), "threads": config.threads, "packages": versions,
              "duration_seconds": duration, "processing_seconds": elapsed,
              "real_time_factor": elapsed / duration,
              "stages": {"transcription": speech["metrics"], "diarization": speakers_result["metrics"],
                         "minutes": summary["metrics"]},
              "models": {kind: verify_model(path, kind) for kind, path in
                         [("transcription", config.transcription_model), ("diarization", config.diarization_model),
                          ("minutes", config.minutes_model)]}}
    write_json(output / "benchmark.json", report)
    return report
