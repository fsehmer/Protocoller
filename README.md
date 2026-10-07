# Protocoller

Local meeting recording, speaker-labeled transcription, and draft meeting minutes.
German and English are the initial evaluation languages. This is a milestone 1
implementation, not a validated release: real capture permissions and model
accuracy/throughput still need the acceptance checks in
[docs/MILESTONE_1.md](docs/MILESTONE_1.md).

## Install and Diagnose

Requires Python 3.12+. System-audio capture currently requires macOS 15+, an active
desktop session, and Apple Command Line Tools. Imported files can be processed on
macOS/Linux. The base CLI has no runtime dependencies.

```sh
uv sync
uv run protocoller doctor
uv run protocoller build-capture
uv run protocoller devices
```

Install model dependencies explicitly; this downloads packages and may compile
the llama.cpp binding. Top-level dependencies are pinned in `pyproject.toml`;
a fully resolved lockfile must be produced with `uv lock` when network access is
available. No lockfile was generated in the restricted implementation environment.

```sh
uv sync --extra inference --extra setup
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
uv run protocoller models import /path/to/whisper-bundle --kind transcription --destination ~/.protocoller/models/transcription
uv run protocoller models import /path/to/community-1 --kind diarization --destination ~/.protocoller/models/diarization
uv run protocoller models import /path/to/model.gguf --kind minutes --destination ~/.protocoller/models/minutes
```

Alternatively, explicitly download each model using its immutable repository
commit SHA. The minutes model also requires the exact GGUF filename:

```sh
uv run protocoller models download --kind transcription --repo OWNER/REPOSITORY --revision FULL_40_CHARACTER_COMMIT_SHA --destination ~/.protocoller/models/transcription
uv run protocoller models download --kind minutes --repo OWNER/REPOSITORY --revision FULL_40_CHARACTER_COMMIT_SHA --filename model.gguf --destination ~/.protocoller/models/minutes
```

Community-1 access requires acceptance of the model's access conditions and a
download credential during online setup. Credentials are not needed for normal
processing and are not written to model manifests. Offline imports must contain
all weights and configuration assets, not just a config that references remote
models. `models import` reports the path to put in your configuration.

Copy `config.example.toml` to `~/.protocoller/config.toml`, update the actual GGUF
filename, and check paths. Relative paths resolve from the configuration file.

```sh
uv run protocoller doctor --config ~/.protocoller/config.toml
uv run protocoller models verify --kind minutes ~/.protocoller/models/minutes/model.gguf
```

Verification checks SHA-256 hashes; processing checks bundle inventory and sizes
before loading. Imports and downloads refuse existing destinations.

## Record Speakers or Headphones

The helper captures computer playback directly, including remote meeting audio
when headphones are used. It records a separate microphone track in `both` mode.
Only audio is saved; ScreenCaptureKit video frames are discarded.

```sh
uv run protocoller record --source both --seconds 30 --output ./recordings/sample
uv run protocoller record --source system --seconds 30 --output ./recordings/playback
```

macOS may request Screen & System Audio Recording and Microphone access. Grant
the requested permissions in System Settings > Privacy & Security and retry if
needed. Recording prints an explicit indicator; Ctrl-C stops and finalizes files.
`--microphone DEVICE_ID` selects an input listed by `devices`.
Unattended permission/setup waits are bounded by the requested duration plus
30 seconds; a timeout reports an error and terminates the helper.

Each new capture directory contains `system.m4a`, optional `microphone.m4a`, and
`capture.json` with source offsets on a common timeline. Missing source audio or
writer failures produce a failed manifest rather than silently claiming success.
Device changes, echo, and long-recording recovery require further validation.
Use headphones during initial combined-source tests to avoid duplicate remote
speech through the microphone. Interrupted M4A files are not yet crash-recoverable;
chunked durable recording is milestone 2 work.

## Process and Review

```sh
uv run protocoller process ./recordings/sample --output ./output/sample --speakers 3
uv run protocoller process ./recordings/playback --source system --output ./output/playback
uv run protocoller process /path/to/meeting.wav --output ./output/imported
```

Output directories must be new. Combined tracks are aligned and mixed with
headroom; original source files stay separate. Each model runs in a separate
worker to release its memory before the next stage. This first implementation
uses CPU inference and short recordings; long-transcript chunking comes later.

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
