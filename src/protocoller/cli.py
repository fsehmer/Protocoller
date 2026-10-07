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
    commands.add_parser("build-capture", help="Compile the macOS capture helper (does not record)")
    commands.add_parser("devices", help="List microphone device IDs (does not record)")
    recording = commands.add_parser("record", help="Record system playback, optionally with microphone input")
    recording.add_argument("--output", type=Path, required=True, help="New recording directory")
    recording.add_argument("--seconds", type=float, default=30, help="Maximum duration; Ctrl-C stops early")
    recording.add_argument("--source", choices=("system", "both"), default="both")
    recording.add_argument("--microphone", default="default", help="Device ID from devices; default uses the OS input")
    processing = commands.add_parser("process", help="Process a local audio file or capture directory offline")
    processing.add_argument("input", type=Path)
    processing.add_argument("--output", type=Path, required=True, help="New output directory")
    processing.add_argument("--config", type=Path, default=data_directory() / "config.toml")
    processing.add_argument("--source", choices=("system", "microphone", "both"), default="both")
    processing.add_argument("--speakers", type=int, help="Optional known speaker count")
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
            report = diagnose(args.config)
            print(json.dumps(report, indent=2))
            return 1 if args.config and not report["models_ready"] else 0
        if args.command in {"build-capture", "devices", "record"}:
            from protocoller.audio.capture import build_helper, record
            if args.command == "build-capture":
                print(build_helper())
            elif args.command == "devices":
                subprocess.run([str(build_helper()), "--devices"], check=True)
            else:
                return record(args.output, args.seconds, args.source, args.microphone)
        elif args.command == "process":
            from protocoller.pipeline import process
            report = process(args.input, args.output, args.config, args.source, args.speakers)
            print(f"Saved transcript and draft minutes to {args.output}; real-time factor {report['real_time_factor']:.2f}")
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
