"""Pause-safe automatic exclusion acceptance.

No device, network or media access. Device rotation state is mocked and the
tests assert that automatic exclusions defer while the user has paused or
stopped rotation, that pending exclusions apply only after active rotation
returns, that manual decisions win, and that no state-changing device command
is issued.
"""
import importlib
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import language_pending
import language_visits


class LanguagePauseGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = {"JEALOUSFIN_STATE_DIR": self.temp.name, "JEALOUSFIN_USERNAME": "fixture",
               "JEALOUSFIN_PASSWORD": "fixture", "JEALOUSFIN_TOKEN": "fixture-token",
               "JEALOUSFIN_SERVER_ID": "fixture-server", "JEALOUSFIN_BIND": "127.0.0.1"}
        with patch.dict(os.environ, env):
            import gateway
            self.g = importlib.reload(gateway)
        self.a, self.b, self.c = ("a" * 32, "b" * 32, "c" * 32)
        for cid in (self.a, self.b, self.c):
            self.g.channels[cid] = {"Id": cid, "Name": cid, "Type": "TvChannel"}
            self.g.sources[cid] = "nltv:fixture:" + cid
        self.db = pathlib.Path(self.temp.name) / "policy.sqlite3"
        with self.g.policy_connect(self.db) as db:
            db.executemany("INSERT INTO channel(id,name) VALUES(?,?)", [(cid, cid) for cid in (self.a, self.b, self.c)])
            db.execute("INSERT INTO meta VALUES('seeded','1')")
        self.g.device_rotation_cache = None
        self.g.device_rotation_checked = 0.0
        self.g.set_language_auto_control(enabled=True, enforcement=True, threshold=1)

    def evidence(self, code, visit=None):
        return {"model": "fixture.bin", "windows": 4, "speechSeconds": 40.0,
                "sampleSeconds": 60.0, "speechCoverage": 0.66,
                "languages": [{"language": code, "probability": 0.97}],
                "deviceVerified": True, "channelChangedBeforeResult": False,
                "lexicalMargin": -0.4 if code == "pt" else 0.5,
                "outcome": "excluded", "visitID": visit or "",
                "launchID": "L1", "streamGeneration": "S1"}

    def rotation(self, cid, paused, surfing=True, launch="L1", stream="S1"):
        return {"currentChannelID": cid, "surfPaused": paused, "surfing": surfing,
                "launchID": launch, "streamGeneration": stream}

    def test_paused_current_channel_defers_then_applies_on_active_rotation(self):
        g = self.g
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.a, True)):
            result = g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese",
                                            0.97, "auto:language_pt_dominant", self.evidence("pt"))
        self.assertEqual(result["reason"], "deferred_paused")
        self.assertEqual(g.spanish_provider_ids, set())
        pending = language_pending.describe(self.temp.name)
        self.assertEqual(pending["entries"], 1)
        self.assertEqual(pending["languages"], {"pt": 1})
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.a, True)), \
                patch.object(g, "reload_spanish_auto_control", return_value=True):
            self.assertEqual(g.process_language_pending_once(), 0)
        self.assertEqual(language_pending.describe(self.temp.name)["entries"], 1)
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.a, False)), \
                patch.object(g, "reload_spanish_auto_control", return_value=True):
            self.assertEqual(g.process_language_pending_once(), 1)
        self.assertEqual(language_pending.describe(self.temp.name)["entries"], 0)
        self.assertIn(g.sources[self.a], g.spanish_provider_ids)
        state = json.loads((pathlib.Path(self.temp.name) / "language-exclusions.json").read_text())
        self.assertEqual(state["entries"][g.sources[self.a]]["language"], "pt")
        self.assertEqual(g.spanish_classification.counts_for(state, self.a, "pt"), 1)

    def test_paused_on_another_channel_defers_before_commit(self):
        g = self.g
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.b, True)):
            result = g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese",
                                            0.97, "auto:language_pt_dominant", self.evidence("pt"))
        self.assertEqual(result["reason"], "deferred_paused")
        self.assertNotIn(g.sources[self.a], g.spanish_provider_ids)
        self.assertEqual(language_pending.describe(self.temp.name)["entries"], 1)
        state = g.spanish_classification.load_or_migrate(self.temp.name)
        self.assertEqual(g.spanish_classification.counts_for(state, self.a, "pt"), 0)
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.b, False)), \
                patch.object(g, "reload_spanish_auto_control", return_value=True):
            self.assertEqual(g.process_language_pending_once(), 1)
        state = g.spanish_classification.load_or_migrate(self.temp.name)
        self.assertEqual(g.spanish_classification.counts_for(state, self.a, "pt"), 1)

    def test_stopped_rotation_on_another_channel_defers_before_commit(self):
        g = self.g
        with patch.object(g, "device_rotation_state",
                          return_value=self.rotation(self.b, False, surfing=False)):
            result = g.language_auto_commit(self.a, g.sources[self.a], "A", "fr", "French",
                                            0.97, "auto:language_fr_dominant", self.evidence("fr"))
        self.assertEqual(result["reason"], "deferred_paused")
        self.assertNotIn(g.sources[self.a], g.spanish_provider_ids)
        self.assertEqual(language_pending.describe(self.temp.name)["languages"], {"fr": 1})

    def test_pending_visit_identity_is_preserved_and_not_double_counted(self):
        g = self.g
        visit = language_visits.new_visit_id()
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.a, True)):
            g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese",
                                   0.97, "auto:language_pt_dominant", self.evidence("pt", visit))
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.a, False)), \
                patch.object(g, "reload_spanish_auto_control", return_value=True):
            self.assertEqual(g.process_language_pending_once(), 1)
        # A later sample from the same physical visit must not count again.
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.a, False)):
            result = g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese",
                                            0.97, "auto:language_pt_dominant", self.evidence("pt", visit))
        self.assertEqual(result["reason"], "duplicate_visit")
        state = g.spanish_classification.load_or_migrate(self.temp.name)
        self.assertEqual(g.spanish_classification.counts_for(state, self.a, "pt"), 1)

    def test_pending_not_applied_while_auto_disabled(self):
        g = self.g
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.a, True)):
            g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese",
                                   0.97, "auto:language_pt_dominant", self.evidence("pt"))
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.a, False)), \
                patch.object(g, "reload_spanish_auto_control", return_value=False):
            self.assertEqual(g.process_language_pending_once(), 0)
        self.assertEqual(language_pending.describe(self.temp.name)["entries"], 1)
        self.assertNotIn(g.sources[self.a], g.spanish_provider_ids)

    def test_uncertain_state_defers_and_never_forwards(self):
        g = self.g
        with patch.object(g, "device_rotation_state", return_value=None):
            result = g.language_auto_commit(self.a, g.sources[self.a], "A", "fr", "French",
                                            0.97, "auto:language_fr_dominant", self.evidence("fr"))
        self.assertEqual(result["reason"], "deferred_uncertain")
        self.assertEqual(language_pending.describe(self.temp.name)["languages"], {"fr": 1})
        with patch.object(g, "device_rotation_state", return_value=None):
            self.assertEqual(g.process_language_pending_once(), 0)
        self.assertEqual(language_pending.describe(self.temp.name)["entries"], 1)

    def test_manual_not_spanish_clears_pending_without_mark(self):
        g = self.g
        with g.lock:
            fresh = g.spanish_classification.load_or_migrate(g.ROOT)
            state, _, _ = g.spanish_classification.apply_manual(
                fresh, "remove", self.a, "A", g.sources[self.a], "manual-allow-1", now=time.time())
            g.spanish_classification.atomic_write(g.ROOT, state)
            g.spanish_state = state
            (g.spanish_provider_ids, g.spanish_manual_provider_ids,
             g.spanish_auto_provider_ids) = g._language_membership(state)
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.a, True)):
            g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese",
                                   0.97, "auto:language_pt_dominant", self.evidence("pt"))
        self.assertEqual(language_pending.describe(self.temp.name)["entries"], 1)
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.a, False)), \
                patch.object(g, "reload_spanish_auto_control", return_value=True):
            self.assertEqual(g.process_language_pending_once(), 0)
        self.assertEqual(language_pending.describe(self.temp.name)["entries"], 0)
        self.assertNotIn(g.sources[self.a], g.spanish_provider_ids)
        state = json.loads((pathlib.Path(self.temp.name) / "language-exclusions.json").read_text())
        self.assertEqual(state["decisions"][g.sources[self.a]]["decision"], "not_spanish")

    def test_paused_current_auto_entry_stays_visible_manual_entry_does_not(self):
        g = self.g
        g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese",
                               0.97, "auto:language_pt_dominant", self.evidence("pt"),
                               respect_pause=False)
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.a, True)):
            self.assertTrue(g.language_visible(self.a))
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.b, False)):
            self.assertFalse(g.language_visible(self.a))
        with g.lock:
            fresh = g.spanish_classification.load_or_migrate(g.ROOT)
            state, _, _ = g.spanish_classification.apply_manual(
                fresh, "add", self.b, "B", g.sources[self.b], "manual-b-1", now=time.time())
            g.spanish_classification.atomic_write(g.ROOT, state)
            g.spanish_state = state
            (g.spanish_provider_ids, g.spanish_manual_provider_ids,
             g.spanish_auto_provider_ids) = g._language_membership(state)
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.b, True)):
            self.assertFalse(g.language_visible(self.b))

    def test_unknown_state_keeps_auto_entry_visible(self):
        g = self.g
        g.language_auto_commit(self.a, g.sources[self.a], "A", "de", "German",
                               0.97, "auto:language_de_dominant", self.evidence("de"),
                               respect_pause=False)
        with patch.object(g, "device_rotation_state", return_value=None):
            self.assertTrue(g.language_visible(self.a))

    def test_language_probe_reads_fresh_launch_within_old_cache_window(self):
        g = self.g
        status_calls = []

        def fake(config, token, action, arguments=None):
            if action == "get_status":
                status_calls.append(action)
                launch = "L1" if len(status_calls) == 1 else "L2"
                return json.dumps({"launchID": launch, "surfPaused": False,
                                   "surfing": True, "currentChannelID": self.a})
            if action == "get_player_snapshot":
                return json.dumps({"channelID": self.a, "streamGeneration": "S1"})
            raise AssertionError("unexpected device action: " + str(action))

        with patch.object(g.prototype_selector, "load_selector",
                          return_value={"deviceIP": "192.168.31.82", "backendPort": 18797}), \
                patch.object(g.prototype_selector, "device_command", side_effect=fake):
            first = g.device_language_probe()
            second = g.device_language_probe()
        self.assertEqual(first["launchID"], "L1")
        self.assertEqual(second["launchID"], "L2")

    def test_language_probe_never_reuses_launch_after_status_failure(self):
        g = self.g
        status_calls = []

        def fake(config, token, action, arguments=None):
            if action == "get_status":
                status_calls.append(action)
                if len(status_calls) > 1:
                    raise RuntimeError("status unavailable")
                return json.dumps({"launchID": "L1", "surfPaused": False,
                                   "surfing": True, "currentChannelID": self.a})
            if action == "get_player_snapshot":
                return json.dumps({"channelID": self.a, "streamGeneration": "S1"})
            raise AssertionError("unexpected device action: " + str(action))

        with patch.object(g.prototype_selector, "load_selector",
                          return_value={"deviceIP": "192.168.31.82", "backendPort": 18797}), \
                patch.object(g.prototype_selector, "device_command", side_effect=fake):
            self.assertEqual(g.device_language_probe()["launchID"], "L1")
            self.assertIsNone(g.device_language_probe())

    def test_language_probe_retries_an_incoherent_status_snapshot_pair(self):
        g = self.g
        status_calls = []

        def fake(config, token, action, arguments=None):
            if action == "get_status":
                status_calls.append(action)
                if len(status_calls) == 1:
                    return json.dumps({"launchID": "L1", "surfPaused": False,
                                       "surfing": True, "currentChannelID": self.b})
                return json.dumps({"launchID": "L2", "surfPaused": False,
                                   "surfing": True, "currentChannelID": self.a})
            if action == "get_player_snapshot":
                return json.dumps({"channelID": self.a, "streamGeneration": "S1"})
            raise AssertionError("unexpected device action: " + str(action))

        with patch.object(g.prototype_selector, "load_selector",
                          return_value={"deviceIP": "192.168.31.82", "backendPort": 18797}), \
                patch.object(g.prototype_selector, "device_command", side_effect=fake):
            probe = g.device_language_probe()
        self.assertEqual(probe["channelID"], self.a)
        self.assertEqual(probe["launchID"], "L2")
        self.assertEqual(len(status_calls), 2)

    def test_language_probe_fails_closed_when_pairs_stay_incoherent(self):
        g = self.g

        def fake(config, token, action, arguments=None):
            if action == "get_status":
                return json.dumps({"launchID": "L1", "surfPaused": False,
                                   "surfing": True, "currentChannelID": self.b})
            if action == "get_player_snapshot":
                return json.dumps({"channelID": self.a, "streamGeneration": "S1"})
            raise AssertionError("unexpected device action: " + str(action))

        with patch.object(g.prototype_selector, "load_selector",
                          return_value={"deviceIP": "192.168.31.82", "backendPort": 18797}), \
                patch.object(g.prototype_selector, "device_command", side_effect=fake):
            self.assertIsNone(g.device_language_probe())

    def test_rotation_state_uses_only_read_only_device_commands(self):
        g = self.g
        calls = []
        def fake(config, token, action, arguments=None):
            calls.append(action)
            if action == "get_status":
                return json.dumps({"launchID": "L1", "surfPaused": True, "surfing": True,
                                   "currentChannelID": self.a, "rotationOwner": "device"})
            if action == "get_player_snapshot":
                return json.dumps({"channelID": self.a, "streamGeneration": "S1"})
            raise AssertionError("unexpected state-changing device action: " + str(action))
        with patch.object(g.prototype_selector, "load_selector", return_value={"deviceIP": "192.168.31.82", "backendPort": 18797}), \
                patch.object(g.prototype_selector, "device_command", side_effect=fake):
            g.device_rotation_cache = None
            g.device_rotation_checked = 0.0
            state = g.device_rotation_state(force=True)
        self.assertEqual(set(calls), {"get_status", "get_player_snapshot"})
        for forbidden in ("pause_surf", "resume_surf", "start_surf", "stop_surf",
                          "next_channel", "previous_channel", "reload_current_stream"):
            self.assertNotIn(forbidden, calls)
        self.assertTrue(state["surfPaused"])
        self.assertEqual(state["currentChannelID"], self.a)

    def test_auto_commit_does_not_issue_device_commands(self):
        g = self.g
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.a, True)), \
                patch.object(g.prototype_selector, "device_command", side_effect=AssertionError("device command issued")):
            result = g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese",
                                            0.97, "auto:language_pt_dominant", self.evidence("pt"))
        self.assertEqual(result["reason"], "deferred_paused")

    def test_non_excluded_outcome_is_recorded_without_playback_action(self):
        g = self.g
        evidence = self.evidence("en")
        evidence["outcome"] = "allowed"
        with patch.object(g, "device_rotation_state", return_value=self.rotation(self.a, True)), \
                patch.object(g.prototype_selector, "device_command", side_effect=AssertionError("device command issued")):
            result = g.language_auto_commit(self.a, g.sources[self.a], "A", "en", "English",
                                            0.97, "auto:allowed_language_en", evidence)
        self.assertTrue(result["changed"])
        state = g.spanish_classification.load_or_migrate(self.temp.name)
        self.assertEqual(g.spanish_classification.counts_for(state, self.a, "en"), 1)


if __name__ == "__main__":
    unittest.main()
