from array import array
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import wave

from protocoller.audio.capture import record
from protocoller.audio.importer import import_audio
from protocoller.audio.normalize import normalize, normalize_source
from protocoller.audio.recording import capture_lock, export_capture
from protocoller.config import Config
from protocoller.io import write_json
from protocoller.pipeline import process


def pcm(frames: int, value: int = 1234) -> bytes:
    samples = array("h", [value] * frames)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def add_chunk(root, source="system", index=1, start=0, frames=160, complete=True):
    directory = root / "chunks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{source}-{index:06d}.pcm"
    path.write_bytes(pcm(frames))
    chunk = {"path": path.relative_to(root).as_posix(), "source": source, "start_seconds": start,
             "sample_rate": 16000, "channels": 1, "sample_width": 2, "frames": frames, "complete": complete}
    write_json(Path(str(path) + ".json"), chunk)
    return chunk


def manifest(root, chunks, status="completed"):
    tracks = {}
    for chunk in chunks:
        track = tracks.setdefault(chunk["source"], {"has_audio": True, "chunks": []})
        track["chunks"].append(chunk)
    write_json(root / "capture.json", {"schema_version": 2, "status": status, "tracks": tracks})


def samples(path):
    with wave.open(str(path), "rb") as stream:
        data = array("h", stream.readframes(stream.getnframes()))
        if sys.byteorder != "little":
            data.byteswap()
        return list(data)


class RecordingTests(unittest.TestCase):
    def test_inherited_capture_lock_survives_parent_descriptor_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with capture_lock(root, create=True) as lock:
                child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                         pass_fds=(lock.fileno(),), stdin=subprocess.PIPE)
            try:
                with self.assertRaisesRegex(ValueError, "still in use"):
                    with capture_lock(root):
                        pass
            finally:
                child.communicate(input=b"", timeout=5)
            with capture_lock(root):
                pass

    def test_recovery_reports_known_lost_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            chunk = add_chunk(root)
            manifest(root, [chunk], "failed")
            (root / chunk["path"]).write_bytes(pcm(100))
            report = export_capture(root, Path(directory) / "recovered", recover=True)
            self.assertEqual(report["tracks"]["system"]["known_missing_frames"], 60)
            self.assertEqual(report["tracks"]["system"]["known_missing_seconds"], 60 / 16000)

    def test_launch_failure_marks_capture_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture"
            with patch("protocoller.audio.capture.build_helper", return_value=Path("/fake")), \
                    patch("protocoller.audio.capture.subprocess.Popen", side_effect=OSError("Cannot launch")):
                with self.assertRaises(OSError):
                    record(output, 1, "system", "default")
            from protocoller.audio.recording import read_manifest
            self.assertEqual(read_manifest(output)["status"], "failed")

    def test_export_preserves_source_offset_and_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            one = add_chunk(root, start=.01)
            two = add_chunk(root, index=2, start=.04)
            manifest(root, [one, two])
            export = Path(directory) / "export"
            report = export_capture(root, export)
            self.assertEqual(report["tracks"]["system"]["frames"], 800)
            self.assertEqual(samples(export / "system.wav"), [0] * 160 + [1234] * 160 + [0] * 320 + [1234] * 160)
            self.assertTrue((export / "capture.json").exists())

    def test_interruption_requires_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            chunk = add_chunk(root, complete=False)
            manifest(root, [chunk], "recording")
            with self.assertRaisesRegex(ValueError, "recover"):
                export_capture(root, Path(directory) / "export")
            report = export_capture(root, Path(directory) / "recovered", recover=True)
            self.assertTrue(report["recovered"])
            self.assertIn("unfinalized", report["tracks"]["system"]["warnings"][0])

    def test_recovery_uses_pcm_length_and_drops_only_partial_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            chunk = add_chunk(root, complete=False)
            manifest(root, [chunk], "failed")
            (root / chunk["path"]).write_bytes(pcm(200) + b"x")
            report = export_capture(root, Path(directory) / "recovered", recover=True)
            self.assertEqual(report["tracks"]["system"]["frames"], 200)
            self.assertEqual(report["tracks"]["system"]["discarded_partial_bytes"], 1)
            self.assertIn("unknown", report["unflushed_tail"])

    def test_sidecars_recover_even_without_capture_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            add_chunk(root)
            report = export_capture(root, Path(directory) / "recovered", recover=True)
            self.assertIn("manifest missing", report["warnings"][0])
            self.assertEqual(report["tracks"]["system"]["frames"], 160)

    def test_missing_metadata_audio_is_reported_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            one = add_chunk(root)
            two = add_chunk(root, index=2, start=.01)
            manifest(root, [one, two], "failed")
            Path(str(root / two["path"]) + ".json").write_text("broken json")
            report = export_capture(root, Path(directory) / "recovered", recover=True)
            self.assertEqual(report["tracks"]["system"]["frames"], 160)
            self.assertTrue(any("timing metadata" in warning for warning in report["warnings"]))

    def test_recovery_omits_empty_source_with_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            one = add_chunk(root)
            empty = add_chunk(root, source="microphone", frames=0, complete=False)
            manifest(root, [one, empty], "failed")
            report = export_capture(root, Path(directory) / "recovered", recover=True)
            self.assertEqual(set(report["tracks"]), {"system"})
            self.assertTrue(any("microphone" in warning for warning in report["warnings"]))

    def test_active_capture_cannot_be_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            chunk = add_chunk(root)
            manifest(root, [chunk], "recording")
            with capture_lock(root, create=True), self.assertRaisesRegex(ValueError, "still in use"):
                export_capture(root, Path(directory) / "recovered", recover=True)

    def test_corrupted_completed_chunk_fails_without_partial_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            chunk = add_chunk(root)
            manifest(root, [chunk])
            (root / chunk["path"]).write_bytes(b"short")
            destination = Path(directory) / "export"
            with self.assertRaisesRegex(ValueError, "corrupted"):
                export_capture(root, destination)
            self.assertFalse(destination.exists())

    def test_overlapping_chunks_are_not_silently_merged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            chunks = [add_chunk(root), add_chunk(root, index=2, start=.005)]
            manifest(root, chunks)
            with self.assertRaisesRegex(ValueError, "overlap"):
                export_capture(root, Path(directory) / "export")

    def test_export_cannot_escape_capture_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            chunk = add_chunk(root)
            chunk["path"] = "../../outside.pcm"
            manifest(root, [chunk])
            with self.assertRaisesRegex(ValueError, "Invalid or missing"):
                export_capture(root, Path(directory) / "export")

    def test_chunked_source_can_be_processed_without_permanent_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            chunk = add_chunk(root, source="microphone")
            manifest(root, [chunk])
            destination = Path(directory) / "normalized.wav"
            self.assertEqual(normalize_source(root, "microphone", destination), .01)
            self.assertEqual(samples(destination), [1234] * 160)

    def test_auto_pipeline_selects_only_available_microphone_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chunk = add_chunk(root / "capture", source="microphone")
            manifest(root / "capture", [chunk])
            config = Config(root / "asr", root / "diar", root / "llm")
            metrics = {"seconds": .1, "peak_rss_bytes": 100}
            minutes = {name: [] for name in ("summary", "topics", "decisions", "action_items", "open_questions")}
            responses = [{"words": [], "language": "en", "metrics": metrics},
                         {"minutes": minutes, "metrics": metrics}]
            with patch("protocoller.pipeline.Config.load", return_value=config), \
                    patch.object(Config, "validate_models"), \
                    patch("protocoller.pipeline.verify_model", return_value={}), \
                    patch("protocoller.pipeline.run_stage", side_effect=responses), patch.dict(os.environ):
                report = process(root / "capture", root / "result", root / "config.toml")
            self.assertEqual(report["duration_seconds"], .01)

    def test_recording_normal_stop_exports_playable_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture"

            def spawn(args, **kwargs):
                chunk = add_chunk(output)
                manifest(output, [chunk])
                helper = MagicMock()
                helper.wait.return_value = 0
                helper.poll.return_value = 0
                return helper

            with patch("protocoller.audio.capture.build_helper", return_value=Path("/fake")), \
                    patch("protocoller.audio.capture.subprocess.Popen", side_effect=spawn):
                self.assertEqual(record(output, 1, "system", "default"), 0)
            self.assertEqual(samples(output / "audio" / "system.wav"), [1234] * 160)


class ImportTests(unittest.TestCase):
    def test_truncated_wav_does_not_publish_a_completed_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "truncated.wav"
            with wave.open(str(source), "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                stream.writeframes(pcm(160))
            source.write_bytes(source.read_bytes()[:-20])
            with self.assertRaisesRegex(ValueError, "truncated"):
                import_audio(source, root / "imported")
            self.assertFalse((root / "imported").exists())

    def test_wav_import_is_playable_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "original.wav"
            with wave.open(str(source), "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                stream.writeframes(pcm(160))
            original = source.read_bytes()
            report = import_audio(source, root / "imported")
            self.assertEqual(report["duration_seconds"], .01)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(samples(root / "imported" / "system.wav"), [1234] * 160)
            with self.assertRaises(FileExistsError):
                import_audio(source, root / "imported")

    def test_failed_import_does_not_publish_partial_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "invalid.wav"
            source.write_bytes(b"invalid")
            with patch("protocoller.audio.importer.normalize", side_effect=ValueError("bad decoder")):
                with self.assertRaisesRegex(ValueError, "bad decoder"):
                    import_audio(source, root / "imported")
            self.assertFalse((root / "imported").exists())
            self.assertFalse(list(root.glob(".audio-import-*")))

    def test_normalization_never_overwrites_existing_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            source.write_bytes(b"original")
            destination = root / "existing.wav"
            destination.write_bytes(b"previous")
            with self.assertRaises(FileExistsError):
                normalize(source, destination)
            self.assertEqual(destination.read_bytes(), b"previous")


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("swiftc"), "Native audio tests require macOS and Swift")
class NativeAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix="protocoller-native-tests-")
        cls.root = Path(cls.workspace.name)
        cls.binary = cls.root / "audio-harness"
        repo = Path(__file__).resolve().parents[1]
        subprocess.run(["swiftc", "-swift-version", "5", "-parse-as-library", "-O",
                        "-module-cache-path", str(repo / ".protocoller" / "native-test-cache"),
                        str(repo / "src/protocoller/audio/native/PCMWriter.swift"),
                        str(repo / "src/protocoller/audio/native/CaptureSamples.swift"),
                        str(repo / "tests/native/AudioHarness.swift"), "-o", str(cls.binary)],
                       check=True, timeout=120)
        cls.environment = patch.dict(os.environ, {"PROTOCOLLER_DATA_DIR": str(repo / ".protocoller")})
        cls.environment.start()

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()
        cls.workspace.cleanup()

    def run_fixture(self, mode, root, success=True):
        result = subprocess.run([str(self.binary), mode, str(root)], capture_output=True, text=True, timeout=90)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_real_writer_preserves_full_hour_of_two_sources(self):
        if shutil.disk_usage(self.root).free < 1024 ** 3:
            self.skipTest("Hour-long storage test needs 1 GiB free")
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            self.run_fixture("long", root / "capture")
            report = export_capture(root / "capture", root / "export")
            self.assertEqual(report["tracks"]["system"]["frames"], 3600 * 16000)
            self.assertEqual(report["tracks"]["microphone"]["frames"], 3600 * 16000 + 4000)
            with wave.open(str(root / "export" / "microphone.wav")) as stream:
                self.assertEqual(stream.readframes(4000), bytes(8000))
                self.assertEqual(stream.readframes(1), pcm(1))
                stream.setpos(stream.getnframes() - 1)
                self.assertEqual(stream.readframes(1), pcm(1))

    def test_sigkill_recovers_finalized_chunks_and_raw_tail(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            result = self.run_fixture("crash", root / "capture", success=False)
            self.assertEqual(result.returncode, -9)
            report = export_capture(root / "capture", root / "recovered", recover=True)
            self.assertEqual(report["tracks"]["system"]["frames"], round(10.5 * 16000))
            self.assertEqual(report["tracks"]["system"]["discarded_partial_bytes"], 1)
            with wave.open(str(root / "recovered" / "system.wav")) as stream:
                stream.setpos(stream.getnframes() - 1)
                self.assertEqual(stream.readframes(1), pcm(1, 2345))

    def test_real_rate_changes_and_source_gaps_keep_timing(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            self.run_fixture("gap", root / "capture")
            report = export_capture(root / "capture", root / "export")
            self.assertEqual(report["tracks"]["system"]["frames"], 32000)
            self.assertEqual(report["tracks"]["microphone"]["frames"], 40000)
            data = samples(root / "export" / "microphone.wav")
            self.assertEqual(data[:4000], [0] * 4000)
            self.assertEqual(data[20000:24000], [0] * 4000)

    def test_native_writer_reports_low_disk_and_backwards_clock(self):
        for mode in ("disk-full", "backwards"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=self.root) as directory:
                result = self.run_fixture(mode, Path(directory))
                self.assertIn("guard passed", result.stdout)

    def test_native_capture_sample_formats_convert_without_codec_service(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            result = self.run_fixture("formats", Path(directory))
            self.assertIn("PCM conversion passed", result.stdout)

    def test_real_stereo_wav_and_aac_import(self):
        import av
        import numpy as np

        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            wav = root / "stereo.wav"
            with wave.open(str(wav), "wb") as stream:
                stream.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                stream.writeframes(pcm(48000 * 2))
            m4a = root / "meeting.m4a"
            with av.open(str(m4a), "w") as output:
                stream = output.add_stream("aac", rate=48000)
                stream.layout = "stereo"
                frame = av.AudioFrame.from_ndarray(np.full((1, 96000), 1234, dtype=np.int16),
                                                  format="s16", layout="stereo")
                frame.sample_rate = 48000
                for packet in stream.encode(frame):
                    output.mux(packet)
                for packet in stream.encode(None):
                    output.mux(packet)
            for source in (wav, m4a):
                report = import_audio(source, root / f"import-{source.suffix}")
                self.assertAlmostEqual(report["duration_seconds"], 1, delta=.03)
                with wave.open(str(root / f"import-{source.suffix}" / "system.wav")) as stream:
                    self.assertEqual((stream.getnchannels(), stream.getsampwidth(), stream.getframerate()), (1, 2, 16000))
