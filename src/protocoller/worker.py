"""Isolated inference stages; normal processing has no Python socket access."""

import json
from pathlib import Path
import resource
import sys
import time

from protocoller.config import Config, offline_environment
from protocoller.io import write_json


def deny_network() -> None:
    def audit(event: str, args: tuple) -> None:
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.sendto", "socket.bind"}:
            raise PermissionError("Protocoller inference is offline; network access is disabled")
    sys.addaudithook(audit)


def main() -> int:
    offline_environment()
    deny_network()
    stage, config_path, input_path, output_path, speakers = sys.argv[1:]
    config = Config.load(Path(config_path))
    start = time.perf_counter()
    if stage == "transcribe":
        from dataclasses import asdict
        from protocoller.transcription.local import transcribe
        words, language = transcribe(Path(input_path), config)
        value = {"words": [asdict(word) for word in words], "language": language}
    elif stage == "diarize":
        from dataclasses import asdict
        from protocoller.diarization.local import diarize
        turns = diarize(Path(input_path), config, int(speakers) if speakers else None)
        value = {"turns": [asdict(turn) for turn in turns]}
    elif stage == "minutes":
        from protocoller.minutes.local import generate
        transcript = json.loads(Path(input_path).read_text())
        value = {"minutes": generate(transcript, config)}
    else:
        raise ValueError(f"Unknown worker stage: {stage}")
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    value["metrics"] = {"seconds": time.perf_counter() - start,
                        "peak_rss_bytes": usage if sys.platform == "darwin" else usage * 1024}
    write_json(Path(output_path), value)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, ImportError, RuntimeError) as error:
        print(f"Processing error: {error}", file=sys.stderr)
        raise SystemExit(1)
