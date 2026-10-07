# Protocoller

Local meeting recording, speaker-labeled transcription, and draft meeting minutes.
German and English are the initial evaluation languages. Milestones 1–3's
software are implemented; physical capture and model accuracy/throughput still
need the acceptance checks in [docs/MILESTONE_1.md](docs/MILESTONE_1.md),
[docs/MILESTONE_2.md](docs/MILESTONE_2.md), and
[docs/MILESTONE_3.md](docs/MILESTONE_3.md).

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

Verification and processing check SHA-256 hashes, bundle inventory, and sizes
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

## Transcribe, Resume, and Review

For transcripts alone, install `uv sync --extra speech`. This avoids the minutes
runtime. Your config needs only `[models].transcription` and `.diarization`;
`minutes` can be omitted. `[runtime].language` accepts `auto`, `de`, or `en`.

```sh
uv run --no-sync protocoller doctor --config ~/.protocoller/config.toml --transcript-only
uv run --no-sync protocoller transcribe ./recordings/sample --output ./output/sample --min-speakers 2 --max-speakers 4
uv run --no-sync protocoller transcribe ./recordings/sample --output ./output/sample --min-speakers 2 --max-speakers 4 --resume
uv run --no-sync protocoller transcript show ./output/sample
```

Omit speaker options for automatic counting. `--speakers 3` sets an exact count;
it cannot be combined with minimum/maximum options. Keep input, source, model
bundles, language, threads, and speaker options unchanged when resuming. Changed
or corrupted checkpoints are rejected. Outputs must initially be new directories;
`--resume` explicitly reopens a milestone 3 job. Earlier outputs remain readable
but have no resumable job manifest.

Combined tracks are aligned and mixed with headroom; original source files stay
separate. `--source auto` selects recorded sources; `--source both` requires both.
Each model runs in a separate offline CPU worker. Progress prints stage names,
transcribed audio position, and diarization steps. Ctrl-C/SIGTERM cancels the
worker and retains completed stages. SIGKILL retains committed checkpoints;
the interrupted stage reruns. A running worker holds the job lock.

Outputs include `transcript.json`, timestamped `transcript.md`, raw model JSON,
`revisions/`, `job.json`, `benchmark.json`, and `.checkpoints/normalized.wav`.
Checkpoints intentionally retain audio for recovery; delete the entire output
folder when removing a meeting. Benchmark elapsed time covers the current
invocation after validation; stage metrics can come from previous invocations.

Speaker IDs (`speaker_001`) follow first appearance within this meeting. Adjacent
words with matching speaker/review flags form readable segments; original word
times remain available. Silence/no detected words yields an empty transcript and
skips diarization. Short recognized utterances are retained. Unknown speakers,
speaker boundaries, weak coverage, and simultaneous voices carry review flags;
overlapping words may be incomplete.

Apply corrections using a JSON patch, for example `corrections.json`:

```json
{
  "expected_revision": 0,
  "speaker_names": {"speaker_001": "Felix"},
  "segments": [{"id": "s000001", "text": "Hallo zusammen.", "speaker": "speaker_001"}]
}
```

```sh
uv run --no-sync protocoller transcript apply ./output/sample corrections.json
uv run --no-sync protocoller transcript export ./output/sample --format markdown --output ./output/reviewed.md
uv run --no-sync protocoller transcript export ./output/sample --format json --output ./output/reviewed.json
```

Read the current revision and IDs with `transcript show` before editing. Patches
apply atomically and reject stale revisions, unknown IDs, or empty text. Use
`"speaker": null` for an unknown voice. Corrections retain stable segment IDs,
original word timing, and immutable prior revisions. Changed text marks word
alignment as stale. Explicit speaker assignment clears uncertainty while keeping
the original overlap flag. Edit through these services; direct changes to
`transcript.json` fail revision validation. Markdown is a derived export.

## Produce Draft Minutes

The existing full pipeline still needs all three model bundles and the
`inference` dependencies:

```sh
uv run --no-sync protocoller process ./recordings/sample --output ./output/with-minutes --speakers 3
uv run --no-sync protocoller process ./recordings/sample --output ./output/with-minutes --speakers 3 --resume
```

A failed minutes stage can resume without repeating transcription or diarization.
Draft `minutes.json`/`minutes.md` link to transcript segment IDs and record the
transcript revision used. Reviewed transcripts survive resume. Existing minutes
are preserved; regeneration after transcript edits and long-transcript chunking
are milestone 4 work. Review all decisions/tasks: valid evidence IDs do not prove
semantic accuracy. Unspecified task owners/deadlines remain null.

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
