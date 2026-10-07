"""Exclusive mutation and content-based checkpoint verification."""

from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import hashlib
import json
from pathlib import Path
import signal
import threading

from protocoller.models import digest

active_lock_fd: ContextVar[int | None] = ContextVar("active_job_lock_fd", default=None)


@contextmanager
def cancellation_signals():
    """Treat service SIGTERM like interactive cancellation and restore the caller's handler."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def stop(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def input_fingerprint(path: Path) -> str:
    if path.is_file():
        return digest(path)
    # Generated playable exports are irrelevant: capture manifests/chunks are authoritative.
    files = [path / "capture.json"]
    manifest = json.loads(files[0].read_text())
    if manifest.get("schema_version") == 2:
        files.extend(sorted((path / "chunks").rglob("*")))
    else:
        for track in manifest.get("tracks", {}).values():
            if track.get("has_audio"):
                candidate = (path / track["path"]).resolve()
                if not candidate.is_relative_to(path.resolve()):
                    raise ValueError("Capture contains an invalid source path")
                files.append(candidate)
    return fingerprint({p.relative_to(path).as_posix(): digest(p) for p in files if p.is_file()})


@contextmanager
def job_lock(output: Path):
    with (output / ".job.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("This job is already processing or being edited") from error
        token = active_lock_fd.set(lock.fileno())
        try:
            yield
        finally:
            active_lock_fd.reset(token)
            fcntl.flock(lock, fcntl.LOCK_UN)
