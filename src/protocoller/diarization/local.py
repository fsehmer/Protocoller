from array import array
from pathlib import Path
import sys
import wave

from protocoller.config import Config
from protocoller.schemas import Turn


def diarize(audio: Path, config: Config, speakers: int | dict | None = None) -> list[Turn]:
    try:
        import torch
        from pyannote.audio import Pipeline
    except ImportError as error:
        raise ValueError("Install speech dependencies: uv sync --extra speech") from error
    torch.set_num_threads(config.threads)
    # Loading from an explicit directory is Community-1's documented offline path.
    pipeline = Pipeline.from_pretrained(str(config.diarization_model))
    with wave.open(str(audio), "rb") as stream:
        samples = array("h", stream.readframes(stream.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    waveform = torch.tensor(samples, dtype=torch.float32).unsqueeze(0) / 32768.0
    kwargs = speakers if isinstance(speakers, dict) else ({"num_speakers": speakers} if speakers else {})
    progress = {}

    def hook(step_name, step_artifact, file=None, total=None, completed=None):
        if total and completed is not None:
            percent = int(100 * completed / total)
            bucket = percent // 10
            if progress.get(step_name) == bucket:
                return
            progress[step_name] = bucket
            message = f"{step_name}: {percent}%"
        else:
            message = f"{step_name}: complete"
        print(f"Diarization: {message}", file=sys.stderr, flush=True)

    output = pipeline({"waveform": waveform, "sample_rate": 16000}, hook=hook, **kwargs)
    annotation = output.speaker_diarization
    return [Turn(turn.start, turn.end, str(speaker)) for turn, _, speaker in annotation.itertracks(yield_label=True)]
