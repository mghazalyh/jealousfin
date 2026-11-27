import random
import unittest

import programme_research as pr


class Clock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class ViewingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.viewing = pr.ActiveViewing(active_seconds=120, stale_seconds=45, clock=self.clock)
        self.cid = "0eae2441335acdf8ad434bc47d98b855"
        self.sid = "session-fixture-1"

    def start(self):
        self.assertTrue(self.viewing.issue(self.cid, self.sid))
        self.viewing.observe({"channelID": self.cid, "playSessionID": self.sid, "action": "start"}, wall=0)

    def progress(self, seconds, position, paused=False):
        self.clock.advance(seconds)
        self.viewing.observe({"channelID": self.cid, "playSessionID": self.sid, "action": "progress",
                              "position": position, "paused": paused}, wall=self.clock.value)

    def test_report_validation_is_bounded(self):
        good = {"ItemId": self.cid, "PlaySessionId": self.sid, "PositionTicks": 150000000, "IsPaused": False}
        report = pr.parse_playback_report(good, "progress")
        self.assertEqual(report["channelID"], self.cid)
        self.assertEqual(report["position"], 15.0)
        self.assertIsNone(pr.parse_playback_report({"ItemId": "bad", "PlaySessionId": self.sid}, "progress"))
        self.assertIsNone(pr.parse_playback_report(dict(good, IsPaused="false"), "progress"))
        self.assertIsNone(pr.parse_playback_report(dict(good, PositionTicks=-1), "progress"))
        self.assertIsNone(pr.parse_playback_report(good, "unknown"))

    def test_touch_or_issue_is_required_and_start_is_not_progress(self):
        self.assertIsNone(self.viewing.observe({"channelID": self.cid, "playSessionID": self.sid, "action": "progress", "position": 1}, wall=1))
        self.viewing.touch(self.cid)
        self.viewing.observe({"channelID": self.cid, "playSessionID": self.sid, "action": "start"}, wall=1)
        self.assertEqual(self.viewing.views(), {})
        self.progress(15, 5)
        self.assertIsNone(self.viewing.views().get(self.cid))
        self.progress(15, 20)
        self.assertEqual(self.viewing.views()[self.cid].active_seconds, 15)

    def test_native_start_position_is_only_credited_after_advancing_progress(self):
        self.assertTrue(self.viewing.issue(self.cid, self.sid))
        self.viewing.observe({"channelID": self.cid, "playSessionID": self.sid,
                              "action": "start", "position": 100, "paused": False})
        self.clock.advance(8)
        self.assertEqual(self.viewing.views(), {})
        self.progress(7, 114)
        self.assertEqual(self.viewing.views()[self.cid].active_seconds, 14)
        self.progress(15, 129)
        self.assertEqual(self.viewing.views()[self.cid].active_seconds, 29)

    def test_confirmed_seconds_require_real_progress_and_reset_on_stall(self):
        self.viewing.issue(self.cid, self.sid)
        self.viewing.observe({"channelID": self.cid, "playSessionID": self.sid,
                              "action": "start", "position": 100, "paused": False})
        self.progress(15, 114)
        self.assertEqual(self.viewing.views()[self.cid].confirmed_seconds, 14)
        self.clock.advance(5)
        self.assertEqual(self.viewing.views()[self.cid].confirmed_seconds, 14)
        self.progress(10, 129)
        self.assertEqual(self.viewing.views()[self.cid].confirmed_seconds, 29)
        self.progress(15, 129)
        self.assertEqual(self.viewing.views()[self.cid].confirmed_seconds, 0)
        self.progress(15, 1300)
        self.assertEqual(self.viewing.views()[self.cid].confirmed_seconds, 0)

    def test_seek_or_pause_cannot_backdate_start(self):
        for paused, position in [(False, 1000), (True, 110)]:
            with self.subTest(paused=paused, position=position):
                self.viewing = pr.ActiveViewing(clock=self.clock)
                self.viewing.issue(self.cid, self.sid)
                self.viewing.observe({"channelID": self.cid, "playSessionID": self.sid,
                                      "action": "start", "position": 100, "paused": False})
                self.progress(15, position, paused=paused)
                self.assertEqual(self.viewing.views(), {})

    def test_extended_viewing_requires_contiguous_progress(self):
        self.start()
        position = 0
        for _ in range(9):
            position += 15
            self.progress(15, position)
        view = self.viewing.views()[self.cid]
        self.assertGreaterEqual(view.active_seconds, 120)
        self.assertEqual(view.session_hash, pr._session_hash(self.sid))
        self.assertNotIn(self.sid, str(self.viewing.diagnostics()))

    def test_pause_stop_and_stale_do_not_count(self):
        self.start()
        for position in (15, 30, 45, 60):
            self.progress(15, position)
        self.assertTrue(self.viewing.views())
        self.progress(15, 60, paused=True)
        self.assertEqual(self.viewing.views(), {})
        self.progress(15, 75)
        for _ in range(8):
            self.progress(15, 75)
        self.progress(46, 75)
        self.assertEqual(self.viewing.views(), {})
        self.viewing.observe({"channelID": self.cid, "playSessionID": self.sid, "action": "stop"}, wall=500)
        self.assertEqual(self.viewing.views(), {})

    def test_active_switch_and_trace_correlation(self):
        other = "a" * 32
        self.start()
        for position in (15, 30, 45):
            self.progress(15, position)
        self.assertTrue(self.viewing.correlate_trace(self.cid, "trace-one"))
        self.assertTrue(self.viewing.views()[self.cid].trace_present)
        self.assertTrue(self.viewing.issue(other, "session-fixture-2"))
        self.viewing.observe({"channelID": other, "playSessionID": "session-fixture-2", "action": "start"}, wall=200)
        for position in (10, 20, 30):
            self.clock.advance(15)
            self.viewing.observe({"channelID": other, "playSessionID": "session-fixture-2", "action": "progress",
                                  "position": position}, wall=self.clock.value)
        self.assertIn(other, self.viewing.views())
        self.clock.advance(46)
        self.viewing.observe({"channelID": other, "playSessionID": "session-fixture-2", "action": "progress",
                              "position": 45}, wall=self.clock.value)
        self.clock.advance(15)
        self.viewing.observe({"channelID": other, "playSessionID": "session-fixture-2", "action": "progress",
                              "position": 60}, wall=self.clock.value)
        self.assertNotIn(self.cid, self.viewing.views())
        self.assertIn(other, self.viewing.views())


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.viewing = pr.ActiveViewing(active_seconds=120, stale_seconds=45, clock=self.clock)
        self.coordinator = pr.ResearchCoordinator(active_seconds=120, clock=self.clock,
                                                  rng=random.Random(7), retry_seconds=(45, 120, 300))
        self.cid = "0eae2441335acdf8ad434bc47d98b855"
        self.sid = "session-fixture-1"
        self.wall = 0

    def warm(self, seconds=121):
        self.viewing.issue(self.cid, self.sid)
        self.viewing.observe({"channelID": self.cid, "playSessionID": self.sid, "action": "start"}, wall=self.wall)
        position = 0
        while self.clock() - 1000 < seconds:
            self.clock.advance(15); self.wall += 15; position += 15
            self.viewing.observe({"channelID": self.cid, "playSessionID": self.sid, "action": "progress",
                                  "position": position}, wall=self.wall)
        return self.viewing.views()

    def test_low_then_high_after_no_evidence_without_six_hour_wait(self):
        views = self.warm()
        plan = self.coordinator.choose_active({self.cid}, views, self.wall, "low", {})
        self.assertEqual((plan.priority, plan.variant, plan.attempt), ("active", "low", 1))
        self.coordinator.record(plan, "missing", self.wall)
        self.assertIsNone(self.coordinator.choose_active({self.cid}, self.viewing.views(), self.wall, "low", {}))
        for _ in range(4):
            self.clock.advance(15); self.wall += 15
            self.viewing.observe({"channelID": self.cid, "playSessionID": self.sid, "action": "progress",
                                  "position": 200 + self.wall}, wall=self.wall)
        plan = self.coordinator.choose_active({self.cid}, self.viewing.views(), self.wall, "low", {})
        self.assertEqual((plan.variant, plan.attempt), ("high", 2))
        self.assertLess(plan.due_at - 0, 320)
        self.coordinator.record(plan, "unverified", self.wall)
        self.assertIsNone(self.coordinator.choose_active({self.cid}, self.viewing.views(), self.wall, "low", {}))

    def test_bounded_attempts_and_verified_stops(self):
        views = self.warm()
        plan = self.coordinator.choose_active({self.cid}, views, self.wall, "low", {})
        self.assertEqual(plan.attempt, 1)
        self.coordinator.record(plan, "verified", self.wall)
        self.assertIsNone(self.coordinator.choose_active({self.cid}, self.viewing.views(), self.wall + 100, "low", {}))
        self.coordinator = pr.ResearchCoordinator(active_seconds=120, clock=self.clock, rng=random.Random(7))
        position = 500
        for attempt in range(1, 4):
            for _ in range(4):
                self.clock.advance(15); self.wall += 15; position += 15
                self.viewing.observe({"channelID": self.cid, "playSessionID": self.sid, "action": "progress",
                                      "position": position}, wall=self.wall)
            plan = self.coordinator.choose_active({self.cid}, self.viewing.views(), self.wall, "low", {})
            self.assertEqual(plan.attempt, attempt)
            self.coordinator.record(plan, "missing", self.wall)
            for _ in range(10):
                self.clock.advance(15); self.wall += 15; position += 15
                self.viewing.observe({"channelID": self.cid, "playSessionID": self.sid, "action": "progress",
                                      "position": position}, wall=self.wall)
        self.assertIsNone(self.coordinator.choose_active({self.cid}, self.viewing.views(), self.wall, "low", {}))

    def test_engineering_high_is_preserved_and_dedup_crosses_windows(self):
        views = self.warm()
        plan = self.coordinator.choose_active({self.cid}, views, self.wall, "high", {})
        self.assertEqual((plan.variant, plan.attempt), ("high", 1))
        self.coordinator.record(plan, "missing", self.wall)
        self.viewing.observe({"channelID": self.cid, "playSessionID": self.sid, "action": "stop"}, wall=self.wall)
        new = "session-fixture-2"
        self.viewing.issue(self.cid, new)
        self.viewing.observe({"channelID": self.cid, "playSessionID": new, "action": "start"}, wall=self.wall)
        position = 0
        for _ in range(9):
            self.clock.advance(15); self.wall += 15; position += 15
            self.viewing.observe({"channelID": self.cid, "playSessionID": new, "action": "progress",
                                  "position": position}, wall=self.wall)
        self.assertIsNone(self.coordinator.choose_active({self.cid}, self.viewing.views(), self.wall + 10, "high", {}))
        self.assertIsNotNone(self.coordinator.choose_active({self.cid}, self.viewing.views(), self.wall + 301, "high", {}))

    def test_below_threshold_and_background_are_not_active(self):
        views = self.warm(60)
        self.assertIsNone(self.coordinator.choose_active({self.cid}, views, self.wall, "low", {}))

    def test_jag_850_second_historical_session_escalates_within_session(self):
        cid = "0eae2441335acdf8ad434bc47d98b855"
        session = "jag-20261127-000133"
        wall = 1795708893  # 2026-11-27 00:01:33 SGT
        self.viewing.issue(cid, session)
        self.viewing.observe({"channelID": cid, "playSessionID": session, "action": "start"}, wall=wall)
        self.wall = wall
        position = 0
        plans = []
        for elapsed in range(15, 851, 15):
            self.clock.advance(15); self.wall += 15; position += 15
            self.viewing.observe({"channelID": cid, "playSessionID": session, "action": "progress",
                                  "position": position}, wall=self.wall)
            plan = self.coordinator.choose_active({cid}, self.viewing.views(), self.wall, "low",
                                                  {cid: {"outcome": "missing", "at": plans[-1].due_at} if plans else {}})
            if plan:
                self.assertLessEqual(self.wall - wall, 850)
                plans.append(plan)
                self.coordinator.record(plan, "missing", self.wall)
        self.assertGreaterEqual(len(plans), 2)
        self.assertEqual(plans[0].variant, "low")
        self.assertEqual(plans[1].variant, "high")
        self.assertLess(plans[1].due_at - wall, 3600)

    def test_one_active_plan_at_a_time_and_other_candidates_wait(self):
        other = "c" * 32
        first = pr.ActiveView(self.cid, "session-a", "hash-a", "window-a", 200, 0, 1, False, True, False, 10)
        second = pr.ActiveView(other, "session-b", "hash-b", "window-b", 150, 0, 1, False, True, False, 10)
        views = {self.cid: first, other: second}
        plan = self.coordinator.choose_active({self.cid, other}, views, self.wall, "low", {})
        self.assertEqual(plan.channel_id, self.cid)
        self.coordinator.record(plan, "missing", self.wall)
        next_plan = self.coordinator.choose_active({self.cid, other}, views, self.wall, "low", {})
        self.assertEqual(next_plan.channel_id, other)


if __name__ == "__main__":
    unittest.main()
