"""Language timing regression: no full-sample transcript for dead decisions.

The inference thread used to run per-window language detection and then always
run a full-sample Whisper transcription.  For window evidence that is already
inconclusive (ambiguous/mixed/insufficient), the transcript cannot influence
the decision, so that extra pass only delayed the result and widened the
stale-session window.  These fixtures prove the pass is skipped without
weakening any threshold or conclusive path.
"""
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import language_audio
import spanish_audio


def analysis():
    window = {"speechSeconds": 6.0, "speechRatio": 0.43, "usable": True}
    windows = []
    for index in range(4):
        row = dict(window)
        row.update({"index": index, "start": index * 14.0, "duration": 14.0})
        windows.append(row)
    return {"duration": 56.0, "speechSeconds": 38.66, "coverage": 0.69, "windows": windows}


def ambiguous_probe():
    probabilities = [("es", 0.9), ("es", 0.9), ("es", 0.5), ("es", 0.5)]
    return iter(probabilities)


class TranscriptDecisionTests(unittest.TestCase):
    def test_window_only_inconclusive_reasons_do_not_need_a_transcript(self):
        for reason in ("ambiguous_language", "mixed_language", "insufficient_speech_windows",
                       "low_speech_coverage", "low_language_confidence", "unknown_language"):
            self.assertFalse(spanish_audio.decision_needs_transcript(
                {"outcome": "inconclusive", "reason": reason}), reason)
        self.assertTrue(spanish_audio.decision_needs_transcript(
            {"outcome": "inconclusive", "reason": "lexical_check_unavailable"}))
        self.assertTrue(spanish_audio.decision_needs_transcript(
            {"outcome": "spanish", "reason": "spanish_dominant"}))
        self.assertTrue(spanish_audio.decision_needs_transcript(
            {"outcome": "excluded", "reason": "language_es_dominant"}))


class SpanishInferTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = object.__new__(spanish_audio.Manager)
        self.manager._tmpdir = self.temp.name
        self.manager._enabled = lambda: True
        self.manager.whisper = "/tmp/fixture-whisper"
        self.manager.model_path = "/tmp/fixture-model.bin"
        self.manager.threads = 1
        self.manager.log = lambda message: None
        self.results = []
        self.manager._record_result = lambda job, decision: self.results.append(decision)

    def test_ambiguous_windows_skip_the_full_transcript_pass(self):
        detections = ambiguous_probe()
        pcm = b"\x00" * (int(spanish_audio.RATE * 56) * spanish_audio.WIDTH)
        with patch.object(spanish_audio, "analyze_speech", return_value=analysis()), \
                patch.object(spanish_audio, "detect_language",
                             side_effect=lambda *a, **k: next(detections)), \
                patch.object(spanish_audio, "transcribe",
                             side_effect=AssertionError("redundant transcript")):
            self.manager._infer({"pcm": pcm, "seconds": 56.0, "cid": "a" * 32})
        self.assertEqual(len(self.results), 1)
        self.assertEqual(self.results[0]["reason"], "ambiguous_language")
        self.assertEqual(self.results[0]["speechSeconds"], 38.66)


class LanguageInferTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = object.__new__(language_audio.Manager)
        self.manager._tmpdir = self.temp.name
        self.manager._enabled = lambda: True
        self.manager.whisper = "/tmp/fixture-whisper"
        self.manager.model_path = "/tmp/fixture-model.bin"
        self.manager.threads = 1
        self.manager.log = lambda message: None
        self.manager.on_speech = None
        self.manager._finalized = {}
        self.manager._mark_finalized_result = lambda *args, **kwargs: None
        self.results = []
        self.manager._record_result = lambda job, decision: self.results.append(decision)

    def test_allowed_windows_still_transcribe_and_handoff(self):
        detections = iter([("en", 0.99)] * 4)
        pcm = b"\x00" * (int(spanish_audio.RATE * 56) * spanish_audio.WIDTH)
        handoffs = []
        self.manager._notify_speech = lambda job, decision, transcript: handoffs.append((decision, transcript))
        with patch.object(spanish_audio, "analyze_speech", return_value=analysis()), \
                patch.object(spanish_audio, "detect_language", side_effect=lambda *a, **k: next(detections)), \
                patch.object(spanish_audio, "transcribe", return_value=("en", "fixture speech")) as transcribe:
            self.manager._infer({"pcm": pcm, "seconds": 56.0, "cid": "a" * 32})
        transcribe.assert_called_once()
        self.assertEqual(self.results[0]["outcome"], "allowed")
        self.assertEqual(handoffs[0][1], ("en", "fixture speech"))

    def test_unknown_future_failure_retains_transcript(self):
        self.assertTrue(spanish_audio.decision_needs_transcript(
            {"outcome": "inconclusive", "reason": "future_reason"}))

    def test_ambiguous_windows_skip_the_full_transcript_pass(self):
        detections = ambiguous_probe()
        pcm = b"\x00" * (int(spanish_audio.RATE * 56) * spanish_audio.WIDTH)
        with patch.object(spanish_audio, "analyze_speech", return_value=analysis()), \
                patch.object(spanish_audio, "detect_language",
                             side_effect=lambda *a, **k: next(detections)), \
                patch.object(spanish_audio, "transcribe",
                             side_effect=AssertionError("redundant transcript")):
            self.manager._infer({"pcm": pcm, "seconds": 56.0, "cid": "a" * 32})
        self.assertEqual(len(self.results), 1)
        self.assertEqual(self.results[0]["reason"], "ambiguous_language")


if __name__ == "__main__":
    unittest.main()
