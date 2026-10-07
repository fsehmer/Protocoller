"""Decode one source into a canonical mono PCM WAV without moving timestamps."""

from array import array
import json
import math
from pathlib import Path
import sys
import wave


def select_source(path: Path, source: str) -> tuple[Path, float]:
    if not path.is_dir():
        return path, 0.0
    manifest = json.loads((path / "capture.json").read_text())
    if manifest.get("status") != "completed":
        raise ValueError("Capture is incomplete or failed; inspect capture.json before processing")
    track = manifest.get("tracks", {}).get(source)
    if not track or not track.get("has_audio"):
        raise ValueError(f"Capture has no audio for {source}")
    file = (path / track["path"]).resolve()
    if not file.is_relative_to(path.resolve()) or not file.is_file():
        raise ValueError("Capture manifest points to an invalid source file")
    offset = track.get("offset_seconds", 0.0)
    if not isinstance(offset, (int, float)) or not math.isfinite(offset) or offset < 0:
        raise ValueError("Capture offset must be finite and non-negative")
    return file, offset


def normalize(source: Path, destination: Path, offset: float = 0.0) -> float:
    if not math.isfinite(offset) or offset < 0:
        raise ValueError("Audio offset must be finite and non-negative")
    if not source.is_file():
        raise ValueError(f"Audio file not found: {source}")
    if source.resolve() == destination.resolve():
        raise ValueError("Normalization output cannot overwrite the source")
    if source.suffix.lower() == ".wav":
        with wave.open(str(source), "rb") as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) == (16000, 1, 2, "NONE"):
                padding = round(offset * 16000)
                duration = (audio.getnframes() + padding) / 16000
                with wave.open(str(destination), "wb") as output:
                    output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    remaining = padding
                    while remaining:
                        count = min(remaining, 16000)
                        output.writeframesraw(bytes(count * 2))
                        remaining -= count
                    while frames := audio.readframes(16000):
                        output.writeframesraw(frames)
                return duration
    try:
        import av
    except ImportError as error:
        raise ValueError("Audio decoding requires the inference extra: uv sync --extra inference") from error
    with av.open(str(source)) as container, wave.open(str(destination), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        remaining = round(offset * 16000)
        while remaining:
            count = min(remaining, 16000)
            output.writeframesraw(bytes(count * 2))
            remaining -= count
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        written = round(offset * 16000)
        first_time = None

        def write_frame(converted) -> None:
            nonlocal written, first_time
            if converted.time is not None:
                if first_time is None:
                    first_time = converted.time
                target = round(offset * 16000) + round((converted.time - first_time) * 16000)
                if target < written - 2:
                    raise ValueError("Decoded audio timestamps move backwards; refusing to change the timeline")
                remaining_gap = max(0, target - written)
                while remaining_gap:
                    count = min(remaining_gap, 16000)
                    output.writeframesraw(bytes(count * 2))
                    remaining_gap -= count
                    written += count
            pcm = converted.to_ndarray().tobytes()
            if sys.byteorder != "little":
                samples = array("h", pcm)
                samples.byteswap()
                pcm = samples.tobytes()
            output.writeframesraw(pcm)
            written += converted.samples

        for frame in container.decode(audio=0):
            # Preserve intra-track gaps as silence as well as the shared-clock offset.
            for converted in resampler.resample(frame):
                write_frame(converted)
        for converted in resampler.resample(None):
            write_frame(converted)
    with wave.open(str(destination), "rb") as audio:
        return audio.getnframes() / 16000


def mix_tracks(paths: list[Path], destination: Path) -> float:
    """Mix aligned mono sources with fixed headroom; originals remain separate."""
    from contextlib import ExitStack
    with ExitStack() as stack:
        sources = [stack.enter_context(wave.open(str(path), "rb")) for path in paths]
        if not sources:
            raise ValueError("No tracks to mix")
        if any((s.getframerate(), s.getnchannels(), s.getsampwidth()) != (16000, 1, 2) for s in sources):
            raise ValueError("Tracks must be normalized before mixing")
        output = stack.enter_context(wave.open(str(destination), "wb"))
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        frame_count = 0
        while True:
            chunks = [array("h", source.readframes(16000)) for source in sources]
            if sys.byteorder != "little":
                for chunk in chunks:
                    chunk.byteswap()
            count = max(map(len, chunks))
            if not count:
                break
            mixed = array("h", (int(sum(chunk[i] if i < len(chunk) else 0 for chunk in chunks) / len(chunks))
                                for i in range(count)))
            if sys.byteorder != "little":
                mixed.byteswap()
            output.writeframesraw(mixed.tobytes())
            frame_count += count
    return frame_count / 16000
