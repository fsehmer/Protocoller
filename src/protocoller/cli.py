"""A small CLI; recording and model downloads are explicit commands."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import wave

from protocoller.config import data_directory


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Local meeting capture, transcripts, and draft minutes")
    cli.add_argument("--version", action="version", version="protocoller 0.1.0")
    commands = cli.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Inspect platform, dependencies, and optional model configuration")
    doctor.add_argument("--config", type=Path)
    doctor.add_argument("--transcript-only", action="store_true", help="Check only speech model bundles")
    commands.add_parser("build-capture", help="Compile the macOS capture helper (does not record)")
    commands.add_parser("devices", help="List microphone device IDs (does not record)")
    recording = commands.add_parser("record", help="Record system playback, optionally with microphone input")
    recording.add_argument("--output", type=Path, required=True, help="New recording directory")
    recording.add_argument("--seconds", type=float, default=30, help="Maximum duration; Ctrl-C stops early")
    recording.add_argument("--source", choices=("system", "microphone", "both"), default="both")
    recording.add_argument("--chunk-seconds", type=float, default=2, help="Durable chunk length (0.1–60 seconds)")
    recording.add_argument("--microphone", default="default", help="Device ID from devices; default uses the OS input")
    for name, help_text in (("process", "Produce a transcript and draft minutes offline"),
                            ("transcribe", "Produce a reviewable transcript without a minutes model")):
        processing = commands.add_parser(name, help=help_text)
        processing.add_argument("input", type=Path)
        processing.add_argument("--output", type=Path, required=True, help="New directory, or existing job with --resume")
        processing.add_argument("--config", type=Path, default=data_directory() / "config.toml")
        processing.add_argument("--source", choices=("auto", "system", "microphone", "both"), default="auto")
        processing.add_argument("--speakers", type=int, help="Optional known speaker count")
        processing.add_argument("--min-speakers", type=int)
        processing.add_argument("--max-speakers", type=int)
        processing.add_argument("--resume", action="store_true", help="Reuse verified successful stages")
    transcript = commands.add_parser("transcript", help="Review or export a local transcript without models")
    reviews = transcript.add_subparsers(dest="operation", required=True)
    show = reviews.add_parser("show", help="Print current transcript JSON, including revision and IDs")
    show.add_argument("directory", type=Path)
    apply = reviews.add_parser("apply", help="Apply an atomic JSON correction patch")
    apply.add_argument("directory", type=Path)
    apply.add_argument("patch", type=Path)
    export = reviews.add_parser("export", help="Export the current reviewed transcript")
    export.add_argument("directory", type=Path)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--format", choices=("json", "markdown"), default="markdown")
    audio = commands.add_parser("audio", help="Import, export, or recover audio without loading models")
    audio_commands = audio.add_subparsers(dest="operation", required=True)
    for operation in ("import", "export", "recover"):
        command = audio_commands.add_parser(operation)
        command.add_argument("input", type=Path)
        command.add_argument("--output", type=Path, required=True, help="New output directory")
    models = commands.add_parser("models", help="Explicit model installation and integrity verification")
    operations = models.add_subparsers(dest="operation", required=True)
    for operation in ("import", "download", "verify"):
        command = operations.add_parser(operation)
        command.add_argument("--kind", choices=("transcription", "diarization", "minutes"), required=True)
        if operation == "verify":
            command.add_argument("path", type=Path)
        else:
            command.add_argument("--destination", type=Path, required=True, help="New model bundle directory")
            if operation == "import":
                command.add_argument("source", type=Path)
            else:
                command.add_argument("--repo", required=True)
                command.add_argument("--revision", required=True, help="Immutable 40-character commit SHA")
                command.add_argument("--filename", help="GGUF filename for minutes downloads")
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            from protocoller.diagnostics import diagnose
            report = diagnose(args.config, transcript_only=args.transcript_only)
            print(json.dumps(report, indent=2))
            return 1 if args.config and not report["models_ready"] else 0
        if args.command in {"build-capture", "devices", "record"}:
            from protocoller.audio.capture import build_helper, record
            if args.command == "build-capture":
                print(build_helper())
            elif args.command == "devices":
                subprocess.run([str(build_helper()), "--devices"], check=True)
            else:
                return record(args.output, args.seconds, args.source, args.microphone, args.chunk_seconds)
        elif args.command == "audio":
            if args.operation == "import":
                from protocoller.audio.importer import import_audio
                report = import_audio(args.input, args.output)
            else:
                from protocoller.audio.recording import export_capture
                report = export_capture(args.input, args.output, recover=args.operation == "recover")
            print(json.dumps(report, indent=2))
            print(f"Saved playable audio to {args.output}")
        elif args.command in {"process", "transcribe"}:
            from protocoller.pipeline import process
            report = process(args.input, args.output, args.config, args.source, args.speakers,
                             min_speakers=args.min_speakers, max_speakers=args.max_speakers,
                             resume=args.resume, transcript_only=args.command == "transcribe")
            artifacts = "transcript" if args.command == "transcribe" else "transcript and draft minutes"
            print(f"Saved {artifacts} to {args.output}; real-time factor {report['real_time_factor']:.2f}")
        elif args.command == "transcript":
            from protocoller.transcription.review import apply_review, export_transcript, load_transcript
            if args.operation == "show":
                print(json.dumps(load_transcript(args.directory), indent=2, ensure_ascii=False))
            elif args.operation == "apply":
                result = apply_review(args.directory, json.loads(args.patch.read_text()))
                print(f"Saved transcript revision {result['revision']} to {args.directory}")
            else:
                export_transcript(args.directory, args.output, args.format)
                print(f"Exported transcript to {args.output}")
        elif args.command == "models":
            from protocoller.models import download_model, import_model, verify_model
            if args.operation == "verify":
                print(json.dumps(verify_model(args.path.resolve(), args.kind, full=True), indent=2))
            elif args.operation == "import":
                print(import_model(args.source, args.destination.resolve(), args.kind))
            else:
                print(download_model(args.repo, args.revision, args.destination.resolve(), args.kind, args.filename))
        return 0
    except (OSError, ValueError, wave.Error, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled; completed transcript artifacts, if any, remain in the output directory.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
