# Milestone 1 Validation

## Implemented

- Python package, CLI, configuration, typed transcript contracts, and test discovery.
- macOS 15+ ScreenCaptureKit helper for system audio alone or system plus microphone.
- Separate audio files and shared-clock offsets; capture manifest and explicit failures.
- Model import/download commands, immutable download revisions, and SHA-256 manifests.
- Sequential local transcription, diarization, evidence-linked draft minutes, and exports.
- Hardware diagnostics, offline worker settings, and benchmark artifact generation.

## Environment and Checks

Implementation environment: macOS 26.6.2, Apple Silicon arm64, Python 3.12.11,
18 logical CPUs, and 128 GiB physical RAM reported by diagnostics.

The native helper was compiled with the installed macOS SDK. Automated tests use
synthetic PCM data and explicit inference test doubles; they do not establish
speech-model accuracy or real capture performance.

All 21 unit/orchestration tests passed. A wheel was built using cached Hatchling
1.32.4, installed into the local environment without networking, and its CLI was
smoke-tested. The wheel includes the Swift source and microphone usage metadata.
A one-second system-audio capture attempt timed out waiting for OS access after
15 seconds; playback capture and microphone synchronization are not yet verified.

Dependency installation could not reach PyPI because DNS/network access is blocked.
Model weights are not present. Consequently no actual inference benchmark, final
model-size selection, or numerical word/diarization error rate is available yet.
Do not treat generated test-double metrics as measured model performance.

## Remaining Acceptance Checks

1. Install the optional dependencies, resolve and commit `uv.lock`, and import
   licensed model bundles with recorded immutable revisions.
2. Grant the capture permissions and record a short remote/local conversation
   through speakers, then headphones. Confirm that playback is captured directly,
   both sources are present, and offsets align with the spoken events.
3. Test output-device changes, unavailable microphone IDs, denied permissions,
   silent playback, and encoder/write failures. Check duplicate remote speech
   with speakers. Device-change handling is experimental until these pass.
4. Process both samples while outbound networking is blocked at OS level. Confirm
   no download/telemetry attempts, no runtime credentials, and no cloud dependency.
5. Compare German and English transcripts and speaker turns to manually annotated
   samples. Audit minutes for fabricated facts, owners, deadlines, and commitments.
6. Record actual `benchmark.json` results, peak memory, and real-time factors;
   select models and set initial accuracy/speed targets from those measurements.

Milestone 1's software foundation is implemented; its hardware/model acceptance
gate remains open until these checks pass. Durable chunked capture, transcript
editing, long-context summarization, and the desktop UI belong to later milestones.
