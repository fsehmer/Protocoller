"""Offline stages with verified checkpoints and resumable transcript review."""

import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import wave

from protocoller.audio.normalize import mix_tracks, normalize_source
from protocoller.config import Config, offline_environment
from protocoller.io import write_json, write_text
from protocoller.minutes.local import render, validate_minutes
from protocoller.models import digest, verify_model
from protocoller.schemas import Turn, Word
from protocoller.storage.jobs import active_lock_fd, cancellation_signals, fingerprint, input_fingerprint, job_lock
from protocoller.transcription.review import build_transcript, load_transcript, publish_transcript


def speaker_options(speakers: int | None, minimum: int | None, maximum: int | None) -> int | dict | None:
    for value in (speakers, minimum, maximum):
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError("Speaker counts must be positive integers")
    if speakers is not None and (minimum is not None or maximum is not None):
        raise ValueError("Use --speakers or --min-speakers/--max-speakers, not both")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("Minimum speaker count cannot exceed maximum")
    if speakers is not None:
        return speakers
    bounds = {key: value for key, value in (("min_speakers", minimum), ("max_speakers", maximum))
              if value is not None}
    return bounds or None


def run_stage(stage: str, config_path: Path, input_path: Path, output_path: Path,
              speakers: int | dict | None) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    print(f"Processing: {stage}", file=sys.stderr, flush=True)
    descriptor = active_lock_fd.get()
    child = subprocess.Popen([sys.executable, "-m", "protocoller.worker", stage, str(config_path.resolve()),
                              str(input_path.resolve()), str(output_path.resolve()),
                              json.dumps(speakers) if speakers is not None else ""], env=env,
                             pass_fds=(descriptor,) if descriptor is not None else ())
    try:
        code = child.wait()
        if code:
            raise subprocess.CalledProcessError(code, child.args)
    except BaseException:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise
    return json.loads(output_path.read_text())


def process(input_path: Path, output: Path, config_path: Path, source: str = "auto",
            speakers: int | None = None, *, min_speakers: int | None = None,
            max_speakers: int | None = None, resume: bool = False,
            transcript_only: bool = False) -> dict:
    offline_environment()
    options = speaker_options(speakers, min_speakers, max_speakers)
    if source not in {"auto", "system", "microphone", "both"}:
        raise ValueError("Unknown recording source")
    config = Config.load(config_path, require_minutes=not transcript_only)
    config.validate_models(include_minutes=not transcript_only)
    if not input_path.exists():
        raise ValueError(f"Audio input does not exist: {input_path}")
    if input_path.is_dir() and output.resolve().is_relative_to(input_path.resolve()):
        raise ValueError("Processing output must be outside the recording directory")
    models = {kind: verify_model(path, kind, full=True) for kind, path in
              [("transcription", config.transcription_model), ("diarization", config.diarization_model)]}
    request = {"pipeline_version": 3, "input_sha256": input_fingerprint(input_path),
               "source": source, "speaker_options": options, "language": config.language,
               "threads": config.threads, "models": dict(models)}
    if resume:
        if not (output / "job.json").is_file():
            raise ValueError("No resumable job here; choose a new output directory")
    else:
        output.mkdir(parents=True, exist_ok=False)
    with job_lock(output), cancellation_signals():
        return _process_locked(input_path, output, config, request, options, models,
                               resume, transcript_only)


def _process_locked(input_path: Path, output: Path, config: Config, request: dict,
                    options: int | dict | None, models: dict, resume: bool, transcript_only: bool) -> dict:
    job_path = output / "job.json"
    job = json.loads(job_path.read_text()) if resume else {
        "schema_version": 1, "request": request, "status": "pending", "stages": {}}
    if not isinstance(job, dict) or job.get("schema_version") != 1 or job.get("request") != request:
        raise ValueError("Input, models, or processing options changed; use a new output directory")
    if not isinstance(job.get("stages"), dict) or any(not isinstance(value, dict)
                                                   for value in job["stages"].values()):
        raise ValueError("Invalid job checkpoints; use a new output directory")
    write_json(job_path, job)
    work = output / ".checkpoints"
    work.mkdir(exist_ok=True)
    # Workers consume a fixed configuration snapshot with absolute local model paths.
    snapshot = work / "config.toml"
    lines = ["[models]", f"transcription={json.dumps(str(config.transcription_model))}",
             f"diarization={json.dumps(str(config.diarization_model))}"]
    if config.minutes_model is not None:
        lines.append(f"minutes={json.dumps(str(config.minutes_model))}")
    lines.extend(["[runtime]", f"language={json.dumps(config.language)}", f"threads={config.threads}",
                  f"context_size={config.context_size}", f"max_tokens={config.max_tokens}"])
    write_text(snapshot, "\n".join(lines) + "\n")
    started = time.perf_counter()
    reused = []

    def completed(stage: str, artifact: Path, key: str) -> bool:
        entry = job["stages"].get(stage)
        if entry is None or entry.get("key") != key:
            return False
        if not artifact.is_file() or digest(artifact) != entry.get("sha256"):
            raise ValueError(f"{stage} checkpoint is missing or changed; use a new output directory")
        print(f"Resuming: reuse completed {stage}", file=sys.stderr, flush=True)
        reused.append(stage)
        return True

    def save(stage: str, artifact: Path, key: str) -> None:
        job["stages"][stage] = {"key": key, "sha256": digest(artifact)}
        write_json(job_path, job)

    def stage(stage_name: str, artifact: Path, source_path: Path, key: str) -> dict:
        if not completed(stage_name, artifact, key):
            job.update(status="running", active_stage=stage_name)
            write_json(job_path, job)
            if stage_name == "diarize" and not speech["words"]:
                print("Diarization: skipped (no transcribed speech)", file=sys.stderr, flush=True)
                result = {"turns": [], "skipped": "no_transcribed_speech",
                          "metrics": {"seconds": 0, "peak_rss_bytes": 0}}
            else:
                result = run_stage(stage_name, snapshot, source_path, artifact, options)
            try:
                if stage_name == "transcribe":
                    if not isinstance(result["words"], list) or not isinstance(result["language"], str):
                        raise ValueError("Invalid transcription output")
                    for value in result["words"]:
                        Word(**value)
                elif stage_name == "diarize":
                    if not isinstance(result["turns"], list):
                        raise ValueError("Invalid diarization output")
                    for value in result["turns"]:
                        Turn(**value)
                if stage_name != "minutes" and any(value["end"] > duration + .05
                        for value in result["words" if stage_name == "transcribe" else "turns"]):
                    raise ValueError("Model timestamps extend beyond the recording")
                if not isinstance(result["metrics"], dict):
                    raise ValueError("Missing stage metrics")
            except (KeyError, TypeError) as error:
                raise ValueError(f"Invalid {stage_name} stage output") from error
            # Also works with adapters used in tests that return their output directly.
            write_json(artifact, result)
            if stage_name == "minutes":
                validate_minutes(result["minutes"], load_transcript(output))
            save(stage_name, artifact, key)
        return json.loads(artifact.read_text())

    try:
        job.update(status="running", active_stage="normalize")
        write_json(job_path, job)
        audio = work / "normalized.wav"
        audio_key = fingerprint(request)
        if not completed("normalize", audio, audio_key):
            print("Processing: normalize", file=sys.stderr, flush=True)
            # An abrupt stop may publish a WAV just before its checkpoint is committed.
            for unfinished in (audio, work / "system.wav", work / "microphone.wav"):
                unfinished.unlink(missing_ok=True)
            sources = [request["source"]]
            if input_path.is_dir() and request["source"] in {"auto", "both"}:
                from protocoller.audio.recording import read_manifest
                available = read_manifest(input_path)["tracks"]
                sources = ["system", "microphone"] if request["source"] == "both" else [
                    name for name in ("system", "microphone") if available.get(name, {}).get("has_audio")]
                if not sources:
                    raise ValueError("Capture has no recorded audio sources")
            if input_path.is_dir() and len(sources) > 1:
                tracks = []
                for name in sources:
                    normalized = work / f"{name}.wav"
                    normalize_source(input_path, name, normalized)
                    tracks.append(normalized)
                mix_tracks(tracks, audio)
                for track in tracks:
                    track.unlink()
            else:
                normalize_source(input_path, sources[0], audio)
            if input_fingerprint(input_path) != request["input_sha256"]:
                raise ValueError("Input changed during normalization; use a new output directory")
            with wave.open(str(audio)) as stream:
                if stream.getnframes() == 0:
                    raise ValueError("Audio is empty")
            save("normalize", audio, audio_key)
        with wave.open(str(audio)) as stream:
            duration = stream.getnframes() / stream.getframerate()
        speech = stage("transcribe", output / "raw-transcription.json", audio, audio_key)
        voices = stage("diarize", output / "raw-diarization.json", audio, audio_key)
        generated = build_transcript(speech, voices, duration)
        if not (output / "transcript.json").exists():
            if job.get("transcript_created") or list((output / "revisions").glob("*.json")):
                raise ValueError("Transcript is missing; restore it from its revision snapshot before resuming")
            publish_transcript(output, generated)
        job["transcript_created"] = True
        write_json(job_path, job)
        transcript = load_transcript(output)
        # Repair derived Markdown after an interruption without overwriting reviewed JSON.
        from protocoller.transcription.review import render_transcript
        write_text(output / "transcript.md", render_transcript(transcript))
        stages = {"transcription": speech["metrics"], "diarization": voices["metrics"]}
        if not transcript_only:
            models["minutes"] = verify_model(config.minutes_model, "minutes", full=True)
            minutes_key = fingerprint({"transcript": transcript, "model": models["minutes"],
                                       "context_size": config.context_size, "max_tokens": config.max_tokens})
            previous = job["stages"].get("minutes")
            if previous and previous["key"] != minutes_key and (output / "minutes.json").exists():
                raise ValueError("Transcript or minutes settings changed; draft regeneration belongs to milestone 4")
            summary = stage("minutes", work / "minutes.json", output / "transcript.json", minutes_key)
            minutes = validate_minutes(summary["minutes"], transcript)
            if not (output / "minutes.json").exists():
                write_json(output / "minutes.json", {"schema_version": 1, "status": "draft",
                           "transcript_revision_id": transcript["revision_id"], **minutes})
            if not (output / "minutes.md").exists():
                write_text(output / "minutes.md", render(minutes))
            stages["minutes"] = summary["metrics"]
        elapsed = time.perf_counter() - started
        versions = {}
        for package in ("faster-whisper", "pyannote.audio", "llama-cpp-python", "av"):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = "not installed"
        report = {"schema_version": 1, "platform": platform.platform(), "processor": platform.machine(),
                  "python": platform.python_version(), "threads": config.threads, "packages": versions,
                  "duration_seconds": duration, "processing_seconds": elapsed,
                  "real_time_factor": elapsed / duration, "stages": stages, "models": models,
                  "resumed": resume, "mode": "transcript" if transcript_only else "transcript_and_minutes"}
        report["reused_stages"] = reused
        report["timing_scope"] = "current invocation after input/model validation; stage metrics may be cached"
        write_json(output / "benchmark.json", report)
        job.update(status="completed", active_stage=None)
        write_json(job_path, job)
        return report
    except BaseException as error:
        # Persist stage/status only; error text can contain meeting content from libraries.
        job["status"] = "cancelled" if isinstance(error, KeyboardInterrupt) else "failed"
        write_json(job_path, job)
        raise
