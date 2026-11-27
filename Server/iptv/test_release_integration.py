"""Release integration fixtures for the language overlay bridge.

These tests exercise the real gateway lifecycle: evidence commits enqueue a
bounded publisher job, the publisher re-probes the verified device channel,
and only the existing authorized ``ui_set_value`` command is emitted.  No
socket, provider, credential or live device is contacted.
"""
import importlib
import json
import os
import pathlib
import queue
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import overlay_bridge as ob
import programme_overlay as po

A, B, C = ("a" * 32, "b" * 32, "c" * 32)


class Recorder:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        self.attempts = []

    def __call__(self, config, token, action, arguments):
        self.attempts.append((action, arguments))
        if self.fail:
            raise RuntimeError("authorized transport requires authentication")
        self.calls.append((action, arguments))
        return "OK"


def evidence(language="pt", name="Portuguese", visit="visit-1", launch="launch-1",
             stream="stream-1", outcome="excluded"):
    return {"model": "fixture.bin", "windows": 4, "speechSeconds": 40.0,
            "sampleSeconds": 60.0, "speechCoverage": 0.66,
            "languages": [{"language": language, "probability": 0.97}],
            "deviceVerified": True, "channelChangedBeforeResult": False,
            "lexicalMargin": -0.4, "outcome": outcome, "visitID": visit,
            "launchID": launch, "streamGeneration": stream}


def v3_auto(provider, cid, language, at):
    return {
        "channelID": cid, "channelName": provider, "markedAt": at,
        "nonce": "auto-" + provider, "source": "auto", "language": language,
        "languageName": {"pt": "Portuguese", "fr": "French", "de": "German"}[language],
        "confidence": 0.95, "reason": "auto:language_" + language + "_dominant",
        "evidence": {"model": "fixture.bin", "windows": 4, "speechSeconds": 40.0,
                     "sampleSeconds": 60.0, "speechCoverage": 0.66,
                     "languages": [{"language": language, "probability": 0.95}],
                     "deviceVerified": True, "channelChangedBeforeResult": False,
                     "lexicalMargin": 0.4},
    }


def v3_manual(provider, cid, at):
    return {
        "channelID": cid, "channelName": provider, "markedAt": at,
        "nonce": "manual-" + provider, "source": "manual", "language": "es",
        "languageName": "Spanish", "confidence": None,
        "reason": "manual-spanish", "evidence": None,
    }


def v3_state(entries, decisions=None):
    return {"schemaVersion": 3, "revision": "language-exclusions-v3",
            "updatedAt": 20.0, "lastNonce": "", "recentNonces": [],
            "entries": entries, "decisions": decisions or {}}


class ReleaseIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = {"JEALOUSFIN_STATE_DIR": self.temp.name,
               "JEALOUSFIN_USERNAME": "fixture", "JEALOUSFIN_PASSWORD": "fixture",
               "JEALOUSFIN_TOKEN": "fixture-token",
               "JEALOUSFIN_SERVER_ID": "fixture-server",
               "JEALOUSFIN_BIND": "127.0.0.1"}
        with patch.dict(os.environ, env):
            import gateway
            self.g = importlib.reload(gateway)
        # These tests exercise the language overlay only. Never construct the
        # combined overlay's real authorized transport (no socket access).
        overlay_patch = patch.object(self.g, "programme_overlay_instance", return_value=None)
        overlay_patch.start()
        self.addCleanup(overlay_patch.stop)
        for cid in (A, B, C):
            self.g.channels[cid] = {"Id": cid, "Name": "Fixture " + cid[0].upper(),
                                    "Type": "TvChannel"}
            self.g.sources[cid] = "nltv:fixture:" + cid
        self.db = pathlib.Path(self.temp.name) / "policy.sqlite3"
        with self.g.policy_connect(self.db) as db:
            db.executemany("INSERT INTO channel(id,name) VALUES(?,?)",
                           [(cid, cid) for cid in (A, B, C)])
            db.execute("INSERT INTO meta VALUES('seeded','1')")
        self.g.set_language_auto_control(enabled=True, enforcement=False, threshold=25)

    def selector(self):
        config = {"schemaVersion": 1, "deviceIP": "192.168.1.10", "backendPort": 18797}
        (pathlib.Path(self.temp.name) / "prototype-source-selector.json").write_text(
            json.dumps(config))
        return config

    def install_bridge(self, recorder, max_retries=2):
        config = self.selector()
        transport = ob.AuthorizedDeviceTransport(
            config, "fixture-device-token", command=recorder)
        bridge = ob.OverlayBridge(transport, min_interval=0.0,
                                  max_retries=max_retries, retry_base=0.001)
        self.g.overlay_bridge_state = bridge
        self.g.overlay_bridge_fingerprint = (config["deviceIP"], config["backendPort"])
        self.g.overlay_bridge_fault = ""
        return bridge

    def probe(self, cid, stream="stream-1"):
        return {"channelID": cid, "launchID": "fixture-launch",
                "streamGeneration": stream}

    def drain(self):
        stop = threading.Event()
        results = []
        while True:
            try:
                job = self.g.overlay_jobs.get_nowait()
            except queue.Empty:
                break
            results.append(self.g._overlay_process(job, stop))
        return results

    def test_transport_emits_exact_command_schema_and_receipt(self):
        captured = {}

        def read_response(host, port, path, method="GET", headers=None, body=None,
                          limit=None, timeout=None):
            captured.update(host=host, port=port, path=path, method=method,
                            headers=headers or {}, body=body)
            return 200, json.dumps({"status": "succeeded", "result": "OK"}).encode(), {}

        config = {"deviceIP": "192.168.1.10", "backendPort": 18797}
        with patch.object(ob.prototype_selector, "read_response", side_effect=read_response):
            ob.AuthorizedDeviceTransport(config, "fixture-device-token").set_value(
                "languageOverlay", "fixture label")
        self.assertEqual(captured["path"], "/command")
        self.assertEqual(captured["method"], "POST")
        command = json.loads(captured["body"])
        self.assertEqual(set(command), {"schemaVersion", "id", "action", "arguments"})
        self.assertEqual(command["schemaVersion"], 1)
        self.assertTrue(command["id"].startswith("ui_set_value-"))
        self.assertEqual(command["action"], "ui_set_value")
        self.assertEqual(command["arguments"], {"key": "form.languageOverlay",
                                                "value": "fixture label"})
        self.assertEqual(captured["headers"]["Authorization"],
                         "Bearer fixture-device-token")

    def test_evidence_commit_publishes_exact_authorized_command(self):
        recorder = Recorder()
        self.install_bridge(recorder)
        with patch.object(self.g, "device_language_probe",
                          return_value=self.probe(A)):
            info = self.g.language_auto_commit(
                A, self.g.sources[A], "Fixture A", "pt", "Portuguese", 0.97,
                "auto:language_pt_dominant", evidence(), respect_pause=False)
            self.assertTrue(info["changed"])
            results = self.drain()
        self.assertIn("published", results)
        self.assertTrue(recorder.calls)
        self.assertTrue(all(action == "ui_set_value" for action, _ in recorder.calls))
        action, arguments = recorder.calls[-1]
        self.assertEqual(action, "ui_set_value")
        self.assertEqual(arguments["key"], "form.languageOverlay")
        value = arguments["value"]
        self.assertIn("Fixture A", value)
        self.assertIn("Portuguese 1/25", value)
        self.assertRegex(value, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        self.assertLessEqual(len(value.encode("utf-8")), ob.MAX_TEXT)
        self.assertEqual(self.g.overlay_status()["channelID"], A)

    def test_channel_change_and_stream_generation_suppress_stale_evidence(self):
        recorder = Recorder()
        self.install_bridge(recorder)
        with patch.object(self.g, "device_language_probe",
                          return_value=self.probe(A)):
            self.g.language_auto_commit(
                A, self.g.sources[A], "Fixture A", "pt", "Portuguese", 0.97,
                "auto:language_pt_dominant", evidence(), respect_pause=False)
            self.drain()
        before = len(recorder.calls)
        with patch.object(self.g, "device_language_probe",
                          return_value=self.probe(B, "stream-2")):
            self.g.overlay_publish_evidence(
                A, "Fixture A", "pt", "Portuguese", 0.97, {"streamGeneration": "stream-1"},
                {"eventID": "evt-old-a", "count": 2, "threshold": 25,
                 "outcome": "excluded"})
            results = self.drain()
            status = self.g.overlay_status()
        self.assertEqual(results, ["suppressed_channel"])
        self.assertEqual(status["channelID"], B)
        self.assertFalse(status["staleLabel"])
        self.assertIn("Fixture B", recorder.calls[-1][1]["value"])
        self.assertIn("waiting for language evidence", recorder.calls[-1][1]["value"])
        self.assertNotIn("Fixture A", recorder.calls[-1][1]["value"])
        self.assertGreaterEqual(len(recorder.calls), before + 1)

        calls = len(recorder.calls)
        with patch.object(self.g, "device_language_probe",
                          return_value=self.probe(A, "stream-2")):
            self.g.overlay_publish_evidence(
                A, "Fixture A", "pt", "Portuguese", 0.97, {"streamGeneration": "stream-1"},
                {"eventID": "evt-stale-stream", "count": 3, "threshold": 25,
                 "outcome": "excluded"})
            results = self.drain()
        self.assertEqual(results, ["stale"])
        self.assertEqual(len(recorder.calls), calls)
        self.assertEqual(self.g.overlay_bridge_fault, "stale_stream_generation")

    def test_transport_auth_failure_is_bounded_and_never_exposes_token(self):
        recorder = Recorder(fail=True)
        self.install_bridge(recorder, max_retries=1)
        with patch.object(self.g, "device_language_probe",
                          return_value=self.probe(A)):
            self.g.language_auto_commit(
                A, self.g.sources[A], "Fixture A", "pt", "Portuguese", 0.97,
                "auto:language_pt_dominant", evidence(), respect_pause=False)
            results = self.drain()
        self.assertEqual(results, ["failed"])
        self.assertGreaterEqual(len(recorder.attempts), 1)
        self.assertLessEqual(len(recorder.attempts), 3)
        self.assertTrue(all(action == "ui_set_value" for action, _ in recorder.attempts))
        status = self.g.overlay_status()
        self.assertIn("transport_unavailable", status["fault"])
        self.assertNotIn("fixture-device-token", json.dumps(status))

    def test_missing_authorized_transport_fails_safe(self):
        (pathlib.Path(self.temp.name) / "prototype-source-selector.json").unlink(missing_ok=True)
        self.g.overlay_bridge_state = None
        self.g.overlay_bridge_fingerprint = None
        self.g.overlay_enqueue("refresh")
        self.assertEqual(self.drain(), ["unavailable"])
        self.assertEqual(self.g.overlay_bridge_fault, "authorized_transport_unavailable")
        self.assertFalse(self.g.overlay_status()["configured"])

    def test_sampler_state_callback_publishes_event_driven_overlay(self):
        recorder = Recorder()
        self.install_bridge(recorder)
        now = time.time()
        with patch.object(self.g, "device_language_probe",
                          return_value=self.probe(A)):
            self.assertTrue(self.g.language_state_callback({
                "channelID": A, "channelName": "Fixture A", "state": "collecting",
                "reason": "collecting", "sampleSeconds": 12.3, "targetSeconds": 55,
                "attempt": 1, "maxAttempts": 4, "surfPaused": True, "surfing": True,
                "visitID": "visit-1", "launchID": "launch-1",
                "streamGeneration": "stream-1", "observedAt": now}))
            results = self.drain()
        self.assertEqual(results, ["published"])
        self.assertIn("collecting 12.3s/55.0s", recorder.calls[-1][1]["value"])
        before = len(recorder.calls)
        with patch.object(self.g, "device_language_probe",
                          return_value=self.probe(A, "stream-2")):
            self.g.language_state_callback({
                "channelID": A, "channelName": "Fixture A", "state": "inconclusive",
                "reason": "sample_too_short", "sampleSeconds": 36.03,
                "targetSeconds": 40, "retrySeconds": 10,
                "streamGeneration": "stream-1", "observedAt": now + 1})
            results = self.drain()
        self.assertEqual(results, ["stale"])
        self.assertEqual(len(recorder.calls), before)
        self.assertEqual(self.g.overlay_bridge_fault, "stale_stream_generation")

    def test_stale_physical_visit_state_is_suppressed_after_departure(self):
        recorder = Recorder()
        self.install_bridge(recorder)
        ledger = self.g.language_visit_identity()
        first = ledger.observe(A, "fixture-launch", "stream-1", now=1000)
        now = time.time()
        with patch.object(self.g, "device_language_probe",
                          return_value=dict(self.probe(A), launchID="fixture-launch")):
            self.g.language_state_callback({
                "channelID": A, "channelName": "Fixture A", "state": "collecting",
                "sampleSeconds": 20, "targetSeconds": 55, "visitID": first["visitID"],
                "launchID": "fixture-launch", "streamGeneration": "stream-1",
                "observedAt": now})
            self.assertEqual(self.drain(), ["published"])
            ledger.observe(B, "fixture-launch", "stream-1", now=1001)
            returned = ledger.observe(A, "fixture-launch", "stream-1", now=1002)
            self.assertNotEqual(returned["visitID"], first["visitID"])
            before = len(recorder.calls)
            self.g.language_state_callback({
                "channelID": A, "channelName": "Fixture A", "state": "inconclusive",
                "reason": "sample_too_short", "sampleSeconds": 36.03,
                "visitID": first["visitID"], "launchID": "fixture-launch",
                "streamGeneration": "stream-1", "observedAt": now + 1})
            self.assertEqual(self.drain(), ["stale"])
        self.assertEqual(len(recorder.calls), before)
        self.assertEqual(self.g.overlay_bridge_fault, "stale_visit")

    def test_manual_correction_is_idempotent_and_publishes_english_count(self):
        recorder = Recorder()
        self.install_bridge(recorder)
        self.g.set_language_auto_control(enabled=True, enforcement=True, threshold=2)
        with patch.object(self.g, "device_language_probe",
                          return_value=self.probe(A)):
            self.assertFalse(self.g.language_auto_commit(
                A, self.g.sources[A], "Fixture A", "pt", "Portuguese", 0.97,
                "auto:language_pt_dominant", evidence(), respect_pause=False)["enforced"])
            result = self.g.language_auto_commit(
                A, self.g.sources[A], "Fixture A", "pt", "Portuguese", 0.97,
                "auto:language_pt_dominant", evidence(visit="visit-2", launch="launch-2"), respect_pause=False)
            self.assertTrue(result["enforced"])
            self.drain()
            marker = json.dumps({"schemaVersion": 2, "action": "mistaken_english",
                                 "channelID": A, "channelName": "Fixture A",
                                 "nonce": "mistake-fixture-1"})
            with patch.object(self.g.prototype_selector, "device_ui_trigger",
                              return_value=marker):
                self.g.apply_device_spanish_request({}, "")
                self.drain()
            state = self.g.spanish_classification.load_or_migrate(self.temp.name)
            english = self.g.spanish_classification.counts_for(state, A, "en")
            remaining = self.g.spanish_classification.counts_for(state, A, "pt")
            calls = len(recorder.calls)
            # A fresh nonce must not walk backwards to the older evidence.
            second = json.dumps({"schemaVersion": 2, "action": "mistaken_english",
                                 "channelID": A, "channelName": "Fixture A",
                                 "nonce": "mistake-fixture-2"})
            with patch.object(self.g.prototype_selector, "device_ui_trigger",
                              return_value=second):
                self.g.apply_device_spanish_request({}, "")
            self.assertEqual(self.drain(), [])
            self.assertEqual(len(recorder.calls), calls)
            state = self.g.spanish_classification.load_or_migrate(self.temp.name)
            self.assertEqual(self.g.spanish_classification.counts_for(state, A, "pt"),
                             remaining)
            self.assertEqual(remaining, 1)
        value = recorder.calls[-1][1]["value"]
        self.assertIn("English %d/2" % english, value)
        self.assertIn("corrected", value)
        self.assertTrue(all(action == "ui_set_value" for action, _ in recorder.calls))
        self.assertFalse(self.g.spanish_excluded(A))

    def test_three_legacy_auto_entries_migrate_to_one_event_and_restore_eligibility(self):
        entries = {
            "nltv:fixture:" + A: v3_auto("nltv:fixture:" + A, A, "pt", 10.0),
            "nltv:fixture:" + B: v3_auto("nltv:fixture:" + B, B, "fr", 11.0),
            "nltv:fixture:" + C: v3_auto("nltv:fixture:" + C, C, "de", 12.0),
        }
        manual_provider = "nltv:fixture:manual"
        entries[manual_provider] = v3_manual(manual_provider, "d" * 32, 13.0)
        self.g.sources["d" * 32] = manual_provider
        (pathlib.Path(self.temp.name) / "language-exclusions.json").write_text(
            json.dumps(v3_state(entries)))
        before = self.g.policy_rows()
        self.g.maybe_reload_spanish(force=True)
        state = self.g.spanish_state
        self.assertEqual(set(state["entries"]), {manual_provider})
        self.assertEqual(len(state["evidence"]["events"]), 3)
        for cid, language in ((A, "pt"), (B, "fr"), (C, "de")):
            self.assertEqual(self.g.spanish_classification.counts_for(state, cid, language), 1)
            self.assertFalse(self.g.spanish_excluded(cid))
        self.assertTrue(self.g.spanish_excluded("d" * 32))
        self.assertEqual(before, self.g.policy_rows())


class CombinedOverlayTests(unittest.TestCase):
    """Installed-IPA combined label while rotation is paused."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = {"JEALOUSFIN_STATE_DIR": self.temp.name,
               "JEALOUSFIN_USERNAME": "fixture", "JEALOUSFIN_PASSWORD": "fixture",
               "JEALOUSFIN_TOKEN": "fixture-token",
               "JEALOUSFIN_SERVER_ID": "fixture-server",
               "JEALOUSFIN_BIND": "127.0.0.1"}
        with patch.dict(os.environ, env):
            import gateway
            self.g = importlib.reload(gateway)
        for cid, name in ((A, "Nurse Jackie"), (B, "Another Show")):
            self.g.channels[cid] = {"Id": cid, "Name": name, "Type": "TvChannel"}
            self.g.sources[cid] = "nltv:fixture:" + cid

    class Transport:
        def __init__(self):
            self.calls = []
            self.fail_fields = set()

        def set_value(self, field, value):
            self.calls.append((field, value))
            if field in self.fail_fields:
                raise RuntimeError("transport down")

    def publisher(self, transport, retries=1):
        return po.ProgrammeOverlayPublisher(transport, max_retries=retries)

    def probe(self, cid, stream="stream-1"):
        return {"channelID": cid, "launchID": "launch-1",
                "streamGeneration": stream, "surfPaused": True, "surfing": True,
                "playing": True}

    def row(self, series="Nurse Jackie", episode="Pilot", year=2009):
        return {"title": series, "episodeTitle": episode, "seasonNumber": 1,
                "episodeNumber": 1, "episodeYear": year,
                "episodeSource": "local-audio-episode-identity",
                "source": "provider-catalogue"}

    def evidence(self, at=999.0):
        return {"eventID": "evt-1", "at": at, "language": "en",
                "languageName": "English", "count": 1, "threshold": 25,
                "phase": "allowed", "confidence": 0.9769}

    def seed_visit(self, cid, stream="stream-1"):
        return self.g.language_visit_identity().observe(cid, "launch-1", stream)["visitID"]

    def test_paused_rotation_publishes_new_combined_evidence_with_scope_guards(self):
        transport = self.Transport()
        publisher = self.publisher(transport)
        self.seed_visit(A)
        with patch.object(self.g, "programme_overlay_instance", return_value=publisher), \
                patch.object(self.g, "device_language_probe", return_value=self.probe(A)), \
                patch.object(self.g, "programme_snapshot", return_value={A: self.row()}):
            result = self.g.overlay_combined_publish(
                A, "Nurse Jackie", probe=self.probe(A),
                evidence=self.evidence(), now=1000.0)
        self.assertEqual(result, "published")
        self.assertEqual(transport.calls, [
            (po.READY_FIELD, po.FALSE),
            (po.FALLBACK_FIELD, po.TRUE),
            (po.FIELD, ""),
            (po.FIELD, "Nurse Jackie — S01E01: Pilot (2009) English — 1/25 — 97.7%"),
            (po.READY_FIELD, po.TRUE),
            (po.FALLBACK_FIELD, po.FALSE),
        ])
        # A probe for another channel can never rewrite the paused label.
        before = list(transport.calls)
        with patch.object(self.g, "programme_overlay_instance", return_value=publisher):
            self.assertEqual(self.g.overlay_combined_publish(
                A, "Nurse Jackie", probe=self.probe(B)), "suppressed_channel")
        self.assertEqual(transport.calls, before)

    def test_scope_transition_replaces_old_label_and_prefers_fallback_on_failure(self):
        transport = self.Transport()
        publisher = self.publisher(transport)
        self.seed_visit(A)
        with patch.object(self.g, "programme_overlay_instance", return_value=publisher), \
                patch.object(self.g, "device_language_probe", return_value=self.probe(A)), \
                patch.object(self.g, "programme_snapshot", return_value={A: self.row()}):
            self.assertEqual(self.g.overlay_combined_publish(
                A, "Nurse Jackie", probe=self.probe(A),
                evidence=self.evidence(), now=1000.0), "published")
        transport.calls.clear()
        self.seed_visit(B, "stream-2")
        with patch.object(self.g, "programme_overlay_instance", return_value=publisher), \
                patch.object(self.g, "device_language_probe",
                             return_value=self.probe(B, "stream-2")), \
                patch.object(self.g, "programme_snapshot", return_value={
                    B: self.row(series="Another Show", episode="Second", year=2020)}):
            result = self.g.overlay_combined_publish(
                B, "Another Show", probe=self.probe(B, "stream-2"),
                evidence=dict(self.evidence(at=1001.0), eventID="evt-2"), now=1002.0)
        self.assertEqual(result, "published")
        self.assertEqual(transport.calls[:3], [
            (po.READY_FIELD, po.FALSE),
            (po.FALLBACK_FIELD, po.TRUE),
            (po.FIELD, ""),
        ])
        self.assertNotIn("Nurse Jackie", " ".join(value for _field, value in transport.calls))
        self.assertIn("Another Show", transport.calls[-3][1])
        # If the value write fails, Ready is never enabled.
        transport.calls.clear()
        transport.fail_fields.add(po.FIELD)
        with patch.object(self.g, "programme_overlay_instance", return_value=publisher), \
                patch.object(self.g, "device_language_probe",
                             return_value=self.probe(B, "stream-2")), \
                patch.object(self.g, "programme_snapshot", return_value={B: self.row()}):
            self.assertEqual(self.g.overlay_combined_publish(
                B, "Another Show", probe=self.probe(B, "stream-2"),
                evidence=self.evidence(at=1003.0), now=1004.0), "failed")
        self.assertNotIn((po.READY_FIELD, po.TRUE), transport.calls)
        self.assertIn((po.FALLBACK_FIELD, po.TRUE), transport.calls)

    def test_bare_series_title_prefers_installed_fallback(self):
        transport = self.Transport()
        publisher = self.publisher(transport)
        self.seed_visit(A)
        with patch.object(self.g, "programme_overlay_instance", return_value=publisher), \
                patch.object(self.g, "device_language_probe", return_value=self.probe(A)), \
                patch.object(self.g, "programme_snapshot", return_value={A: self.row()}):
            self.assertEqual(self.g.overlay_combined_publish(
                A, "Nurse Jackie", probe=self.probe(A),
                evidence=self.evidence(), now=1000.0), "published")
        transport.calls.clear()
        with patch.object(self.g, "programme_overlay_instance", return_value=publisher), \
                patch.object(self.g, "device_language_probe", return_value=self.probe(A)), \
                patch.object(self.g, "programme_snapshot",
                             return_value={A: {"title": "Nurse Jackie (2009)"}}):
            self.assertEqual(self.g.overlay_combined_publish(
                A, "Nurse Jackie", probe=self.probe(A), evidence=None, now=1001.0),
                "fallback")
        self.assertEqual(transport.calls[:3], [
            (po.READY_FIELD, po.FALSE),
            (po.FALLBACK_FIELD, po.TRUE),
            (po.FIELD, ""),
        ])
        self.assertNotIn((po.READY_FIELD, po.TRUE), transport.calls)

    def test_status_reports_existing_native_abi(self):
        status = self.g.overlay_combined_status()
        self.assertEqual(status["layoutRevision"], "programme-language-existing-ipa-20261127")
        self.assertEqual(status["field"], "form.programmeOverlay")
        self.assertEqual(status["readyField"], "form.programmeOverlayReady")
        self.assertEqual(status["fallbackField"], "form.programmeOverlayFallback")


class StagedLayoutTests(unittest.TestCase):
    def test_final_compact_night_layout_preserves_correction_and_presentation(self):
        path = HERE.parent.parent / "Examples" / "three-panel-night-fit-language-evidence-20261127.json"
        layout = json.loads(path.read_text())
        self.assertEqual(layout["revision"], "three-panel-night-fit-language-evidence-20261127")
        presentation = layout["presentation"]
        self.assertEqual(presentation["fullscreenControlTheme"], "night")
        self.assertEqual(presentation["fullscreenOpacity"], 0.35)
        self.assertEqual(presentation["fullscreenButtonHeight"], 44)
        self.assertEqual(presentation["fullscreenIconSize"], 24)
        self.assertTrue(presentation["fullscreenFitContent"])
        nodes = []

        def walk(items):
            for node in items:
                nodes.append(node)
                walk(node.get("children", []))

        for slot in layout["slots"]:
            walk(slot["nodes"])
        by_id = {node["id"]: node for node in nodes}
        self.assertIn("prototype-source-switcher", by_id)
        self.assertEqual(by_id["prototype-source-switcher"]["kind"], "web")
        hidden = by_id["language-overlay-value"]
        label = by_id["language-evidence"]
        self.assertEqual(hidden["kind"], "input")
        self.assertEqual(label["kind"], "text")
        self.assertEqual(hidden["valueKey"], "form.languageOverlay")
        self.assertEqual(label["valueKey"], "form.languageOverlay")
        correction = by_id["language-mistaken"]["action"]["arguments"]["source"]
        self.assertIn("mistaken_english", correction)
        self.assertIn('schemaVersion:2', correction)
        self.assertNotIn("next_channel", correction)
        self.assertIn("schemaVersion:1", by_id["spanish-mark"]["action"]["arguments"]["source"])
        self.assertIn('action:"add"', by_id["spanish-mark"]["action"]["arguments"]["source"])
        self.assertIn('action:"remove"', by_id["spanish-unmark"]["action"]["arguments"]["source"])
        html = " ".join(node.get("html", "") for node in nodes)
        self.assertNotIn("fetch(", html)
        self.assertNotIn("XMLHttpRequest", html)
        commands = {node.get("action", {}).get("command") for node in nodes if node.get("action")}
        self.assertLessEqual(commands, {"script"})


if __name__ == "__main__":
    unittest.main()
