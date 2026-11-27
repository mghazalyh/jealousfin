"""Incident fixtures: coherent identity capture and combined publication.

These tests reproduce the live shape reported for the 2026-11-27 identity /
overlay lane:

* 576 channels and hundreds of programme/language decisions exist, yet
  ``combinedPublished`` and ``episodeAccepted`` stay at zero while the device
  control path repeatedly times out after a new launch;
* the episode rejection reason is the opaque ``device_unavailable``;
* safe fallback rewrites are already deduplicated.

No socket, credential, provider or live device is contacted.  The only device
surface is the existing authorized read/command adapter, replaced by a
recording fixture.
"""
import importlib
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import programme_overlay as po

A = "a" * 32
B = "b" * 32
PROVIDER_A = "nltv:us:FixtureOne.us"


def atomic_status(channel=A, launch="launch-1", stream="stream-1"):
    return {"launchID": launch, "currentChannelID": channel,
            "streamGeneration": stream, "surfPaused": False, "surfing": True,
            "playing": True}


def legacy_status(channel=A, launch="launch-1"):
    # Installed-IPA status without an atomic stream generation.
    return {"launchID": launch, "currentChannelID": channel,
            "surfPaused": False, "surfing": True, "playing": True}


class GatewayFixture(unittest.TestCase):
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
        self.selector = {"schemaVersion": 1, "deviceIP": "192.168.1.10",
                         "backendPort": 18797}

    def device_patches(self, status_fn, command_fn=None):
        command = command_fn or (lambda config, token, action, arguments=None:
                                 (_ for _ in ()).throw(AssertionError(action)))
        return [patch.object(self.g.prototype_selector, "load_selector",
                             return_value=self.selector),
                patch.object(self.g.prototype_selector, "device_status",
                             side_effect=status_fn),
                patch.object(self.g.prototype_selector, "device_command",
                             side_effect=command)]


class DeviceProbeCoalescingTests(GatewayFixture):
    def test_concurrent_callers_share_one_coherent_read(self):
        entered = threading.Event()
        reads = []

        def status(config, token):
            reads.append(1)
            entered.set()
            time.sleep(0.3)
            return dict(atomic_status())

        outcome = {}

        def worker():
            outcome["first"] = self.g.device_language_probe()

        patches = self.device_patches(status)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        thread = threading.Thread(target=worker, name="incident-probe")
        thread.start()
        try:
            self.assertTrue(entered.wait(2.0), "device read never started")
            second = self.g.device_language_probe()
        finally:
            thread.join(5.0)
        self.assertFalse(thread.is_alive())
        # The second caller arrived while the first read was in flight and must
        # reuse that exact coherent observation rather than issuing a second
        # 3s/8s device call.
        self.assertEqual(len(reads), 1)
        self.assertEqual(outcome["first"], second)
        self.assertEqual(outcome["first"]["channelID"], A)
        self.assertEqual(self.g.device_probe_diagnostics()["shared"], 1)

    def test_sequential_probes_still_read_a_fresh_launch(self):
        reads = []

        def status(config, token):
            reads.append(1)
            return dict(atomic_status(launch="L1" if len(reads) == 1 else "L2"))

        patches = self.device_patches(status)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        first = self.g.device_language_probe()
        second = self.g.device_language_probe()
        # No completed-probe cache: a launch change between calls is observed.
        self.assertEqual(first["launchID"], "L1")
        self.assertEqual(second["launchID"], "L2")
        self.assertEqual(len(reads), 2)
        self.assertEqual(self.g.device_probe_diagnostics()["shared"], 0)


class DeviceProbeFaultTests(GatewayFixture):
    def test_snapshot_timeout_is_not_reported_as_a_generic_device_failure(self):
        def status(config, token):
            return dict(legacy_status())

        def command(config, token, action, arguments=None):
            raise TimeoutError("get_player_snapshot timed out")

        patches = self.device_patches(status, command)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.assertIsNone(self.g.episode_probe())
        diagnostics = self.g.episode_probe_diagnostics
        self.assertEqual(diagnostics["last"]["reason"], "device_unavailable")
        self.assertEqual(diagnostics["last"]["detail"], "device_snapshot_unavailable")
        self.assertEqual(
            self.g.device_probe_diagnostics()["byReason"]["device_snapshot_unavailable"], 1)

    def test_incoherent_pairs_are_reported_distinctly(self):
        def status(config, token):
            return dict(legacy_status(channel=B))

        def command(config, token, action, arguments=None):
            return json.dumps({"channelID": A, "streamGeneration": "stream-1"})

        patches = self.device_patches(status, command)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.assertIsNone(self.g.device_language_probe())
        self.assertEqual(self.g.device_probe_diagnostics()["fault"], "device_incoherent")

    def test_trial_status_exposes_the_probe_breakdown(self):
        status = self.g.episode_audio_status()
        self.assertIn("deviceProbe", status)
        self.assertIn("byReason", status["deviceProbe"])


class CombinedFallbackDedupTests(unittest.TestCase):
    class Transport:
        def __init__(self):
            self.calls = []

        def set_value(self, field, value):
            self.calls.append((field, value))

    def scope(self, **overrides):
        values = dict(channel_id=A, provider_id=PROVIDER_A,
                      channel_name="Fixture One", launch_id="launch-1",
                      visit_id="visit-1", stream_generation="stream-1")
        values.update(overrides)
        return po.OverlayScope(**values)

    def test_reload_inside_one_visit_does_not_rewrite_the_fallback(self):
        transport = self.Transport()
        publisher = po.ProgrammeOverlayPublisher(transport)
        self.assertEqual(publisher.clear(scope=self.scope()), "fallback")
        self.assertEqual(len(transport.calls), 3)
        transport.calls.clear()
        # A reload changes only the stream generation. The fallback node is
        # already safe for this physical visit, so three device writes are
        # avoided.
        reloaded = self.scope(stream_generation="stream-2")
        self.assertEqual(publisher.clear(scope=reloaded), "fallback")
        self.assertEqual(transport.calls, [])
        self.assertEqual(publisher.publish(reloaded, "Verified label", "e2"),
                         "published")
        self.assertEqual(transport.calls, [
            (po.FIELD, "Verified label"),
            (po.READY_FIELD, po.TRUE),
            (po.FALLBACK_FIELD, po.FALSE),
        ])

    def test_a_new_launch_still_rewrites_the_safe_fallback(self):
        transport = self.Transport()
        publisher = po.ProgrammeOverlayPublisher(transport)
        self.assertEqual(publisher.clear(scope=self.scope()), "fallback")
        transport.calls.clear()
        self.assertEqual(
            publisher.clear(scope=self.scope(launch_id="launch-2",
                                             visit_id="visit-2",
                                             stream_generation="stream-3")),
            "fallback")
        self.assertEqual(transport.calls, [
            (po.READY_FIELD, po.FALSE),
            (po.FALLBACK_FIELD, po.TRUE),
            (po.FIELD, ""),
        ])


class CombinedEvidenceJobTests(GatewayFixture):
    class LanguageBridge:
        def __init__(self):
            self.current_channel_id = ""
            self.latest_snapshot = None
            self.events = []

        def observe_channel(self, cid, name):
            self.current_channel_id = cid
            return "published"

        def publish_evidence(self, cid, name, evidence):
            self.events.append((cid, name, evidence))
            return "published"

        def status(self):
            return {"status": "published", "fault": "", "pending": False}

        def pump(self):
            return "published"

        def confirm_correction(self, *args):
            pass

    class OverlayTransport:
        def __init__(self):
            self.calls = []

        def set_value(self, field, value):
            self.calls.append((field, value))

    def setUp(self):
        super().setUp()
        self.g.channels[A] = {"Id": A, "Name": "Nurse Jackie", "Type": "TvChannel"}
        self.g.sources[A] = PROVIDER_A

    def row(self):
        # Bare series title: the combined label must still appear because the
        # language evidence is verified for this exact visit.
        return {"title": "Nurse Jackie"}

    def probe(self, stream="stream-1"):
        return {"channelID": A, "launchID": "launch-1",
                "streamGeneration": stream, "surfPaused": False,
                "surfing": True, "playing": True}

    def seed(self):
        return self.g.language_visit_identity().observe(
            A, "launch-1", "stream-1")["visitID"]

    def job(self, visit_id):
        payload = {"launchID": "launch-1", "streamGeneration": "stream-1",
                   "visitID": visit_id}
        info = {"eventID": "evt-1", "count": 1, "threshold": 25,
                "outcome": "allowed"}
        self.assertTrue(self.g.overlay_publish_evidence(
            A, "Nurse Jackie", "en", "English", 0.97, payload, info))
        job = self.g.overlay_jobs.get_nowait()
        self.assertEqual(job["launchID"], "launch-1")
        self.assertEqual(job["visitID"], visit_id)
        return job

    def test_confirmed_evidence_reaches_the_combined_display(self):
        visit_id = self.seed()
        transport = self.OverlayTransport()
        publisher = po.ProgrammeOverlayPublisher(transport)
        bridge = self.LanguageBridge()
        with patch.object(self.g, "overlay_bridge_instance", return_value=bridge), \
                patch.object(self.g, "programme_overlay_instance",
                             return_value=publisher), \
                patch.object(self.g, "device_language_probe",
                             return_value=self.probe()), \
                patch.object(self.g, "programme_snapshot",
                             return_value={A: self.row()}):
            result = self.g._overlay_process(self.job(visit_id), threading.Event())
        self.assertEqual(result, "published")
        self.assertEqual(publisher.status()["published"], 1)
        self.assertIn((po.FIELD,
                       "Nurse Jackie English — 1/25 — 97.0%"), transport.calls)
        self.assertIn((po.READY_FIELD, po.TRUE), transport.calls)
        stats = self.g.overlay_combined_status()["stats"]
        self.assertEqual(stats["published"], 1)

    def test_a_failed_recheck_never_switches_the_stale_label_on(self):
        visit_id = self.seed()
        transport = self.OverlayTransport()
        publisher = po.ProgrammeOverlayPublisher(transport)
        bridge = self.LanguageBridge()
        probes = [self.probe(), None]

        def device(*args, **kwargs):
            return probes.pop(0)

        with patch.object(self.g, "overlay_bridge_instance", return_value=bridge), \
                patch.object(self.g, "programme_overlay_instance",
                             return_value=publisher), \
                patch.object(self.g, "device_language_probe", side_effect=device), \
                patch.object(self.g, "programme_snapshot",
                             return_value={A: self.row()}):
            result = self.g._overlay_process(self.job(visit_id), threading.Event())
        # The language bridge may report published, but the combined node must
        # stay off because the fresh presentation recheck failed.
        self.assertEqual(result, "published")
        self.assertEqual(publisher.status()["published"], 0)
        self.assertNotIn((po.READY_FIELD, po.TRUE), transport.calls)
        self.assertIn((po.FALLBACK_FIELD, po.TRUE), transport.calls)
        self.assertIn(self.g.overlay_combined_status()["stats"]["lastResult"],
                      ("fallback", "scope_changed", "unverified"))


if __name__ == "__main__":
    unittest.main()
