"""Focused fixtures for foreground PlaybackInfo admission recovery.

A failed device status read starts a short per-device backoff.  Background
catalogue/policy consumers must keep failing closed immediately, while a
foreground English PlaybackInfo may wait that backoff out only when a fresh
category read plus the worst-case upstream resolution still fit inside the
native request deadline.

No socket, credential, provider or live device is contacted.  The device and
provider boundaries are replaced by recording fixtures.
"""
import importlib
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
import prototype_selector as ps

CID = "c" * 32
SOURCE = "nltv:us:FixtureOne.us"
TOKEN = "fixture-device-token"


def status(category):
    return {"preferences": {"surfCategory": category}}


class RecoveryFixture(unittest.TestCase):
    def setUp(self):
        ps.reset_status_observations()
        self.addCleanup(ps.reset_status_observations)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = {"JEALOUSFIN_STATE_DIR": self.temp.name,
               "JEALOUSFIN_USERNAME": "fixture",
               "JEALOUSFIN_PASSWORD": "fixture",
               "JEALOUSFIN_TOKEN": "fixture-token",
               "JEALOUSFIN_SERVER_ID": "fixture-server",
               "JEALOUSFIN_BIND": "127.0.0.1"}
        with patch.dict(os.environ, env):
            import gateway
            self.g = importlib.reload(gateway)
        self.selector = {"schemaVersion": 1, "deviceIP": "192.168.1.10",
                         "backendPort": 18797}
        self.g.sources = {CID: SOURCE}
        self.key = self.g._device_probe_key(self.selector)

    def arm_backoff(self, clock, seconds=2.0, failures=1):
        now = clock[0]
        self.g.DEVICE_PROBE_BACKOFF[self.key] = {
            "failures": failures, "until": now + seconds,
            "at": now, "attemptAt": now, "reason": "device_status_unavailable"}

    def fake_wait(self, clock):
        def wait(step):
            clock[0] += step
        return wait

    def test_plan_reserves_worst_case_provider_and_selector_timeouts(self):
        clock = [100.0]
        self.arm_backoff(clock, 4.5)
        with patch.object(self.g, "DEVICE_PROBE_CLOCK", lambda: clock[0]):
            plan = self.g._playback_admission_plan(
                self.selector, CID, now=clock[0])
        self.assertIsNotNone(plan)
        wait, deadline = plan
        self.assertEqual(wait, 4.5)
        # 20 s request budget - 8 s provider - 6 s selector wait - 1 s margin.
        self.assertAlmostEqual(deadline - clock[0], 5.0)

    def test_two_base_resolution_has_no_safe_budget_and_is_refused(self):
        clock = [100.0]
        with patch.object(self.g, "DEVICE_PROBE_CLOCK", lambda: clock[0]):
            self.g.gateway_config.update(
                ukManifest="https://provider.example/manifest.json",
                ukTrialProviderIds=[SOURCE], ukTrialEnabled=False)
            self.arm_backoff(clock, 0.5)
            self.assertIsNone(self.g._playback_admission_plan(
                self.selector, CID, now=clock[0]))
            plan = self.g._playback_admission_plan(
                self.selector, CID, now=clock[0] + 0.6)
            self.assertIsNone(plan)

    def test_refused_budget_makes_no_device_or_provider_calls(self):
        clock = [100.0]
        device_calls = []
        with patch.object(self.g, "DEVICE_PROBE_CLOCK", lambda: clock[0]), \
                patch.object(self.g.prototype_selector, "device_status",
                             side_effect=lambda *a, **k: device_calls.append(1)), \
                patch.object(self.g, "PLAYBACK_ADMISSION_WAIT",
                             side_effect=AssertionError("must not wait")):
            self.arm_backoff(clock, 6.0)
            with self.assertRaises(RuntimeError):
                self.g.selected_profile_admitted(
                    self.selector, TOKEN, foreground=True, cid=CID)
        self.assertEqual(device_calls, [])
        self.assertEqual(self.g.PLAYBACK_ADMISSION_STATS["refused"], 1)

    def test_elapsed_initial_read_consumes_recovery_budget(self):
        clock = [104.0]
        self.arm_backoff(clock, 2.0)
        with patch.object(self.g, "DEVICE_PROBE_CLOCK", lambda: clock[0]):
            self.assertIsNone(self.g._playback_admission_plan(
                self.selector, CID, started=100.0))

    def test_rate_limit_remaining_is_reserved(self):
        clock = [100.0]
        self.arm_backoff(clock, 0.0)
        with patch.object(self.g, "DEVICE_PROBE_CLOCK", lambda: clock[0]):
            plan = self.g._playback_admission_plan(self.selector, CID)
        self.assertAlmostEqual(plan[0], self.g.DEVICE_PROBE_MIN_SPACING)

    def test_selector_only_backoff_uses_foreground_recovery(self):
        with patch.object(ps, "status_observation_remaining", return_value=0.5), \
                patch.object(self.g, "_playback_profile_recovery",
                             return_value="american") as recovery:
            self.assertEqual(self.g.selected_profile_admitted(
                self.selector, TOKEN, foreground=True, cid=CID), "american")
            recovery.assert_called_once()

    def test_initial_failed_read_passes_original_start_to_recovery(self):
        clock = [100.0]
        def failed(*args):
            clock[0] += 3.0
            raise RuntimeError("status unavailable")
        with patch.object(self.g, "DEVICE_PROBE_CLOCK", lambda: clock[0]), \
                patch.object(ps, "status_observation_remaining", side_effect=[0, 2]), \
                patch.object(ps, "selected_profile", side_effect=failed), \
                patch.object(self.g, "_playback_profile_recovery",
                             return_value="american") as recovery:
            self.assertEqual(self.g.selected_profile_admitted(
                self.selector, TOKEN, foreground=True, cid=CID), "american")
            recovery.assert_called_once_with(self.selector, TOKEN, CID, 100.0)

    def test_foreground_reservation_defers_background_probe(self):
        key = self.g._playback_flight_key(self.selector, TOKEN)
        self.g.PLAYBACK_ADMISSION_FLIGHTS[key] = self.g._PlaybackAdmissionFlight(key)
        try:
            with patch.object(ps, "selected_profile", return_value="american") as read:
                with self.assertRaises(RuntimeError):
                    self.g.selected_profile_admitted(self.selector, TOKEN)
                read.assert_not_called()
                self.assertEqual(self.g.selected_profile_admitted(
                    self.selector, TOKEN, _recovery=True), "american")
                read.assert_called_once()
        finally:
            self.g.PLAYBACK_ADMISSION_FLIGHTS.pop(key, None)

    def test_background_consumer_stays_fail_closed_and_does_not_wait(self):
        clock = [100.0]
        selected = []
        with patch.object(self.g, "DEVICE_PROBE_CLOCK", lambda: clock[0]), \
                patch.object(self.g, "PLAYBACK_ADMISSION_WAIT",
                             side_effect=AssertionError("must not wait")), \
                patch.object(self.g.prototype_selector, "selected_profile",
                             side_effect=lambda *a, **k: selected.append(1) or "malay"):
            # Remaining backoff fits the budget, but this is a background call.
            self.arm_backoff(clock, 0.5)
            with self.assertRaises(RuntimeError):
                self.g.selected_profile_admitted(self.selector, TOKEN)
        self.assertEqual(selected, [])
        self.assertEqual(self.g.PLAYBACK_ADMISSION_STATS["started"], 0)

    def blocking_wait(self, clock, waiting, release):
        def wait(step):
            waiting.set()
            self.assertTrue(release.wait(2.0))
            clock[0] += step
        return wait

    def test_foreground_recovery_coalesces_one_fresh_category_read(self):
        clock = [100.0]
        reads = []
        waiting = threading.Event()
        release = threading.Event()

        def device(config, token):
            reads.append(1)
            return status("Movies & Series")

        results = []
        errors = []

        def caller():
            try:
                results.append(self.g.selected_profile_admitted(
                    self.selector, TOKEN, foreground=True, cid=CID))
            except Exception as error:
                errors.append(error)

        with patch.object(self.g, "DEVICE_PROBE_CLOCK", lambda: clock[0]), \
                patch.object(self.g, "PLAYBACK_ADMISSION_WAIT",
                             side_effect=self.blocking_wait(
                                 clock, waiting, release)), \
                patch.object(self.g.prototype_selector, "device_status",
                             side_effect=device):
            self.arm_backoff(clock, 2.0)
            owner = threading.Thread(target=caller, name="admission-owner")
            owner.start()
            self.assertTrue(waiting.wait(2.0), "admission wait never started")
            flight = list(self.g.PLAYBACK_ADMISSION_FLIGHTS.values())[0]
            waiter_thread = threading.Thread(target=caller, name="admission-waiter")
            waiter_thread.start()
            deadline = time.monotonic() + 2.0
            while flight.waiters < 1 and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(flight.waiters, 1)
            release.set()
            owner.join(3.0)
            waiter_thread.join(3.0)

        self.assertFalse(owner.is_alive())
        self.assertFalse(waiter_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, ["malay", "malay"])
        self.assertEqual(len(reads), 1)
        stats = self.g.PLAYBACK_ADMISSION_STATS
        self.assertEqual((stats["started"], stats["shared"], stats["recovered"]),
                         (1, 1, 1))

    def test_recovery_observes_a_category_change_without_caching(self):
        clock = [100.0]
        categories = ["Movies & Series", "All Channels"]
        calls = []

        def device(config, token):
            calls.append(1)
            return status(categories.pop(0))

        with patch.object(self.g, "DEVICE_PROBE_CLOCK", lambda: clock[0]), \
                patch.object(self.g, "PLAYBACK_ADMISSION_WAIT",
                             side_effect=self.fake_wait(clock)), \
                patch.object(self.g.prototype_selector, "device_status",
                             side_effect=device):
            self.arm_backoff(clock, 2.0)
            first = self.g.selected_profile_admitted(
                self.selector, TOKEN, foreground=True, cid=CID)
            self.arm_backoff(clock, 2.0)
            second = self.g.selected_profile_admitted(
                self.selector, TOKEN, foreground=True, cid=CID)
        self.assertEqual((first, second), ("malay", "american"))
        self.assertEqual(len(calls), 2)

    def test_failure_wakes_waiters_without_an_extra_read(self):
        clock = [100.0]
        reads = []
        waiting = threading.Event()
        release = threading.Event()

        def device(config, token):
            reads.append(1)
            raise RuntimeError("Prototype device status unavailable")

        errors = []

        def caller():
            try:
                self.g.selected_profile_admitted(
                    self.selector, TOKEN, foreground=True, cid=CID)
            except Exception as error:
                errors.append(error)

        with patch.object(self.g, "DEVICE_PROBE_CLOCK", lambda: clock[0]), \
                patch.object(self.g, "PLAYBACK_ADMISSION_WAIT",
                             side_effect=self.blocking_wait(
                                 clock, waiting, release)), \
                patch.object(self.g.prototype_selector, "device_status",
                             side_effect=device):
            self.arm_backoff(clock, 2.0)
            owner = threading.Thread(target=caller, name="admission-failure-owner")
            owner.start()
            self.assertTrue(waiting.wait(2.0), "admission wait never started")
            flight = list(self.g.PLAYBACK_ADMISSION_FLIGHTS.values())[0]
            waiter_thread = threading.Thread(target=caller,
                                             name="admission-failure-waiter")
            waiter_thread.start()
            deadline = time.monotonic() + 2.0
            while flight.waiters < 1 and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(flight.waiters, 1)
            release.set()
            owner.join(3.0)
            waiter_thread.join(3.0)

        self.assertFalse(owner.is_alive())
        self.assertFalse(waiter_thread.is_alive())
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(errors), 2)
        self.assertEqual(self.g.PLAYBACK_ADMISSION_STATS["failures"], 1)
        self.assertIn(self.key, self.g.DEVICE_PROBE_BACKOFF)

    def test_flight_key_scopes_credentials_without_exposing_them(self):
        other = dict(self.selector, deviceIP="192.168.1.11")
        first = self.g._playback_flight_key(self.selector, TOKEN)
        self.assertEqual(first, self.g._playback_flight_key(self.selector, TOKEN))
        self.assertNotEqual(first, self.g._playback_flight_key(self.selector, "other"))
        self.assertNotEqual(first, self.g._playback_flight_key(other, TOKEN))
        self.assertNotIn(TOKEN, repr(first))
        self.assertNotIn(TOKEN, str(first))

    def test_recovery_is_restricted_to_english_playback_info(self):
        path = "/Items/" + CID + "/PlaybackInfo"
        self.assertTrue(self.g.playback_admission_foreground(path, CID, False))
        self.assertFalse(self.g.playback_admission_foreground(path, CID, True))
        self.assertFalse(self.g.playback_admission_foreground(
            "/Users/u/FavoriteItems/" + CID, CID, False))
        self.assertFalse(self.g.playback_admission_foreground(path, "d" * 32, False))


if __name__ == "__main__":
    unittest.main()
