# Milestone 3 — Reviewable Speaker-Labeled Transcripts

## Implementation Status

The CLI and application services are implemented. Real model accuracy and the
multi-speaker audio acceptance check remain pending: this environment has no
installed faster-whisper/pyannote runtime or registered speech model weights.
The automated fixtures replace inference explicitly and do not establish speech
recognition or diarization quality.

## Delivered Behavior

- `transcribe` runs local normalization, transcription, diarization, reconciliation,
  and exports without requiring a minutes model. The `speech` dependency extra
  excludes llama.cpp. `doctor --transcript-only` checks speech bundles alone.
- Anonymous meeting-local IDs follow first speaker appearance; original model
  labels remain in raw output and the transcript's label map. Adjacent words are
  grouped into readable segments without losing word timestamps.
- Automatic speaker counting remains the default. `--speakers` supplies an exact
  count; `--min-speakers`/`--max-speakers` supply validated bounds. These are model
  constraints, not guarantees that the correct number of people will be found.
- Unknown voices, low temporal coverage, speaker boundaries, and simultaneous
  speakers have explicit flags and candidate speaker IDs. Duplicate turn intervals
  are unioned so they cannot inflate assignment coverage. Reconciliation searches
  active intervals rather than scanning all turns for every word.
- No detected words produces an empty transcript and skips diarization; no voice
  labels or speech are invented. Recognized short utterances are retained. Missing
  words during simultaneous speech remain a known model limitation.
- Terminal progress shows stage transitions, completed transcription position,
  and diarization step progress without printing meeting text.
- `transcript show`, `apply`, and `export` share validated application services.
  Atomic patches rename speakers and correct segment text/assignments. Expected
  revisions reject stale edits; IDs and timestamps remain stable. Original word
  timings are retained but marked unaligned after text corrections. Explicit
  assignment resolves speaker uncertainty while retaining overlap information.

## Persistence and Resume

`job.json` records request fingerprints, stage status, and artifact hashes.
`.checkpoints/normalized.wav` and raw stage JSON persist successful results.
`--resume` checks input content, model hashes, source selection, language, threads,
and count options before reusing completed stages. Changed or corrupted artifacts
fail explicitly. Uncommitted normalization outputs are replaced on retry.

Each job has an exclusive file lock inherited by inference workers. Ctrl-C and
SIGTERM stop/reap the current worker and preserve completed stages. Abrupt process
termination preserves committed results; an incomplete stage reruns. A surviving
worker retains the lock until it exits.

Schema version 2 `transcript.json` is the authoritative current revision;
`revisions/<revision-id>.json` contains immutable snapshots. Markdown is derived
and can be rebuilt on resume. Review preserves raw model output. Direct JSON edits
are rejected when they differ from the saved revision. If current JSON is removed,
restore the intended snapshot to `transcript.json` before resuming; the pipeline
refuses to silently replace reviewed text with initial inference.

All checkpoints and revisions contain local meeting data. Delete the whole job
folder to remove them. Existing milestone 1/2 transcript exports can still be read
and exported, but lack the checkpoints/revision metadata required for resume and
patch review. Create a new job to use those features.

The existing `process` command remains compatible and also supports resume.
Failed minutes generation reuses completed speech stages. Drafts record their
transcript revision and remain separate from transcript corrections. Existing
minutes are preserved; changed transcripts require the regeneration workflow
planned for milestone 4.

## Validation

Run:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
ruff check --no-cache src tests
```

The suite includes 75 tests: 47 existing capture/core cases and 28 transcript,
review, adapter, and recovery cases. A real subprocess is killed during
orchestration and a fresh process verifies that completed transcription is reused.
Other checks cover worker cancellation, inherited locks, failed diarization/minutes,
changed input/model content, checkpoint corruption, partial normalization, silence,
short words, overlap, grouping, immutable revisions, stale patches, and CLI exports.
Model adapter tests check local loading, word timestamps, range forwarding, and
progress hooks using explicit test doubles. Native audio tests remain unchanged.
Lint and byte compilation pass. Wheel and source distribution builds used cached
`hatchling==1.32.4`; installed-wheel smoke checks verified the new CLI commands,
review/export workflow, and inclusion of Python services and native capture files.

## Remaining Acceptance Work

1. Install `uv sync --extra speech`, import complete local model bundles, and run
   `doctor --config PATH --transcript-only`.
2. Transcribe consented German and English recordings with two to six speakers,
   silence, short replies, turn boundaries, noise, and overlapping speech. Compare
   automatic counting with exact and ranged hints; audit text, timestamps, and
   assignments against annotations.
3. Test failure/resume and correction/export on those real results. Record WER,
   diarization error, peak RAM, and inference time from an uninterrupted run.
   Resume benchmarks report current invocation elapsed time with cached stage
   metrics; they do not establish inference throughput.
4. Repeat with outbound networking blocked at the OS level. The Python worker
   audit guard does not establish isolation for native libraries.

Backend contracts follow the official
[faster-whisper documentation](https://github.com/SYSTRAN/faster-whisper) and
[Community-1 model card](https://huggingface.co/pyannote/speaker-diarization-community-1).
