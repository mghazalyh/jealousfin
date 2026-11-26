"""Physical-visit identity acceptance for automatic language evidence.

No device, network or media access.  The ledger consumes only the read-only
launch/channel/stream observations the sampler already probes; the store tests
prove that one physical visit can contribute at most one conclusive automatic
event while a verified departure and return inside the old cooldown still
counts.
"""
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import language_audio
import language_catalog
import language_exclusions
import language_visits
import spanish_audio as base

A = "a" * 32
B = "b" * 32
C = "c" * 32
LAUNCH = "launch-1"


def evidence(language="pt"):
    return {
        "model": "fixture.bin", "windows": 4, "speechSeconds": 40.0,
        "sampleSeconds": 60.0, "speechCoverage": 0.66,
        "languages": [{"language": language, "probability": 0.97}],
        "deviceVerified": True, "channelChangedBeforeResult": False,
        "lexicalMargin": -0.4,
    }


class VisitLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.ledger = language_visits.Ledger(self.root)

    def test_first_observation_and_reload_are_one_visit(self):
        first = self.ledger.observe(A, LAUNCH, "S1", now=1000)
        self.assertTrue(first["verified"])
        self.assertTrue(first["newVisit"])
        reload = self.ledger.observe(A, LAUNCH, "S2", now=1001)
        self.assertTrue(reload["verified"])
        self.assertFalse(reload["newVisit"])
        self.assertEqual(reload["visitID"], first["visitID"])
        later_sample = self.ledger.observe(A, LAUNCH, "S2", now=1601)
        self.assertEqual(later_sample["visitID"], first["visitID"])
        self.assertFalse(later_sample["newVisit"])

    def test_departure_and_return_inside_cooldown_is_new_visit(self):
        first = self.ledger.observe(A, LAUNCH, "S1", now=1000)
        self.ledger.observe(B, LAUNCH, "S2", now=1010)
        returned = self.ledger.observe(A, LAUNCH, "S3", now=1060)
        self.assertTrue(returned["verified"])
        self.assertTrue(returned["newVisit"])
        self.assertEqual(returned["reason"], "returned_after_departure")
        self.assertNotEqual(returned["visitID"], first["visitID"])

    def test_new_launch_is_a_new_visit_even_for_same_channel(self):
        first = self.ledger.observe(A, "launch-1", "S1", now=1000)
        second = self.ledger.observe(A, "launch-2", "S2", now=1001)
        self.assertTrue(second["newVisit"])
        self.assertNotEqual(second["visitID"], first["visitID"])

    def test_missing_launch_or_channel_fails_closed(self):
        self.assertFalse(self.ledger.observe(A, "", "S1", now=1)["verified"])
        self.assertFalse(self.ledger.observe("", LAUNCH, "S1", now=1)["verified"])
        self.assertFalse(self.ledger.observe("not-a-channel", LAUNCH, "S1", now=1)["verified"])

    def test_identity_survives_restart(self):
        first = self.ledger.observe(A, LAUNCH, "S1", now=1000)
        reopened = language_visits.Ledger(self.root)
        again = reopened.observe(A, LAUNCH, "S2", now=1005)
        self.assertEqual(again["visitID"], first["visitID"])
        self.assertFalse(again["newVisit"])

    def test_departed_channel_is_pruned_before_active(self):
        with patch.object(language_visits, "MAX_CHANNELS", 10):
            for index in range(15):
                cid = ("%032x" % index)[-32:]
                self.ledger.observe(cid, LAUNCH, "S", now=index)
            self.ledger.observe(A, LAUNCH, "S", now=9999)
            snapshot = self.ledger.snapshot()
        self.assertLessEqual(len(snapshot["launches"][LAUNCH]["channels"]), 10)
        self.assertEqual(snapshot["launches"][LAUNCH]["currentChannelID"], A)


class VisitStoreDedupTests(unittest.TestCase):
    def record(self, state, channel, visit, language, at, launch=LAUNCH):
        return language_exclusions.record_evidence(
            state, "nltv:us:" + channel, channel, "Fixture", language,
            language_catalog.language_name(language), 0.97,
            "auto:language_" + language + "_dominant", evidence(language),
            outcome="excluded", visit_id=visit, launch_id=launch,
            stream_generation="stream-1", now=at, threshold=25, enforcement=False)

    def test_one_count_per_visit_even_when_language_changes_and_reloads(self):
        state = language_exclusions.empty()
        visit = language_visits.new_visit_id()
        state, first = self.record(state, A, visit, "pt", 1000)
        state, reload = self.record(state, A, visit, "pt", 1001)
        state, language_change = self.record(state, A, visit, "fr", 1002)
        state, after_old_window = self.record(state, A, visit, "pt", 1601)
        self.assertTrue(first["changed"])
        self.assertEqual(reload["reason"], "duplicate_visit")
        self.assertEqual(language_change["reason"], "duplicate_visit")
        self.assertEqual(after_old_window["reason"], "duplicate_visit")
        self.assertEqual(language_exclusions.counts_for(state, A, "pt"), 1)
        self.assertEqual(language_exclusions.counts_for(state, A, "fr"), 0)

    def test_ledger_reload_and_continuous_view_are_one_event(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        ledger = language_visits.Ledger(temp.name)
        first = ledger.observe(A, LAUNCH, "S1", now=1000)
        state = language_exclusions.empty()
        state, recorded = self.record(state, A, first["visitID"], "pt", 1000)
        self.assertTrue(recorded["changed"])
        reload = ledger.observe(A, LAUNCH, "S2", now=1601)
        self.assertEqual(reload["visitID"], first["visitID"])
        state, duplicate = self.record(state, A, reload["visitID"], "pt", 1601)
        self.assertEqual(duplicate["reason"], "duplicate_visit")
        self.assertEqual(language_exclusions.counts_for(state, A, "pt"), 1)
        ledger.observe(B, LAUNCH, "S3", now=1610)
        returned = ledger.observe(A, LAUNCH, "S4", now=1620)
        self.assertNotEqual(returned["visitID"], first["visitID"])
        state, revisited = self.record(state, A, returned["visitID"], "pt", 1620)
        self.assertTrue(revisited["changed"])
        self.assertEqual(language_exclusions.counts_for(state, A, "pt"), 2)

    def test_verified_revisit_inside_cooldown_counts(self):
        state = language_exclusions.empty()
        first_visit = language_visits.new_visit_id()
        second_visit = language_visits.new_visit_id()
        state, first = self.record(state, A, first_visit, "pt", 1000)
        state, second = self.record(state, A, second_visit, "pt", 1060)
        self.assertTrue(first["changed"])
        self.assertTrue(second["changed"])
        self.assertEqual(language_exclusions.counts_for(state, A, "pt"), 2)

    def test_unverified_identity_still_fails_closed(self):
        state = language_exclusions.empty()
        state, first = self.record(state, A, "sample:1000", "pt", 1000)
        state, retry = self.record(state, A, "sample:1601", "pt", 1601)
        self.assertTrue(first["changed"])
        self.assertEqual(retry["reason"], "duplicate_visit")
        state, new_launch = self.record(state, A, "sample:1602", "pt", 1602, launch="launch-2")
        self.assertTrue(new_launch["changed"])

    def test_manual_correction_does_not_suppress_a_later_visit(self):
        state = language_exclusions.empty()
        first_visit = language_visits.new_visit_id()
        second_visit = language_visits.new_visit_id()
        provider = "nltv:us:" + A
        state, first = self.record(state, A, first_visit, "pt", 1000)
        self.assertTrue(first["changed"])
        state, retract = language_exclusions.retract_evidence(
            state, provider, A, "Fixture", "correct-1", now=1010,
            threshold=25, enforcement=False)
        self.assertEqual(retract["reason"], "retracted_english")
        state, second = self.record(state, A, second_visit, "pt", 1060)
        self.assertTrue(second["changed"])
        self.assertEqual(language_exclusions.counts_for(state, A, "pt"), 1)


class SamplerVisitSuppressionTests(unittest.TestCase):
    def manager(self):
        manager = object.__new__(base.Manager)
        manager._lock = __import__("threading").RLock()
        manager._visit_results = {}
        return manager

    def test_long_continuous_visit_is_suppressed_after_one_conclusive_sample(self):
        manager = self.manager()
        visit = language_visits.new_visit_id()
        last = {"at": time.time() - 5000, "visitID": visit,
                "verified": True, "conclusive": True}
        self.assertTrue(manager._suppress_duplicate_visit(
            last, {"visitID": visit, "verified": True}, time.time()))

    def test_verified_revisit_is_not_suppressed_by_cooldown(self):
        manager = self.manager()
        first = language_visits.new_visit_id()
        second = language_visits.new_visit_id()
        last = {"at": time.time() - 5, "visitID": first,
                "verified": True, "conclusive": True}
        self.assertFalse(manager._suppress_duplicate_visit(
            last, {"visitID": second, "verified": True}, time.time()))

    def test_retry_after_inconclusive_sample_waits_for_cooldown(self):
        manager = self.manager()
        visit = language_visits.new_visit_id()
        now = time.time()
        last = {"at": now - 5, "visitID": visit,
                "verified": True, "conclusive": False}
        self.assertTrue(manager._suppress_duplicate_visit(
            last, {"visitID": visit, "verified": True}, now))
        self.assertFalse(manager._suppress_duplicate_visit(
            last, {"visitID": visit, "verified": True},
            now + base.FINALIZE_COOLDOWN_SECONDS + 1))

    def test_unverified_sample_uses_bounded_cooldown(self):
        manager = self.manager()
        now = time.time()
        last = {"at": now - 5, "visitID": "", "verified": False, "conclusive": False}
        self.assertTrue(manager._suppress_duplicate_visit(
            last, {"visitID": "", "verified": False}, now))
        self.assertFalse(manager._suppress_duplicate_visit(
            last, {"visitID": "", "verified": False},
            now + base.FINALIZE_COOLDOWN_SECONDS + 1))


class StaleJobTests(unittest.TestCase):
    def test_reload_in_same_visit_is_current_but_previous_visit_is_stale(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        ledger = language_visits.Ledger(temp.name)
        first = ledger.observe(A, LAUNCH, "S1", now=1000)
        manager = object.__new__(language_audio.Manager)
        manager.visits = ledger
        manager.probe = lambda: {"channelID": A, "launchID": LAUNCH, "streamGeneration": "S2"}
        job = {"cid": A, "launchID": LAUNCH, "streamGeneration": "S1",
               "visitID": first["visitID"]}
        self.assertTrue(manager._job_is_current(job, manager.probe()))
        ledger.observe(B, LAUNCH, "S2", now=1010)
        second = ledger.observe(A, LAUNCH, "S3", now=1020)
        self.assertNotEqual(second["visitID"], first["visitID"])
        self.assertFalse(manager._job_is_current(job, manager.probe()))

    def test_unverified_job_keeps_strict_stream_check(self):
        manager = object.__new__(language_audio.Manager)
        manager.visits = None
        job = {"cid": A, "launchID": LAUNCH, "streamGeneration": "S1", "visitID": ""}
        self.assertFalse(manager._identity_matches(
            job, {"channelID": A, "launchID": LAUNCH, "streamGeneration": "S2"}))
        self.assertTrue(manager._identity_matches(
            job, {"channelID": A, "launchID": LAUNCH, "streamGeneration": "S1"}))


class MigrationFixtureTests(unittest.TestCase):
    def test_v3_fixture_migrates_once_and_preserves_manual_decisions(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = pathlib.Path(temp.name)
        auto = {
            "channelID": A, "channelName": "A", "markedAt": 10.0,
            "nonce": "auto-a", "source": "auto", "language": "pt",
            "languageName": "Portuguese", "confidence": 0.95,
            "reason": "auto:language_pt_dominant", "evidence": evidence("pt"),
        }
        manual = {
            "channelID": B, "channelName": "B", "markedAt": 11.0,
            "nonce": "manual-b", "source": "manual", "language": "es",
            "languageName": "Spanish", "confidence": None,
            "reason": "manual-spanish", "evidence": None,
        }
        state = {
            "schemaVersion": 3, "revision": "language-exclusions-v3",
            "updatedAt": 12.0, "lastNonce": "", "recentNonces": [],
            "entries": {"nltv:us:" + A: auto, "nltv:us:" + B: manual},
            "decisions": {
                "nltv:us:" + A: {"decision": "spanish", "language": "es",
                                 "languageName": "Spanish", "at": 10.0,
                                 "nonce": "auto-a", "channelID": A, "channelName": "A"},
                "nltv:us:" + B: {"decision": "spanish", "language": "es",
                                 "languageName": "Spanish", "at": 11.0,
                                 "nonce": "manual-b", "channelID": B, "channelName": "B"},
            },
        }
        (root / "language-exclusions.json").write_text(__import__("json").dumps(state))
        migrated = language_exclusions.load_or_migrate(root)
        self.assertEqual(migrated["schemaVersion"], 4)
        self.assertEqual(set(migrated["entries"]), {"nltv:us:" + B})
        self.assertEqual(len(migrated["evidence"]["events"]), 1)
        self.assertEqual(language_exclusions.counts_for(migrated, A, "pt"), 1)
        self.assertEqual(migrated["decisions"]["nltv:us:" + B]["decision"], "spanish")
        again = language_exclusions.load_or_migrate(root)
        self.assertEqual(again, migrated)
        self.assertEqual(len(again["evidence"]["events"]), 1)


if __name__ == "__main__":
    unittest.main()
