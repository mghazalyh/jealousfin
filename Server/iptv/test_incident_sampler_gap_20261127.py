"""Incident regression: identity-safe sampler gap continuation.

The World Poker Tour visit finalized at 12 s because every segment arriving
more than ``VISIT_GAP_SECONDS`` after the previous tap unconditionally ended
the sample. Playback buffering produces those gaps while the device remains on
the same verified channel, launch, stream generation and visit.

These fixtures drive the local sampler synchronously with an in-memory probe
and visit ledger. No thread, device, socket, credential, media URL or provider
is contacted.
"""
import pathlib
import queue
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import language_visits
import spanish_audio

CID = "a" * 32
PROVIDER = "nltv:us:WorldPokerTour.us"
LAUNCH = "launch-wpt"
STREAM = "stream-1"
VISIT = "visit-" + "1" * 32
OTHER_VISIT = "visit-" + "2" * 32
BASE = "https://media.example/wpt.m3u8"


class ProbeFixture:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        index = min(self.calls - 1, len(self.values) - 1)
        return self.values[index]


class VisitFixture:
    def __init__(self, visit_id=VISIT):
        self.visit_id = visit_id
        self.calls = []

    def observe(self, channel_id, launch_id, stream_generation=None, now=None):
        self.calls.append((channel_id, launch_id, stream_generation, now))
        if not channel_id or not launch_id or not self.visit_id:
            return {"visitID": "", "verified": False, "newVisit": False,
                    "reason": "identity_unavailable", "channelID": channel_id or "",
                    "launchID": launch_id or "", "streamGeneration": stream_generation or ""}
        return {"visitID": self.visit_id, "verified": True, "newVisit": False,
                "reason": "same_visit", "channelID": channel_id,
                "launchID": launch_id, "streamGeneration": stream_generation or ""}


def status(stream=STREAM, launch=LAUNCH):
    return {"channelID": CID, "launchID": launch, "streamGeneration": stream,
            "surfPaused": False, "surfing": True, "playing": True}


class SamplerGapIncidentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.audio = self.pcm(1.0)
        self.probe = ProbeFixture(status(), status())
        self.visits = VisitFixture()
        self.decode = patch.object(
            spanish_audio, "decode_audio",
            side_effect=lambda *args, **kwargs: self.audio)
        self.decode.start()
        self.addCleanup(self.decode.stop)
        with patch.object(spanish_audio.Manager, "_check_tools", return_value=None), \
                patch.object(spanish_audio.Manager, "_sweep_stale", return_value=None):
            self.manager = spanish_audio.Manager(
                pathlib.Path(self.temp.name), "/tmp/fixture-model.bin",
                lambda *args, **kwargs: {"changed": True, "reason": "auto_add"},
                lambda: True, probe=self.probe, log=lambda message: None,
                visits=self.visits)
        self.manager.fault = None
        self.manager._jobs = queue.Queue()

    def pcm(self, seconds):
        return b"\x00\x00" * int(spanish_audio.RATE * seconds)

    def event(self, rid, wall, base=BASE):
        return {"cid": CID, "provider": PROVIDER, "name": "World Poker Tour",
                "rid": rid, "base": base, "data": b"segment", "is_audio": True,
                "contentType": "", "keyBytes": None, "keyRid": None, "iv": None,
                "sequence": None, "wall": wall}

    def first_sample(self, wall=1000.0):
        self.manager._decode_event(self.event("r0", wall))
        return self.manager._samples[CID]

    def test_fresh_identity_restarts_sample_without_reusing_unverified_bytes(self):
        self.probe.values = [None, status()]
        self.audio = self.pcm(10.0)
        original = self.first_sample()
        self.manager._housekeeping()
        fresh = self.manager._samples[CID]
        self.assertIsNot(fresh, original)
        self.assertEqual(fresh.visit["visitID"], VISIT)
        self.assertEqual(fresh.bases[BASE].seconds, 0.0)
        self.assertEqual(fresh.bases[BASE].seen, {"r0"})
        epoch = fresh.recovery_after
        wall = fresh.started
        def event(rid, started, tapped, when):
            result = self.event(rid, when)
            result["requestStartedAt"] = started
            result["tappedMonotonic"] = tapped
            return result
        self.manager._decode_event(event("stale", epoch - 1, epoch + 1, wall + 1))
        self.manager._decode_event(event("r0", epoch + 1, epoch + 2, wall + 2))
        self.assertEqual(fresh.bases[BASE].seconds, 0.0)
        self.manager._decode_event(event("fresh", epoch + 2, epoch + 3, wall + 3))
        self.assertEqual(fresh.bases[BASE].seconds, 10.0)
        self.assertEqual(fresh.bases[BASE].segments, 1)

    def test_unverified_gap_recovery_discards_trigger_segment(self):
        self.probe.values = [None, status()]
        original = self.first_sample()
        self.manager._decode_event(self.event("r1", 1000 + spanish_audio.VISIT_GAP_SECONDS + 1))
        fresh = self.manager._samples[CID]
        self.assertIsNot(fresh, original)
        self.assertEqual(fresh.bases[BASE].seconds, 0.0)
        self.assertEqual(self.probe.calls, 2)

    def test_recovery_requires_coherent_visit_and_stream(self):
        self.probe.values = [None, status("")]
        sample = self.first_sample()
        self.manager._housekeeping()
        self.assertIs(self.manager._samples[CID], sample)

    def test_gap_same_verified_identity_keeps_bytes_and_sample(self):
        sample = self.first_sample()
        base = sample.bases[BASE]
        original_pcm = bytes(base.pcm)
        self.assertEqual(base.segments, 1)

        self.manager._decode_event(
            self.event("r1", 1000.0 + spanish_audio.VISIT_GAP_SECONDS + 2.0))

        self.assertIs(self.manager._samples[CID], sample)
        self.assertEqual(sample.started, 1000.0)
        self.assertEqual(base.segments, 2)
        self.assertEqual(bytes(base.pcm), original_pcm + self.audio)
        self.assertNotIn(CID, self.manager._finalized)
        self.assertEqual(self.probe.calls, 2)

    def test_changed_stream_is_not_continuity_and_never_mixes_bytes(self):
        self.audio = self.pcm(40.0)
        sample = self.first_sample()
        base = sample.bases[BASE]
        original_pcm = bytes(base.pcm)
        self.probe.values = [status(STREAM), status("stream-2")]

        self.manager._decode_event(
            self.event("r1", 1000.0 + spanish_audio.VISIT_GAP_SECONDS + 2.0))

        self.assertNotIn(CID, self.manager._samples)
        self.assertEqual(bytes(base.pcm), original_pcm)
        self.assertEqual(base.segments, 1)
        marker = self.manager._finalized[CID]
        self.assertEqual(marker["reason"], "visit_gap")
        self.assertTrue(marker["pending"])
        self.assertEqual(marker["streamGeneration"], STREAM)
        self.assertEqual(self.probe.calls, 2)

    def test_changed_visit_starts_fresh_sample_without_old_bytes(self):
        self.audio = self.pcm(40.0)
        sample = self.first_sample()
        base = sample.bases[BASE]
        original_pcm = bytes(base.pcm)
        self.visits.visit_id = OTHER_VISIT

        self.manager._decode_event(
            self.event("r1", 1000.0 + spanish_audio.VISIT_GAP_SECONDS + 2.0))

        marker = self.manager._finalized[CID]
        self.assertEqual(marker["visitID"], VISIT)
        self.assertEqual(marker["reason"], "visit_gap")
        fresh = self.manager._samples[CID]
        self.assertIsNot(fresh, sample)
        self.assertEqual(fresh.visit["visitID"], OTHER_VISIT)
        self.assertEqual(bytes(fresh.bases[BASE].pcm), self.audio)
        self.assertEqual(fresh.bases[BASE].segments, 1)
        self.assertEqual(bytes(base.pcm), original_pcm)
        self.assertEqual(base.segments, 1)
        self.assertEqual(self.probe.calls, 2)

    def test_unavailable_gap_probe_fails_closed_without_extra_probe(self):
        self.audio = self.pcm(40.0)
        sample = self.first_sample()
        base = sample.bases[BASE]
        original_pcm = bytes(base.pcm)
        self.probe.values = [status(), None]

        self.manager._decode_event(
            self.event("r1", 1000.0 + spanish_audio.VISIT_GAP_SECONDS + 2.0))

        self.assertNotIn(CID, self.manager._samples)
        self.assertEqual(bytes(base.pcm), original_pcm)
        self.assertEqual(base.segments, 1)
        marker = self.manager._finalized[CID]
        self.assertEqual(marker["reason"], "visit_gap")
        self.assertTrue(marker["pending"])
        self.assertEqual(self.probe.calls, 2)

    def test_out_of_order_segment_is_not_continuity_or_a_gap(self):
        self.audio = self.pcm(10.0)
        sample = self.first_sample(wall=1000.0)
        base = sample.bases[BASE]
        original_pcm = bytes(base.pcm)
        self.visits.visit_id = OTHER_VISIT

        self.manager._decode_event(self.event("r1", 995.0))

        self.assertIs(self.manager._samples[CID], sample)
        self.assertEqual(sample.last_tap, 1000.0)
        self.assertEqual(bytes(base.pcm), original_pcm)
        self.assertEqual(base.segments, 1)
        self.assertNotIn(CID, self.manager._finalized)
        self.assertEqual(self.probe.calls, 1)

    def test_gap_continuation_preserves_pcm_memory_bound(self):
        self.audio = self.pcm(10.0)
        sample = self.first_sample()
        base = sample.bases[BASE]
        wall = 1000.0
        for index in range(1, 5):
            wall += spanish_audio.VISIT_GAP_SECONDS + 1.0
            self.manager._decode_event(self.event("r%d" % index, wall))
            self.assertIs(self.manager._samples[CID], sample)

        self.assertEqual(base.segments, 5)
        self.assertEqual(base.seconds, 50.0)
        self.assertEqual(len(base.pcm), int(spanish_audio.MAX_PCM_SECONDS * spanish_audio.BYTES_PER_SECOND) - 25 * spanish_audio.BYTES_PER_SECOND)

        captured = {}
        original_finalize = self.manager._finalize

        def capture(cid, reason, *args, **kwargs):
            captured["base"] = self.manager._samples[cid].bases[BASE]
            return original_finalize(cid, reason, *args, **kwargs)

        self.manager._finalize = capture
        self.audio = self.pcm(100.0)
        wall += spanish_audio.VISIT_GAP_SECONDS + 1.0
        self.manager._decode_event(self.event("r-bound", wall))

        self.assertLessEqual(captured["base"].seconds, spanish_audio.MAX_PCM_SECONDS)
        self.assertLessEqual(len(captured["base"].pcm),
                             int(spanish_audio.MAX_PCM_SECONDS * spanish_audio.BYTES_PER_SECOND))
        self.assertEqual(captured["base"].seconds, spanish_audio.MAX_PCM_SECONDS)


if __name__ == "__main__":
    unittest.main()
