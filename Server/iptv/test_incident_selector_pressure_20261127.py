"""Focused selector-pressure fixtures for the installed-IPA gateway lane.

The gateway calls ``prototype_selector.selected_profile`` on ordinary
``/LiveTv/Channels``, ``/LiveTv/Programs`` and ``/Jealousfin/Policy`` requests.
Each call used to issue an independent 3 s device status read, so a slow or
timed-out device amplified client retries into more device reads.

These tests replace the device boundary with a recording fixture.  No socket,
credential, live device or production state is contacted.
"""
import threading
import time
import unittest
from unittest.mock import patch

import prototype_selector as ps

CFG = {"schemaVersion": 1, "deviceIP": "192.168.31.90", "backendPort": 18797}
OTHER = {"schemaVersion": 1, "deviceIP": "192.168.31.91", "backendPort": 18797}


def status(category):
    return {"preferences": {"surfCategory": category}}


class SelectorPressureTests(unittest.TestCase):
    def setUp(self):
        ps.reset_status_observations()
        self.addCleanup(ps.reset_status_observations)

    def test_concurrent_callers_share_one_in_flight_read(self):
        entered = threading.Event()
        release = threading.Event()
        reads = []

        def fake_status(config, token):
            reads.append(1)
            entered.set()
            self.assertTrue(release.wait(2.0))
            return status("Movies & Series")

        with patch.object(ps, "device_status", side_effect=fake_status):
            results = []
            errors = []

            def worker():
                try:
                    results.append(ps.selected_profile(CFG))
                except Exception as error:
                    errors.append(error)

            owner = threading.Thread(target=worker, name="selector-owner")
            owner.start()
            self.assertTrue(entered.wait(2.0), "device read never started")
            flight = ps.STATUS_FLIGHTS[(CFG["deviceIP"], ps.DEVICE_PORT)]
            callers = [threading.Thread(target=worker, name="selector-waiter")
                       for _ in range(7)]
            for caller in callers:
                caller.start()
            deadline = time.monotonic() + 2.0
            while flight.waiters < len(callers) and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(flight.waiters, len(callers))
            release.set()
            owner.join(3.0)
            for caller in callers:
                caller.join(3.0)

        self.assertFalse(owner.is_alive())
        self.assertFalse(any(caller.is_alive() for caller in callers))
        self.assertEqual(errors, [])
        self.assertEqual(results, ["malay"] * 8)
        self.assertEqual(len(reads), 1)
        self.assertEqual(ps.status_observation_diagnostics()["shared"], 7)

    def test_waiter_timeout_fails_closed_without_duplicate_read(self):
        entered = threading.Event()
        release = threading.Event()
        reads = []

        def fake_status(config, token):
            reads.append(1)
            entered.set()
            self.assertTrue(release.wait(2.0))
            return status("All Channels")

        outcome = {}
        with patch.object(ps, "device_status", side_effect=fake_status), \
                patch.object(ps, "STATUS_WAIT_SECONDS", 0.05):
            owner = threading.Thread(
                target=lambda: outcome.setdefault("value", ps.selected_profile(CFG)),
                name="selector-timeout-owner")
            owner.start()
            self.assertTrue(entered.wait(2.0), "device read never started")
            with self.assertRaises(RuntimeError) as error:
                ps.selected_profile(CFG)
            self.assertIn("observation timeout", str(error.exception))
            self.assertEqual(len(reads), 1)
            release.set()
            owner.join(3.0)

        self.assertFalse(owner.is_alive())
        self.assertEqual(outcome["value"], "american")
        with patch.object(ps, "device_status", return_value=status("Movies & Series")) as read:
            self.assertEqual(ps.selected_profile(CFG), "malay")
        self.assertEqual(read.call_count, 1)
        self.assertEqual(ps.status_observation_diagnostics()["timeouts"], 1)

    def test_owner_failure_wakes_waiters_and_starts_per_device_backoff(self):
        now = [1000.0]
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def fake_status(config, token):
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2.0))
            raise RuntimeError("Prototype device status unavailable")

        errors = []

        def worker():
            try:
                ps.selected_profile(CFG)
            except Exception as error:
                errors.append(error)

        with patch.object(ps, "device_status", side_effect=fake_status), \
                patch.object(ps, "STATUS_CLOCK", lambda: now[0]):
            owner = threading.Thread(target=worker, name="selector-failure-owner")
            owner.start()
            self.assertTrue(entered.wait(2.0), "device read never started")
            waiter = threading.Thread(target=worker, name="selector-failure-waiter")
            waiter.start()
            flight = ps.STATUS_FLIGHTS[(CFG["deviceIP"], ps.DEVICE_PORT)]
            deadline = time.monotonic() + 2.0
            while flight.waiters < 1 and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(flight.waiters, 1)
            release.set()
            owner.join(3.0)
            waiter.join(3.0)
            self.assertFalse(owner.is_alive())
            self.assertFalse(waiter.is_alive())
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(errors), 2)
            with self.assertRaises(RuntimeError) as error:
                ps.selected_profile(CFG)
            self.assertIn("backoff", str(error.exception))
            self.assertEqual(len(calls), 1)

    def test_failure_backoff_expires_and_preserves_user_transition(self):
        now = [1000.0]
        responses = [RuntimeError("Prototype device status unavailable"),
                     status("Movies & Series"), status("All Channels")]
        calls = []

        def fake_status(config, token):
            calls.append(1)
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(ps, "device_status", side_effect=fake_status), \
                patch.object(ps, "STATUS_CLOCK", lambda: now[0]), \
                patch.object(ps, "STATUS_BACKOFF_SECONDS", 2.0):
            with self.assertRaises(RuntimeError):
                ps.selected_profile(CFG)
            self.assertEqual(len(calls), 1)
            now[0] += 0.5
            with self.assertRaises(RuntimeError) as error:
                ps.selected_profile(CFG)
            self.assertIn("backoff", str(error.exception))
            self.assertEqual(len(calls), 1)
            now[0] += 2.0
            self.assertEqual(ps.selected_profile(CFG), "malay")
            self.assertEqual(len(calls), 2)
            self.assertEqual(ps.selected_profile(CFG), "american")
            self.assertEqual(len(calls), 3)

    def test_repeated_and_transitioning_categories_are_never_cached(self):
        responses = [status("Movies & Series"), status("All Channels"),
                     status("All Channels"), status("Favorites")]
        calls = []

        def fake_status(config, token):
            calls.append(1)
            return responses.pop(0)

        with patch.object(ps, "device_status", side_effect=fake_status):
            self.assertEqual(ps.selected_profile(CFG), "malay")
            self.assertEqual(ps.selected_profile(CFG), "american")
            self.assertEqual(ps.selected_profile(CFG), "american")
            self.assertEqual(ps.selected_profile(CFG), "american")
        self.assertEqual(len(calls), 4)

    def test_ambiguous_or_malformed_category_fails_closed(self):
        bad_statuses = [
            {},
            {"preferences": None},
            {"preferences": {}},
            {"preferences": {"surfCategory": "Unknown"}},
            {"preferences": {"surfCategory": 42}},
            {"preferences": ["Movies & Series"]},
        ]
        for bad in bad_statuses:
            ps.reset_status_observations()
            with patch.object(ps, "device_status", return_value=bad):
                with self.assertRaises(RuntimeError) as error:
                    ps.selected_profile(CFG)
                self.assertIn("category unavailable", str(error.exception))

    def test_backoff_is_scoped_to_one_device(self):
        calls = []

        def fake_status(config, token):
            calls.append(config["deviceIP"])
            if config["deviceIP"] == CFG["deviceIP"]:
                raise RuntimeError("Prototype device status unavailable")
            return status("Movies & Series")

        with patch.object(ps, "device_status", side_effect=fake_status):
            with self.assertRaises(RuntimeError):
                ps.selected_profile(CFG)
            self.assertEqual(ps.selected_profile(OTHER), "malay")
            with self.assertRaises(RuntimeError) as error:
                ps.selected_profile(CFG)
            self.assertIn("backoff", str(error.exception))
        self.assertEqual(calls, [CFG["deviceIP"], OTHER["deviceIP"]])


if __name__ == "__main__":
    unittest.main()
