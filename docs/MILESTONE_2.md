# Milestone 2: Audio Import and Durable Recording

## Implemented

- Microphone-only, system-only, and combined capture with microphone device IDs.
- Elapsed time and independent source levels in dBFS, printed once per second.
- Mono PCM16 chunks at each source's native sample rate, with independent timing
  sidecars and a version 2 capture manifest. The default chunk duration is two seconds.
- Synchronization of completed chunks before metadata publication, plus periodic
  checkpoints of the active tail. No compressed-container finalization is needed
  to recover samples already present in raw files.
- Explicit low-disk-space, write, permission, unavailable-device, backwards-clock,
  microphone-disconnection, and unexpected-session-stop error paths.
- Graceful Ctrl-C/SIGTERM handling, bounded setup waits, and a native parent monitor
  that stops an orphaned helper. The helper inherits the recording lock so an
  interrupted parent cannot expose an active recording to recovery.
- Atomic audio import and WAV export, explicit crash recovery, and processing of
  both version 1 captures and new chunked recordings. Source selection defaults
  to the tracks actually present; explicitly requesting both still requires both.

## Commands

After installing with `uv sync --extra audio`, use the installed CLI directly or
`uv run --no-sync` to avoid dependency downloads during normal operation:

```sh
uv run --no-sync protocoller devices
uv run --no-sync protocoller record --source microphone --seconds 3600 --output recordings/local
uv run --no-sync protocoller record --source both --seconds 3600 --chunk-seconds 2 --output recordings/online
uv run --no-sync protocoller audio import meeting.m4a --output recordings/imported
uv run --no-sync protocoller audio export recordings/online --output output/playable
uv run --no-sync protocoller audio recover recordings/interrupted --output recordings/recovered
```

Normal recording also creates playable `audio/system.wav` and/or
`audio/microphone.wav`. Imports, exports, and recovery all require a new output
directory and preserve the original recordings. They do not load inference models.
Non-canonical WAV and compressed formats need the `audio` extra (PyAV and NumPy).
Microphone-only capture uses AVFoundation without ScreenCaptureKit access.

## Automated Validation

All 47 tests passed on the milestone 1 development Mac. Checks include:

- The production Swift writer receives a full 60 minutes of synthetic PCM for
  two sources. Export preserves every sample, a 250 ms microphone offset, and
  the final samples in both files. This tests storage duration, not 60 minutes
  of real-time microphone/ScreenCaptureKit scheduling.
- A native child is terminated with SIGKILL after ten seconds of completed chunks
  and a half-second unfinished tail. Recovery restores all 10.5 seconds, drops
  and reports one incomplete PCM byte, and does not require a refreshed manifest.
- Actual stereo 48 kHz WAV and encoded AAC/M4A files are imported through PyAV
  into mono PCM16 WAV. Real 44.1/48/32 kHz chunk conversion preserves durations
  and source gaps.
- Planar/interleaved float and interleaved integer capture buffers are downmixed
  without invoking a codec service. Low-space and backwards-timestamp guards
  are exercised in the production native writer.
- Corrupt, overlapping, missing-metadata, and truncated chunks; an empty source;
  source-path validation; inherited lock lifetime; failed launch; normal stop;
  and pipeline source selection have automated coverage.

The native helper compiles with the installed macOS SDK. Python lint and syntax
checks pass. PyAV 16.1.0 and NumPy 2.4.6 were available in the local package cache
and installed for these checks without networking; the audio extra pins those
versions. Model inference remains outside this milestone's validation.

Run the tests with:

```sh
PROTOCOLLER_DATA_DIR=.protocoller PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Native tests require macOS and Swift. The hour-long storage fixture uses temporary
files, needs at least 1 GiB free, and does not record microphone or system audio.

## Remaining Manual Acceptance

On a machine with capture permissions granted, record a real 60-minute meeting
with speakers and then headphones. Verify playback, both sources' final samples,
recording levels, synchronization, and real-time behavior. Exercise Ctrl-C,
denied permissions, unplugging the selected microphone, and output-device changes.

Recovery reports known missing frames and partial-frame bytes in `recovery.json`.
Missing timing sidecars are reported and omitted instead of assigning invented
timestamps. Samples never written to disk cannot be recovered; their duration is
unknown. Check these reports before using a recovered meeting. Power-loss
durability and device switching need physical testing; no such test is claimed.
