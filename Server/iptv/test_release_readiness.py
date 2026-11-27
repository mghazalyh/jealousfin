"""Release-readiness and deployment-continuity acceptance.

No device, socket, credential or production access. All checks are local and
read-only apart from the isolated temporary state directory.
"""
import importlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
import sys
sys.path.insert(0, str(HERE))

import deployment_continuity as continuity
import overlay_bridge as ob
import programme_overlay as po

A, B, C = ("a" * 32, "b" * 32, "c" * 32)
SUPPORTED_STYLES = {"normal", "headline", "caption", "prominent",
                    "destructive", "muted"}
INSTALLED_FIXTURE = HERE / "fixtures" / "programme-language-existing-ipa-20261127.json"


def gateway_module(temp):
    env = {"JEALOUSFIN_STATE_DIR": temp,
           "JEALOUSFIN_USERNAME": "fixture", "JEALOUSFIN_PASSWORD": "fixture",
           "JEALOUSFIN_TOKEN": "fixture-token",
           "JEALOUSFIN_SERVER_ID": "fixture-server",
           "JEALOUSFIN_BIND": "127.0.0.1",
           "JEALOUSFIN_SOURCE_PROFILE": "american"}
    with patch.dict(os.environ, env, clear=True):
        import gateway
        return importlib.reload(gateway)


def walk(nodes):
    for node in nodes:
        if not isinstance(node, dict):
            continue
        yield node
        yield from walk(node.get("children") or [])


class IntegratedSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.g = gateway_module(self.temp.name)

    def test_language_episode_and_schedule_surfaces_coexist(self):
        import language_audio
        import schedule_episode
        # Paused-retry lane.
        self.assertTrue(callable(self.g.episode_probe))
        self.assertTrue(callable(self.g.episode_retry_callback))
        self.assertTrue(callable(self.g.episode_observer_instance))
        self.assertTrue(callable(language_audio.Manager.request_episode_retry))
        self.assertGreater(language_audio.EPISODE_RETRY_SECONDS, 0)
        self.assertLessEqual(language_audio.MAX_EPISODE_RETRY_ATTEMPTS, 2)
        # Schedule-discovery lane.
        self.assertTrue(callable(schedule_episode.resolve_current))
        self.assertTrue(callable(self.g.schedule_episode_status))
        status = self.g.schedule_episode_status()
        self.assertEqual(status["revision"], schedule_episode.REVISION)
        self.assertIn("mappings", status)
        # Existing IPA combined overlay lane.
        self.assertEqual(self.g.overlay_combined_status()["layoutRevision"],
                         "programme-language-existing-ipa-20261127")
        self.assertEqual(po.KEY, "form.programmeOverlay")
        self.assertEqual(po.READY_KEY, "form.programmeOverlayReady")
        self.assertEqual(po.FALLBACK_KEY, "form.programmeOverlayFallback")
        self.assertEqual(ob.STYLES, SUPPORTED_STYLES)

    def test_status_defaults_fail_closed_without_a_device(self):
        audio = self.g.episode_audio_status()
        self.assertFalse(audio["enabled"])
        self.assertIn("probe", audio)
        self.assertEqual(audio["probe"]["checks"], 0)
        overlay = self.g.overlay_combined_status()
        self.assertFalse(overlay["configured"])
        self.assertEqual(overlay["layoutRevision"],
                         "programme-language-existing-ipa-20261127")
        self.assertEqual(overlay["nativeAbi"],
                         "existing headline text node programme-combined-native-v1")
        # No selector means no authorized transport is constructed.
        self.assertEqual(overlay["revision"], po.REVISION)


class InstalledIpaCompatibilityTests(unittest.TestCase):
    def test_hotpatch_uses_only_installed_form_bindings_and_styles(self):
        layout = json.loads(INSTALLED_FIXTURE.read_text())
        po.validate_existing_ipa_layout(layout)
        nodes = [n for slot in layout["slots"] for n in walk(slot.get("nodes") or [])]
        by_id = {n["id"]: n for n in nodes if n.get("id")}
        native = by_id["programme-combined-native-v1"]
        self.assertEqual(native["style"], "headline")
        self.assertEqual(native["valueKey"], po.KEY)
        self.assertEqual(native["visibleWhen"], po.READY_KEY)
        self.assertEqual(by_id["programme-bottom-left-v1"]["visibleWhen"],
                         po.FALLBACK_KEY)
        for node in nodes:
            if node.get("style") is not None:
                self.assertIn(node["style"], SUPPORTED_STYLES)
                self.assertNotEqual(node["style"], "programme")
        # A draft 22 px style is not part of the installed ABI.
        self.assertNotIn("programme", ob.STYLES)

    def test_no_shipped_layout_uses_the_draft_programme_style(self):
        for path in sorted(list((HERE / "fixtures").glob("*.json")) +
                           list((ROOT / "Examples").glob("*.json"))):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError) as error:
                self.fail("unreadable shipped layout fixture %s: %s" % (path, error))
            if not isinstance(data, dict):
                continue
            for slot in data.get("slots") or []:
                for node in walk(slot.get("nodes") or []):
                    self.assertNotEqual(node.get("style"), "programme", str(path))


class EpisodeProvenanceCompatibilityTests(unittest.TestCase):
    def allowed_sources(self):
        text = (ROOT / "Sources" / "Domain.swift").read_text()
        match = re.search(r"let allowedEpisodeSources=\[([^\]]*)\]", text)
        self.assertIsNotNone(match, "native provenance allowlist is missing")
        return set(re.findall(r'"([^"]+)"', match.group(1)))

    def emitted_sources(self):
        found = set()
        for path in (HERE).glob("*.py"):
            if path.name.startswith("test_"):
                continue
            for match in re.finditer(
                    r"episodeSource['\"]?\s*:\s*['\"]([^'\"]+)['\"]",
                    path.read_text()):
                found.add(match.group(1))
        return found

    def test_every_server_episode_source_is_accepted_by_native_parser(self):
        emitted = self.emitted_sources()
        self.assertTrue(emitted)
        self.assertLessEqual(emitted, self.allowed_sources(),
                             "server emits provenance the native parser drops")

    def test_audio_and_schedule_provenance_are_covered(self):
        allowed = self.allowed_sources()
        for value in ("pluto-xmltv", "catalog-schedule",
                      "local-audio-episode-identity"):
            self.assertIn(value, allowed)
        # audio-clue-catalog is a verification label, not an episode source.
        self.assertNotIn("audio-clue-catalog", self.emitted_sources())


class StartupHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.g = gateway_module(self.temp.name)
        for cid in (A, B, C):
            self.g.channels[cid] = {"Id": cid, "Name": cid, "Type": "TvChannel"}
            self.g.sources[cid] = cid
        self.db = pathlib.Path(self.temp.name) / "policy.sqlite3"
        with self.g.policy_connect(self.db) as db:
            db.executemany("INSERT INTO channel(id,name) VALUES(?,?)",
                           [(cid, cid) for cid in (A, B, C)])
            db.execute("INSERT INTO meta VALUES('seeded','1')")

    def test_readiness_requires_seeded_policy_and_valid_generation(self):
        rows = self.g.policy_rows()
        self.assertEqual(set(rows), {A, B, C})
        state = self.g.MEDIA_CONTINUITY.begin_generation(now=1000.0)
        self.assertIsNotNone(state)
        persisted = continuity.read_state(self.temp.name)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["generation"], state["generation"])
        self.assertEqual(persisted["schemaVersion"], continuity.SCHEMA_VERSION)
        audio = self.g.episode_audio_status()
        self.assertFalse(audio["enabled"])
        self.assertIn("probe", audio)
        self.assertIn("publication", audio)
        overlay = self.g.overlay_combined_status()
        self.assertEqual(overlay["layoutRevision"],
                         "programme-language-existing-ipa-20261127")
        self.assertEqual(overlay["field"], po.KEY)
        schedule = self.g.schedule_episode_status()
        self.assertEqual(schedule["revision"], "schedule-episode-20261127")
        self.assertIn(schedule["catalogFault"], (None, "empty-catalog"))
        for mapping in schedule["mappings"]:
            self.assertTrue(mapping["mappingOnly"])
            self.assertFalse(mapping["staticCurrentEpisode"])
        self.assertEqual(len(self.g.MEDIA_CONTINUITY._handles), 0)

    def test_unseeded_policy_is_not_reported_ready(self):
        pathlib.Path(self.temp.name, "policy.sqlite3").unlink()
        with self.assertRaises((sqlite3.Error, RuntimeError, OSError)):
            self.g.policy_rows()


class ReleaseManifestTests(unittest.TestCase):
    def manifest(self):
        text = (ROOT / "docs" / "episode-release-20261127.md").read_text()
        section = text.split("## File manifest", 1)[1]
        block = section.split("```", 2)[1]
        return [line.strip() for line in block.splitlines() if line.strip()]

    def test_every_manifest_path_exists(self):
        paths = self.manifest()
        self.assertTrue(paths)
        self.assertIn("Server/iptv/gateway.py", paths)
        self.assertIn("Server/iptv/programme_overlay.py", paths)
        self.assertIn("docs/episode-release-20261127.md", paths)
        for relative in paths:
            self.assertTrue((ROOT / relative).exists(), relative)

    def test_manifest_matches_committed_delta_shape(self):
        paths = set(self.manifest())
        # No production, device, credential or generated state may be shipped.
        for relative in paths:
            self.assertNotIn("..", pathlib.PurePosixPath(relative).parts)
            self.assertNotIn("policy.sqlite3", relative)
            self.assertNotIn("media-handles.jsonl", relative)
            self.assertNotIn("token", relative.casefold())

    def test_manifest_is_the_exact_delta_from_the_verified_base(self):
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "8a6921d"],
                cwd=str(ROOT), capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git metadata unavailable")
        expected = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        self.assertEqual(set(self.manifest()), expected)


class BackupRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.g = gateway_module(self.temp.name)
        for cid in (A, B, C):
            self.g.channels[cid] = {"Id": cid, "Name": cid, "Type": "TvChannel"}
            self.g.sources[cid] = cid
        self.db = pathlib.Path(self.temp.name) / "policy.sqlite3"
        with self.g.policy_connect(self.db) as db:
            db.executemany("INSERT INTO channel(id,name) VALUES(?,?)",
                           [(cid, cid) for cid in (A, B, C)])
            db.execute("INSERT INTO meta VALUES('seeded','1')")
        self.rid = "f" * 32
        self.g.MEDIA_CONTINUITY.begin_generation(now=1000.0)
        self.g.MEDIA_CONTINUITY.record_handle(self.rid, A, "playlist", 1001.0)

    def test_documented_backup_and_restore_restores_startup_health(self):
        with tempfile.TemporaryDirectory() as backup_temp:
            backup = pathlib.Path(backup_temp) / "gateway"
            shutil.copytree(self.temp.name, backup)
            live = pathlib.Path(self.temp.name)
            (live / "policy.sqlite3").unlink()
            (live / continuity.STATE_FILE).write_text("{bad")
            with self.assertRaises((sqlite3.Error, RuntimeError, OSError)):
                self.g.policy_rows()
            self.assertIsNone(continuity.read_state(live))
            shutil.copy2(backup / "policy.sqlite3", live / "policy.sqlite3")
            shutil.copy2(backup / continuity.STATE_FILE, live / continuity.STATE_FILE)
            self.assertEqual(set(self.g.policy_rows()), {A, B, C})
            restored = continuity.read_state(live)
            self.assertIsNotNone(restored)
            self.assertIsNotNone(restored["generation"])
            # The handle ledger survives the restore and is re-read by a new
            # process generation.
            active = continuity.Continuity(live)
            active.begin_generation(now=1010.0)
            self.assertEqual(len(active._handles), 1)


class DeploymentContinuityBoundaryTests(unittest.TestCase):
    """Restart-correlated stalls must not be confused with real failures."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.g = gateway_module(self.temp.name)
        for cid in (A, B, C):
            self.g.channels[cid] = {"Id": cid, "Name": cid, "Type": "TvChannel"}
            self.g.sources[cid] = cid
        self.db = pathlib.Path(self.temp.name) / "policy.sqlite3"
        with self.g.policy_connect(self.db) as db:
            db.executemany("INSERT INTO channel(id,name) VALUES(?,?)",
                           [(cid, cid) for cid in (A, B, C)])
            db.execute("INSERT INTO meta VALUES('seeded','1')")
        self.rid = "d" * 32
        self.restart_at = 1010.0

    def restart(self, start=1000.0, restart_at=None):
        restart_at = self.restart_at if restart_at is None else restart_at
        old = continuity.Continuity(self.temp.name)
        old.begin_generation(now=start)
        old.record_handle(self.rid, C, "playlist", start + 1)
        active = continuity.Continuity(self.temp.name)
        active.begin_generation(now=restart_at)
        self.g.MEDIA_CONTINUITY = active
        return active

    def failures(self, cid=C):
        return self.g.policy_rows()[cid]["failures"]

    def code(self, trace):
        with self.g.policy_connect(self.db) as db:
            row = db.execute("SELECT code FROM observed WHERE id=?",
                             ("native-policy:" + trace + ":failure",)).fetchone()
        return row[0] if row else None

    def test_two_stale_hits_after_restart_suppress_only_startup_stall(self):
        active = self.restart()
        self.assertIsNotNone(active.stale_handle(self.rid, 1011.0))
        self.assertIsNotNone(active.stale_handle(self.rid, 1012.0))
        before = self.failures()
        with patch.object(self.g.time, "time", return_value=1013.0):
            self.g.policy_event(C, "READY-RESTART", "failure",
                                "Video frames stalled after startup")
        self.assertEqual(self.failures(), before)
        self.assertEqual(self.code("READY-RESTART"),
                         "NATIVE_POLICY_DEPLOYMENT_IGNORED")

    def test_single_stale_hit_is_a_real_failure(self):
        active = self.restart()
        self.assertIsNotNone(active.stale_handle(self.rid, 1011.0))
        before = self.failures()
        with patch.object(self.g.time, "time", return_value=1012.0):
            self.g.policy_event(C, "READY-ONEHIT", "failure",
                                "Video frames stalled after startup")
        self.assertEqual(self.failures(), before + 1)
        self.assertEqual(self.code("READY-ONEHIT"), "NATIVE_POLICY_failure")

    def test_real_playback_failures_are_never_suppressed(self):
        active = self.restart()
        active.stale_handle(self.rid, 1011.0)
        active.stale_handle(self.rid, 1012.0)
        before = self.failures()
        reasons = ("Playback stalled", "Stream HTTP 403",
                   "Video froze after startup", "Network player error -1008")
        for index, reason in enumerate(reasons):
            trace = "READY-REAL-" + str(index)
            with patch.object(self.g.time, "time", return_value=1013.0 + index):
                self.g.policy_event(C, trace, "failure", reason)
            self.assertEqual(self.code(trace), "NATIVE_POLICY_failure", reason)
        # The first real failure starts the hiatus; later failures are still
        # recorded as real (not deployment-ignored) but cannot re-penalise.
        self.assertEqual(self.failures(), before + 1)

    def test_other_channel_cannot_borrow_stale_handle_evidence(self):
        active = self.restart()
        active.stale_handle(self.rid, 1011.0)
        active.stale_handle(self.rid, 1012.0)
        before = self.failures(A)
        with patch.object(self.g.time, "time", return_value=1013.0):
            self.g.policy_event(A, "READY-OTHER", "failure",
                                "Video frames stalled after startup")
        self.assertEqual(self.failures(A), before + 1)
        self.assertEqual(self.code("READY-OTHER"), "NATIVE_POLICY_failure")

    def test_stall_outside_recovery_window_is_a_real_failure(self):
        active = self.restart()
        active.stale_handle(self.rid, 1011.0)
        active.stale_handle(self.rid, 1012.0)
        before = self.failures()
        late = self.restart_at + continuity.RECOVERY_WINDOW_SECONDS + 10
        with patch.object(self.g.time, "time", return_value=late):
            self.g.policy_event(C, "READY-LATE", "failure",
                                "Video frames stalled after startup")
        self.assertEqual(self.failures(), before + 1)
        self.assertEqual(self.code("READY-LATE"), "NATIVE_POLICY_failure")

    def test_same_generation_handle_is_not_stale_so_the_failure_is_real(self):
        active = continuity.Continuity(self.temp.name)
        active.begin_generation(now=2000.0)
        active.record_handle(self.rid, C, "playlist", 2001.0)
        self.g.MEDIA_CONTINUITY = active
        self.assertIsNone(active.stale_handle(self.rid, 2002.0))
        before = self.failures()
        with patch.object(self.g.time, "time", return_value=2002.0):
            self.g.policy_event(C, "READY-NORESTART", "failure",
                                "Video frames stalled after startup")
        self.assertEqual(self.failures(), before + 1)
        self.assertEqual(self.code("READY-NORESTART"), "NATIVE_POLICY_failure")

    def test_corrupt_generation_state_fails_closed_at_the_gateway(self):
        active = self.restart()
        active.stale_handle(self.rid, 1011.0)
        active.stale_handle(self.rid, 1012.0)
        state = pathlib.Path(self.temp.name, continuity.STATE_FILE)
        state.write_text("{bad")
        before = self.failures()
        with patch.object(self.g.time, "time", return_value=1013.0):
            self.g.policy_event(C, "READY-CORRUPT", "failure",
                                "Video frames stalled after startup")
        self.assertEqual(self.failures(), before + 1)
        self.assertEqual(self.code("READY-CORRUPT"), "NATIVE_POLICY_failure")


if __name__ == "__main__":
    unittest.main()
