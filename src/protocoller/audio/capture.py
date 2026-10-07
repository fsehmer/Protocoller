"""Build and invoke the native macOS capture helper without a shell."""

import hashlib
import math
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import threading

from protocoller.config import data_directory
from protocoller.io import write_json


def build_helper() -> Path:
    if platform.system() != "Darwin" or int(platform.mac_ver()[0].split(".")[0]) < 15:
        raise ValueError("System-audio capture currently requires macOS 15+; file processing is portable")
    swift = shutil.which("swiftc")
    if not swift:
        raise ValueError("Install Apple Command Line Tools (xcode-select --install) to build capture")
    source = Path(__file__).parent / "native" / "Capture.swift"
    sources = sorted(source.parent.glob("*.swift"))
    info = source.with_name("Info.plist")
    digest = hashlib.sha256(b"".join(path.read_bytes() for path in sources) + info.read_bytes()).hexdigest()[:16]
    build = data_directory() / "bin"
    build.mkdir(parents=True, exist_ok=True)
    executable = build / f"capture-{digest}"
    if not executable.exists():
        cache = build / "module-cache"
        subprocess.run([swift, "-swift-version", "5", "-parse-as-library", "-O",
                        "-module-cache-path", str(cache), *map(str, sources), "-o", str(executable),
                        "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist",
                        "-Xlinker", str(info)], check=True)
    return executable


def _stop_helper(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=3)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        process.kill()
        process.wait()


def _mark_failed(output: Path, reason: str) -> None:
    from protocoller.audio.recording import read_manifest
    try:
        manifest = read_manifest(output)
        if manifest.get("status") != "completed":
            manifest.update(status="failed", error=reason)
            write_json(output / "capture.json", manifest)
    except (OSError, ValueError):
        pass  # Disk-full errors must not hide the original failure; sidecars survive.


def record(output: Path, seconds: float, source: str, microphone: str,
           chunk_seconds: float = 2) -> int:
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Recording duration must be a finite positive number")
    if source not in {"system", "microphone", "both"}:
        raise ValueError("Recording source must be system, microphone, or both")
    if not math.isfinite(chunk_seconds) or not 0.1 <= chunk_seconds <= 60:
        raise ValueError("Chunk duration must be between 0.1 and 60 seconds")
    helper = build_helper()
    # A fresh directory prevents accidental overwrite of earlier recordings.
    output.mkdir(parents=True, exist_ok=False)
    from protocoller.audio.recording import capture_lock, export_capture
    with capture_lock(output, create=True) as lock:
        sources = ["system", "microphone"] if source == "both" else [source]
        write_json(output / "capture.json", {"schema_version": 2, "status": "preparing",
                   "tracks": {name: {"chunks": [], "has_audio": False} for name in sources}})
        try:
            process = subprocess.Popen([str(helper), str(output.resolve()), str(seconds), source, microphone,
                                        str(chunk_seconds)], pass_fds=(lock.fileno(),))
        except OSError as error:
            _mark_failed(output, str(error))
            raise
        prior_term = None
        if threading.current_thread() is threading.main_thread():
            prior_term = signal.getsignal(signal.SIGTERM)

            def interrupted(signum, frame):
                raise KeyboardInterrupt

            signal.signal(signal.SIGTERM, interrupted)
        try:
            try:
                result = process.wait(timeout=seconds + 30)
            except KeyboardInterrupt:
                process.send_signal(signal.SIGINT)
                try:
                    result = process.wait(timeout=5)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    _stop_helper(process)
                    raise ValueError("Capture cancelled; use audio recover to salvage any recorded chunks")
            except subprocess.TimeoutExpired:
                _stop_helper(process)
                raise ValueError("Capture did not finish; grant macOS recording permissions and retry. Use audio recover for saved chunks")
            if result:
                _mark_failed(output, "Capture stopped with an error; inspect saved chunks and use audio recover")
                return result
            export_capture(output, output / "audio", locked=True)
            print(f"Playable WAV files saved to {output / 'audio'}")
            return 0
        except BaseException as error:
            if process.poll() is None:
                _stop_helper(process)
            _mark_failed(output, str(error))
            raise
        finally:
            if prior_term is not None:
                signal.signal(signal.SIGTERM, prior_term)
