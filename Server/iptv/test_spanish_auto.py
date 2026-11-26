"""Local acceptance for automatic Spanish audio triage.

No provider, live device, media URL or credential is used. Real inference
fixtures are generated locally with macOS `say` + ffmpeg and are skipped when
tooling is unavailable. All temporary audio is deleted by the test.
"""
import array
import json
import math
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(HERE))
import spanish_audio
import spanish_classification


def pcm_bursts(seconds=20.0, amplitude=0.18):
    """Speech-like amplitude modulation: voiced bursts separated by pauses."""
    samples = []
    period = 0.8
    for index in range(int(seconds * spanish_audio.RATE)):
        phase = (index / spanish_audio.RATE) % period
        level = amplitude if phase < 0.5 else 0.004
        samples.append(int(32767 * level * math.sin(2 * math.pi * 180 * index / spanish_audio.RATE)))
    data = array.array("h", samples)
    return data.tobytes()


def pcm_tone(seconds=20.0, amplitude=0.2):
    samples = [int(32767 * amplitude * math.sin(2 * math.pi * 440 * index / spanish_audio.RATE))
               for index in range(int(seconds * spanish_audio.RATE))]
    return array.array("h", samples).tobytes()


def pcm_silence(seconds=20.0):
    return b"\x00\x00" * int(seconds * spanish_audio.RATE)


def window(language, probability, usable=True):
    return {"usable": usable, "language": language, "probability": probability,
            "speechSeconds": 8.0, "speechRatio": 0.55, "duration": 15.0, "start": 0.0}


class ClassificationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)

    def test_v1_migration_preserves_manual_marks(self):
        provider = "nltv:us:fixture"
        cid = "a" * 32
        v1 = {"schemaVersion": 1, "revision": "spanish-classification-v1", "updatedAt": 10.0,
              "lastNonce": "n1", "entries": {provider: {"channelID": cid, "channelName": "Fixture",
                                                         "markedAt": 9.0, "nonce": "n1"}}}
        (self.root / "spanish-classification.json").write_text(json.dumps(v1))
        state = spanish_classification.load(self.root)
        self.assertEqual(state["schemaVersion"], 2)
        self.assertEqual(spanish_classification.excluded(state), {provider})
        self.assertEqual(state["entries"][provider]["source"], "manual")
        self.assertEqual(state["decisions"][provider]["decision"], "spanish")

    def test_manual_override_blocks_auto_and_replayed_nonce(self):
        provider = "nltv:us:fixture"
        cid = "a" * 32
        state = spanish_classification.empty()
        state, changed, reason = spanish_classification.apply_manual(state, "add", cid, "Fixture", provider, "manual-1", now=10)
        self.assertTrue(changed)
        self.assertEqual(reason, "manual_add")
        same, changed, reason = spanish_classification.apply_manual(state, "add", cid, "Fixture", provider, "manual-1", now=11)
        self.assertFalse(changed)
        self.assertEqual(reason, "replayed_nonce")
        state, changed, reason = spanish_classification.apply_manual(state, "remove", cid, "Fixture", provider, "manual-2", now=12)
        self.assertTrue(changed)
        self.assertEqual(state["decisions"][provider]["decision"], "not_spanish")
        self.assertEqual(spanish_classification.excluded(state), set())
        auto, changed, reason = spanish_classification.apply_auto(
            state, provider, cid, "Fixture", 0.99, "auto:spanish_dominant",
            {"model": "fixture.bin", "windows": 4, "speechSeconds": 40.0, "sampleSeconds": 60.0,
             "speechCoverage": 0.66, "languages": [{"language": "es", "probability": 0.99}],
             "deviceVerified": True, "channelChangedBeforeResult": False, "lexicalMargin": 0.5}, now=13)
        self.assertFalse(changed)
        self.assertEqual(reason, "manual_not_spanish")
        self.assertEqual(spanish_classification.excluded(auto), set())

    def test_auto_never_downgrades_manual_and_never_removes(self):
        provider = "nltv:us:fixture"
        cid = "a" * 32
        state = spanish_classification.empty()
        state, _, _ = spanish_classification.apply_manual(state, "add", cid, "Fixture", provider, "m1", now=1)
        evidence = {"model": "fixture.bin", "windows": 4, "speechSeconds": 40.0, "sampleSeconds": 60.0,
                    "speechCoverage": 0.66, "languages": [{"language": "es", "probability": 0.99}],
                    "deviceVerified": True, "channelChangedBeforeResult": False, "lexicalMargin": 0.5}
        same, changed, reason = spanish_classification.apply_auto(state, provider, cid, "Fixture", 0.99, "auto:spanish_dominant", evidence)
        self.assertFalse(changed)
        self.assertEqual(reason, "manual_spanish")
        self.assertEqual(same["entries"][provider]["source"], "manual")
        self.assertEqual(spanish_classification.excluded(same), {provider})

    def test_concurrent_manual_and_auto_writers_do_not_lose_updates(self):
        lock = threading.Lock()
        providers = ["nltv:us:auto-%d" % i for i in range(12)]
        evidence = {"model": "fixture.bin", "windows": 4, "speechSeconds": 40.0, "sampleSeconds": 60.0,
                    "speechCoverage": 0.66, "languages": [{"language": "es", "probability": 0.99}],
                    "deviceVerified": True, "channelChangedBeforeResult": False, "lexicalMargin": 0.5}

        def mutate(callback):
            with lock:
                state = spanish_classification.load(self.root)
                state, _, _ = callback(state)
                spanish_classification.atomic_write(self.root, state)

        def auto_add(provider, cid):
            return lambda state: spanish_classification.apply_auto(state, provider, cid, provider, 0.95, "auto:spanish_dominant", evidence)

        def manual_add():
            mutate(lambda state: spanish_classification.apply_manual(state, "add", "a" * 32, "Manual", "nltv:us:manual", "manual-nonce", now=time.time()))

        def manual_remove():
            mutate(lambda state: spanish_classification.apply_manual(
                state, "remove", "0" * 32, providers[0], providers[0], "allow-nonce", now=time.time()))

        threads = [threading.Thread(target=lambda p=provider, i=index: mutate(auto_add(p, ("%032d" % i)[-32:])))
                   for index, provider in enumerate(providers)]
        threads.append(threading.Thread(target=manual_add))
        threads.append(threading.Thread(target=manual_remove))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        state = spanish_classification.load(self.root)
        self.assertEqual(state["entries"]["nltv:us:manual"]["source"], "manual")
        # The explicit allow for the first provider must survive every racing
        # automatic add: the provider is either absent or never re-added.
        self.assertNotIn(providers[0], state["entries"])
        self.assertEqual(state["decisions"][providers[0]]["decision"], "not_spanish")
        self.assertGreaterEqual(len(state["entries"]), 12)


class SpeechAnalysisTests(unittest.TestCase):
    def test_speech_like_audio_is_covered(self):
        analysis = spanish_audio.analyze_speech(pcm_bursts(20.0))
        self.assertGreater(analysis["speechSeconds"], 4.0)
        self.assertGreater(analysis["coverage"], 0.2)
        self.assertGreaterEqual(len([w for w in analysis["windows"] if w["usable"]]), 2)

    def test_silence_and_steady_tone_are_rejected(self):
        for pcm in (pcm_silence(20.0), pcm_tone(20.0, 0.2)):
            analysis = spanish_audio.analyze_speech(pcm)
            self.assertLess(analysis["coverage"], 0.05)
            decision = spanish_audio.decide(analysis, [], (None, 0, 0))
            self.assertEqual(decision["outcome"], "inconclusive")

    def test_conservative_decision_matrix(self):
        analysis = {"duration": 60.0, "speechSeconds": 40.0, "coverage": 0.66}
        self.assertEqual(spanish_audio.decide(analysis, [window("es", 0.96)] * 4, (0.5, 5, 1))["outcome"], "spanish")
        self.assertEqual(spanish_audio.decide(analysis, [window("en", 0.98)] * 4, (0.0, 0, 0))["outcome"], "non_spanish")
        self.assertEqual(spanish_audio.decide(analysis, [window("pt", 0.97)] * 4, (0.0, 0, 0))["outcome"], "non_spanish")
        self.assertEqual(spanish_audio.decide(analysis, [window("es", 0.96), window("en", 0.96), window("es", 0.96), window("en", 0.96)], (0.0, 1, 1))["outcome"], "inconclusive")
        self.assertEqual(spanish_audio.decide({"duration": 60.0, "speechSeconds": 8.0, "coverage": 0.1}, [window("es", 0.96)] * 4, (0.5, 5, 1))["outcome"], "inconclusive")
        self.assertEqual(spanish_audio.decide(analysis, [window("es", 0.96)] * 4, (-0.8, 1, 9))["outcome"], "inconclusive")
        self.assertEqual(spanish_audio.decide(analysis, [window("es", 0.90)] * 4, (None, 0, 0))["outcome"], "inconclusive")

    def test_lexical_margin(self):
        self.assertGreater(spanish_audio.lexical_margin("Hola, el gobierno anunció nuevas medidas para las familias y los niños")[0], 0)
        self.assertLess(spanish_audio.lexical_margin("Olá, o governo anunciou novas medidas para as famílias e os negócios")[0], 0)


class RealtimeFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.model = os.environ.get("JEALOUSFIN_TEST_WHISPER_MODEL") or str(
            pathlib.Path.home() / ".config" / "jealousfin" / "models" / "ggml-small.bin")
        cls.whisper = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
        cls.ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        cls.say = shutil.which("say")
        if not (cls.say and pathlib.Path(cls.whisper).exists() and pathlib.Path(cls.ffmpeg).exists() and pathlib.Path(cls.model).is_file()):
            raise unittest.SkipTest("local speech fixtures unavailable")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def _fixture(self, language, voice, text):
        source = pathlib.Path(self.temp.name) / (language + ".aiff")
        target = pathlib.Path(self.temp.name) / (language + ".wav")
        text_file = pathlib.Path(self.temp.name) / (language + ".txt")
        text_file.write_text(text, encoding="utf-8")
        subprocess.run([self.say, "-v", voice, "-r", "170", "-f", str(text_file), "-o", str(source)],
                       check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(source), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(target)],
                       check=True, timeout=60)
        return target

    def _language(self, path):
        detected = spanish_audio.detect_language(path, self.whisper, self.model)
        self.assertIsNotNone(detected)
        return detected

    def test_multilingual_tts_fixtures(self):
        es = self._fixture("es", "Eddy (Spanish (Mexico))",
                           "Hola, bienvenidos al noticiero. El gobierno anunció nuevas medidas para las familias y los niños. "
                           "Los expertos señalan que la situación mejorará. El equipo nacional ganó el partido. "
                           "Muchas personas salieron a celebrar con sus amigos y vecinos. La ciudad abrirá un nuevo museo. "
                           "El clima será soleado durante el fin de semana.")
        en = self._fixture("en", "Samantha",
                           "Hello and welcome to the evening news. The government announced new measures for families and children. "
                           "Experts say the situation will improve. The national team won the match. "
                           "Many people went out to celebrate with their friends and neighbors. The city will open a new museum. "
                           "The weather will be sunny through the weekend.")
        pt = self._fixture("pt", "Eddy (Portuguese (Brazil))",
                           "Olá e bem-vindos ao noticiário. O governo anunciou novas medidas para as famílias e as crianças. "
                           "Os especialistas dizem que a situação vai melhorar. A seleção nacional venceu a partida. "
                           "Muitas pessoas saíram para comemorar com seus amigos e vizinhos. A cidade vai abrir um novo museu. "
                           "O tempo vai ficar ensolarado durante o fim de semana.")
        self.assertEqual(self._language(es)[0], "es")
        self.assertEqual(self._language(en)[0], "en")
        self.assertEqual(self._language(pt)[0], "pt")

    def test_aes128_plaintext_and_encrypted_ts_decode(self):
        try:
            from cryptography.hazmat.primitives import padding
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except Exception:
            self.skipTest("cryptography unavailable")
        source = pathlib.Path(self.temp.name) / "aes-source.ts"
        subprocess.run([self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                        "-t", "2", "-c:a", "aac", "-f", "mpegts", str(source)], check=True, timeout=30)
        plain = source.read_bytes()
        key = os.urandom(16); iv = os.urandom(16)
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plain) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        work = pathlib.Path(self.temp.name)
        plain_pcm = spanish_audio.decode_audio(plain, None, self.ffmpeg, work, key=key, iv="0x" + iv.hex())
        encrypted_pcm = spanish_audio.decode_audio(encrypted, None, self.ffmpeg, work, key=key, iv="0x" + iv.hex())
        self.assertGreater(len(plain_pcm), 1000)
        self.assertGreater(len(encrypted_pcm), 1000)
        self.assertEqual(len(plain_pcm), len(encrypted_pcm))

    def test_noise_and_silence_fixtures_are_inconclusive(self):
        silence = pathlib.Path(self.temp.name) / "silence.wav"
        noise = pathlib.Path(self.temp.name) / "noise.wav"
        subprocess.run([self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "20", str(silence)], check=True, timeout=30)
        subprocess.run([self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "anoisesrc=color=white:r=16000:a=0.1", "-t", "20", str(noise)], check=True, timeout=30)
        for path in (silence, noise):
            with wave.open(str(path), "rb") as handle:
                pcm = handle.readframes(handle.getnframes())
            analysis = spanish_audio.analyze_speech(pcm)
            self.assertEqual(spanish_audio.decide(analysis, [], (None, 0, 0))["outcome"], "inconclusive")


class GatewayIntegrationTests(unittest.TestCase):
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
        self.g.audio_playlists.clear(); self.g.audio_segments.clear()
        self.g.map_resources.clear(); self.g.map_bases.clear(); self.g.resource_bases.clear()

    def test_master_and_audio_playlist_rendition_tagging(self):
        master = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1000000,AUDIO="aud"
video.m3u8
"""
        self.g.rewrite(master.encode(), "https://1.1.1.1/master.m3u8", self.a)
        self.assertEqual(len(self.g.audio_playlists), 1)
        audio_rid = next(iter(self.g.audio_playlists))
        media = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:1
#EXT-X-MAP:URI="init.mp4"
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x000102030405060708090a0b0c0d0e0f
#EXTINF:6,
seg1.ts
#EXTINF:6,
seg2.ts
"""
        self.g.rewrite(media.encode(), "https://1.1.1.1/audio.m3u8", self.a, audio_only=True)
        self.assertEqual(len(self.g.audio_segments), 2)
        self.assertTrue(all(rid in self.g.resource_bases for rid in self.g.audio_segments))
        self.assertTrue(self.g.map_resources)
        map_rid = next(iter(self.g.map_resources.values()))
        self.assertIn(map_rid, self.g.map_bases)
        self.assertIn(audio_rid, self.g.audio_playlists)
        self.assertEqual(len(self.g.key_resources), 1)
        self.assertTrue(all(self.g.segment_keys.get(rid, {}).get("keyRid") for rid in self.g.audio_segments))
        self.assertTrue(self.g.manifest_quality[self.a]["playlistEncrypted"])
        for rid in list(self.g.audio_segments) + [map_rid] + list(self.g.key_resources):
            self.g.remove_resource(rid)
        self.assertEqual(self.g.audio_segments, set())
        self.assertEqual(self.g.key_resources, {})

    def test_manual_marker_path_and_auto_override(self):
        g = self.g
        provider = g.sources[self.a]
        add = json.dumps({"schemaVersion": 1, "action": "add", "channelID": self.a,
                          "channelName": "Fixture A", "nonce": "manual-add-1"})
        with patch.object(g.prototype_selector, "device_ui_trigger", return_value=add):
            g.apply_device_spanish_request({}, "")
        self.assertIn(provider, g.spanish_provider_ids)
        self.assertEqual(g.spanish_state["entries"][provider]["source"], "manual")
        evidence = {"model": "fixture.bin", "windows": 4, "speechSeconds": 40.0, "sampleSeconds": 60.0,
                    "speechCoverage": 0.66, "languages": [{"language": "es", "probability": 0.96}],
                    "deviceVerified": True, "channelChangedBeforeResult": False, "lexicalMargin": 0.4}
        self.assertTrue(g.spanish_auto_commit(self.b, g.sources[self.b], "Fixture B", 0.96,
                                              "auto:spanish_dominant", evidence)["changed"])
        self.assertIn(g.sources[self.b], g.spanish_provider_ids)
        remove = json.dumps({"schemaVersion": 1, "action": "remove", "channelID": self.a,
                             "channelName": "Fixture A", "nonce": "manual-remove-1"})
        with patch.object(g.prototype_selector, "device_ui_trigger", return_value=remove):
            g.apply_device_spanish_request({}, "")
        self.assertNotIn(provider, g.spanish_provider_ids)
        self.assertIn(g.sources[self.b], g.spanish_provider_ids)  # separate provider untouched
        self.assertEqual(g.spanish_auto_commit(self.a, provider, "Fixture A", 0.96,
                                               "auto:spanish_dominant", evidence)["reason"], "manual_not_spanish")

    def test_maintenance_window_suppresses_only_listed_failure(self):
        g = self.g
        now = time.time()
        g.MAINTENANCE_FILE.write_text(json.dumps({
            "schemaVersion": 1, "start": now - 1, "until": now + 120,
            "reason": "fixture maintenance", "launchID": "fixture-launch",
            "channelID": self.c, "allowedReasons": ["Jellyfin HTTP 401"],
        }))
        g.maintenance_state = None; g.maintenance_checked_at = 0.0
        g.policy_event(self.c, "TRACE-MAINT", "failure", "Jellyfin HTTP 401")
        with self.g.policy_connect(self.db) as db:
            row = db.execute("SELECT failures,hiatus_until FROM channel WHERE id=?", (self.c,)).fetchone()
            self.assertEqual(row, (0, 0.0))
            self.assertTrue(db.execute("SELECT 1 FROM observed WHERE id=?", ("native-policy:TRACE-MAINT:failure",)).fetchone())
        g.policy_event(self.c, "TRACE-REAL", "failure", "PLAYBACK_STALLED")
        with self.g.policy_connect(self.db) as db:
            failures, hiatus = db.execute("SELECT failures,hiatus_until FROM channel WHERE id=?", (self.c,)).fetchone()
            self.assertEqual(failures, 1)
            self.assertGreater(hiatus, now)


class SamplerIntegrationTests(unittest.TestCase):
    def test_sampler_marks_sampled_channel_only(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        calls = []
        stop = threading.Event()
        pcm = pcm_bursts(5.2)
        language = {"value": "es"}
        text = {"value": "Hola, el gobierno anunció nuevas medidas para las familias y los niños."}

        def on_spanish(cid, provider, name, confidence, reason, evidence):
            calls.append((cid, provider, confidence, reason))
            return {"changed": True, "reason": "auto_add"}

        with patch.object(spanish_audio.Manager, "_check_tools", return_value=None), \
                patch.object(spanish_audio, "decode_audio", return_value=pcm), \
                patch.object(spanish_audio, "detect_language", side_effect=lambda *a, **k: (language["value"], 0.96)), \
                patch.object(spanish_audio, "transcribe", side_effect=lambda *a, **k: (language["value"], text["value"])):
            manager = spanish_audio.Manager(pathlib.Path(temp.name), "/tmp/fixture-model.bin", on_spanish,
                                            lambda: True, probe=lambda: {"channelID": "a" * 32},
                                            log=lambda message: None)
            manager.fault = None
            manager.start()
            try:
                for index in range(12):
                    manager.tap("a" * 32, "nltv:us:a", "Channel A", ("%032d" % index)[-32:],
                                "https://media.example/a.m3u8", b"segment", False)
                deadline = time.time() + 20
                while time.time() < deadline and not calls:
                    time.sleep(0.1)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], "a" * 32)
                self.assertEqual(calls[0][1], "nltv:us:a")
                status = manager.status()
                self.assertGreaterEqual(status["counters"]["spanish"], 1)
                self.assertEqual(status["last"]["channelID"], "a" * 32)
            finally:
                manager.stop()


if __name__ == "__main__":
    unittest.main()
