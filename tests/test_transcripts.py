"""Transcript review and real filesystem checkpoints with deterministic model doubles."""

from copy import deepcopy
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from protocoller.cli import main
from protocoller.config import Config
from protocoller.io import write_json
from protocoller.minutes.local import SECTIONS
from protocoller.models import import_model
from protocoller.pipeline import process, run_stage, speaker_options
from protocoller.schemas import Turn, Word, reconcile
from protocoller.storage.jobs import job_lock
from protocoller.transcription.review import (
    apply_review, build_transcript, export_transcript, load_transcript,
    publish_transcript, render_transcript,
)
from test_core import make_wav

METRICS = {"seconds": .1, "peak_rss_bytes": 100}
SPEECH = {"words": [{"start": 0, "end": .2, "text": " Hallo"},
                    {"start": .2, "end": .4, "text": " zusammen."},
                    {"start": .6, "end": .8, "text": " Yes."}], "language": "de", "metrics": METRICS}
VOICES = {"turns": [{"start": .6, "end": 1, "speaker": "Z"},
                    {"start": 0, "end": .5, "speaker": "Q"}], "metrics": METRICS}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        publish_transcript(self.root, build_transcript(SPEECH, VOICES, 1))

    def test_chronological_speakers_group_words_and_preserve_timestamps(self):
        transcript = load_transcript(self.root)
        self.assertEqual(transcript["speaker_model_labels"], {"speaker_001": "Q", "speaker_002": "Z"})
        self.assertEqual([s["text"] for s in transcript["segments"]], ["Hallo zusammen.", "Yes."])
        self.assertEqual(transcript["segments"][0]["words"], SPEECH["words"][:2])
        self.assertIn("00:00:00.600", render_transcript(transcript))

    def test_silence_has_no_invented_words_or_speakers(self):
        value = build_transcript({"words": [], "language": "en"}, VOICES, 1)
        self.assertEqual(value["status"], "no_speech")
        self.assertEqual(value["segments"], [])
        self.assertEqual(value["speaker_names"], {})
        self.assertIn("No speech detected", render_transcript(value))

    def test_overlap_boundary_unknown_and_short_utterance_flags(self):
        speech = {"words": [{"start": 0, "end": .01, "text": "Ja"},
                            {"start": .2, "end": .4, "text": "one"},
                            {"start": .5, "end": .6, "text": "two"},
                            {"start": .8, "end": .9, "text": "unknown"}], "language": "en"}
        voices = {"turns": [{"start": 0, "end": .3, "speaker": "A"},
                            {"start": .3, "end": .6, "speaker": "B"},
                            {"start": .52, "end": .58, "speaker": "A"}]}
        segments = build_transcript(speech, voices, 1)["segments"]
        self.assertFalse(segments[0]["ambiguous"])
        self.assertEqual(segments[1]["review_reasons"], ["speaker_boundary"])
        self.assertFalse(segments[1]["overlap"])
        self.assertTrue(segments[2]["overlap"])
        self.assertEqual(len(segments[2]["candidate_speakers"]), 2)
        self.assertIsNone(segments[3]["speaker"])
        self.assertEqual(segments[3]["review_reasons"], ["no_speaker_turn"])

    def test_duplicate_turns_do_not_bias_speaker_coverage(self):
        segment = reconcile([Word(0, 1, "hello")], [Turn(0, .3, "A"), Turn(0, .3, "A"),
                                                  Turn(.3, 1, "B")])[0]
        self.assertEqual(segment.speaker, "B")

    def test_atomic_corrections_keep_original_revision_and_word_timing(self):
        original = load_transcript(self.root)
        corrected = apply_review(self.root, {"expected_revision": 0,
            "speaker_names": {"speaker_001": "Felix"},
            "segments": [{"id": "s000001", "text": "Hallo alle.", "speaker": "speaker_002"}]})
        self.assertEqual(corrected["revision"], 1)
        self.assertEqual(corrected["parent_revision_id"], original["revision_id"])
        self.assertEqual(corrected["segments"][0]["id"], "s000001")
        self.assertEqual(corrected["segments"][0]["words"], original["segments"][0]["words"])
        self.assertEqual(corrected["segments"][0]["word_timing_status"], "unaligned_after_correction")
        self.assertEqual(json.loads((self.root / "revisions" / f"{original['revision_id']}.json").read_text()), original)
        self.assertEqual(corrected["speaker_names"]["speaker_001"], "Felix")
        self.assertIn("Hallo alle.", (self.root / "transcript.md").read_text())

    def test_stale_invalid_patch_leaves_current_revision_unchanged(self):
        original = load_transcript(self.root)
        for changes in [{"expected_revision": 2}, {"expected_revision": 0, "speaker_names": {"missing": "Name"}},
                        {"expected_revision": 0, "segments": [{"id": "s000001", "text": "valid"},
                                                               {"id": "missing", "text": "invalid"}]},
                        {"expected_revision": 0, "segments": [{"id": "s000001", "speaker": "missing"}]},
                        {"expected_revision": 0, "segments": [{"id": "s000001", "start": .1}]}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                apply_review(self.root, changes)
            self.assertEqual(load_transcript(self.root), original)

    def test_user_assignment_resolves_ambiguity_without_hiding_overlap(self):
        current = load_transcript(self.root)
        current["segments"][0].update(ambiguous=True, overlap=True)
        # Use a fresh job because existing revisions are immutable.
        other = self.root / "other"
        publish_transcript(other, current)
        result = apply_review(other, {"expected_revision": 0,
                                      "segments": [{"id": "s000001", "speaker": "speaker_001"}]})
        self.assertFalse(result["segments"][0]["ambiguous"])
        self.assertTrue(result["segments"][0]["overlap"])

    def test_exports_escape_text_and_do_not_overwrite(self):
        apply_review(self.root, {"expected_revision": 0, "speaker_names": {"speaker_001": "<script>*X*"}})
        output = self.root / "export.md"
        export_transcript(self.root, output, "markdown")
        text = output.read_text()
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn('id="s000001"', text)
        with self.assertRaises(ValueError):
            export_transcript(self.root, output, "markdown")
        export_transcript(self.root, self.root / "copy.json", "json")
        self.assertEqual(json.loads((self.root / "copy.json").read_text()), load_transcript(self.root))

    def test_concurrent_job_and_manual_edit_are_rejected(self):
        with job_lock(self.root), self.assertRaisesRegex(ValueError, "already"):
            apply_review(self.root, {"expected_revision": 0, "speaker_names": {"speaker_001": "Name"}})
        current = load_transcript(self.root)
        current["speaker_names"]["speaker_001"] = "Manual edit"
        write_json(self.root / "transcript.json", current)
        with self.assertRaisesRegex(ValueError, "immutable"):
            load_transcript(self.root)

    def test_cli_patch_and_export_need_no_models(self):
        write_json(self.root / "patch.json", {"expected_revision": 0, "speaker_names": {"speaker_001": "Felix"}})
        self.assertEqual(main(["transcript", "apply", str(self.root), str(self.root / "patch.json")]), 0)
        self.assertEqual(main(["transcript", "export", str(self.root), "--output", str(self.root / "cli.md")]), 0)
        self.assertIn("Felix", (self.root / "cli.md").read_text())


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for kind in ("transcription", "diarization"):
            source = self.root / (kind + "-original")
            source.mkdir()
            for name in (("model.bin", "config.json", "tokenizer.json") if kind == "transcription"
                         else ("config.yaml", "weights.bin")):
                (source / name).write_bytes(b"test-only")
            import_model(source, self.root / kind, kind)
        self.config = self.root / "config.toml"
        self.config.write_text('[models]\ntranscription="transcription"\ndiarization="diarization"\n')
        self.audio = self.root / "meeting.wav"
        make_wav(self.audio, [100] * 16000)
        self.output = self.root / "job"
        self.calls = []
        self.fail = None
        self.silence = False

    def adapter(self, stage, config_path, input_path, output_path, speakers):
        self.calls.append(stage)
        if stage == self.fail:
            raise RuntimeError("simulated stage failure")
        if stage == "transcribe":
            return {**deepcopy(SPEECH), "words": []} if self.silence else deepcopy(SPEECH)
        if stage == "diarize":
            return deepcopy(VOICES)
        return {"minutes": {name: [] for name in SECTIONS}, "metrics": METRICS}

    def process(self, **kwargs):
        with patch("protocoller.pipeline.run_stage", side_effect=self.adapter), redirect_stderr(io.StringIO()):
            return process(self.audio, self.output, self.config, transcript_only=True, **kwargs)

    def test_failed_diarization_resumes_without_retranscribing(self):
        self.fail = "diarize"
        with self.assertRaises(RuntimeError):
            self.process()
        self.assertEqual(json.loads((self.output / "job.json").read_text())["status"], "failed")
        self.assertTrue((self.output / "raw-transcription.json").is_file())
        self.fail = None
        self.calls.clear()
        report = self.process(resume=True)
        self.assertEqual(self.calls, ["diarize"])
        self.assertTrue(report["resumed"])
        self.assertTrue((self.output / "transcript.md").is_file())

    def test_resume_preserves_review_and_repairs_derived_markdown(self):
        self.process()
        reviewed = apply_review(self.output, {"expected_revision": 0, "speaker_names": {"speaker_001": "Felix"}})
        (self.output / "transcript.md").unlink()
        self.calls.clear()
        self.process(resume=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(load_transcript(self.output), reviewed)
        self.assertIn("Felix", (self.output / "transcript.md").read_text())

    def test_changed_input_options_and_same_size_model_are_rejected(self):
        self.process()
        with self.assertRaisesRegex(ValueError, "changed"):
            self.process(resume=True, speakers=2)
        make_wav(self.audio, [200] * 16000)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.process(resume=True)
        make_wav(self.audio, [100] * 16000)
        (self.root / "transcription" / "model.bin").write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.process(resume=True)

    def test_corrupt_checkpoint_is_not_silently_reused(self):
        self.process()
        (self.output / "raw-transcription.json").write_text("{}")
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "checkpoint"):
            self.process(resume=True)
        self.assertEqual(self.calls, [])

    def test_deleted_reviewed_transcript_is_not_replaced_with_raw_text(self):
        self.process()
        apply_review(self.output, {"expected_revision": 0, "segments": [{"id": "s000001", "text": "Correction"}]})
        (self.output / "transcript.json").unlink()
        with self.assertRaisesRegex(ValueError, "restore"):
            self.process(resume=True)

    def test_silence_skips_diarization_without_inventing_turns(self):
        self.silence = True
        self.process()
        self.assertEqual(self.calls, ["transcribe"])
        self.assertEqual(load_transcript(self.output)["status"], "no_speech")
        self.assertEqual(json.loads((self.output / "raw-diarization.json").read_text())["turns"], [])
        self.process(resume=True)
        self.assertEqual(self.calls, ["transcribe"])

    def test_no_minutes_model_needed_and_speaker_range_reaches_worker(self):
        self.assertIsNone(Config.load(self.config, require_minutes=False).minutes_model)
        from protocoller.diagnostics import diagnose
        self.assertTrue(diagnose(self.config, transcript_only=True)["models_ready"])
        self.assertFalse(diagnose(self.config)["models_ready"])
        with patch("protocoller.pipeline.run_stage", side_effect=self.adapter) as stage:
            process(self.audio, self.output, self.config, transcript_only=True, min_speakers=2, max_speakers=4)
        self.assertEqual(stage.call_args.args[-1], {"min_speakers": 2, "max_speakers": 4})
        self.assertFalse((self.output / "minutes.json").exists())

    def test_cancelled_job_retains_successful_stage(self):
        def cancelled(*args):
            if args[0] == "diarize":
                raise KeyboardInterrupt
            return self.adapter(*args)
        with patch("protocoller.pipeline.run_stage", side_effect=cancelled), self.assertRaises(KeyboardInterrupt):
            process(self.audio, self.output, self.config, transcript_only=True)
        self.assertEqual(json.loads((self.output / "job.json").read_text())["status"], "cancelled")
        self.calls.clear()
        self.process(resume=True)
        self.assertEqual(self.calls, ["diarize"])

    def test_invalid_stage_output_is_not_committed_as_a_checkpoint(self):
        with patch("protocoller.pipeline.run_stage", return_value={"words": [{"start": 1, "end": 0, "text": "bad"}],
                     "language": "en", "metrics": METRICS}), self.assertRaises(ValueError):
            process(self.audio, self.output, self.config, transcript_only=True)
        self.assertNotIn("transcribe", json.loads((self.output / "job.json").read_text())["stages"])

    def test_uncommitted_normalized_wav_is_replaced_on_resume(self):
        self.fail = "transcribe"
        with self.assertRaises(RuntimeError):
            self.process()
        job = json.loads((self.output / "job.json").read_text())
        del job["stages"]["normalize"]
        write_json(self.output / "job.json", job)
        make_wav(self.output / ".checkpoints" / "system.wav", [200] * 16000)
        self.fail = None
        self.process(resume=True)
        self.assertFalse((self.output / ".checkpoints" / "system.wav").exists())
        self.assertTrue((self.output / "transcript.json").exists())

    def test_failed_minutes_resume_reuses_both_models(self):
        model = self.root / "original.gguf"
        model.write_bytes(b"GGUFtest-only")
        path = import_model(model, self.root / "minutes", "minutes")
        with self.config.open("a") as stream:
            stream.write(f'minutes="{path}"\n')
        self.fail = "minutes"
        with patch("protocoller.pipeline.run_stage", side_effect=self.adapter), self.assertRaises(RuntimeError):
            process(self.audio, self.output, self.config)
        self.assertTrue((self.output / "transcript.json").is_file())
        self.fail = None
        self.calls.clear()
        with patch("protocoller.pipeline.run_stage", side_effect=self.adapter):
            process(self.audio, self.output, self.config, resume=True)
        self.assertEqual(self.calls, ["minutes"])
        self.calls.clear()
        with patch("protocoller.pipeline.run_stage", side_effect=self.adapter):
            process(self.audio, self.output, self.config, resume=True)
        self.assertEqual(self.calls, [])

    def test_sigkill_retains_completed_transcription_for_a_fresh_process(self):
        # The subprocess runs real orchestration/storage; only model inference is replaced.
        code = '''
import json, sys, time
from pathlib import Path
import protocoller.pipeline as pipeline
root = Path(sys.argv[1])
pause = sys.argv[2] == "pause"
def stage(name, config, source, output, speakers):
    if name == "diarize" and pause:
        (root / "ready").write_text("ready")
        time.sleep(30)
    return json.loads((root / (name + "-fixture.json")).read_text())
pipeline.run_stage = stage
pipeline.process(root / "meeting.wav", root / "job", root / "config.toml",
                 transcript_only=True, resume=not pause)
'''
        write_json(self.root / "transcribe-fixture.json", SPEECH)
        write_json(self.root / "diarize-fixture.json", VOICES)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        child = subprocess.Popen([sys.executable, "-c", code, str(self.root), "pause"], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / "ready").exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.root / "ready").exists())
            child.kill()
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        speech_digest = (self.output / "raw-transcription.json").read_bytes()
        result = subprocess.run([sys.executable, "-c", code, str(self.root), "resume"], env=env,
                                capture_output=True, text=True, timeout=5, check=True)
        self.assertIn("reuse completed transcribe", result.stderr)
        self.assertEqual((self.output / "raw-transcription.json").read_bytes(), speech_digest)
        self.assertEqual(load_transcript(self.output)["segments"][0]["speaker"], "speaker_001")


class AdapterTests(unittest.TestCase):
    def test_speaker_option_validation(self):
        self.assertIsNone(speaker_options(None, None, None))
        self.assertEqual(speaker_options(3, None, None), 3)
        for args in [(0, None, None), (True, None, None), (2, 1, None), (None, 4, 2), (None, -1, None)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                speaker_options(*args)

    def test_whisper_adapter_is_local_and_reports_word_progress(self):
        from protocoller.transcription.local import transcribe
        backend = MagicMock()
        backend.WhisperModel.return_value.transcribe.return_value = (
            [SimpleNamespace(end=1, words=[SimpleNamespace(start=0, end=1, word="Hallo")])],
            SimpleNamespace(language="de", duration=1))
        with patch.dict("sys.modules", {"faster_whisper": backend}), redirect_stderr(io.StringIO()) as progress:
            words, language = transcribe(Path("audio.wav"), Config(Path("local/asr"), Path("diar"), None))
        self.assertTrue(backend.WhisperModel.call_args.kwargs["local_files_only"])
        self.assertTrue(backend.WhisperModel.return_value.transcribe.call_args.kwargs["word_timestamps"])
        self.assertEqual(language, "de")
        self.assertEqual(words[0].text, "Hallo")
        self.assertIn("1.0/1.0", progress.getvalue())

    def test_diarization_adapter_passes_ranges_and_retains_regular_overlap(self):
        from protocoller.diarization.local import diarize
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.wav"
            make_wav(audio, [100] * 16000)
            backend, torch = MagicMock(), MagicMock()
            pipeline = backend.Pipeline.from_pretrained.return_value
            annotation = pipeline.return_value.speaker_diarization
            annotation.itertracks.return_value = [(SimpleNamespace(start=0, end=1), "a", "A"),
                                                  (SimpleNamespace(start=.2, end=.8), "b", "B")]
            with patch.dict("sys.modules", {"torch": torch, "pyannote.audio": backend}):
                turns = diarize(audio, Config(Path("asr"), Path("local/diar"), None), {"min_speakers": 2})
            self.assertEqual(len(turns), 2)
            self.assertEqual(pipeline.call_args.kwargs["min_speakers"], 2)
            self.assertIn("hook", pipeline.call_args.kwargs)
            backend.Pipeline.from_pretrained.assert_called_once_with("local/diar")
            with redirect_stderr(io.StringIO()) as progress:
                hook = pipeline.call_args.kwargs["hook"]
                hook("segmentation", None, total=10, completed=5)
                hook("segmentation", None, total=10, completed=5)
            self.assertEqual(progress.getvalue().count("50%"), 1)

    def test_cancelled_worker_is_terminated_and_reaped(self):
        child = MagicMock()
        child.wait.side_effect = [KeyboardInterrupt, subprocess.TimeoutExpired("worker", 5), 0]
        child.poll.return_value = None
        with patch("protocoller.pipeline.subprocess.Popen", return_value=child), self.assertRaises(KeyboardInterrupt):
            run_stage("diarize", Path("config"), Path("audio"), Path("output"), None)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 3)

    def test_worker_inherits_job_lock_during_inference(self):
        with tempfile.TemporaryDirectory() as directory, job_lock(Path(directory)), \
                patch("protocoller.pipeline.subprocess.Popen") as spawn:
            spawn.return_value.wait.return_value = 1
            with self.assertRaises(subprocess.CalledProcessError):
                run_stage("diarize", Path("config"), Path("audio"), Path("output"), None)
            self.assertEqual(len(spawn.call_args.kwargs["pass_fds"]), 1)

    def test_interval_sweep_matches_unsorted_words_and_nested_turns(self):
        words = [Word(2, 2.1, "later"), Word(0, 3, "long"), Word(.2, .3, "early")]
        turns = [Turn(.1, .4, "A"), Turn(2, 2.2, "B"), Turn(0, .1, "C")]
        segments = reconcile(words, turns)
        self.assertEqual([s.candidate_speakers for s in segments], [("B",), ("A", "B", "C"), ("A",)])
        self.assertEqual([s.id for s in segments], ["s000001", "s000002", "s000003"])


if __name__ == "__main__":
    unittest.main()
