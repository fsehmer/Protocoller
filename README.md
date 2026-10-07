# Protocoller

Local meeting recording, speaker-labeled transcription, and draft meeting minutes.
German and English are the initial evaluation languages. Milestones 1 and 2's
software are implemented; physical capture and model accuracy/throughput still
need the acceptance checks in [docs/MILESTONE_1.md](docs/MILESTONE_1.md) and
[docs/MILESTONE_2.md](docs/MILESTONE_2.md).

## Install and Diagnose

Requires Python 3.12+. System-audio capture currently requires macOS 15+, an active
desktop session, and Apple Command Line Tools. Imported files can be processed on
macOS/Linux. The base CLI has no runtime dependencies.

```sh
uv sync --extra audio
uv run --no-sync protocoller doctor
uv run --no-sync protocoller build-capture
uv run --no-sync protocoller devices
```

Install model dependencies explicitly; this downloads packages and may compile
the llama.cpp binding. Top-level dependencies are pinned in `pyproject.toml`;
Normal commands use `--no-sync` to avoid dependency resolution or downloads.
A fully resolved lockfile must be produced with `uv lock` when network access is
available. No lockfile was generated in the restricted implementation environment.

```sh
uv sync --extra audio --extra inference --extra setup
```

To run the base CLI and tests directly without installing anything:

```sh
PYTHONPATH=src .venv/bin/python -m protocoller doctor
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

## Set Up Local Models

Use a multilingual CTranslate2 Whisper bundle, the complete Community-1
diarization bundle, and a chat-capable instruction model in GGUF format with an
embedded chat template. Model sizes remain provisional until benchmarked;
start with a multilingual small Whisper model and a quantized small instruction
model for short feasibility clips. Check each model's license and access terms.

Import already-downloaded files without networking:

```sh
uv run --no-sync protocoller models import /path/to/whisper-bundle --kind transcription --destination ~/.protocoller/models/transcription
uv run --no-sync protocoller models import /path/to/community-1 --kind diarization --destination ~/.protocoller/models/diarization
uv run --no-sync protocoller models import /path/to/model.gguf --kind minutes --destination ~/.protocoller/models/minutes
```

Alternatively, explicitly download each model using its immutable repository
commit SHA. The minutes model also requires the exact GGUF filename:

```sh
uv run --no-sync protocoller models download --kind transcription --repo OWNER/REPOSITORY --revision FULL_40_CHARACTER_COMMIT_SHA --destination ~/.protocoller/models/transcription
uv run --no-sync protocoller models download --kind minutes --repo OWNER/REPOSITORY --revision FULL_40_CHARACTER_COMMIT_SHA --filename model.gguf --destination ~/.protocoller/models/minutes
```

Community-1 access requires acceptance of the model's access conditions and a
download credential during online setup. Credentials are not needed for normal
processing and are not written to model manifests. Offline imports must contain
all weights and configuration assets, not just a config that references remote
models. `models import` reports the path to put in your configuration.

Copy `config.example.toml` to `~/.protocoller/config.toml`, update the actual GGUF
filename, and check paths. Relative paths resolve from the configuration file.

```sh
uv run --no-sync protocoller doctor --config ~/.protocoller/config.toml
uv run --no-sync protocoller models verify --kind minutes ~/.protocoller/models/minutes/model.gguf
```

Verification checks SHA-256 hashes; processing checks bundle inventory and sizes
before loading. Imports and downloads refuse existing destinations.

## Record Speakers or Headphones

The helper captures computer playback directly, including remote meeting audio
when headphones are used. It records a separate microphone track in `both` mode.
Only audio is saved; ScreenCaptureKit video frames are discarded.

```sh
uv run --no-sync protocoller record --source both --seconds 30 --output ./recordings/sample
uv run --no-sync protocoller record --source system --seconds 30 --output ./recordings/playback
uv run --no-sync protocoller record --source microphone --seconds 3600 --output ./recordings/in-person
```

macOS may request Screen & System Audio Recording and Microphone access. Grant
the requested permissions in System Settings > Privacy & Security and retry if
needed. Microphone-only mode uses AVFoundation and does not require screen
recording access. Recording prints elapsed time and a dBFS level for each source;
Ctrl-C stops and flushes chunks.
`--microphone DEVICE_ID` selects an input listed by `devices`.
Unattended permission/setup waits are bounded by the requested duration plus
30 seconds; a timeout reports an error and terminates the helper.

New recordings store mono PCM16 chunks and timing sidecars under `chunks/`, plus
a version 2 `capture.json`. Chunk length defaults to two seconds and can be set
with `--chunk-seconds` (0.1–60 seconds). Each completed chunk is flushed to disk;
the active tail is checkpointed when samples arrive, at least once per second.
Normal stop also writes playable 16 kHz WAV files under `audio/`.

Low disk space, write failures, missing samples, backwards timestamps, and a
disconnected selected microphone stop capture explicitly. Existing chunks stay
available. Source format changes start new chunks; system playback follows the
OS output route. Physical device switching and echo still need manual validation.
Use headphones during initial combined-source tests to avoid duplicate playback
through the microphone.

## Import, Export, and Recover Audio

These commands need no speech or language models:

```sh
uv run --no-sync protocoller audio import /path/to/meeting.wav --output ./recordings/imported
uv run --no-sync protocoller audio import /path/to/meeting.m4a --output ./recordings/imported-call
uv run --no-sync protocoller audio export ./recordings/sample --output ./output/playable
uv run --no-sync protocoller audio recover ./recordings/interrupted --output ./recordings/recovered
```

Canonical mono PCM16 WAV import uses Python's standard library. Other formats
are decoded locally through PyAV (`uv sync --extra audio`). Codec availability
depends on the decoder; unsupported
or corrupt files report errors without publishing a partial import. Imported
audio becomes a playable 16 kHz mono WAV; the original file is untouched.

Recovery reads independently persisted chunk sidecars, reconstructs timing and
gaps, and includes complete PCM frames from an unfinished tail. `recovery.json`
reports unfinalized/changed chunks, discarded partial-frame bytes, and missing
metadata. Samples never written to disk cannot be recovered; their duration is
unknown. Source recordings are preserved, and exporting/recovering an active
recording is blocked by a file lock. WAV exports are published in a new directory
only when assembly succeeds. Milestone 1 M4A captures remain readable, but raw
chunk recovery applies only to milestone 2 recordings.

## Process and Review

```sh
uv run --no-sync protocoller process ./recordings/sample --output ./output/sample --speakers 3
uv run --no-sync protocoller process ./recordings/playback --source system --output ./output/playback
uv run --no-sync protocoller process /path/to/meeting.wav --output ./output/imported
```

Output directories must be new. Combined tracks are aligned and mixed with
headroom; original source files stay separate. Each model runs in a separate
worker to release its memory before the next stage. This first implementation
uses CPU inference and short recordings; long-transcript chunking comes later.
`process` defaults to `--source auto`, selecting the sources present in the
recording; `--source both` explicitly requires both tracks. Recovered/imported
directories can be passed directly to `process`.

Outputs include `transcript.json`, timestamped `transcript.md`, raw transcription
and diarization JSON, draft `minutes.json`/`minutes.md`, and `benchmark.json` with
stage timings, peak worker memory, dependency versions, and model manifests.
If minutes generation fails, completed transcript exports remain available.

Speakers have anonymous meeting-local IDs. Review ambiguous/overlapping speech
and every decision/task before using minutes. Evidence links are validated, but
they cannot prove semantic accuracy. Missing task owners/deadlines remain null.

Normal processing loads explicit local paths, forces offline/telemetry settings,
and blocks Python network operations in inference workers. This is not an OS-level
firewall for native libraries; release acceptance still requires testing with
outbound traffic blocked. There is no cloud fallback. Data defaults to
`~/.protocoller`; `PROTOCOLLER_DATA_DIR` overrides that location. Audio and exports
remain in the explicitly selected directories, subject to your backup settings.

## References

- [Apple ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit)
- [faster-whisper: local models, word timestamps, CPU execution](https://github.com/SYSTRAN/faster-whisper)
- [Community-1: offline loading and access conditions](https://huggingface.co/pyannote/speaker-diarization-community-1)
- [llama-cpp-python: installation and structured local chat output](https://github.com/abetlen/llama-cpp-python)
