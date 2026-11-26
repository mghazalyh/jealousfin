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
        self.assertEqual(migrated["schemaVersion"], 3)
        self.assertEqual(migrated["decisions"][provider]["decision"], "not_spanish")
        self.assertEqual(migrated["decisions"][provider]["language"], "all")
        self.assertTrue((self.root / "language-exclusions.json").exists())
        self.assertTrue((self.root / "spanish-classification.json").exists())

    def test_auto_stores_exact_language_not_spanish(self):
        state = language_exclusions.empty()
        for code, name, cid in (("pt", "Portuguese", "a" * 32), ("fr", "French", "b" * 32),
                                ("de", "German", "c" * 32), ("es", "Spanish", "d" * 32)):
            state, changed, why = language_exclusions.apply_auto(
                state, "nltv:us:" + code, cid, code, code, name, 0.95,
                "auto:language_" + code + "_dominant", None)
            self.assertTrue(changed)
        self.assertEqual(language_exclusions.describe(state)["languages"],
                         {"pt": 1, "fr": 1, "de": 1, "es": 1})
        self.assertEqual(state["entries"]["nltv:us:pt"]["language"], "pt")
        self.assertEqual(state["entries"]["nltv:us:pt"]["languageName"], "Portuguese")
        self.assertNotEqual(state["entries"]["nltv:us:pt"]["language"], "es")

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
        self.assertGreaterEqual(len(state["entries"]), 8)
        self.assertEqual(language_exclusions.describe(state)["languages"].get("pt", 0), 1)

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

        def on_exclusion(cid, provider, name, code, language_name, confidence, reason, evidence):
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

    def test_english_is_allowed_and_not_excluded(self):
        calls, status = self._run("en", "en", "Hello and welcome to the news. The government announced new measures.")
        self.assertEqual(calls, [])
        self.assertGreaterEqual(status["counters"]["allowed"], 1)
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

    def test_auto_commit_stores_exact_languages_and_filters(self):
        g = self.g
        evidence = {"model": "fixture.bin", "windows": 4, "speechSeconds": 40.0, "sampleSeconds": 60.0,
                    "speechCoverage": 0.66, "languages": [{"language": "pt", "probability": 0.97}],
                    "deviceVerified": True, "channelChangedBeforeResult": False, "lexicalMargin": -0.4}
        self.assertTrue(g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese", 0.97,
                                               "auto:language_pt_dominant", evidence)["changed"])
        evidence_es = dict(evidence, languages=[{"language": "es", "probability": 0.98}], lexicalMargin=0.5)
        self.assertTrue(g.language_auto_commit(self.b, g.sources[self.b], "B", "es", "Spanish", 0.98,
                                               "auto:language_es_dominant", evidence_es)["changed"])
        g.maybe_reload_spanish(force=True)
        self.assertEqual(g.spanish_provider_ids, {g.sources[self.a], g.sources[self.b]})
        state = json.loads((pathlib.Path(self.temp.name) / "language-exclusions.json").read_text())
        self.assertEqual(state["entries"][g.sources[self.a]]["language"], "pt")
        self.assertEqual(state["entries"][g.sources[self.b]]["language"], "es")
        self.assertTrue(hasattr(g, "language_excluded"))
        with self.g.policy_connect(self.db) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM channel").fetchone()[0], 3)
        description = self.g.spanish_classification.describe(state)
        self.assertEqual(description["languages"], {"pt": 1, "es": 1})
        self.assertIn(g.sources[self.a], g.spanish_provider_ids)

    def test_manual_marker_path_and_not_spanish_blocks_auto(self):
        g = self.g
        provider = g.sources[self.a]
        add = json.dumps({"schemaVersion": 1, "action": "add", "channelID": self.a,
                          "channelName": "Fixture A", "nonce": "manual-add-1"})
        with patch.object(g.prototype_selector, "device_ui_trigger", return_value=add):
            g.apply_device_spanish_request({}, "")
        self.assertIn(provider, g.spanish_provider_ids)
        self.assertEqual(g.spanish_state["entries"][provider]["language"], "es")
        evidence = {"model": "fixture.bin", "windows": 4, "speechSeconds": 40.0, "sampleSeconds": 60.0,
                    "speechCoverage": 0.66, "languages": [{"language": "pt", "probability": 0.97}],
                    "deviceVerified": True, "channelChangedBeforeResult": False, "lexicalMargin": -0.4}
        self.assertEqual(g.language_auto_commit(self.a, provider, "A", "pt", "Portuguese", 0.97,
                                                "auto:language_pt_dominant", evidence)["reason"], "manual_exclusion")
        remove = json.dumps({"schemaVersion": 1, "action": "remove", "channelID": self.a,
                             "channelName": "Fixture A", "nonce": "manual-remove-1"})
        with patch.object(g.prototype_selector, "device_ui_trigger", return_value=remove):
            g.apply_device_spanish_request({}, "")
        self.assertNotIn(provider, g.spanish_provider_ids)
        self.assertEqual(g.language_auto_commit(self.a, provider, "A", "pt", "Portuguese", 0.97,
                                                "auto:language_pt_dominant", evidence)["reason"], "manual_not_spanish")

    def test_control_endpoint_preserves_exclusions(self):
        g = self.g
        evidence = {"model": "fixture.bin", "windows": 4, "speechSeconds": 40.0, "sampleSeconds": 60.0,
                    "speechCoverage": 0.66, "languages": [{"language": "pt", "probability": 0.97}],
                    "deviceVerified": True, "channelChangedBeforeResult": False, "lexicalMargin": -0.4}
        g.language_auto_commit(self.a, g.sources[self.a], "A", "pt", "Portuguese", 0.97,
                               "auto:language_pt_dominant", evidence)
        g.set_spanish_auto_enabled(False)
        self.assertFalse(g.reload_spanish_auto_control(force=True))
        self.assertEqual(len(g.spanish_provider_ids), 1)
        g.set_spanish_auto_enabled(True)
        self.assertTrue(g.reload_spanish_auto_control(force=True))
        self.assertIn(g.sources[self.a], g.spanish_provider_ids)


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
