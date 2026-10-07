"""Create a local, playable meeting input from an existing audio file."""

from pathlib import Path
import tempfile

from protocoller.audio.normalize import normalize
from protocoller.io import write_json


def import_audio(source: Path, destination: Path) -> dict:
    if not source.is_file():
        raise ValueError(f"Audio file not found: {source}")
    if destination.exists():
        raise FileExistsError(f"Output directory already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".audio-import-") as directory:
        stage = Path(directory) / "import"
        stage.mkdir()
        duration = normalize(source, stage / "system.wav")
        if duration <= 0:
            raise ValueError("Imported audio contains no samples")
        report = {"original_filename": source.name, "duration_seconds": duration,
                  "sample_rate": 16000, "channels": 1, "sample_width": 2}
        write_json(stage / "import.json", report)
        write_json(stage / "capture.json", {"schema_version": 1, "status": "completed", "tracks": {
            "system": {"path": "system.wav", "offset_seconds": 0, "has_audio": True}}})
        stage.rename(destination)
    return report
