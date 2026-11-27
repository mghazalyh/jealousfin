import json
import pathlib
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

import deployment_continuity as continuity


class DeploymentContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.cid = "b" * 32
        self.other = "c" * 32
        self.rid = "a" * 32

    def restart(self, issued_at=1000.0, restart_at=1010.0):
        first = continuity.Continuity(self.root)
        first.begin_generation(now=issued_at)
        first.record_handle(self.rid, self.cid, "playlist", issued_at + 1)
        second = continuity.Continuity(self.root)
        second.begin_generation(now=restart_at)
        return second

    def test_old_handle_is_stale_only_after_a_new_generation(self):
        first = continuity.Continuity(self.root)
        first.begin_generation(now=1000.0)
        first.record_handle(self.rid, self.cid, "playlist", 1001.0)
        self.assertIsNone(first.stale_handle(self.rid, 1002.0))
        second = continuity.Continuity(self.root)
        second.begin_generation(now=1010.0)
        self.assertIsNotNone(second.stale_handle(self.rid, 1011.0))

    def test_correlated_startup_failure_is_suppressed(self):
        active = self.restart()
        self.assertIsNotNone(active.stale_handle(self.rid, 1011.0))
        self.assertIsNotNone(active.stale_handle(self.rid, 1012.0))
        audit = continuity.failure_ignores(
            self.root, self.cid, "Video frames stalled after startup", 1013.0)
        self.assertEqual(audit["kind"], "deployment_restart_media_handles")
        self.assertEqual(audit["staleHitCount"], 2)

    def test_every_declared_startup_reason_requires_correlation(self):
        for reason in continuity.FAILURE_REASONS:
            with tempfile.TemporaryDirectory() as temp:
                root = pathlib.Path(temp)
                old = continuity.Continuity(root)
                old.begin_generation(now=1000.0)
                old.record_handle(self.rid, self.cid, "playlist", 1001.0)
                active = continuity.Continuity(root)
                active.begin_generation(now=1010.0)
                active.stale_handle(self.rid, 1011.0)
                active.stale_handle(self.rid, 1012.0)
                self.assertIsNotNone(continuity.failure_ignores(
                    root, self.cid, reason, 1013.0), reason)

    def test_unrelated_channel_cannot_borrow_stale_handle_evidence(self):
        active = self.restart()
        active.stale_handle(self.rid, 1011.0)
        active.stale_handle(self.rid, 1012.0)
        self.assertIsNone(continuity.failure_ignores(
            self.root, self.other, "Video frames stalled after startup", 1013.0))

    def test_one_stale_hit_is_not_enough(self):
        active = self.restart()
        active.stale_handle(self.rid, 1011.0)
        self.assertIsNone(continuity.failure_ignores(
            self.root, self.cid, "Video frames stalled after startup", 1012.0))

    def test_old_stale_hits_outside_lookback_are_rejected(self):
        # Inside the recovery window, but the last stale hit is older than the
        # 90 s lookback relative to the failure.
        active = self.restart()
        active.stale_handle(self.rid, 1011.0)
        active.stale_handle(self.rid, 1012.0)
        self.assertIsNone(continuity.failure_ignores(
            self.root, self.cid, "Video frames stalled after startup", 1110.0))

    def test_failure_outside_recovery_window_is_not_suppressed(self):
        # 1010 + 180 + 5 = 1195 is the last accepted instant. Use recent
        # stale hits so only the window boundary can reject this failure.
        active = self.restart()
        active.stale_handle(self.rid, 1190.0)
        active.stale_handle(self.rid, 1191.0)
        self.assertIsNone(continuity.failure_ignores(
            self.root, self.cid, "Video frames stalled after startup", 1196.0))

    def test_two_distinct_stale_handles_for_the_same_channel_are_enough(self):
        old = continuity.Continuity(self.root)
        old.begin_generation(now=1000.0)
        other = "e" * 32
        old.record_handle(self.rid, self.cid, "playlist", 1001.0)
        old.record_handle(other, self.cid, "segment", 1002.0)
        active = continuity.Continuity(self.root)
        active.begin_generation(now=1010.0)
        self.assertIsNotNone(active.stale_handle(self.rid, 1011.0))
        self.assertIsNotNone(active.stale_handle(other, 1012.0))
        audit = continuity.failure_ignores(
            self.root, self.cid, "playback stalled after startup", 1013.0)
        self.assertEqual(audit["staleHitCount"], 2)

    def test_pre_start_failure_cannot_be_suppressed(self):
        active = self.restart()
        active.stale_handle(self.rid, 1011.0)
        active.stale_handle(self.rid, 1012.0)
        self.assertIsNone(continuity.failure_ignores(
            self.root, self.cid, "Video frames stalled after startup", 1004.0))

    def test_future_stale_evidence_cannot_be_suppressed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            old = continuity.Continuity(root)
            old.begin_generation(now=1000.0)
            old.record_handle(self.rid, self.cid, "playlist", 1001.0)
            active = continuity.Continuity(root)
            active.begin_generation(now=1010.0)
            # Records after the failure instant are outside the upper bound.
            with (root / continuity.STALE_FILE).open("a", encoding="utf-8") as handle:
                for at in (1013.0, 1014.0):
                    handle.write(json.dumps({"c": self.cid, "h": "x", "k": "playlist",
                                             "at": at}) + "\n")
            self.assertIsNone(continuity.failure_ignores(
                root, self.cid, "Video frames stalled after startup", 1012.5))

    def test_real_stall_reason_is_never_suppressed(self):
        active = self.restart()
        active.stale_handle(self.rid, 1011.0)
        active.stale_handle(self.rid, 1012.0)
        for reason in ("Playback stalled", "PLAYBACK_STALLED", "Stream HTTP 403",
                       "Video froze after startup", "Network player error -1008"):
            self.assertIsNone(continuity.failure_ignores(self.root, self.cid, reason, 1013.0))

    def test_new_generation_handles_are_not_stale(self):
        active = self.restart()
        active.record_handle(self.rid, self.cid, "playlist", 1011.0)
        self.assertIsNone(active.stale_handle(self.rid, 1012.0))

    def test_missing_or_corrupt_state_fails_closed(self):
        active = self.restart()
        active.stale_handle(self.rid, 1011.0)
        active.stale_handle(self.rid, 1012.0)
        state = self.root / continuity.STATE_FILE
        state.write_text("{bad")
        self.assertIsNone(continuity.failure_ignores(
            self.root, self.cid, "Video frames stalled after startup", 1013.0))
        state.unlink()
        self.assertIsNone(continuity.failure_ignores(
            self.root, self.cid, "Video frames stalled after startup", 1013.0))

    def test_corrupt_handle_and_stale_records_are_ignored(self):
        self.restart()
        path = self.root / continuity.HANDLE_FILE
        path.write_text('{"schemaVersion":1,"h":"' + self.rid + '","c":"' + self.cid + '","k":"x","g":"' + "d" * 32 + '","at":1}\n')
        corrupt = continuity.Continuity(self.root)
        corrupt.begin_generation(now=1020.0)
        self.assertIsNone(corrupt.stale_handle(self.rid, 1021.0))
        stale = self.root / continuity.STALE_FILE
        stale.write_text('{"c":"' + self.cid + '","at":"bad"}\n')
        self.assertEqual(continuity.stale_hits(self.root, self.cid, 1020.0, 1023.0), [])

    def test_ledger_contains_no_url_token_or_query(self):
        active = self.restart()
        active.stale_handle(self.rid, 1011.0)
        active.stale_handle(self.rid, 1012.0)
        for name in (continuity.HANDLE_FILE, continuity.STALE_FILE):
            path = self.root / name
            text = path.read_text()
            self.assertNotIn(self.rid, text)
            self.assertNotIn("http://", text)
            self.assertNotIn("https://", text)
            self.assertNotIn("token", text.casefold())
            self.assertNotIn("api_key", text.casefold())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_duplicate_event_identity_is_left_to_the_observed_table(self):
        active = self.restart()
        active.stale_handle(self.rid, 1011.0)
        active.stale_handle(self.rid, 1012.0)
        audit = continuity.failure_ignores(
            self.root, self.cid, "Playback stalled after startup", 1013.0)
        continuity.record_ignored(self.root, "observer", "PLAYBACK_STALLED", "trace-1", "Playback stalled after startup", audit)
        continuity.record_ignored(self.root, "observer", "PLAYBACK_STALLED", "trace-1", "Playback stalled after startup", audit)
        rows = [json.loads(line) for line in (self.root / continuity.IGNORED_FILE).read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["originalCode"] for row in rows}, {"PLAYBACK_STALLED"})
        self.assertEqual({row["trace"] for row in rows}, {"trace-1"})

    def test_failure_reason_matching_is_whitespace_and_case_insensitive_but_bounded(self):
        self.assertTrue(continuity.failure_reason_matches("  VIDEO   frames stalled after startup  "))
        self.assertFalse(continuity.failure_reason_matches("Video frames stalled after startup " + "x" * 600))

    def test_concurrent_handle_records_remain_valid(self):
        active = continuity.Continuity(self.root)
        active.begin_generation(now=1000.0)
        rids = ["%032x" % number for number in range(1, 65)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda rid: active.record_handle(rid, self.cid, "segment", 1001.0), rids))
        self.assertTrue(all(results))
        second = continuity.Continuity(self.root)
        second.begin_generation(now=1010.0)
        self.assertTrue(all(second.stale_handle(rid, 1011.0) for rid in rids[:2]))
        self.assertEqual(len(continuity.stale_hits(self.root, self.cid, 1010.0, 1012.0)), 2)


if __name__ == "__main__":
    unittest.main()
