"""Build and invoke the native macOS capture helper without a shell."""

import hashlib
import math
from pathlib import Path
import platform
import shutil
import subprocess

from protocoller.config import data_directory


def build_helper() -> Path:
    if platform.system() != "Darwin" or int(platform.mac_ver()[0].split(".")[0]) < 15:
        raise ValueError("System-audio capture currently requires macOS 15+; file processing is portable")
    swift = shutil.which("swiftc")
    if not swift:
        raise ValueError("Install Apple Command Line Tools (xcode-select --install) to build capture")
    source = Path(__file__).parent / "native" / "Capture.swift"
    info = source.with_name("Info.plist")
    digest = hashlib.sha256(source.read_bytes() + info.read_bytes()).hexdigest()[:16]
    build = data_directory() / "bin"
    build.mkdir(parents=True, exist_ok=True)
    executable = build / f"capture-{digest}"
    if not executable.exists():
        cache = build / "module-cache"
        subprocess.run([swift, "-swift-version", "5", "-parse-as-library", "-O",
                        "-module-cache-path", str(cache), str(source), "-o", str(executable),
                        "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist",
                        "-Xlinker", str(info)], check=True)
    return executable


def record(output: Path, seconds: float, source: str, microphone: str) -> int:
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Recording duration must be a finite positive number")
    helper = build_helper()
    # A fresh directory prevents accidental overwrite of earlier recordings.
    output.mkdir(parents=True, exist_ok=False)
    process = subprocess.Popen([str(helper), str(output.resolve()), str(seconds), source, microphone])
    while True:
        try:
            # ScreenCaptureKit can wait indefinitely on an unattended OS prompt.
            return process.wait(timeout=seconds + 30)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise ValueError("Capture did not finish; grant macOS recording permissions and retry")
        except KeyboardInterrupt:
            # The helper receives terminal SIGINT too and finalizes its audio files.
            import signal
            process.send_signal(signal.SIGINT)
