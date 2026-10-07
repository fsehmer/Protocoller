"""Validated transcript contracts, readable exports, and immutable review snapshots."""

from copy import deepcopy
import html
import json
import math
from pathlib import Path
import uuid

from protocoller.io import write_json, write_text
from protocoller.schemas import Turn, Word, reconcile
from protocoller.storage.jobs import job_lock


def build_transcript(speech: dict, diarization: dict, duration: float) -> dict:
    words = sorted((Word(**value) for value in speech["words"]), key=lambda w: (w.start, w.end))
    original = [Turn(**value) for value in diarization["turns"]]
    if not words:
        original = []
    # Canonical IDs follow first audible turn, with a deterministic tie-break.
    voices = sorted({turn.speaker for turn in original},
                    key=lambda voice: (min(t.start for t in original if t.speaker == voice), voice))
    mapping = {voice: f"speaker_{i + 1:03d}" for i, voice in enumerate(voices)}
    turns = [Turn(t.start, t.end, mapping[t.speaker]) for t in original]
    if any(item.end > duration + .05 for item in [*words, *turns]):
        raise ValueError("Model timestamps extend beyond the recording")
    segments = []
    for word, assigned in zip(words, reconcile(words, turns)):
        candidates = list(assigned.candidate_speakers)
        reasons = []
        if assigned.speaker is None:
            reasons.append("no_speaker_turn")
        if assigned.overlap:
            reasons.append("simultaneous_speakers")
        elif len(candidates) > 1:
            reasons.append("speaker_boundary")
        elif assigned.ambiguous and candidates:
            reasons.append("low_turn_coverage")
        details = {"start": word.start, "end": word.end, "text": word.text}
        # Group adjacent words only when review flags/voice agree; retain word timing.
        if (segments and segments[-1]["speaker"] == assigned.speaker
                and segments[-1]["review_reasons"] == reasons
                and segments[-1]["candidate_speakers"] == candidates
                and 0 <= word.start - segments[-1]["end"] <= .8
                and word.end - segments[-1]["start"] <= 12
                and not segments[-1]["text"].endswith((".", "!", "?"))):
            segment = segments[-1]
            separator = "" if word.text[:1] in ",.;:!?" else " "
            segment["text"] += separator + word.text.strip()
            segment["end"] = word.end
            segment["words"].append(details)
        else:
            segments.append({**assigned.to_dict(), "id": f"s{len(segments) + 1:06d}",
                             "words": [details], "candidate_speakers": candidates,
                             "review_reasons": reasons, "review_status": "unreviewed",
                             "word_timing_status": "aligned"})
    value = {"schema_version": 2, "revision": 0, "revision_id": uuid.uuid4().hex,
             "parent_revision_id": None, "language": speech["language"],
             "duration_seconds": duration, "segments": segments,
             "speaker_names": {voice: f"Speaker {i + 1}" for i, voice in enumerate(mapping.values())},
             "speaker_model_labels": {canonical: original for original, canonical in mapping.items()},
             "status": "no_speech" if not words else "needs_review"}
    validate_transcript(value)
    return value


def validate_transcript(value: dict) -> None:
    if not isinstance(value, dict) or value.get("schema_version") not in (1, 2):
        raise ValueError("Unsupported transcript schema")
    if not isinstance(value.get("language"), str) or not value["language"].strip():
        raise ValueError("Transcript language cannot be empty")
    duration = value.get("duration_seconds")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Transcript duration must be finite and positive")
    names = value.get("speaker_names")
    if not isinstance(names, dict) or any(not isinstance(k, str) or not k or
            not isinstance(v, str) or not v.strip() for k, v in names.items()):
        raise ValueError("Invalid speaker names")
    segments = value.get("segments")
    if not isinstance(segments, list):
        raise ValueError("Transcript segments must be a list")
    seen, last = set(), -1.0
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("Invalid transcript segment")
        identity = segment.get("id")
        if not isinstance(identity, str) or not identity.startswith("s") or not identity[1:].isdigit() or identity in seen:
            raise ValueError("Invalid or duplicate segment ID")
        seen.add(identity)
        try:
            word = Word(segment["start"], segment["end"], segment["text"])
        except (KeyError, TypeError) as error:
            raise ValueError("Invalid transcript interval/text") from error
        if word.start < last or word.end > duration + .05:
            raise ValueError("Transcript timestamps must be ordered within the recording")
        last = word.start
        if segment.get("speaker") is not None and segment["speaker"] not in names:
            raise ValueError("Unknown speaker ID")
        if any(type(segment.get(flag)) is not bool for flag in ("ambiguous", "overlap")):
            raise ValueError("Transcript review flags must be booleans")
    if value.get("schema_version") == 2:
        if type(value.get("revision")) is not int or value["revision"] < 0:
            raise ValueError("Invalid transcript revision")
        if not isinstance(value.get("revision_id"), str) or len(value["revision_id"]) != 32 or any(
                c not in "0123456789abcdef" for c in value["revision_id"]):
            raise ValueError("Invalid transcript revision ID")


def load_transcript(directory: Path) -> dict:
    value = json.loads((directory / "transcript.json").read_text())
    validate_transcript(value)
    if value["schema_version"] == 2:
        snapshot = directory / "revisions" / f"{value['revision_id']}.json"
        if not snapshot.is_file() or json.loads(snapshot.read_text()) != value:
            raise ValueError("Transcript differs from its immutable revision; apply corrections with transcript apply")
    return value


def timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def render_transcript(value: dict) -> str:
    validate_transcript(value)
    lines = ["# Transcript", "", f"Language: {value['language']} · Revision: {value.get('revision', 0)}", "",
             "Speaker labels are meeting-local. Bracketed flags require review.", ""]
    if not value["segments"]:
        lines.extend(["No speech detected. No words or speakers have been invented.", ""])
    for segment in value["segments"]:
        name = value["speaker_names"].get(segment["speaker"], "Unknown")
        flags = []
        if segment["ambiguous"]:
            flags.append("review speaker")
        if segment["overlap"]:
            flags.append("overlap: simultaneous words may be incomplete")
        suffix = " [" + "; ".join(flags) + "]" if flags else ""
        # Escape user/model text so exports cannot inject HTML or Markdown formatting.
        def escaped(text: str) -> str:
            text = html.escape(text).replace("\n", " ")
            for char in "\\`*_[]":
                text = text.replace(char, "\\" + char)
            return text
        lines.extend([f'<a id="{segment["id"]}"></a>',
                      f"[{timestamp(segment['start'])}–{timestamp(segment['end'])}] "
                      f"**{escaped(name)}**{suffix}: {escaped(segment['text'])}", ""])
    return "\n".join(lines)


def publish_transcript(directory: Path, value: dict) -> None:
    """Caller owns the job lock. JSON is authoritative; Markdown is a derived view."""
    validate_transcript(value)
    if value.get("schema_version") == 2:
        snapshot = directory / "revisions" / f"{value['revision_id']}.json"
        if snapshot.exists():
            if json.loads(snapshot.read_text()) != value:
                raise ValueError("An immutable transcript revision cannot be overwritten")
        else:
            write_json(snapshot, value)
    write_json(directory / "transcript.json", value)
    write_text(directory / "transcript.md", render_transcript(value))


def apply_review(directory: Path, changes: dict) -> dict:
    """Apply one atomic patch with optimistic concurrency; raw stage files stay untouched."""
    if not isinstance(changes, dict) or set(changes) - {"expected_revision", "speaker_names", "segments"}:
        raise ValueError("Review patch accepts expected_revision, speaker_names, and segments only")
    with job_lock(directory):
        current = load_transcript(directory)
        if current.get("schema_version") != 2:
            raise ValueError("Review requires a milestone 3 transcript (schema version 2)")
        if type(changes.get("expected_revision")) is not int or changes["expected_revision"] != current["revision"]:
            raise ValueError("Transcript revision changed; reload before editing")
        updated = deepcopy(current)
        names = changes.get("speaker_names", {})
        if not isinstance(names, dict):
            raise ValueError("speaker_names must map IDs to display names")
        for speaker, name in names.items():
            if speaker not in updated["speaker_names"] or not isinstance(name, str) or not name.strip():
                raise ValueError("Rename requires an existing speaker and a nonempty display name")
            updated["speaker_names"][speaker] = name.strip()
        edits = changes.get("segments", [])
        if not isinstance(edits, list):
            raise ValueError("segments must be a list of corrections")
        by_id = {s["id"]: s for s in updated["segments"]}
        seen = set()
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) - {"id", "text", "speaker"} or "id" not in edit:
                raise ValueError("Segment corrections accept id, text, and speaker only")
            identity = edit["id"]
            if not isinstance(identity, str) or identity not in by_id or identity in seen:
                raise ValueError("Unknown or duplicate corrected segment ID")
            seen.add(identity)
            segment = by_id[identity]
            if "text" in edit:
                if not isinstance(edit["text"], str) or not edit["text"].strip():
                    raise ValueError("Corrected text cannot be empty")
                segment["text"] = edit["text"].strip()
                segment["word_timing_status"] = "unaligned_after_correction"
            if "speaker" in edit:
                voice = edit["speaker"]
                if voice is not None and (not isinstance(voice, str) or voice not in updated["speaker_names"]):
                    raise ValueError("Speaker assignment must be an existing ID or null")
                segment["speaker"] = voice
                segment["ambiguous"] = voice is None
                segment["review_reasons"] = ["user_marked_unknown"] if voice is None else []
            segment["review_status"] = "corrected"
        if updated == current:
            return current
        updated.update(revision=current["revision"] + 1, revision_id=uuid.uuid4().hex,
                       parent_revision_id=current["revision_id"])
        publish_transcript(directory, updated)
        return updated


def export_transcript(directory: Path, output: Path, format: str) -> None:
    if format not in {"json", "markdown"}:
        raise ValueError("Export format must be json or markdown")
    with job_lock(directory):
        value = load_transcript(directory)
        if output.resolve() in {p.resolve() for p in directory.rglob("*") if p.is_file()}:
            raise ValueError("Export to a new file outside the job artifacts")
        if output.exists():
            raise FileExistsError(f"Export already exists: {output}")
        if format == "json":
            write_json(output, value)
        else:
            write_text(output, render_transcript(value))
