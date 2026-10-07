from pathlib import Path
import sys

from protocoller.config import Config
from protocoller.schemas import Word


def transcribe(audio: Path, config: Config) -> tuple[list[Word], str]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise ValueError("Install speech dependencies: uv sync --extra speech") from error
    model = WhisperModel(str(config.transcription_model), device="cpu", compute_type="int8",
                         cpu_threads=config.threads, local_files_only=True)
    segments, info = model.transcribe(str(audio), word_timestamps=True, vad_filter=True,
                                     language=None if config.language == "auto" else config.language)
    words = []
    last_report = -30.0
    for segment in segments:
        for word in segment.words or []:
            if word.end > word.start and word.word.strip():
                words.append(Word(word.start, word.end, word.word))
        if segment.end - last_report >= 30 or segment.end >= info.duration:
            print(f"Transcription: {min(segment.end, info.duration):.1f}/{info.duration:.1f} seconds",
                  file=sys.stderr, flush=True)
            last_report = segment.end
    print("Transcription: complete", file=sys.stderr, flush=True)
    return words, info.language
