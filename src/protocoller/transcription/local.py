from pathlib import Path

from protocoller.config import Config
from protocoller.schemas import Word


def transcribe(audio: Path, config: Config) -> tuple[list[Word], str]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise ValueError("Install the inference extra to transcribe: uv sync --extra inference") from error
    model = WhisperModel(str(config.transcription_model), device="cpu", compute_type="int8",
                         cpu_threads=config.threads, local_files_only=True)
    segments, info = model.transcribe(str(audio), word_timestamps=True, vad_filter=True,
                                     language=None if config.language == "auto" else config.language)
    words = []
    for segment in segments:
        for word in segment.words or []:
            if word.end > word.start and word.word.strip():
                words.append(Word(word.start, word.end, word.word))
    return words, info.language
