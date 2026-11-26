"""Regression tests for read-only surf pause reconciliation.

No iPad, network or media access. These tests lock in the rule that a
pre-existing user pause, or a user pause made during maintenance, must never
be overridden by an automatic resume. The module never returns a
state-changing command.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import surf_maintenance


def snap(paused, launch="L1", stream="S1", surfing=True, cid="a" * 32):
    return {"surfPaused": paused, "surfing": surfing, "currentChannelID": cid,
            "launchID": launch, "streamGeneration": stream}


class SurfMaintenanceTests(unittest.TestCase):
    def assert_leave(self, result):
        self.assertEqual(result["action"], "leave")
        for command in ("resume_surf", "start_surf", "next_channel",
                        "previous_channel", "reload_current_stream", "pause_surf", "stop_surf"):
            self.assertIn(command, result["forbiddenStateChanges"])
        self.assertTrue(result["note"])

    def test_pre_paused_is_never_resumed(self):
        result = surf_maintenance.reconcile(
            snap(True), [{"at": 1, "source": "maintenance", "action": "pause", "id": "m1"},
                         {"at": 2, "source": "maintenance", "action": "reload", "id": "m2"}],
            snap(False), "L1", "S1")
        self.assert_leave(result)
        self.assertEqual(result["preserve"], "paused")
        self.assertEqual(result["reason"], "initial_paused_no_user_resume")

    def test_user_pause_during_maintenance_is_preserved(self):
        result = surf_maintenance.reconcile(
            snap(False), [{"at": 3, "source": "user", "action": "pause", "id": "u1"}],
            snap(False), "L1", "S1")
        self.assert_leave(result)
        self.assertEqual(result["preserve"], "paused")
        self.assertEqual(result["reason"], "user_paused_during_maintenance")

    def test_user_resume_during_maintenance_is_preserved(self):
        result = surf_maintenance.reconcile(
            snap(True), [{"at": 4, "source": "user", "action": "resume", "id": "u2"}],
            snap(True), "L1", "S1")
        self.assert_leave(result)
        self.assertEqual(result["preserve"], "running")
        self.assertEqual(result["reason"], "user_resumed_during_maintenance")

    def test_paused_after_resume_is_user_pause(self):
        result = surf_maintenance.reconcile(
            snap(True), [{"at": 4, "source": "user", "action": "resume", "id": "u2"},
                         {"at": 5, "source": "user", "action": "pause", "id": "u3"}],
            snap(False), "L1", "S1")
        self.assert_leave(result)
        self.assertEqual(result["preserve"], "paused")

    def test_stale_launch_or_stream_preserves_unknown(self):
        for kwargs in ({"current_launch": "L2", "current_stream": "S1"},
                       {"current_launch": "L1", "current_stream": "S2"}):
            result = surf_maintenance.reconcile(snap(True), [], snap(True), **kwargs)
            self.assert_leave(result)
            self.assertEqual(result["preserve"], "unknown")
            self.assertTrue(result["stale"])
            self.assertIn(result["reason"], ("stale_launch", "stale_stream"))

    def test_uncertain_state_never_changes_anything(self):
        result = surf_maintenance.reconcile({"surfPaused": "unknown"}, [], None)
        self.assert_leave(result)
        self.assertEqual(result["preserve"], "unknown")
        self.assertTrue(result["stale"])

    def test_duplicate_user_events_do_not_duplicate_or_advance(self):
        result = surf_maintenance.reconcile(
            snap(False), [{"at": 3, "source": "user", "action": "pause", "id": "u1"},
                          {"at": 3, "source": "user", "action": "pause", "id": "u1"},
                          {"at": 4, "source": "maintenance", "action": "next", "id": "m1"}],
            snap(True), "L1", "S1")
        self.assert_leave(result)
        self.assertEqual(result["preserve"], "paused")

    def test_final_paused_after_running_is_reported_not_resumed(self):
        result = surf_maintenance.reconcile(snap(False), [], snap(True), "L1", "S1")
        self.assert_leave(result)
        self.assertEqual(result["preserve"], "running")
        self.assertTrue(result["finalObservedPaused"])
        self.assertIn("next_channel", result["forbiddenStateChanges"])

    def test_document_report(self):
        result = surf_maintenance.report({
            "initial": snap(True), "events": [], "final": snap(False),
            "current_launch": "L1", "current_stream": "S1",
        })
        self.assert_leave(result)
        self.assertEqual(result["preserve"], "paused")

    def test_report_rejects_unbounded_shape(self):
        with self.assertRaises(ValueError):
            surf_maintenance.report({"initial": snap(True), "unexpected": True})


if __name__ == "__main__":
    unittest.main()
