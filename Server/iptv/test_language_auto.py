"""Generalized language-exclusion acceptance.

Covers the explicit whisper.cpp code mapping, v3 store migration/provenance,
manual correction, race safety, gateway wiring and real local multilingual
TTS fixtures (Spanish, Portuguese, French, German, English, Mandarin, Korean,
Japanese, mixed and silence). No provider, live device, URL or credential is
used.
"""
import array
import json
import math
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import language_catalog
import language_exclusions
import language_audio
import language_visits
import spanish_audio as base


def pcm_bursts(seconds=20.0, amplitude=0.18):
    samples = []
    period = 0.8
    for index in range(int(seconds * base.RATE)):
        phase = (index / base.RATE) % period
        level = amplitude if phase < 0.5 else 0.004
        samples.append(int(32767 * level * math.sin(2 * math.pi * 180 * index / base.RATE)))
    return array.array("h", samples).tobytes()


def window(language, probability, usable=True):
    return {"usable": usable, "language": language, "probability": probability,
            "speechSeconds": 8.0, "speechRatio": 0.55, "duration": 15.0, "start": 0.0}


class LanguageCatalogTests(unittest.TestCase):
    def test_supported_and_policy_sets(self):
        required = {"es", "pt", "fr", "de", "it", "nl", "pl", "cs", "sk", "hu", "ro",
                    "bg", "el", "ru", "uk", "be", "sv", "no", "da", "fi", "is", "et",
                    "lv", "lt", "hr", "sr", "bs", "sl", "sq", "mk", "ca", "gl", "eu",
                    "cy", "mt", "br", "oc", "fo", "nn", "lb"}
        self.assertTrue(required <= language_catalog.EXCLUDED)
        self.assertEqual(language_catalog.ALLOWED, {"en", "zh", "yue", "ko"})
        self.assertNotIn("ga", language_catalog.SUPPORTED)  # not in whisper.cpp 1.9.5
        for code in ("ja", "ar", "tr", "id", "ms", "hi", "th"):
            self.assertEqual(language_catalog.classify(code), "unauthorized")
            self.assertNotIn(code, language_catalog.EXCLUDED)
        self.assertFalse(language_catalog.ALLOWED & language_catalog.EXCLUDED)
        self.assertTrue((language_catalog.ALLOWED | language_catalog.EXCLUDED) <= set(language_catalog.SUPPORTED))
        self.assertEqual(language_catalog.classify("es"), "excluded")
        self.assertEqual(language_catalog.classify("pt"), "excluded")
        self.assertEqual(language_catalog.classify("zh"), "allowed")
        self.assertEqual(language_catalog.classify("yue"), "allowed")
        self.assertEqual(language_catalog.classify("ko"), "allowed")


class LanguageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)

    def test_migrates_legacy_spanish_entry_and_allow_decision(self):
        import spanish_classification as legacy
        provider = "nltv:us:fixture"
        cid = "a" * 32
        state = legacy.empty()
        state, _ = legacy.apply(state, "add", cid, "Fixture", provider, "m1", now=10)
        legacy.atomic_write(self.root, state)
        state, _ = legacy.apply(state, "remove", cid, "Fixture", provider, "m2", now=11)
        legacy.atomic_write(self.root, state)
        migrated = language_exclusions.load_or_migrate(self.root)
        self.assertEqual(migrated["schemaVersion"], 4)
        self.assertEqual(migrated["decisions"][provider]["decision"], "not_spanish")
        self.assertEqual(migrated["decisions"][provider]["language"], "all")
        self.assertTrue((self.root / "language-exclusions.json").exists())
        self.assertTrue((self.root / "spanish-classification.json").exists())

    def test_auto_records_exact_language_evidence_not_first_detection_exclusion(self):
        state = language_exclusions.empty()
        for code, name, cid in (("pt", "Portuguese", "a" * 32), ("fr", "French", "b" * 32),
                                ("de", "German", "c" * 32), ("es", "Spanish", "d" * 32)):
            state, changed, why = language_exclusions.apply_auto(
                state, "nltv:us:" + code, cid, code, code, name, 0.95,
                "auto:language_" + code + "_dominant", None)
            self.assertTrue(changed)
            self.assertEqual(why, "evidence_recorded")
        self.assertEqual(state["entries"], {})
        self.assertEqual(language_exclusions.describe(state)["evidence"]["languages"],
                         {"pt": 1, "fr": 1, "de": 1, "es": 1})
        latest = language_exclusions.latest_evidence(state, provider_id="nltv:us:pt")
        self.assertEqual(latest["language"], "pt")
        self.assertEqual(latest["languageName"], "Portuguese")
        self.assertNotEqual(latest["language"], "es")

    def test_manual_spanish_and_not_spanish_override_auto(self):
        state = language_exclusions.empty()
        provider = "nltv:us:fixture"
        cid = "a" * 32
        state, changed, reason = language_exclusions.apply_manual(state, "add", cid, "Fixture", provider, "m1", now=1)
        self.assertTrue(changed)
        self.assertEqual(state["entries"][provider]["language"], "es")
        blocked, changed, reason = language_exclusions.apply_auto(
            state, provider, cid, "Fixture", "pt", "Portuguese", 0.99,
            "auto:language_pt_dominant", None)
        self.assertFalse(changed)
        self.assertEqual(reason, "manual_exclusion")
        removed, changed, reason = language_exclusions.apply_manual(state, "remove", cid, "Fixture", provider, "m2", now=2)
        self.assertTrue(changed)
        self.assertNotIn(provider, removed["entries"])
        self.assertEqual(removed["decisions"][provider]["language"], "all")
        again, changed, reason = language_exclusions.apply_auto(
            removed, provider, cid, "Fixture", "pt", "Portuguese", 0.99,
            "auto:language_pt_dominant", None)
        self.assertFalse(changed)
        self.assertEqual(reason, "manual_not_spanish")
        same, changed, reason = language_exclusions.apply_manual(removed, "add", cid, "Fixture", provider, "m2", now=3)
        self.assertFalse(changed)
        self.assertEqual(reason, "replayed_nonce")

    def test_concurrent_generalized_writers_preserve_manual_allow(self):
        lock = threading.Lock()
        providers = ["nltv:us:%s-%d" % (code, index)
                     for index, code in enumerate(("pt", "pt", "fr", "de", "es", "it", "nl", "pl"))]

        def mutate(callback):
            with lock:
                state = language_exclusions.load_or_migrate(self.root)
                state, _, _ = callback(state)
                language_exclusions.atomic_write(self.root, state)

        def auto(provider, code, index):
            name = language_catalog.language_name(code)
            return lambda state: language_exclusions.apply_auto(
                state, provider, ("%032d" % index)[-32:], name, code, name, 0.95,
                "auto:language_" + code + "_dominant", None)

        threads = [threading.Thread(target=lambda p=provider, c=code, i=index: mutate(auto(p, c, i)))
                   for index, (provider, code) in enumerate(zip(providers, ("pt", "pt", "fr", "de", "es", "it", "nl", "pl")))]
        threads.append(threading.Thread(target=lambda: mutate(
            lambda state: language_exclusions.apply_manual(state, "remove", "0" * 32, providers[0], providers[0], "allow-1", now=time.time()))))
        threads.append(threading.Thread(target=lambda: mutate(
            lambda state: language_exclusions.apply_manual(state, "add", "1" * 32, "Manual", "nltv:us:manual", "manual-1", now=time.time()))))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        state = language_exclusions.load_or_migrate(self.root)
        self.assertNotIn(providers[0], state["entries"])
        self.assertEqual(state["decisions"][providers[0]]["decision"], "not_spanish")
        self.assertEqual(state["entries"]["nltv:us:manual"]["language"], "es")
        self.assertEqual(len(state["entries"]), 1)
        self.assertEqual(language_exclusions.counts_for(state, ("%032d" % 1)[-32:], "pt"), 1)
        self.assertGreaterEqual(language_exclusions.describe(state)["evidence"]["languages"].get("pt", 0), 1)

    def test_legacy_apply_returns_two_values(self):
        state = language_exclusions.empty()
        result = language_exclusions.apply(state, "add", "a" * 32, "Fixture", "nltv:us:x", "n1")
        self.assertEqual(len(result), 2)


class LanguageDecisionTests(unittest.TestCase):
    def setUp(self):
        self.analysis = {"duration": 60.0, "speechSeconds": 40.0, "coverage": 0.66}

    def test_excluded_allowed_unauthorized_matrix(self):
        for code in ("es", "pt", "fr", "de", "it", "nl", "pl", "cs", "sk", "hu", "ro",
                     "bg", "el", "ru", "uk", "be", "sv", "no", "da", "fi", "is", "et",
                     "lv", "lt", "hr", "sr", "bs", "sl", "sq", "mk", "ca", "gl", "eu",
                     "cy", "mt", "br", "oc", "fo", "nn", "lb"):
            decision = language_audio.decide_language(
                self.analysis, [window(code, 0.97)] * 4,
                (None, 0, 0) if code not in ("es", "pt") else ((0.5, 5, 1) if code == "es" else (-0.5, 1, 5)),
                code)
            self.assertEqual(decision["outcome"], "excluded", code)
            self.assertEqual(decision["language"], code)
        for code in ("en", "zh", "yue", "ko"):
            decision = language_audio.decide_language(self.analysis, [window(code, 0.97)] * 4, (None, 0, 0), code)
            self.assertEqual(decision["outcome"], "allowed", code)
        for code in ("ja", "ar", "tr", "hi", "th", "id", "ms"):
            decision = language_audio.decide_language(self.analysis, [window(code, 0.97)] * 4, (None, 0, 0), code)
            self.assertEqual(decision["outcome"], "unauthorized", code)
            self.assertNotIn(code, language_catalog.EXCLUDED)

    def test_ambiguous_mixed_silence_and_confusion_are_not_excluded(self):
        analyses = [
            (self.analysis, [window("es", 0.97), window("en", 0.97), window("es", 0.97), window("en", 0.97)], (0.0, 1, 1), "es"),
            (self.analysis, [window("pt", 0.97), window("fr", 0.97), window("pt", 0.97), window("fr", 0.97)], (None, 0, 0), None),
            ({"duration": 60.0, "speechSeconds": 8.0, "coverage": 0.1}, [window("es", 0.97)] * 4, (0.5, 5, 1), "es"),
            (self.analysis, [window("es", 0.70)] * 4, (0.5, 5, 1), "es"),
            (self.analysis, [window("es", 0.97)] * 4, (-0.8, 1, 9), "es"),
            (self.analysis, [window("pt", 0.97)] * 4, (0.8, 9, 1), "pt"),
            (self.analysis, [window("es", 0.97)] * 4, (0.5, 5, 1), "en"),
        ]
        for analysis, windows, lexical, full in analyses:
            decision = language_audio.decide_language(analysis, windows, lexical, full)
            self.assertEqual(decision["outcome"], "inconclusive", (windows, lexical, full))

    def test_legacy_decide_wrapper(self):
        es = language_audio.decide(self.analysis, [window("es", 0.97)] * 4, (0.5, 5, 1))
        pt = language_audio.decide(self.analysis, [window("pt", 0.97)] * 4, (-0.5, 1, 5))
        en = language_audio.decide(self.analysis, [window("en", 0.97)] * 4, (0.0, 0, 0))
        self.assertEqual(es["outcome"], "spanish")
        self.assertEqual(pt["outcome"], "non_spanish")
        self.assertEqual(en["outcome"], "non_spanish")


class LanguageSamplerTests(unittest.TestCase):
    def _run(self, language, detected_language, text):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        calls = []
        pcm = pcm_bursts(5.2)

        def on_exclusion(cid, provider, name, code, language_name, confidence, reason, evidence, session=None):
            calls.append({"cid": cid, "language": code, "name": language_name,
                          "confidence": confidence, "reason": reason})
            return {"changed": True, "reason": "auto_add"}

        with patch.object(base.Manager, "_check_tools", return_value=None), \
                patch.object(base, "decode_audio", return_value=pcm), \
                patch.object(base, "detect_language", side_effect=lambda *a, **k: (detected_language, 0.96)), \
                patch.object(base, "transcribe", side_effect=lambda *a, **k: (detected_language, text)):
            manager = language_audio.Manager(
                pathlib.Path(temp.name), "/tmp/fixture-model.bin", on_exclusion,
                lambda: True, probe=lambda: {"channelID": "a" * 32}, log=lambda message: None)
            manager.fault = None
            manager.start()
            try:
                for index in range(12):
                    manager.tap("a" * 32, "nltv:us:a", "Channel A", ("%032d" % index)[-32:],
                                "https://1.1.1.1/a.m3u8", b"segment", False)
                deadline = time.time() + 20
                while time.time() < deadline and not calls and manager.status()["counters"]["tested"] < 1:
                    time.sleep(0.1)
                return calls, manager.status()
            finally:
                manager.stop()

    def test_portuguese_is_stored_as_portuguese_not_spanish(self):
        calls, status = self._run("pt", "pt", "Olá, o governo anunciou novas medidas. Obrigado, você e os negócios.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["language"], "pt")
        self.assertEqual(calls[0]["name"], "Portuguese")
        self.assertEqual(status["counters"]["excluded"], 1)
        self.assertEqual(status["counters"]["spanish"], 0)
        self.assertEqual(status["languages"].get("pt"), 1)

    def test_spanish_is_stored_as_spanish(self):
        calls, status = self._run("es", "es", "Hola, el gobierno anunció nuevas medidas para las familias y los niños.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["language"], "es")
        self.assertEqual(status["counters"]["spanish"], 1)

    def test_english_is_allowed_and_recorded_without_whitelist(self):
        calls, status = self._run("en", "en", "Hello and welcome to the news. The government announced new measures.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["language"], "en")
        self.assertGreaterEqual(status["counters"]["allowed"], 1)
        self.assertGreaterEqual(status["counters"]["evidence"], 1)
        self.assertEqual(status["counters"]["excluded"], 0)


class LanguageGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = {"JEALOUSFIN_STATE_DIR": self.temp.name, "JEALOUSFIN_USERNAME": "fixture",
               "JEALOUSFIN_PASSWORD": "fixture", "JEALOUSFIN_TOKEN": "fixture-token",
               "JEALOUSFIN_SERVER_ID": "fixture-server", "JEALOUSFIN_BIND": "127.0.0.1"}
        with patch.dict(os.environ, env):
            import importlib
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
        self.g.set_language_auto_control(enabled=True, enforcement=False, threshold=25)

    def evidence(self, language="pt", language_name="Portuguese", visit="visit-1", launch="launch-1",
                 outcome="excluded", lexical=-0.4):
        return {"model": "fixture.bin", "windows": 4, "speechSeconds": 40.0, "sampleSeconds": 60.0,
                "speechCoverage": 0.66, "languages": [{"language": language, "probability": 0.97}],
                "deviceVerified": True, "channelChangedBeforeResult": False, "lexicalMargin": lexical,
                "outcome": outcome, "visitID": visit, "launchID": launch, "streamGeneration": "stream-1"}

    def test_auto_commit_records_exact_evidence_without_observation_mode_filter(self):
        g = self.g
        self.assertTrue(g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese", 0.97,
                                               "auto:language_pt_dominant", self.evidence(), respect_pause=False)["changed"])
        self.assertTrue(g.language_auto_commit(self.b, g.sources[self.b], "B", "es", "Spanish", 0.98,
                                               "auto:language_es_dominant", self.evidence("es", "Spanish", "visit-es", lexical=0.5), respect_pause=False)["changed"])
        g.maybe_reload_spanish(force=True)
        self.assertEqual(g.spanish_provider_ids, set())
        state = json.loads((pathlib.Path(self.temp.name) / "language-exclusions.json").read_text())
        self.assertEqual(state["entries"], {})
        self.assertEqual(g.spanish_classification.counts_for(state, self.a, "pt"), 1)
        self.assertEqual(g.spanish_classification.counts_for(state, self.b, "es"), 1)
        description = self.g.spanish_classification.describe(state)
        self.assertEqual(description["evidence"]["languages"], {"pt": 1, "es": 1})
        with self.g.policy_connect(self.db) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM channel").fetchone()[0], 3)

    def test_enforcement_threshold_adds_auto_entry_and_toggle_restores_eligibility(self):
        g = self.g
        g.set_language_auto_control(enabled=True, enforcement=True, threshold=2)
        evidence = self.evidence()
        self.assertFalse(g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese", 0.97,
                                                "auto:language_pt_dominant", evidence, respect_pause=False)["enforced"])
        second = self.evidence(visit="visit-2", launch="launch-2")
        result = g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese", 0.97,
                                        "auto:language_pt_dominant", second, respect_pause=False)
        self.assertTrue(result["enforced"])
        g.maybe_reload_spanish(force=True)
        self.assertIn(g.sources[self.a], g.spanish_provider_ids)
        self.assertTrue(g.spanish_excluded(self.a))
        with self.g.policy_connect(self.db) as db:
            before = db.execute("SELECT failures,hiatus_until,banned,sustained FROM channel WHERE id=?", (self.a,)).fetchone()
        g.set_language_auto_control(enabled=True, enforcement=False, threshold=2)
        g.maybe_reload_spanish(force=True)
        self.assertFalse(g.spanish_excluded(self.a))
        with self.g.policy_connect(self.db) as db:
            after = db.execute("SELECT failures,hiatus_until,banned,sustained FROM channel WHERE id=?", (self.a,)).fetchone()
        self.assertEqual(before, after)

    def test_manual_marker_path_and_not_spanish_blocks_auto(self):
        g = self.g
        provider = g.sources[self.a]
        add = json.dumps({"schemaVersion": 1, "action": "add", "channelID": self.a,
                          "channelName": "Fixture A", "nonce": "manual-add-1"})
        with patch.object(g.prototype_selector, "device_ui_trigger", return_value=add):
            g.apply_device_spanish_request({}, "")
        self.assertIn(provider, g.spanish_provider_ids)
        self.assertEqual(g.spanish_state["entries"][provider]["language"], "es")
        self.assertEqual(g.language_auto_commit(self.a, provider, "A", "pt", "Portuguese", 0.97,
                                                "auto:language_pt_dominant", self.evidence(), respect_pause=False)["reason"], "manual_exclusion")
        remove = json.dumps({"schemaVersion": 1, "action": "remove", "channelID": self.a,
                             "channelName": "Fixture A", "nonce": "manual-remove-1"})
        with patch.object(g.prototype_selector, "device_ui_trigger", return_value=remove):
            g.apply_device_spanish_request({}, "")
        self.assertNotIn(provider, g.spanish_provider_ids)
        self.assertEqual(g.language_auto_commit(self.a, provider, "A", "pt", "Portuguese", 0.97,
                                                "auto:language_pt_dominant", self.evidence("pt", "Portuguese", "visit-2", "launch-2"), respect_pause=False)["reason"], "manual_not_spanish")

    def test_control_endpoint_preserves_evidence_and_manual_exclusions(self):
        g = self.g
        provider = g.sources[self.a]
        manual_provider = g.sources[self.b]
        add = json.dumps({"schemaVersion": 1, "action": "add", "channelID": self.b,
                          "channelName": "Fixture B", "nonce": "manual-b-1"})
        with patch.object(g.prototype_selector, "device_ui_trigger", return_value=add):
            g.apply_device_spanish_request({}, "")
        g.language_auto_commit(self.a, provider, "A", "pt", "Portuguese", 0.97,
                               "auto:language_pt_dominant", self.evidence(), respect_pause=False)
        g.set_spanish_auto_enabled(False)
        self.assertFalse(g.reload_spanish_auto_control(force=True))
        self.assertIn(manual_provider, g.spanish_provider_ids)
        self.assertNotIn(provider, g.spanish_provider_ids)
        g.set_spanish_auto_enabled(True)
        self.assertTrue(g.reload_spanish_auto_control(force=True))
        state = json.loads((pathlib.Path(self.temp.name) / "language-exclusions.json").read_text())
        self.assertEqual(g.spanish_classification.counts_for(state, self.a, "pt"), 1)

    def test_legacy_auto_entries_restore_eligibility_without_touching_policy(self):
        g = self.g
        before = g.policy_rows()
        entries = {
            "nltv:fixture:" + self.a: v3_auto("nltv:fixture:" + self.a, self.a, "pt", 10.0),
            "nltv:fixture:" + self.b: v3_auto("nltv:fixture:" + self.b, self.b, "fr", 11.0),
            "nltv:fixture:" + self.c: v3_manual("nltv:fixture:" + self.c, self.c, 12.0),
        }
        decisions = {
            "nltv:fixture:" + self.a: {"decision": "spanish", "language": "es", "languageName": "Spanish",
                                       "at": 10.0, "nonce": "auto-a", "channelID": self.a, "channelName": "A"},
            "nltv:fixture:" + self.c: {"decision": "spanish", "language": "es", "languageName": "Spanish",
                                       "at": 12.0, "nonce": "manual-c", "channelID": self.c, "channelName": "C"},
        }
        (pathlib.Path(self.temp.name) / "language-exclusions.json").write_text(
            json.dumps(v3_state(entries, decisions)))
        g.maybe_reload_spanish(force=True)
        self.assertFalse(g.spanish_excluded(self.a))
        self.assertFalse(g.spanish_excluded(self.b))
        self.assertTrue(g.spanish_excluded(self.c))
        self.assertEqual(set(g.spanish_state["entries"]), {"nltv:fixture:" + self.c})
        self.assertEqual(len(g.spanish_state["evidence"]["events"]), 2)
        self.assertEqual(before, g.policy_rows())

    def test_observation_defaults_off_and_threshold_is_runtime_adjustable(self):
        g = self.g
        g.SPANISH_AUTO_CONTROL_FILE.unlink()
        self.assertFalse(g.reload_spanish_auto_control(force=True))
        self.assertFalse(g.SPANISH_AUTO_ENFORCEMENT)
        self.assertEqual(g.SPANISH_EVIDENCE_THRESHOLD, 25)
        self.assertEqual(g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese", 0.97,
                                                "auto:language_pt_dominant", self.evidence(), respect_pause=False)["reason"], "observation_disabled")
        control = g.set_language_auto_control(enabled=True, enforcement=False, threshold=13)
        self.assertEqual(control["threshold"], 13)
        self.assertTrue(g.reload_spanish_auto_control(force=True))
        self.assertEqual(g.SPANISH_EVIDENCE_THRESHOLD, 13)
        with self.assertRaises(ValueError):
            g.set_language_auto_control(threshold=0)

    def test_maintenance_window_skips_language_evidence(self):
        g = self.g
        now = time.time()
        g.MAINTENANCE_FILE.write_text(json.dumps({
            "schemaVersion": 1, "start": now - 1, "until": now + 120,
            "reason": "language fixture maintenance", "launchID": "fixture-launch",
            "channelID": self.a, "allowedReasons": ["fixture"],
        }))
        g.maintenance_state = None
        g.maintenance_checked_at = 0.0
        result = g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese", 0.97,
                                        "auto:language_pt_dominant", self.evidence(), respect_pause=False)
        self.assertEqual(result["reason"], "maintenance")
        state = g.spanish_classification.load_or_migrate(self.temp.name)
        self.assertEqual(g.spanish_classification.counts_for(state, self.a, "pt"), 0)

    def test_mistaken_english_retracts_once_without_playback_or_policy_change(self):
        g = self.g
        g.set_language_auto_control(enabled=True, enforcement=True, threshold=1)
        provider = g.sources[self.a]
        self.assertTrue(g.language_auto_commit(self.a, provider, "A", "pt", "Portuguese", 0.97,
                                               "auto:language_pt_dominant", self.evidence(), respect_pause=False)["enforced"])
        self.assertTrue(g.spanish_excluded(self.a))
        before = g.policy_rows()
        marker = json.dumps({"schemaVersion": 2, "action": "mistaken_english", "channelID": self.a,
                             "channelName": "Fixture A", "nonce": "mistake-gateway-1"})
        with patch.object(g.prototype_selector, "device_ui_trigger", return_value=marker):
            g.apply_device_spanish_request({}, "")
        self.assertFalse(g.spanish_excluded(self.a))
        state = json.loads((pathlib.Path(self.temp.name) / "language-exclusions.json").read_text())
        self.assertEqual(g.spanish_classification.counts_for(state, self.a, "pt"), 0)
        self.assertEqual(g.spanish_classification.counts_for(state, self.a, "en"), 1)
        self.assertNotIn(provider, state["decisions"])
        self.assertEqual(before, g.policy_rows())
        with patch.object(g.prototype_selector, "device_ui_trigger", return_value=marker):
            g.apply_device_spanish_request({}, "")
        state = json.loads((pathlib.Path(self.temp.name) / "language-exclusions.json").read_text())
        self.assertEqual(g.spanish_classification.counts_for(state, self.a, "pt"), 0)
        self.assertEqual(g.spanish_classification.counts_for(state, self.a, "en"), 1)


def v3_auto(provider, cid, language, at, confidence=0.95):
    return {
        "channelID": cid, "channelName": provider, "markedAt": at,
        "nonce": "auto-" + provider, "source": "auto", "language": language,
        "languageName": language_catalog.language_name(language), "confidence": confidence,
        "reason": "auto:language_" + language + "_dominant",
        "evidence": {"model": "fixture.bin", "windows": 4, "speechSeconds": 40.0,
                     "sampleSeconds": 60.0, "speechCoverage": 0.66,
                     "languages": [{"language": language, "probability": confidence}],
                     "deviceVerified": True, "channelChangedBeforeResult": False,
                     "lexicalMargin": 0.4},
    }


def v3_manual(provider, cid, at):
    return {
        "channelID": cid, "channelName": provider, "markedAt": at,
        "nonce": "manual-" + provider, "source": "manual", "language": "es",
        "languageName": "Spanish", "confidence": None, "reason": "manual-spanish",
        "evidence": None,
    }


def v3_state(entries, decisions=None):
    return {
        "schemaVersion": 3, "revision": "language-exclusions-v3", "updatedAt": 100.0,
        "lastNonce": "nonce-1", "recentNonces": ["nonce-1"],
        "entries": entries, "decisions": decisions or {},
    }


class LanguageEvidenceGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)

    def event(self, state, channel, language, name, visit, launch, now, threshold=25,
              enforcement=False, outcome="excluded"):
        return language_exclusions.record_evidence(
            state, "nltv:us:" + channel, channel, channel, language, name, 0.97,
            "auto:language_" + language + "_dominant", {"deviceVerified": True,
            "channelChangedBeforeResult": False, "model": "fixture.bin", "windows": 4},
            outcome=outcome, visit_id=visit, launch_id=launch, now=now,
            threshold=threshold, enforcement=enforcement)

    def test_three_legacy_auto_records_migrate_to_one_event_each_and_restore_eligibility(self):
        a, b, c, manual, allowed = ("a" * 32, "b" * 32, "c" * 32, "d" * 32, "e" * 32)
        entries = {
            "nltv:us:a": v3_auto("nltv:us:a", a, "pt", 10.0),
            "nltv:us:b": v3_auto("nltv:us:b", b, "fr", 11.0),
            "nltv:us:c": v3_auto("nltv:us:c", c, "de", 12.0),
            "nltv:us:manual": v3_manual("nltv:us:manual", manual, 13.0),
        }
        decisions = {
            "nltv:us:a": {"decision": "spanish", "language": "es", "languageName": "Spanish",
                          "at": 10.0, "nonce": "auto-a", "channelID": a, "channelName": "A"},
            "nltv:us:manual": {"decision": "spanish", "language": "es", "languageName": "Spanish",
                               "at": 13.0, "nonce": "manual-manual", "channelID": manual, "channelName": "Manual"},
            "nltv:us:allowed": {"decision": "not_spanish", "language": "all", "languageName": "Any language",
                                "at": 14.0, "nonce": "allow-e", "channelID": allowed, "channelName": "Allowed"},
        }
        state = language_exclusions.migrate(v3_state(entries, decisions))
        self.assertEqual(state["schemaVersion"], 4)
        self.assertEqual(set(state["entries"]), {"nltv:us:manual"})
        self.assertEqual(language_exclusions.manual_excluded(state), {"nltv:us:manual"})
        self.assertEqual(language_exclusions.auto_excluded(state), set())
        self.assertEqual(len(state["evidence"]["events"]), 3)
        for provider, cid, code in (("nltv:us:a", a, "pt"), ("nltv:us:b", b, "fr"), ("nltv:us:c", c, "de")):
            self.assertEqual(language_exclusions.counts_for(state, cid, code), 1)
            latest = language_exclusions.latest_evidence(state, provider_id=provider)
            self.assertEqual(latest["language"], code)
            self.assertIsNotNone(latest["at"])
        self.assertNotIn("nltv:us:a", state["decisions"])
        self.assertEqual(state["decisions"]["nltv:us:manual"]["decision"], "spanish")
        self.assertEqual(state["decisions"]["nltv:us:allowed"]["decision"], "not_spanish")
        language_exclusions.atomic_write(self.root, state)
        reloaded = language_exclusions.load_or_migrate(self.root)
        self.assertEqual(reloaded, state)

    def test_english_is_evidence_not_a_permanent_whitelist(self):
        state = language_exclusions.empty()
        for index in range(2):
            state, info = self.event(state, "a" * 32, "en", "English", "en-" + str(index),
                                     "launch-" + str(index), 1 + index, outcome="allowed")
            self.assertTrue(info["changed"])
        self.assertEqual(state["decisions"], {})
        self.assertEqual(language_exclusions.counts_for(state, "a" * 32, "en"), 2)
        for index in range(25):
            state, info = self.event(state, "a" * 32, "es", "Spanish", "es-" + str(index),
                                     "launch-es-" + str(index), 10 + index,
                                     threshold=25, enforcement=True)
        self.assertTrue(info["enforced"])
        self.assertEqual(language_exclusions.counts_for(state, "a" * 32, "es"), 25)
        self.assertEqual(state["entries"]["nltv:us:" + "a" * 32]["source"], "auto")
        self.assertNotIn("nltv:us:" + "a" * 32, state["decisions"])
        with self.assertRaises(ValueError):
            language_exclusions.record_evidence(
                state, "nltv:us:" + "a" * 32, "a" * 32, "A", "es", "Spanish", 0.97,
                "auto:inconclusive", {}, outcome="inconclusive", now=200)

    def test_threshold_observation_only_then_enforcement_and_manual_allow(self):
        state = language_exclusions.empty()
        channel = "a" * 32
        for index in range(25):
            state, info = self.event(state, channel, "es", "Spanish", "v-" + str(index),
                                     "l-" + str(index), 100 + index, threshold=25, enforcement=False)
            self.assertFalse(info["enforced"])
        self.assertEqual(state["entries"], {})
        self.assertEqual(language_exclusions.counts_for(state, channel, "es"), 25)
        state, info = self.event(state, channel, "es", "Spanish", "v-25", "l-25", 200,
                                 threshold=25, enforcement=True)
        self.assertTrue(info["enforced"])
        self.assertEqual(state["entries"]["nltv:us:" + channel]["source"], "auto")
        state, changed, reason = language_exclusions.apply_manual(
            state, "remove", channel, "A", "nltv:us:" + channel, "allow-after", now=300)
        self.assertTrue(changed)
        self.assertNotIn("nltv:us:" + channel, state["entries"])
        state, info = self.event(state, channel, "es", "Spanish", "v-26", "l-26", 400,
                                 threshold=25, enforcement=True)
        self.assertFalse(info["changed"])
        self.assertEqual(info["reason"], "manual_not_spanish")
        self.assertEqual(language_exclusions.counts_for(state, channel, "es"), 26)
        for bad in (0, 1001, "25"):
            with self.assertRaises(ValueError):
                self.event(state, "b" * 32, "es", "Spanish", "bad", "bad", 500,
                           threshold=bad, enforcement=False)

    def test_mistaken_english_retracts_once_without_whitelist(self):
        state = language_exclusions.empty()
        channel = "a" * 32
        provider = "nltv:us:" + channel
        for index in range(2):
            state, info = self.event(state, channel, "es", "Spanish", "v-" + str(index),
                                     "l-" + str(index), 10 + index, threshold=2, enforcement=True)
        self.assertTrue(info["enforced"])
        self.assertIn(provider, state["entries"])
        original_event = info["eventID"]
        state, info = language_exclusions.retract_evidence(
            state, provider, channel, "A", "mistake-1", now=20, threshold=2, enforcement=True)
        self.assertTrue(info["changed"])
        self.assertEqual(info["retractedEventID"], original_event)
        self.assertNotIn(provider, state["entries"])
        self.assertEqual(language_exclusions.counts_for(state, channel, "es"), 1)
        self.assertEqual(language_exclusions.counts_for(state, channel, "en"), 1)
        self.assertNotIn(provider, state["decisions"])
        self.assertEqual(state["evidence"]["events"][original_event]["retractedBy"], "mistake-1")
        self.assertEqual(language_exclusions.latest_evidence(state, provider_id=provider)["language"], "en")
        replay, info = language_exclusions.retract_evidence(
            state, provider, channel, "A", "mistake-1", now=21, threshold=2, enforcement=True)
        self.assertFalse(info["changed"])
        self.assertEqual(info["reason"], "replayed_nonce")
        self.assertEqual(replay, state)
        same_latest, info = language_exclusions.retract_evidence(
            state, provider, channel, "A", "mistake-fresh-nonce", now=22,
            threshold=2, enforcement=True)
        self.assertFalse(info["changed"])
        self.assertEqual(info["reason"], "already_corrected")
        self.assertEqual(language_exclusions.counts_for(same_latest, channel, "es"), 1)
        state, info = self.event(state, channel, "es", "Spanish", "v-2", "l-2", 30,
                                 threshold=2, enforcement=True)
        self.assertTrue(info["enforced"])
        self.assertIn(provider, state["entries"])
        manual = language_exclusions.empty()
        manual, _, _ = language_exclusions.apply_manual(manual, "add", "b" * 32, "B", "nltv:us:b", "m-1", now=1)
        manual, info = language_exclusions.retract_evidence(
            manual, "nltv:us:b", "b" * 32, "B", "mistake-2", now=2, threshold=2, enforcement=True)
        self.assertFalse(info["changed"])
        self.assertEqual(info["reason"], "no_evidence")
        self.assertIn("nltv:us:b", manual["entries"])

    def test_visit_dedup_and_stale_session_identity(self):
        state = language_exclusions.empty()
        channel = "a" * 32
        visit_one = language_visits.new_visit_id()
        visit_two = language_visits.new_visit_id()
        visit_three = language_visits.new_visit_id()
        state, first = self.event(state, channel, "es", "Spanish", visit_one, "launch-1", 1000)
        state, duplicate = self.event(state, channel, "es", "Spanish", visit_one, "launch-1", 1001)
        self.assertTrue(first["changed"])
        self.assertFalse(duplicate["changed"])
        self.assertEqual(duplicate["reason"], "duplicate_visit")
        state, revisit = self.event(state, channel, "es", "Spanish", visit_two, "launch-1", 1002)
        self.assertTrue(revisit["changed"])
        state, new_session = self.event(state, channel, "es", "Spanish", visit_three, "launch-2", 1003)
        self.assertTrue(new_session["changed"])
        self.assertEqual(language_exclusions.counts_for(state, channel, "es"), 3)
        manager = object.__new__(language_audio.Manager)
        job = {"cid": channel, "launchID": "launch-1", "streamGeneration": "stream-1"}
        self.assertTrue(manager._identity_matches(job, {"channelID": channel, "launchID": "launch-1", "streamGeneration": "stream-1"}))
        self.assertFalse(manager._identity_matches(job, {"channelID": "b" * 32, "launchID": "launch-1", "streamGeneration": "stream-1"}))
        self.assertFalse(manager._identity_matches(job, {"channelID": channel, "launchID": "launch-2", "streamGeneration": "stream-1"}))
        self.assertFalse(manager._identity_matches(job, {"channelID": channel, "launchID": "launch-1", "streamGeneration": "stream-2"}))
        self.assertFalse(manager._identity_matches(job, None))

    def test_manager_skips_stale_and_inconclusive_samples_without_counts(self):
        manager = object.__new__(language_audio.Manager)
        calls = []
        manager.fault = None
        manager.control = lambda: True
        manager._control_cache = (time.monotonic(), True)
        manager.probe = lambda: {"channelID": "a" * 32, "launchID": "launch-1", "streamGeneration": "stream-1"}
        manager.model_path = "/tmp/fixture-model.bin"
        manager.threads = 1
        manager._lock = threading.RLock()
        manager.telemetry = language_audio.Manager._empty_state(manager)
        manager.log = lambda message: None
        manager._persist = lambda force=False: None
        manager.on_exclusion = lambda *args, **kwargs: calls.append(args) or {
            "changed": True, "reason": "evidence_recorded",
            "count": 1, "threshold": 25, "enforced": False}
        job = {"cid": "a" * 32, "provider": "nltv:us:" + "a" * 32, "name": "A", "deviceVerified": True,
               "changed": False, "launchID": "launch-1", "streamGeneration": "stream-1", "visitID": "v-1"}
        decision = {"outcome": "excluded", "reason": "language_es_dominant", "language": "es",
                    "languageName": "Spanish", "confidence": 0.97, "windows": 4,
                    "speechSeconds": 40.0, "sampleSeconds": 60.0, "speechCoverage": 0.66,
                    "languages": [], "lexicalMargin": 0.4}
        manager._record_result(job, decision)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manager.telemetry["counters"]["evidence"], 1)
        stale = dict(job, launchID="launch-old", visitID="v-old")
        manager._record_result(stale, decision)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manager.telemetry["counters"]["stale"], 1)
        manager._record_result(dict(job, changed=True), decision)
        self.assertEqual(len(calls), 1)
        manager._record_result(dict(job, deviceVerified=None), decision)
        self.assertEqual(len(calls), 1)
        inconclusive = dict(decision, outcome="inconclusive", reason="mixed_language", language=None,
                            languageName=None, confidence=0.0)
        manager._record_result(dict(job, visitID="v-2"), inconclusive)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manager.telemetry["counters"]["excluded"], 1)
        self.assertGreaterEqual(manager.telemetry["counters"]["inconclusive"], 4)

    def test_overlay_is_hotpatch_only_and_has_no_playback_actions(self):
        path = HERE.parent.parent / "Examples" / "language-evidence-overlay-20261127.json"
        layout = json.loads(path.read_text())
        self.assertEqual(layout["schemaVersion"], 1)
        nodes = []
        def walk(items):
            for node in items:
                nodes.append(node)
                walk(node.get("children", []))
        for slot in layout["slots"]:
            self.assertIn(slot["location"], {
                "player.fullscreen.category", "player.fullscreen.controls",
                "player.fullscreen.top", "player.fullscreen.bottom", "more.bottom"})
            walk(slot["nodes"])
        commands = {node.get("action", {}).get("command") for node in nodes if node.get("action")}
        forbidden = {"play", "pause", "resume", "stop", "next_channel", "previous_channel",
                     "start_surf", "stop_surf", "pause_surf", "resume_surf", "reload_current_stream",
                     "seek_live", "seek_relative", "open_channel"}
        self.assertFalse(commands & forbidden)
        html = " ".join(node.get("html", "") for node in nodes)
        sources = " ".join(node.get("action", {}).get("arguments", {}).get("source", "")
                           for node in nodes if node.get("action"))
        self.assertIn("mistaken_english", sources)
        self.assertIn("ui_set_value", sources)
        self.assertIn("refresh_catalogue", sources)
        self.assertIn("surfPaused", html)
        self.assertNotIn("fetch(", html)
        self.assertNotIn("XMLHttpRequest", html)
        self.assertNotIn("next_channel", html)
        hidden = next(node for node in nodes if node["id"] == "language-overlay-value")
        label = next(node for node in nodes if node["id"] == "language-evidence")
        self.assertEqual(hidden["kind"], "input")
        self.assertEqual(label["kind"], "text")
        self.assertEqual(hidden["valueKey"], label["valueKey"])
        self.assertEqual(hidden["valueKey"], "form.languageOverlay")


class LanguageRealtimeFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.model = os.environ.get("JEALOUSFIN_TEST_WHISPER_MODEL") or str(
            pathlib.Path.home() / ".config" / "jealousfin" / "models" / "ggml-small.bin")
        cls.whisper = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
        cls.ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        cls.say = shutil.which("say")
        if not (cls.say and pathlib.Path(cls.whisper).exists() and pathlib.Path(cls.ffmpeg).exists() and pathlib.Path(cls.model).is_file()):
            raise unittest.SkipTest("local language fixtures unavailable")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def _fixture(self, code, voice, text, seconds=60):
        source = pathlib.Path(self.temp.name) / (code + ".aiff")
        target = pathlib.Path(self.temp.name) / (code + ".wav")
        text_file = pathlib.Path(self.temp.name) / (code + ".txt")
        text_file.write_text(text, encoding="utf-8")
        subprocess.run([self.say, "-v", voice, "-r", "160", "-f", str(text_file), "-o", str(source)],
                       check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-stream_loop", "4", "-i", str(source), "-t", str(seconds),
                        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(target)],
                       check=True, timeout=60)
        return target

    def _classify(self, path):
        with wave.open(str(path), "rb") as handle:
            pcm = handle.readframes(handle.getnframes())
        work = pathlib.Path(tempfile.mkdtemp(dir=self.temp.name))
        try:
            analysis = base.analyze_speech(pcm)
            windows = []
            for item in analysis["windows"]:
                if not item["usable"]:
                    continue
                start = int(item["start"] * base.RATE) * base.WIDTH
                end = int((item["start"] + item["duration"]) * base.RATE) * base.WIDTH
                window_path = work / ("w%d.wav" % item["index"])
                base._write_wav(window_path, pcm[start:end])
                detected = base.detect_language(window_path, self.whisper, self.model)
                if detected:
                    windows.append({
                        "usable": True, "language": detected[0], "probability": detected[1],
                        "speechSeconds": item["speechSeconds"], "speechRatio": item["speechRatio"],
                        "duration": item["duration"], "start": item["start"],
                    })
            full = work / "full.wav"
            base._write_wav(full, pcm)
            transcript = base.transcribe(full, self.whisper, self.model, work)
            lexical = base.lexical_margin(transcript[1]) if transcript else (None, 0, 0)
            return language_audio.decide_language(analysis, windows, lexical, transcript[0] if transcript else None)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_required_language_fixtures(self):
        fixtures = {
            "es": ("Eddy (Spanish (Mexico))", "excluded", "es",
                   "Hola, bienvenidos al noticiero. El gobierno anunció nuevas medidas para las familias y los niños. Los expertos señalan que la situación mejorará. El equipo nacional ganó el partido."),
            "pt": ("Eddy (Portuguese (Brazil))", "excluded", "pt",
                   "Olá e bem-vindos ao noticiário. O governo anunciou novas medidas para as famílias e as crianças. Os especialistas dizem que a situação vai melhorar. A seleção nacional venceu a partida."),
            "fr": ("Eddy (French (France))", "excluded", "fr",
                   "Bonjour et bienvenue au journal. Le gouvernement a annoncé de nouvelles mesures pour les familles et les enfants. Les experts disent que la situation va s'améliorer. L'équipe nationale a gagné le match."),
            "de": ("Anna", "excluded", "de",
                   "Hallo und willkommen bei den Nachrichten. Die Regierung hat neue Maßnahmen für Familien und Kinder angekündigt. Experten sagen, dass sich die Lage verbessern wird. Die Nationalmannschaft hat das Spiel gewonnen."),
            "en": ("Samantha", "allowed", "en",
                   "Hello and welcome to the news. The government announced new measures for families and children. Experts say the situation will improve. The national team won the match."),
            "zh": ("Eddy (Chinese (China mainland))", "allowed", "zh",
                   "你好，欢迎收看今天的新闻节目。政府今天宣布了一系列新的措施，目的是支持家庭和儿童。专家表示，如果投资继续，情况将在未来几个月内改善。国家足球队在昨天的比赛中获胜。"),
            "ko": ("Eddy (Korean (South Korea))", "allowed", "ko",
                   "안녕하세요, 뉴스에 오신 것을 환영합니다. 정부는 가족과 어린이를 위한 새로운 조치를 발표했습니다. 전문가들은 상황이 개선될 것이라고 말합니다. 국가대표팀이 경기에서 승리했습니다."),
            "ja": ("Kyoko", "unauthorized", "ja",
                   "こんにちは、ニュースへようこそ。政府は家族と子供のための新しい対策を発表しました。専門家は状況が改善すると述べています。代表チームが試合に勝ちました。"),
        }
        for code, (voice, expected_outcome, expected_language, text) in fixtures.items():
            with self.subTest(code=code):
                path = self._fixture(code, voice, text)
                decision = self._classify(path)
                self.assertEqual(decision["outcome"], expected_outcome, (code, decision))
                self.assertEqual(decision["language"], expected_language, (code, decision))
                if expected_outcome == "unauthorized":
                    self.assertNotIn(decision["language"], language_catalog.EXCLUDED)

    def test_mixed_and_silence_are_not_excluded(self):
        es = self._fixture("mix-es", "Eddy (Spanish (Mexico))",
                           "Hola, bienvenidos al noticiero. El gobierno anunció nuevas medidas para las familias y los niños.", seconds=30)
        en = self._fixture("mix-en", "Samantha",
                           "Hello and welcome to the news. The government announced new measures for families and children.", seconds=30)
        mixed = pathlib.Path(self.temp.name) / "mixed.wav"
        subprocess.run([self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(es), "-i", str(en), "-filter_complex",
                        "[0:a][1:a]concat=n=2:v=0:a=1", "-t", "60", str(mixed)], check=True, timeout=60)
        decision = self._classify(mixed)
        self.assertEqual(decision["outcome"], "inconclusive", decision)
        silence = pathlib.Path(self.temp.name) / "silence.wav"
        subprocess.run([self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "60", str(silence)], check=True, timeout=30)
        with wave.open(str(silence), "rb") as handle:
            pcm = handle.readframes(handle.getnframes())
        analysis = base.analyze_speech(pcm)
        self.assertEqual(language_audio.decide_language(analysis, [], (None, 0, 0))["outcome"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
