"""Validate and assemble durable PCM recordings without inference dependencies."""

from contextlib import contextmanager
import fcntl
import json
import math
from pathlib import Path
import tempfile
import wave

from protocoller.io import write_json

RATE = 16000


@contextmanager
def capture_lock(path: Path, create: bool = False):
    lock = path / ".capture.lock"
    if not create and not lock.exists():
        yield
        return
    with lock.open("a+b" if create else "rb") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Capture is still in use; stop recording before exporting or recovering") from error
        # Closing releases the lock. A capture child inherits this descriptor,
        # so killing its parent cannot expose a still-running capture to recovery.
        yield stream


def read_manifest(capture: Path) -> dict:
    manifest = json.loads((capture / "capture.json").read_text())
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tracks"), dict):
        raise ValueError("Invalid capture manifest")
    if any(source not in {"system", "microphone"} or not isinstance(track, dict)
           for source, track in manifest["tracks"].items()):
        raise ValueError("Invalid capture tracks")
    return manifest


def local_file(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name:
        raise ValueError("Invalid recording file path")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Invalid or missing recording file: {name}")
    return path


def validate_chunk(capture: Path, chunk: dict, source: str, recover: bool) -> tuple[Path, int]:
    if not isinstance(chunk, dict) or chunk.get("source") != source:
        raise ValueError("Chunk source does not match its track")
    for key in ("sample_rate", "frames", "channels", "sample_width"):
        if type(chunk.get(key)) is not int:
            raise ValueError(f"Invalid chunk {key}")
    if not 1 <= chunk["sample_rate"] <= 192000 or chunk["frames"] < 0:
        raise ValueError("Invalid chunk rate or frame count")
    if chunk["channels"] != 1 or chunk["sample_width"] != 2:
        raise ValueError("Recording chunks must be mono PCM16")
    start = chunk.get("start_seconds")
    if type(start) not in (int, float) or not math.isfinite(start) or start < 0:
        raise ValueError("Invalid chunk start timestamp")
    path = local_file(capture, chunk.get("path"))
    size = path.stat().st_size
    frames = size // 2
    if not recover and (chunk.get("complete") is not True or size != chunk["frames"] * 2):
        raise ValueError("Chunk is incomplete or corrupted; use audio recover to salvage it")
    return path, frames


def write_silence(stream: wave.Wave_write, frames: int) -> None:
    while frames:
        count = min(frames, RATE)
        stream.writeframesraw(bytes(count * 2))
        frames -= count


def assemble_source(capture: Path, source: str, destination: Path,
                    chunks: list[dict] | None = None, recover: bool = False) -> dict:
    from protocoller.audio.normalize import normalize

    try:
        manifest = read_manifest(capture)
    except (OSError, ValueError):
        if not recover or chunks is None:
            raise
        manifest = {"schema_version": 2, "tracks": {}}
    if chunks is None:
        if manifest.get("schema_version") != 2 or manifest.get("status") != "completed":
            raise ValueError("Capture is incomplete or failed; use audio recover first")
        track = manifest["tracks"].get(source, {})
        chunks = track.get("chunks", [])
    if not isinstance(chunks, list) or not chunks:
        raise ValueError(f"No recoverable chunks for {source}")
    checked = [(chunk, *validate_chunk(capture, chunk, source, recover)) for chunk in chunks]
    checked.sort(key=lambda item: item[0]["start_seconds"])
    if len({path for _, path, _ in checked}) != len(checked):
        raise ValueError("Duplicate recording chunks")
    if destination.exists():
        raise FileExistsError(f"Audio output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    warnings = []
    discarded_bytes = 0
    missing_frames = 0
    missing_seconds = 0.0
    written = 0
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".assemble-") as directory:
        work = Path(directory)
        staged = work / "track.wav"
        with wave.open(str(staged), "wb") as output:
            output.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            for index, (chunk, raw, frames) in enumerate(checked):
                if recover:
                    discarded_bytes += raw.stat().st_size % 2
                    missing = max(0, chunk["frames"] - frames)
                    missing_frames += missing
                    missing_seconds += missing / chunk["sample_rate"]
                    if not chunk.get("complete") or frames != chunk["frames"]:
                        warnings.append(f"{raw.name}: recovered {frames} frames from an unfinalized or changed chunk")
                if not frames:
                    warnings.append(f"{raw.name}: empty chunk omitted")
                    continue
                wrapped = work / f"raw-{index}.wav"
                with wave.open(str(wrapped), "wb") as wrapper, raw.open("rb") as input_stream:
                    wrapper.setparams((1, 2, chunk["sample_rate"], 0, "NONE", "not compressed"))
                    remaining = frames * 2
                    while remaining:
                        data = input_stream.read(min(remaining, 1024 * 1024))
                        if not data:
                            raise ValueError("Recording chunk changed while exporting")
                        wrapper.writeframesraw(data)
                        remaining -= len(data)
                normalized = work / f"normalized-{index}.wav"
                normalize(wrapped, normalized)
                target = round(chunk["start_seconds"] * RATE)
                expected = round(frames * RATE / chunk["sample_rate"])
                if target < written - 2:
                    raise ValueError("Recording chunks overlap; refusing to overwrite recorded speech")
                if target > written:
                    write_silence(output, target - written)
                    written = target
                skip = max(0, written - target)
                with wave.open(str(normalized), "rb") as input_stream:
                    if abs(input_stream.getnframes() - expected) > 2:
                        raise ValueError("Decoded chunk duration does not match its recorded timestamps")
                    input_stream.setpos(min(skip, input_stream.getnframes()))
                    remaining = max(0, expected - skip)
                    while remaining:
                        data = input_stream.readframes(min(remaining, RATE))
                        if not data:
                            write_silence(output, remaining)  # At most two resampling-rounding frames.
                            written += remaining
                            break
                        output.writeframesraw(data)
                        count = len(data) // 2
                        written += count
                        remaining -= count
                wrapped.unlink()
                normalized.unlink()
        if written == 0:
            raise ValueError(f"No recoverable audio for {source}")
        with staged.open("rb") as stream:
            import os
            os.fsync(stream.fileno())
        staged.rename(destination)
    return {"frames": written, "duration_seconds": written / RATE,
            "discarded_partial_bytes": discarded_bytes, "known_missing_frames": missing_frames,
            "known_missing_seconds": missing_seconds, "warnings": warnings}


def export_capture(capture: Path, destination: Path, recover: bool = False,
                   locked: bool = False) -> dict:
    if not locked:
        with capture_lock(capture):
            return export_capture(capture, destination, recover, locked=True)
    warnings = []
    try:
        manifest = read_manifest(capture)
    except (OSError, ValueError):
        if not recover:
            raise
        manifest = {"schema_version": 2, "tracks": {}}
        warnings.append("Capture manifest missing or invalid; reconstructed from chunk sidecars")
    if manifest.get("schema_version") != 2:
        raise ValueError("Chunk export/recovery requires a milestone 2 recording")
    if not recover and manifest.get("status") != "completed":
        raise ValueError("Capture is incomplete or failed; use audio recover first")
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Output directory already exists: {destination}")
    entries: dict[str, list[dict]] = {}
    if recover:
        for sidecar in sorted((capture / "chunks").glob("*.pcm.json")):
            try:
                chunk = json.loads(sidecar.read_text())
                source = chunk.get("source")
                if source not in {"system", "microphone"}:
                    raise ValueError("Unknown source")
                validate_chunk(capture, chunk, source, True)
                entries.setdefault(source, []).append(chunk)
            except (ValueError, OSError, AttributeError, TypeError) as error:
                warnings.append(f"{sidecar.name}: omitted ({error})")
        known = {chunk["path"] for chunks in entries.values() for chunk in chunks}
        for raw in sorted((capture / "chunks").glob("*.pcm")):
            if raw.relative_to(capture).as_posix() not in known:
                warnings.append(f"{raw.name}: audio omitted because valid timing metadata is missing")
    else:
        entries = {source: track.get("chunks", []) for source, track in manifest["tracks"].items()}
    if not entries or not any(entries.values()):
        raise ValueError("No recoverable recording chunks found")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Publish the complete export together; partial results never look completed.
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".capture-export-") as directory:
        work = Path(directory) / "export"
        work.mkdir()
        tracks = {}
        reports = {}
        for source, chunks in entries.items():
            if source not in {"system", "microphone"}:
                raise ValueError("Invalid recording source")
            if not chunks:
                warnings.append(f"{source}: no audio recovered")
                continue
            if recover and not any(local_file(capture, chunk["path"]).stat().st_size >= 2 for chunk in chunks):
                warnings.append(f"{source}: no complete PCM frames recovered")
                continue
            reports[source] = assemble_source(capture, source, work / f"{source}.wav", chunks, recover)
            tracks[source] = {"path": f"{source}.wav", "offset_seconds": 0, "has_audio": True}
        if not tracks:
            raise ValueError("No recoverable recording sources")
        for source in manifest["tracks"]:
            if source not in tracks:
                warnings.append(f"{source}: requested source has no recoverable audio")
        report = {"recovered": recover, "tracks": reports, "warnings": warnings,
                  "unflushed_tail": "Any samples not present in PCM files cannot be recovered; their duration is unknown" if recover else None}
        write_json(work / "capture.json", {"schema_version": 1, "status": "completed",
                                           "recovered": recover, "tracks": tracks})
        write_json(work / "recovery.json" if recover else work / "export.json", report)
        work.rename(destination)
    return report
