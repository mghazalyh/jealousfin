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


class DeviceProbeSingleFlightDefectTests(GatewayFixture):
    def start_patches(self, status_fn, command_fn=None):
        patches = self.device_patches(status_fn, command_fn)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_waiter_timeout_fails_closed_without_a_duplicate_owner(self):
        entered = threading.Event()
        release = threading.Event()
        reads = []

        def status(config, token):
            reads.append(1)
            entered.set()
            release.wait(5.0)
            return dict(atomic_status(launch="L-timeout"))

        self.start_patches(status)
        outcome = {}

        def owner():
            outcome["value"] = self.g.device_language_probe()

        with patch.object(self.g, "DEVICE_PROBE_WAIT_SECONDS", 0.1):
            thread = threading.Thread(target=owner, name="probe-owner")
            thread.start()
            try:
                self.assertTrue(entered.wait(2.0), "owner never reached the device")
                started = time.monotonic()
                self.assertIsNone(self.g.device_language_probe())
                self.assertLess(time.monotonic() - started, 2.0)
                # The timed-out waiter must not start a second device read.
                self.assertEqual(len(reads), 1)
                self.assertEqual(
                    self.g.device_probe_diagnostics()["timeouts"], 1)
            finally:
                release.set()
                thread.join(5.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["value"]["launchID"], "L-timeout")
        self.assertEqual(len(reads), 1)
        # The gate is not wedged: after the owner completes, a new call starts
        # an independent fresh observation.
        self.assertEqual(self.g.device_language_probe()["launchID"], "L-timeout")
        self.assertEqual(len(reads), 2)

    def test_owner_exception_completes_the_flight_and_does_not_wedge_the_gate(self):
        entered = threading.Event()
        fail = threading.Event()
        calls = []

        def probe_once():
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                fail.wait(5.0)
                raise RuntimeError("device read exploded")
            return dict(atomic_status(launch="L-recovered"))

        errors = {}
        outcome = {}

        def call(key):
            try:
                outcome[key] = self.g.device_language_probe()
            except BaseException as error:
                errors[key] = error

        with patch.object(self.g, "_device_language_probe_once",
                          side_effect=probe_once), \
                patch.object(self.g, "DEVICE_PROBE_WAIT_SECONDS", 5.0):
            owner = threading.Thread(target=call, args=("owner",), name="probe-owner")
            owner.start()
            self.assertTrue(entered.wait(2.0), "owner never started")
            flight = self.g.DEVICE_PROBE_CURRENT
            waiting = threading.Event()
            original_wait = flight.event.wait

            def observed_wait(timeout=None):
                waiting.set()
                return original_wait(timeout)

            flight.event.wait = observed_wait
            waiter = threading.Thread(target=call, args=("waiter",), name="probe-waiter")
            waiter.start()
            self.assertTrue(waiting.wait(2.0), "waiter never joined the flight")
            fail.set()
            owner.join(5.0)
            waiter.join(5.0)
            # A later call starts a new owner instead of hanging on the failed
            # flight.
            self.assertEqual(self.g.device_language_probe()["launchID"], "L-recovered")
        self.assertFalse(owner.is_alive())
        self.assertFalse(waiter.is_alive())
        self.assertEqual(str(errors["owner"]), "device read exploded")
        self.assertEqual(str(errors["waiter"]), "device read exploded")
        self.assertNotIn("owner", outcome)
        self.assertNotIn("waiter", outcome)
        self.assertEqual(len(calls), 2)

    def test_waiter_bound_to_old_flight_ignores_a_newer_flight(self):
        first_entered = threading.Event()
        first_release = threading.Event()
        second_entered = threading.Event()
        second_release = threading.Event()
        reads = []

        def status(config, token):
            index = len(reads)
            reads.append(1)
            if index == 0:
                first_entered.set()
                first_release.wait(5.0)
                return dict(atomic_status(launch="L1"))
            if index == 1:
                second_entered.set()
                second_release.wait(5.0)
                return dict(atomic_status(launch="L2"))
            return dict(atomic_status(launch="L3"))

        self.start_patches(status)
        outcome = {}

        def call(key):
            outcome[key] = self.g.device_language_probe()

        first = threading.Thread(target=call, args=("first",), name="probe-first")
        first.start()
        self.assertTrue(first_entered.wait(2.0), "first owner never started")
        first_flight = self.g.DEVICE_PROBE_CURRENT
        waiting = threading.Event()
        proceed = threading.Event()
        original_wait = first_flight.event.wait

        def held_wait(timeout=None):
            # Hold the waiter after the old owner completes so a newer flight can
            # begin before the waiter reacquires the lock.
            waiting.set()
            proceed.wait(5.0)
            return original_wait(timeout)

        first_flight.event.wait = held_wait
        waiter = threading.Thread(target=call, args=("waiter",), name="probe-waiter")
        waiter.start()
        self.assertTrue(waiting.wait(2.0), "waiter never joined the first flight")
        first_release.set()
        first.join(5.0)
        self.assertFalse(first.is_alive())

        second = threading.Thread(target=call, args=("second",), name="probe-second")
        second.start()
        try:
            self.assertTrue(second_entered.wait(2.0), "newer flight never started")
            proceed.set()
            waiter.join(5.0)
        finally:
            second_release.set()
            second.join(5.0)
        self.assertFalse(waiter.is_alive())
        self.assertFalse(second.is_alive())
        # The waiter refers to the flight it joined, not the newer L2 flight.
        self.assertEqual(outcome["first"]["launchID"], "L1")
        self.assertEqual(outcome["waiter"]["launchID"], "L1")
        self.assertEqual(outcome["second"]["launchID"], "L2")
        self.assertEqual(len(reads), 2)

    def test_fresh_probe_fault_is_isolated_from_the_inflight_shared_flight(self):
        entered = threading.Event()
        release = threading.Event()
        reads = []

        def status(config, token):
            reads.append(1)
            if len(reads) == 1:
                entered.set()
                release.wait(5.0)
                return dict(atomic_status(launch="L-shared"))
            raise RuntimeError("fresh status unavailable")

        self.start_patches(status)
        outcome = {}

        def owner():
            outcome["value"] = self.g.device_language_probe()

        thread = threading.Thread(target=owner, name="probe-owner")
        thread.start()
        try:
            self.assertTrue(entered.wait(2.0), "shared owner never started")
            # Fresh observation runs independently and fails on its own read.
            self.assertIsNone(self.g.device_language_probe(share=False))
            self.assertEqual(self.g._device_probe_local_fault(),
                             "device_status_unavailable")
            # It must not contaminate the still-running shared flight.
            midpoint = self.g.device_probe_diagnostics()
            self.assertEqual(midpoint["fault"], "")
            self.assertNotIn("device_status_unavailable", midpoint["byReason"])
        finally:
            release.set()
            thread.join(5.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["value"]["launchID"], "L-shared")
        diagnostics = self.g.device_probe_diagnostics()
        self.assertEqual(diagnostics["fault"], "")
        self.assertNotIn("device_status_unavailable", diagnostics["byReason"])
        self.assertEqual(diagnostics["probes"], 1)
        self.assertEqual(len(reads), 2)


class DeviceProbeAdmissionBackoffTests(GatewayFixture):
    def start_patches(self, status_fn, command_fn=None):
        patches = self.device_patches(status_fn, command_fn)
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_failed_shared_probe_opens_backoff_and_retries_after_the_window(self):
        clock = [100.0]
        reads = []

        def status(config, token):
            reads.append(1)
            if len(reads) == 1:
                raise RuntimeError("status unavailable")
            return dict(atomic_status(launch="L2", stream="S2"))

        self.start_patches(status)
        with patch.object(self.g, "DEVICE_PROBE_CLOCK",
                          side_effect=lambda: clock[0]):
            self.assertIsNone(self.g.device_language_probe(origin="episode"))
            diagnostics = self.g.device_probe_diagnostics()
            self.assertEqual(diagnostics["probes"], 1)
            self.assertEqual(diagnostics["failures"], 1)
            self.assertEqual(diagnostics["backoffStarted"], 1)
            self.assertEqual(
                diagnostics["byReason"]["device_status_unavailable"], 1)
            self.assertTrue(diagnostics["backoff"]["active"])
            self.assertEqual(
                diagnostics["backoff"]["remainingSeconds"], 2.0)

            clock[0] = 101.0
            self.assertIsNone(self.g.device_language_probe(origin="episode"))
            self.assertEqual(len(reads), 1)
            diagnostics = self.g.device_probe_diagnostics()
            self.assertEqual(diagnostics["rejectedBackoff"], 1)
            self.assertEqual(
                diagnostics["byOrigin"]["episode"]["rejected"], 1)

            clock[0] = 103.0
            value = self.g.device_language_probe(origin="episode")
            self.assertEqual(value["launchID"], "L2")
            self.assertEqual(len(reads), 2)
            diagnostics = self.g.device_probe_diagnostics()
            self.assertFalse(diagnostics["backoff"]["active"])
            self.assertEqual(diagnostics["recovered"], 1)
            self.assertEqual(
                diagnostics["byOrigin"]["episode"]["admitted"], 2)

    def test_backoff_escalates_to_the_cap_and_resets_after_a_quiet_window(self):
        clock = [100.0]
        failing = [True]

        def status(config, token):
            if failing[0]:
                raise RuntimeError("status unavailable")
            return dict(atomic_status(launch="L-reset"))

        self.start_patches(status)
        with patch.object(self.g, "DEVICE_PROBE_CLOCK",
                          side_effect=lambda: clock[0]):
            delays = (2.0, 4.0, 8.0, 16.0, 20.0)
            for index, delay in enumerate(delays):
                self.assertIsNone(self.g.device_language_probe(origin="episode"))
                diagnostics = self.g.device_probe_diagnostics()
                self.assertEqual(
                    diagnostics["backoff"]["remainingSeconds"], delay)
                if index == len(delays) - 1:
                    self.assertEqual(
                        diagnostics["backoff"]["consecutiveFailures"], 5)
                else:
                    clock[0] += delay

            failing[0] = False
            clock[0] = 251.0
            value = self.g.device_language_probe(origin="episode")
            self.assertEqual(value["launchID"], "L-reset")
            diagnostics = self.g.device_probe_diagnostics()
            self.assertFalse(diagnostics["backoff"]["active"])
            self.assertEqual(self.g.DEVICE_PROBE_BACKOFF, {})

    def test_fresh_probe_never_returns_the_previous_positive_identity(self):
        clock = [100.0]
        reads = []

        def status(config, token):
            reads.append(1)
            if len(reads) == 1:
                return dict(atomic_status(launch="L1", stream="S1"))
            if len(reads) == 2:
                raise RuntimeError("status unavailable")
            return dict(atomic_status(launch="L3", stream="S3"))

        self.start_patches(status)
        with patch.object(self.g, "DEVICE_PROBE_CLOCK",
                          side_effect=lambda: clock[0]):
            first = self.g.device_language_probe(share=False, origin="fresh")
            self.assertEqual(first["channelID"], A)
            self.assertIsNone(
                self.g.device_language_probe(share=False, origin="fresh"))

            clock[0] = 100.5
            self.assertIsNone(
                self.g.device_language_probe(share=False, origin="fresh"))
            self.assertEqual(len(reads), 2)
            diagnostics = self.g.device_probe_diagnostics()
            self.assertEqual(diagnostics["freshAdmitted"], 2)
            self.assertEqual(diagnostics["freshRejected"], 1)
            self.assertEqual(
                diagnostics["byOrigin"]["fresh"]["failures"], 1)
            self.assertTrue(diagnostics["backoff"]["active"])

            clock[0] = 103.0
            recovered = self.g.device_language_probe(share=False,
                                                     origin="fresh")
            self.assertEqual(recovered["launchID"], "L3")
            self.assertEqual(self.g.device_probe_diagnostics()["recovered"], 1)

    def test_rotation_state_drops_cached_identity_when_recheck_fails(self):
        reads = []

        def status(config, token):
            reads.append(1)
            if len(reads) == 1:
                return dict(atomic_status(channel=A, launch="L1",
                                          stream="S1"))
            raise RuntimeError("status unavailable")

        self.start_patches(status)
        first = self.g.device_rotation_state(force=True)
        self.assertEqual(first["currentChannelID"], A)
        self.assertEqual(first["launchID"], "L1")
        self.assertIsNone(self.g.device_rotation_state(force=True))
        self.assertIsNone(self.g.device_rotation_cache)
        self.assertEqual(len(reads), 2)

    def test_selected_profile_fails_closed_while_backoff_is_active(self):
        clock = [100.0]

        def status(config, token):
            raise RuntimeError("status unavailable")

        self.start_patches(status)
        with patch.object(self.g, "DEVICE_PROBE_CLOCK",
                          side_effect=lambda: clock[0]), \
                patch.object(self.g.prototype_selector, "selected_profile",
                             return_value="malay") as selected:
            self.assertIsNone(self.g.device_language_probe(origin="episode"))
            with self.assertRaises(RuntimeError) as raised:
                self.g.selected_profile_admitted(self.selector, "token")
            self.assertEqual(str(raised.exception), "Device probe backoff")
            selected.assert_not_called()

            clock[0] = 103.0
            self.assertEqual(
                self.g.selected_profile_admitted(self.selector, "token"),
                "malay")
            selected.assert_called_once()
            self.assertEqual(self.g.device_probe_diagnostics()["recovered"], 1)


class OverlayPumpAdmissionTests(GatewayFixture):
    class PendingBridge:
        def __init__(self, pending=True):
            self.pending = pending

        def status(self):
            return {"pending": self.pending}

    def test_pending_pump_requests_coalesce_then_replace_a_stale_entry(self):
        clock = [100.0]
        bridge = self.PendingBridge()
        with patch.object(self.g, "overlay_bridge_state", bridge), \
                patch.object(self.g, "OVERLAY_ADMISSION_CLOCK",
                             side_effect=lambda: clock[0]):
            self.assertTrue(self.g.overlay_request_pump())
            self.assertEqual(self.g.overlay_jobs.qsize(), 1)
            self.assertFalse(self.g.overlay_request_pump())
            self.assertEqual(self.g.overlay_jobs.qsize(), 1)
            clock[0] = 100.0 + self.g.OVERLAY_PUMP_QUEUE_TTL + 0.1
            self.assertTrue(self.g.overlay_request_pump())
            self.assertEqual(self.g.overlay_jobs.qsize(), 2)
        stats = dict(self.g.overlay_admission_stats)
        self.assertEqual(stats["pumpRequests"], 3)
        self.assertEqual(stats["pumpEnqueued"], 2)
        self.assertEqual(stats["pumpCoalesced"], 1)
        self.assertEqual(stats["pumpStale"], 1)

    def test_minimum_spacing_blocks_a_new_pump_that_is_not_pending(self):
        clock = [100.0]
        bridge = self.PendingBridge()
        with patch.object(self.g, "overlay_bridge_state", bridge), \
                patch.object(self.g, "OVERLAY_ADMISSION_CLOCK",
                             side_effect=lambda: clock[0]):
            self.assertTrue(self.g.overlay_request_pump())
            with self.g.overlay_pump_lock:
                self.g.overlay_pump_pending = False

            clock[0] = 100.5
            self.assertFalse(self.g.overlay_request_pump())
            self.assertEqual(self.g.overlay_jobs.qsize(), 1)

            clock[0] = 101.1
            self.assertTrue(self.g.overlay_request_pump())
            self.assertEqual(self.g.overlay_jobs.qsize(), 2)
        stats = dict(self.g.overlay_admission_stats)
        self.assertEqual(stats["pumpRequests"], 3)
        self.assertEqual(stats["pumpEnqueued"], 2)
        self.assertEqual(stats["pumpCoalesced"], 1)

    def test_an_idle_bridge_does_not_record_or_enqueue_a_pump(self):
        clock = [100.0]
        bridge = self.PendingBridge(pending=False)
        with patch.object(self.g, "overlay_bridge_state", bridge), \
                patch.object(self.g, "OVERLAY_ADMISSION_CLOCK",
                             side_effect=lambda: clock[0]):
            self.assertFalse(self.g.overlay_request_pump())
        self.assertEqual(self.g.overlay_jobs.qsize(), 0)
        self.assertEqual(self.g.overlay_admission_stats["pumpRequests"], 0)


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

    def test_old_launch_evidence_and_state_jobs_are_rejected_before_publish(self):
        for kind in ("evidence", "state"):
            with self.subTest(kind=kind):
                transport = self.OverlayTransport()
                publisher = po.ProgrammeOverlayPublisher(transport)
                bridge = self.LanguageBridge()
                if kind == "evidence":
                    payload = {"launchID": "launch-old",
                               "streamGeneration": "stream-1", "visitID": ""}
                    info = {"eventID": "evt-old", "count": 1, "threshold": 25,
                            "outcome": "allowed"}
                    self.assertTrue(self.g.overlay_publish_evidence(
                        A, "Nurse Jackie", "en", "English", 0.97, payload, info))
                else:
                    self.assertTrue(self.g.overlay_publish_state(
                        {"channelID": A, "channelName": "Nurse Jackie",
                         "launchID": "launch-old",
                         "streamGeneration": "stream-1", "visitID": "",
                         "state": "waiting", "observedAt": time.time()}))
                job = self.g.overlay_jobs.get_nowait()
                with patch.object(self.g, "overlay_bridge_instance",
                                  return_value=bridge), \
                        patch.object(self.g, "programme_overlay_instance",
                                     return_value=publisher), \
                        patch.object(self.g, "device_language_probe",
                                     return_value=self.probe()), \
                        patch.object(self.g, "programme_snapshot",
                                     return_value={A: self.row()}):
                    result = self.g._overlay_process(job, threading.Event())
                self.assertEqual(result, "stale")
                self.assertEqual(self.g.overlay_bridge_fault, "stale_launch")
                self.assertEqual(bridge.events, [])
                self.assertEqual(publisher.status()["published"], 0)

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
