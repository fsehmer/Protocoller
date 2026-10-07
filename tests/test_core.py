from array import array
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import wave

from protocoller.audio.capture import record
from protocoller.audio.normalize import mix_tracks, normalize, select_source
from protocoller.cli import main
from protocoller.config import Config, offline_environment
from protocoller.io import write_json
from protocoller.minutes.local import SECTIONS, validate_minutes
from protocoller.models import download_model, import_model, verify_model
from protocoller.pipeline import process
from protocoller.schemas import Turn, Word, reconcile


def make_wav(path: Path, values: list[int]) -> None:
    samples = array("h", values)
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(samples.tobytes())


class TimelineTests(unittest.TestCase):
    def test_boundary_and_overlap_stay_reviewable(self):
        words = [Word(0, 1, "Hallo"), Word(1, 2, "meeting"), Word(2, 3, "yes"), Word(4, 5, "unknown")]
        turns = [Turn(0, 1.5, "A"), Turn(1.5, 3, "B"), Turn(2.2, 2.8, "A")]
        result = reconcile(words, turns)
        self.assertEqual(result[0].speaker, "A")
        self.assertFalse(result[0].ambiguous)
        self.assertTrue(result[1].ambiguous)
        self.assertFalse(result[1].overlap)
        self.assertTrue(result[2].overlap)
        self.assertTrue(result[2].ambiguous)
        self.assertIsNone(result[3].speaker)
        self.assertEqual([item.id for item in result], ["s000001", "s000002", "s000003", "s000004"])

    def test_invalid_intervals_rejected(self):
        for start, end in [(-1, 0), (1, 1), (2, 1), (0, float("inf")), (float("nan"), 1)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                Word(start, end, "test")


class AudioTests(unittest.TestCase):
    def test_decoder_preserves_internal_gap_and_source_offset(self):
        def frame(timestamp, value):
            samples = array("h", [value] * 160)
            return SimpleNamespace(time=timestamp, samples=160,
                                   to_ndarray=lambda: samples)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "track.m4a"
            source.write_bytes(b"decoder test double")
            decoder = MagicMock()
            decoder.__enter__.return_value.decode.return_value = [object(), object()]
            resampler = MagicMock()
            resampler.resample.side_effect = [[frame(0, 1000)], [frame(.02, 2000)], []]
            backend = SimpleNamespace(open=lambda path: decoder,
                                      AudioResampler=lambda **kwargs: resampler)
            with patch.dict(sys.modules, {"av": backend}):
                self.assertAlmostEqual(normalize(source, root / "out.wav", .01), .04)
            with wave.open(str(root / "out.wav")) as stream:
                samples = array("h", stream.readframes(640))
                if sys.byteorder != "little":
                    samples.byteswap()
                self.assertEqual(list(samples), [0] * 160 + [1000] * 160 + [0] * 160 + [2000] * 160)

    def test_padding_and_mixing_preserve_shared_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_wav(root / "one.wav", [1000] * 160)
            make_wav(root / "two.wav", [2000] * 160)
            self.assertAlmostEqual(normalize(root / "two.wav", root / "padded.wav", .01), .02)
            self.assertAlmostEqual(mix_tracks([root / "one.wav", root / "padded.wav"], root / "mix.wav"), .02)
            with wave.open(str(root / "mix.wav")) as stream:
                samples = array("h", stream.readframes(320))
                if sys.byteorder != "little":
                    samples.byteswap()
                self.assertEqual(list(samples[:160]), [500] * 160)
                self.assertEqual(list(samples[160:]), [1000] * 160)

    def test_capture_manifest_rejects_failed_and_traversal_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "capture.json", {"status": "failed"})
            with self.assertRaisesRegex(ValueError, "incomplete"):
                select_source(root, "system")
            write_json(root / "capture.json", {"status": "completed", "tracks": {
                "system": {"has_audio": True, "path": "../outside.wav", "offset_seconds": 0}}})
            with self.assertRaisesRegex(ValueError, "invalid source"):
                select_source(root, "system")

    def test_invalid_recording_duration_does_not_build_or_capture(self):
        with patch("protocoller.audio.capture.build_helper") as build:
            for duration in [0, -1, float("nan"), float("inf")]:
                with self.assertRaises(ValueError):
                    record(Path("unused"), duration, "both", "default")
            build.assert_not_called()

    def test_stalled_capture_terminates_and_kills_helper(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("protocoller.audio.capture.build_helper", return_value=Path("/fake/helper")), \
                patch("protocoller.audio.capture.subprocess.Popen") as spawn:
            helper = spawn.return_value
            helper.wait.side_effect = [subprocess.TimeoutExpired("helper", 31),
                                       subprocess.TimeoutExpired("helper", 3), None]
            with self.assertRaisesRegex(ValueError, "permissions"):
                record(Path(directory) / "new", 1, "system", "default")
            helper.terminate.assert_called_once()
            helper.kill.assert_called_once()


class ModelTests(unittest.TestCase):
    def test_import_cannot_recursively_copy_into_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("model.bin", "config.json", "tokenizer.json"):
                (root / name).write_text("{}")
            with self.assertRaisesRegex(ValueError, "outside the source"):
                import_model(root, root / "nested", "transcription")

    def test_model_import_integrity_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.gguf"
            model.write_bytes(b"GGUF1234")
            installed = import_model(model, root / "installed", "minutes")
            self.assertEqual(verify_model(installed, "minutes", full=True)["kind"], "minutes")
            with self.assertRaisesRegex(ValueError, "already exists"):
                import_model(model, root / "installed", "minutes")
            installed.write_bytes(b"GGUF4321")
            with self.assertRaisesRegex(ValueError, "integrity"):
                verify_model(installed, "minutes", full=True)

    def test_extra_files_in_bundle_fail_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.gguf"
            model.write_bytes(b"GGUF")
            installed = import_model(model, root / "installed", "minutes")
            (installed.parent / "extra").write_text("unexpected")
            with self.assertRaisesRegex(ValueError, "files changed"):
                verify_model(installed, "minutes")

    def test_download_rejects_unpinned_revision_before_network(self):
        with self.assertRaisesRegex(ValueError, "immutable"):
            download_model("example/model", "main", Path("unused"), "transcription")

    def test_missing_tokenizer_fails_before_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model.bin").write_bytes(b"weights")
            (root / "config.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                import_model(root, root / "dest", "transcription")


class MinutesTests(unittest.TestCase):
    def setUp(self):
        self.transcript = {"segments": [{"id": "s000001", "text": "Discuss tomorrow"}]}
        self.minutes = {name: [] for name in SECTIONS}
        self.minutes["action_items"] = [{"text": "Discuss tomorrow", "segment_ids": ["s000001"],
                                         "owner": None, "deadline": None}]

    def test_unspecified_owner_and_deadline_remain_null(self):
        self.assertIsNone(validate_minutes(self.minutes, self.transcript)["action_items"][0]["owner"])

    def test_unknown_or_missing_evidence_rejected(self):
        for refs in [[], ["made-up"], [None]]:
            with self.subTest(refs=refs):
                self.minutes["action_items"][0]["segment_ids"] = refs
                with self.assertRaises(ValueError):
                    validate_minutes(self.minutes, self.transcript)

    def test_unexpected_generated_fields_rejected(self):
        self.minutes["action_items"][0]["injected"] = "execute command"
        with self.assertRaises(ValueError):
            validate_minutes(self.minutes, self.transcript)


class OfflineTests(unittest.TestCase):
    def test_environment_overrides_telemetry_opt_in(self):
        with patch.dict(os.environ, {"PYANNOTE_METRICS_ENABLED": "1", "HF_HUB_OFFLINE": "0"}):
            offline_environment()
            self.assertEqual(os.environ["PYANNOTE_METRICS_ENABLED"], "0")
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")

    def test_worker_guard_blocks_dns_and_socket_access(self):
        code = '''
from protocoller.worker import deny_network
import socket
deny_network()
for call in [lambda: socket.getaddrinfo('example.com', 443),
             lambda: socket.socket().connect(('127.0.0.1', 12345))]:
    try:
        call()
    except PermissionError:
        continue
    raise SystemExit('Network operation was not blocked')
'''
        subprocess.run([sys.executable, "-c", code], check=True)


class ConfigTests(unittest.TestCase):
    def test_relative_paths_resolve_from_configuration_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[models]\ntranscription="asr"\ndiarization="diar"\nminutes="llm.gguf"\n')
            self.assertEqual(Config.load(path).transcription_model, (Path(directory) / "asr").resolve())

    def test_invalid_context_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[models]\ntranscription="asr"\ndiarization="diar"\nminutes="llm.gguf"\n'
                            '[runtime]\ncontext_size=100\nmax_tokens=200\n')
            with self.assertRaisesRegex(ValueError, "smaller"):
                Config.load(path)

    def test_cli_configuration_error_is_actionable(self):
        with redirect_stderr(io.StringIO()) as error:
            result = main(["process", "missing.wav", "--config", "missing.toml", "--output", "unused"])
        self.assertEqual(result, 1)
        self.assertIn("missing.toml", error.getvalue())


class PipelineTests(unittest.TestCase):
    """Exercise orchestration and exports; model outputs are explicit test doubles."""

    def test_pipeline_preserves_raw_output_and_links_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asr = root / "asr"
            asr.mkdir()
            for name in ("model.bin", "config.json", "tokenizer.json"):
                (asr / name).write_text("{}")
            diar = root / "diar"
            diar.mkdir()
            (diar / "config.yaml").write_text("test only")
            (diar / "weights.bin").write_bytes(b"test only")
            llm = root / "model.gguf"
            llm.write_bytes(b"GGUFtest only")
            model_paths = [import_model(asr, root / "asr-installed", "transcription"),
                           import_model(diar, root / "diar-installed", "diarization"),
                           import_model(llm, root / "llm-installed", "minutes")]
            config = root / "config.toml"
            config.write_text("[models]\n" + "\n".join(f'{key}="{path}"' for key, path in
                              zip(("transcription", "diarization", "minutes"), model_paths)))
            make_wav(root / "input.wav", [100] * 16000)

            def fake_stage(stage, config_path, input_path, output_path, speakers):
                metrics = {"seconds": .1, "peak_rss_bytes": 100}
                if stage == "transcribe":
                    return {"words": [{"start": 0, "end": 1, "text": "Hallo meeting"}], "language": "de", "metrics": metrics}
                if stage == "diarize":
                    return {"turns": [{"start": 0, "end": 1, "speaker": "SPEAKER_00"}], "metrics": metrics}
                value = {name: [] for name in SECTIONS}
                value["summary"] = [{"text": "Meeting greeting", "segment_ids": ["s000001"]}]
                return {"minutes": value, "metrics": metrics}

            with patch("protocoller.pipeline.run_stage", side_effect=fake_stage), patch.dict(os.environ):
                report = process(root / "input.wav", root / "out", config)
            transcript = json.loads((root / "out" / "transcript.json").read_text())
            self.assertEqual(transcript["segments"][0]["speaker"], "SPEAKER_00")
            self.assertIn("transcript.md#s000001", (root / "out" / "minutes.md").read_text())
            self.assertTrue((root / "out" / "raw-transcription.json").is_file())
            self.assertTrue((root / "out" / "raw-diarization.json").is_file())
            self.assertTrue((root / "out" / "benchmark.json").is_file())
            self.assertEqual(report["duration_seconds"], 1)
            self.assertFalse(list((root / "out").glob(".processing-*")))
            with self.assertRaises(FileExistsError):
                process(root / "input.wav", root / "out", config)


if __name__ == "__main__":
    unittest.main()
