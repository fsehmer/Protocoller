# Protocoller Implementation Plan

## Goal and Current State

Build a tool that records meetings, transcribes speech with speaker separation, and generates editable meeting minutes. Audio, transcripts, speaker processing, and text generation must stay on the user's computer.

The repository currently contains Python project metadata (`Python >=3.12`) and a contributor guide, but no application implementation or dependencies. Everything below is proposed work.

## Scope and Working Assumptions

- Start with macOS as the development platform; keep the processing core portable. Confirm the deployment operating system, processor, and available RAM before choosing model sizes.
- Initially support microphone recording and existing audio files. Capture of remote participants through system audio is a separate platform-specific milestone.
- Evaluate German and English as initial languages; confirm the actual meeting languages during the first milestone.
- The first release records during the meeting and performs final processing afterward. Live captions are a later enhancement.
- Speakers initially receive meeting-specific labels such as `Speaker 1`. Users assign participant names after reviewing the audio. Automatic recognition across meetings is outside the initial scope.
- Dependencies and model weights may be downloaded during explicit setup, or imported from an offline bundle. Normal operation must work without internet access and without runtime credentials.

## User Workflow

1. Create a meeting, enter its title and optional agenda, and select an audio device or file.
2. Start recording with a visible recording indicator, input level, elapsed time, and stop control.
3. Stop recording and run transcription and speaker diarization locally.
4. Review timestamped speech, rename speakers, and correct text or speaker assignments.
5. Generate draft minutes from the reviewed transcript.
6. Review decisions and tasks against their transcript references, edit the draft, and export it.

## Architecture

```text
Microphone / audio file
          |
Durable local recording + meeting metadata
          |
Audio normalization (shared timeline)
          |
Transcription + speaker diarization
          |
Timestamp reconciliation + transcript review
          |
Local language model + evidence validation
          |
Editable minutes + Markdown / JSON exports
```

Keep recording independent of inference so slow models cannot interrupt capture. Run expensive processing in a worker process with progress reporting, cancellation, and checkpoints. Load models sequentially where possible to limit peak memory.

### Proposed Components

| Component | Initial approach | Selection condition |
| --- | --- | --- |
| Audio capture | `sounddevice`/PortAudio adapter and durable PCM files | Verify device permissions, packaging, and long-recording reliability on the target OS |
| Transcription | `faster-whisper` behind a backend interface | Benchmark multilingual models on the actual hardware; use CPU first where accelerator support is unavailable |
| Speaker diarization | Local `pyannote.audio` Community-1 pipeline | Verify Python compatibility, complete offline model loading, resource usage, and model access terms |
| Minutes generation | Quantized instruction model through a local `llama.cpp` runtime | Choose model and quantization by RAM, language quality, context needs, and model license |
| Persistence | SQLite metadata plus per-meeting files | Support atomic updates, schema versions, and deletion of all meeting artifacts |
| Interaction | CLI first; desktop interface after pipeline validation | Reuse the same application services across both interfaces |

`faster-whisper` documents CPU quantization, word timestamps, and voice activity filtering. These make it a candidate rather than a proven hardware choice. See the [official repository](https://github.com/SYSTRAN/faster-whisper).

Community-1 documents loading a downloaded pipeline from disk and provides regular and exclusive diarization outputs. Use the local model, subject to setup access conditions; retain overlap information for review. See the [official model card](https://huggingface.co/pyannote/speaker-diarization-community-1).

`llama.cpp` supports local inference and Apple Silicon acceleration. Its suitability for this application's minutes must be measured with a selected model. See the [official repository](https://github.com/ggml-org/llama.cpp).

### Proposed Repository Layout

```text
src/protocoller/
  cli.py
  config.py
  schemas.py
  audio/          # capture, import, normalization
  transcription/  # backend adapters, word timestamps
  diarization/    # speaker turns and reconciliation
  minutes/        # prompts, chunking, validation, rendering
  storage/        # meetings, revisions, checkpoints
  ui/             # desktop interface, introduced later
tests/
  fixtures/       # small, consented or synthetic recordings
  integration/
docs/
```

Store recordings and model weights in configurable application data directories outside the repository. Ignore local artifacts in version control when Git is initialized.

## Data Contracts and Processing Rules

- **Meeting:** ID, title, date/time zone, language, agenda, recording sources, processing status, and model/configuration versions.
- **Transcript:** stable segment IDs, start/end times in the original recording, text, optional word timing, speaker ID, and ambiguity/overlap flags. Preserve raw model output alongside reviewed revisions.
- **Speakers:** meeting-local IDs and user-provided display names. Diarization separates voices; it does not establish a person's identity.
- **Minutes:** overview, topics, decisions, action items, and open questions. Each substantive item carries supporting transcript segment IDs. Missing owners and deadlines remain unspecified.

Normalize audio to the selected models' requirements while preserving the source and original timeline. Avoid concatenating speech-only spans without a timestamp mapping. Assign words to speaker turns by temporal overlap; flag uncertain boundaries instead of guessing. Do not claim to recover all words spoken simultaneously.

For long transcripts, extract structured facts from overlapping chunks, reconcile duplicate items, then assemble minutes. Validate output schemas and evidence references. Treat transcript content as input data, including any spoken instructions. Evidence links help review but do not prove that a generated statement is accurate. Regeneration must use an explicit transcript revision and preserve user-edited minutes.

## Implementation Milestones

### 1. Establish Feasibility and Project Foundation

- Confirm hardware, languages, typical duration, participant count, and microphone versus online-meeting requirements.
- Add package/build configuration, CLI entry point, typed schemas, configuration loading, and test discovery.
- Create explicit model installation/import and diagnostic commands; pin compatible dependencies and model revisions.
- Benchmark representative short recordings for accuracy, processing time, memory, and offline execution.

**Exit:** one sample runs through transcription, diarization, and draft minutes with networking disabled; document measured hardware limits and select the initial models.

### 2. Implement Audio Import and Recording

- Import WAV first, then add other formats through a decoder adapter.
- List/select devices, handle microphone permission errors, and show input levels.
- Stream recording to recoverable chunks with a manifest; handle disk-full, device loss, stop, and interruption.

**Exit:** a 60-minute recording remains playable and complete after normal stop; interruption preserves already-flushed audio and reports any loss.

### 3. Produce Reviewable Speaker-Labeled Transcripts

- Implement model adapters, transcription, diarization, timestamp reconciliation, and progress reporting.
- Support automatic speaker counts plus an optional expected count/range.
- Export timestamped Markdown and structured JSON; support speaker renaming and transcript corrections through application services.
- Persist stage results so a failed job resumes without redoing successful stages.

**Exit:** a multi-speaker recording yields stable meeting-local speaker labels and editable transcript data; silence, short utterances, and overlap have explicit behavior.

### 4. Generate Grounded Meeting Minutes

- Implement local generation, versioned prompts, structured extraction, long-transcript chunking, and validation.
- Provide a default template for summary, discussion, decisions, tasks, and open questions.
- Link items to transcript segments and support regeneration after corrections.
- Export Markdown minutes and JSON; keep generated drafts separate from reviewed versions.

**Exit:** manually audited fixtures produce supported decisions and tasks without inventing names, deadlines, or commitments; malformed or unsupported results are flagged for review.

### 5. Add a Desktop Review Workflow

- Add meeting creation, recording controls, processing progress, and recoverable error messages.
- Provide synchronized audio playback, transcript correction, speaker renaming, minutes review, export, and deletion.
- Select a desktop toolkit after checking distribution and licensing requirements; keep inference off the UI thread.

**Exit:** a user completes the full workflow without using a terminal after setup, including recovery from a failed processing stage.

### 6. Package and Validate the First Release

- Document installation, offline model import, hardware profiles, data locations, and troubleshooting.
- Package the supported platform, verify microphone permissions, and test installation on a clean machine.
- Test long meetings and complete offline workflows; publish benchmark results and known limits.

**Exit:** recording, review, and export work with networking blocked, no automatic downloads, and no external service dependency.

### Later: Live Captions and Online Meetings

Add incremental transcription with provisional speaker labels only after batch quality and hardware throughput are established. Reconcile provisional output during final processing. Separately implement system-audio capture per OS, preserving separate microphone and remote tracks where available and handling echo and synchronization.

## Validation Strategy

Use standard-library `unittest` for schemas, timestamp reconciliation, chunk deduplication, evidence validation, revisions, and recovery behavior. Keep model-dependent integration tests separate from fast tests. Once tests exist, run `python -m unittest discover -s tests -v`.

Build a consented evaluation set covering German/English, two to six speakers, silence, accents, noise, interruptions, overlap, and a long meeting. Annotate sample transcripts and speaker turns. Measure word error rate, diarization error rate with documented overlap/collar settings, peak RAM, and processing time divided by audio duration.

Set numerical accuracy and speed release thresholds after milestone 1 establishes a baseline on target hardware. Independently require valid exports, preserved timestamps, reviewable ambiguous speech, no invented commitments in the audited minutes fixtures, and successful operation with outbound networking blocked. Unit coverage alone cannot establish model quality.

## Local Data and Operational Requirements

- Require an explicit start action and visible recording state; expose retention and deletion controls.
- Load model assets from explicit local paths. Disable telemetry, update checks, and hidden downloads in all dependencies; verify actual network behavior during acceptance testing.
- If a local model server is used, bind it to loopback and manage its lifecycle with the application.
- Keep meeting content out of diagnostic logs by default. Keep download credentials out of configuration exports and source control.
- Explain that local files remain subject to the user's backup or synchronization settings. Delete temporary audio, revisions, and derived artifacts when deleting a meeting.
- Fail with actionable messages when models, memory, permissions, devices, or disk space are unavailable; never fall back to a cloud service.

## First Implementation Slice

Start with a CLI that accepts a local WAV file and produces `transcript.json`, `transcript.md`, and `minutes.md` in a chosen output directory. Validate this complete offline path before adding microphone capture and the desktop interface. Command names, dependencies, model sizes, and hardware requirements become final only after the feasibility milestone.
