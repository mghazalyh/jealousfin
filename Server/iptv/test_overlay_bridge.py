"""Fixture tests for the additive language overlay adapter.

No provider, credential, production gateway or live device is contacted.
The mock transport records exactly the command payload the existing
authorized device path would receive.
"""
import json
import pathlib
import unittest

import overlay_bridge as ob

ROOT = pathlib.Path(__file__).resolve().parents[2]
LAYOUT_PATH = ROOT / "Examples" / "language-evidence-overlay-20261127.json"
FIXTURE_PATH = pathlib.Path(__file__).with_name("fixtures") / "language-overlay-evidence.json"
A = "a" * 32
B = "b" * 32
C = "c" * 32


class Clock:
    def __init__(self, monotonic=1000.0, wall=1764201600.0):
        self.monotonic = monotonic
        self.wall = wall

    def now(self):
        return self.monotonic

    def time(self):
        return self.wall

    def advance(self, seconds):
        self.monotonic += seconds
        self.wall += seconds


class MockTransport:
    def __init__(self, failures=0):
        self.calls = []
        self.failures = failures

    def set_value(self, field, value):
        self.calls.append((field, value))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("mock transport unavailable")


def load_fixture():
    return json.loads(FIXTURE_PATH.read_text())


def find_nodes(layout):
    found = {}

    def walk(nodes):
        for node in nodes:
            found[node["id"]] = node
            walk(node.get("children") or [])

    for slot in layout["slots"]:
        walk(slot["nodes"])
    for page in layout["pages"]:
        walk(page["nodes"])
    return found


def editable_fields(layout):
    return {node["valueKey"] for node in find_nodes(layout).values()
            if node["kind"] in ob.EDITABLE_KINDS}


class LayoutContractTests(unittest.TestCase):
    def setUp(self):
        self.layout = json.loads(LAYOUT_PATH.read_text())

    def test_layout_is_valid_and_preserves_source_switcher_and_controls(self):
        ob.validate_layout(self.layout)
        nodes = find_nodes(self.layout)
        switcher = nodes["prototype-source-switcher"]
        self.assertEqual(switcher["kind"], "web")
        self.assertIn("selectSource", switcher["html"])
        self.assertIn("English", switcher["html"])
        self.assertIn("Malay", switcher["html"])
        for node_id in ("spanish-mark", "spanish-unmark", "language-mistaken",
                        "spanish-request-value", "spanish-read"):
            self.assertIn(node_id, nodes)
        self.assertEqual(nodes["spanish-read"]["id"], "spanish-read")

    def test_hidden_editable_field_shares_key_with_text_label(self):
        nodes = find_nodes(self.layout)
        hidden = nodes["language-overlay-value"]
        label = nodes["language-evidence"]
        self.assertEqual(hidden["kind"], "input")
        self.assertEqual(hidden["valueKey"], "form.languageOverlay")
        self.assertEqual(hidden["visibleWhen"], "form.languageOverlayVisible")
        self.assertEqual(label["kind"], "text")
        self.assertEqual(label["valueKey"], "form.languageOverlay")
        # The hidden condition references a form key that has no editable node.
        self.assertNotIn("form.languageOverlayVisible", editable_fields(self.layout))
        self.assertTrue(ob.binding_truth(ob.resolve_binding("form.languageOverlayVisible", {"languageOverlay": "x"})) is False)
        self.assertEqual(ob.resolve_binding("form.languageOverlay", {"languageOverlay": "x"}), "x")

    def test_manual_correction_action_uses_current_channel_and_v2_marker(self):
        nodes = find_nodes(self.layout)
        source = nodes["language-mistaken"]["action"]["arguments"]["source"]
        self.assertIn("currentChannelID", source)
        self.assertIn("mistaken_english", source)
        self.assertIn("schemaVersion:2", source)
        self.assertIn("ui_set_value", source)
        self.assertEqual(nodes["spanish-mark"]["action"]["arguments"]["source"].count("schemaVersion:1"), 1)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.transport = MockTransport()
        self.bridge = ob.OverlayBridge(
            self.transport, min_interval=0.0,
            clock=self.clock.now, wallclock=self.clock.time)
        self.fixture = load_fixture()

    def event(self, index):
        return dict(self.fixture["events"][index])

    def test_publish_updates_single_form_field_with_full_snapshot(self):
        evidence = self.event(0)
        self.assertEqual(self.bridge.observe_channel(A, "Fixture English One"), "published")
        self.assertEqual(self.bridge.publish_evidence(A, "Fixture English One", evidence), "published")
        self.assertEqual(len(self.transport.calls), 2)
        field, value = self.transport.calls[-1]
        self.assertEqual(field, "languageOverlay")
        self.assertIn("Fixture English One", value)
        self.assertIn("Spanish 7/25", value)
        self.assertIn("sampling", value)
        self.assertIn("82.0%", value)
        self.assertIn("2025-11-27T00:00:00Z", value)
        self.assertLessEqual(len(value.encode("utf-8")), ob.MAX_TEXT)

    def test_authorized_transport_uses_existing_device_command_shape(self):
        seen = {}

        def command(config, token, action, arguments):
            seen.update(config=config, token=token, action=action, arguments=arguments)
            return "OK"

        transport = ob.AuthorizedDeviceTransport({"deviceIP": "192.0.2.10"}, "fixture-token", command=command)
        transport.set_value("languageOverlay", "snapshot")
        self.assertEqual(seen["action"], "ui_set_value")
        self.assertEqual(seen["arguments"], {"key": "form.languageOverlay", "value": "snapshot"})
        self.assertEqual(seen["token"], "fixture-token")
        self.assertNotIn("fixture-token", repr(transport))

    def state(self, phase, attempt, max_attempts, at, visit="visit-1", stream="stream-1"):
        return {"state": phase, "reason": "sample_too_short" if phase == "inconclusive" else "collecting",
                "sampleSeconds": 4.99, "targetSeconds": 55.0,
                "attempt": attempt, "maxAttempts": max_attempts,
                "visitID": visit, "streamGeneration": stream, "at": at}

    def test_exhausted_retry_is_not_overwritten_by_late_collecting_state(self):
        self.bridge.observe_channel(A, "Fixture English One")
        self.assertEqual(self.bridge.publish_state(
            A, "Fixture English One", self.state("inconclusive", 4, 4, 100.0)), "published")
        calls = list(self.transport.calls)
        # The late collecting event belongs to the same exhausted attempt and
        # must not hide the terminal result.
        self.assertEqual(self.bridge.publish_state(
            A, "Fixture English One", self.state("collecting", 4, 4, 101.0)), "superseded")
        self.assertEqual(self.transport.calls, calls)
        self.assertIn("retry limit reached", calls[-1][1])
        self.assertEqual(self.bridge.latest_state.phase, "inconclusive")
        # A new physical visit is a genuinely new label and is allowed.
        self.assertEqual(self.bridge.publish_state(
            A, "Fixture English One", self.state("collecting", 1, 4, 200.0,
                                                 visit="visit-2")), "published")
        self.assertIn("collecting", self.transport.calls[-1][1])

    def test_exhausted_grace_window_and_newer_attempt_boundaries(self):
        self.bridge.observe_channel(A, "Fixture English One")
        self.assertEqual(self.bridge.publish_state(
            A, "Fixture English One", self.state("inconclusive", 4, 4, 100.0)), "published")
        # Exactly the 5 s grace boundary still belongs to the terminal attempt.
        self.assertEqual(self.bridge.publish_state(
            A, "Fixture English One", self.state("collecting", 4, 4, 105.0)), "superseded")
        # A genuinely newer attempt is allowed even inside the former grace.
        self.assertEqual(self.bridge.publish_state(
            A, "Fixture English One", self.state("collecting", 5, 8, 106.0)), "published")
        self.assertIn("collecting", self.transport.calls[-1][1])
        # Outside the grace window the documented behaviour is to accept it.
        self.assertEqual(self.bridge.publish_state(
            A, "Fixture English One", self.state("collecting", 6, 8, 112.0)), "published")

    def test_retryable_inconclusive_does_not_block_the_next_collecting_state(self):
        self.bridge.observe_channel(A, "Fixture English One")
        self.assertEqual(self.bridge.publish_state(
            A, "Fixture English One", self.state("inconclusive", 1, 4, 100.0)), "published")
        self.assertEqual(self.bridge.publish_state(
            A, "Fixture English One", self.state("collecting", 2, 4, 101.0)), "published")
        self.assertIn("collecting", self.transport.calls[-1][1])

    def test_duplicate_event_is_deduplicated(self):
        evidence = self.event(0)
        self.bridge.observe_channel(A, "Fixture English One")
        self.assertEqual(self.bridge.publish_evidence(A, "Fixture English One", evidence), "published")
        before = len(self.transport.calls)
        self.assertEqual(self.bridge.publish_evidence(A, "Fixture English One", evidence), "duplicate")
        self.assertEqual(len(self.transport.calls), before)

    def test_channel_transition_replaces_and_suppresses_stale_evidence(self):
        self.bridge.observe_channel(A, "Fixture English One")
        self.assertEqual(self.bridge.publish_evidence(A, "Fixture English One", self.event(0)), "published")
        self.bridge.observe_channel(B, "Fixture English Two")
        transition = self.transport.calls[-1][1]
        self.assertIn("Fixture English Two", transition)
        self.assertIn("waiting for language evidence", transition)
        before = len(self.transport.calls)
        self.assertEqual(self.bridge.publish_evidence(A, "Fixture English One", self.event(1)),
                         "suppressed_channel")
        self.assertEqual(len(self.transport.calls), before)
        self.assertNotIn("Fixture English One", transition)
        self.assertEqual(self.bridge.publish_evidence(B, "Fixture English Two", self.event(2)), "published")
        self.assertIn("Fixture English Two", self.transport.calls[-1][1])
        self.assertIn("English 4/25", self.transport.calls[-1][1])

    def test_out_of_order_evidence_is_suppressed(self):
        newer = self.event(1)
        older = self.event(0)
        self.bridge.observe_channel(A, "Fixture English One")
        self.assertEqual(self.bridge.publish_evidence(A, "Fixture English One", newer), "published")
        before = len(self.transport.calls)
        self.assertEqual(self.bridge.publish_evidence(A, "Fixture English One", older), "stale")
        self.assertEqual(len(self.transport.calls), before)
        self.assertIn("8/25", self.transport.calls[-1][1])

    def test_rate_limit_defers_then_publishes_on_next_lifecycle_call(self):
        bridge = ob.OverlayBridge(self.transport, min_interval=1.0,
                                  clock=self.clock.now, wallclock=self.clock.time)
        bridge.observe_channel(A, "Fixture English One")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(bridge.publish_evidence(A, "Fixture English One", self.event(0)), "rate_limited")
        self.assertEqual(len(self.transport.calls), 1)
        self.clock.advance(1.0)
        self.assertEqual(bridge.pump(), "published")
        self.assertEqual(len(self.transport.calls), 2)

    def test_retry_is_bounded_then_recovers_on_new_evidence(self):
        self.assertEqual(self.bridge.observe_channel(A, "Fixture English One"), "published")
        self.transport.failures = 2
        self.assertEqual(self.bridge.publish_evidence(A, "Fixture English One", self.event(0)), "retry")
        self.clock.advance(0.25)
        self.assertEqual(self.bridge.pump(), "retry")
        self.clock.advance(0.50)
        self.assertEqual(self.bridge.pump(), "published")
        self.assertEqual(len(self.transport.calls), 4)
        self.assertEqual(self.bridge.fault, "")

    def test_transport_failure_is_bounded_and_fails_safe(self):
        self.assertEqual(self.bridge.observe_channel(A, "Fixture English One"), "published")
        self.transport.failures = 10
        self.assertEqual(self.bridge.publish_evidence(A, "Fixture English One", self.event(0)), "retry")
        self.clock.advance(0.25)
        self.assertEqual(self.bridge.pump(), "retry")
        self.clock.advance(0.50)
        self.assertEqual(self.bridge.pump(), "failed")
        self.assertEqual(len(self.transport.calls), 4)
        self.assertIn("transport_unavailable", self.bridge.fault)
        self.assertEqual(self.bridge.status()["pending"], False)
        self.transport.failures = 0
        self.assertEqual(self.bridge.publish_evidence(A, "Fixture English One", self.event(1)), "published")

    def test_pending_result_expires_instead_of_publishing_late(self):
        self.assertEqual(self.bridge.observe_channel(A, "Fixture English One"), "published")
        self.transport.failures = 10
        self.assertEqual(self.bridge.publish_evidence(A, "Fixture English One", self.event(0)), "retry")
        self.clock.advance(31.0)
        self.assertEqual(self.bridge.pump(), "stale")
        self.assertEqual(self.bridge.status()["pending"], False)
        self.assertEqual(self.bridge.fault, "stale_pending")

    def test_event_driven_states_are_truthful_and_bounded(self):
        self.bridge.observe_channel(A, "Fixture English One")
        states = [
            ({"state": "collecting", "reason": "collecting", "sampleSeconds": 12.3,
              "targetSeconds": 55, "attempt": 1, "maxAttempts": 4, "at": 1764201601.0},
             ["collecting 12.3s/55.0s", "attempt 1/4"]),
            ({"state": "inference", "sampleSeconds": 55, "at": 1764201602.0},
             ["inference running"]),
            ({"state": "inconclusive", "reason": "sample_too_short",
              "sampleSeconds": 36.03, "targetSeconds": 40, "attempt": 1,
              "maxAttempts": 4, "retrySeconds": 10, "at": 1764201603.0},
             ["inconclusive: sample_too_short", "36.0s/40.0s", "retry in 10s"]),
            ({"state": "inconclusive", "reason": "sample_too_short",
              "sampleSeconds": 36.03, "targetSeconds": 40, "attempt": 4,
              "maxAttempts": 4, "at": 1764201604.0},
             ["retry limit reached"]),
            ({"state": "deferred", "reason": "deferred_paused", "language": "pt",
              "languageName": "Portuguese", "at": 1764201605.0},
             ["Portuguese", "deferred", "deferred_paused"]),
            ({"state": "no_audio", "at": 1764201606.0}, ["no audio"]),
            ({"state": "disabled", "at": 1764201607.0},
             ["language detection disabled"]),
        ]
        for evidence, expected in states:
            with self.subTest(state=evidence["state"]):
                self.assertEqual(self.bridge.publish_state(A, "Fixture English One", evidence),
                                 "published")
                for text in expected:
                    self.assertIn(text, self.transport.calls[-1][1])

    def test_stale_state_and_old_evidence_do_not_replace_current_label(self):
        self.bridge.observe_channel(A, "Fixture English One")
        self.assertEqual(self.bridge.publish_evidence(A, "Fixture English One", self.event(0)),
                         "published")
        before = len(self.transport.calls)
        self.assertEqual(self.bridge.publish_state(A, "Fixture English One", {
            "state": "collecting", "sampleSeconds": 5, "targetSeconds": 55,
            "at": self.event(0)["at"] - 1}), "stale")
        self.assertEqual(len(self.transport.calls), before)
        self.assertEqual(self.bridge.publish_state(A, "Fixture English One", {
            "state": "collecting", "sampleSeconds": 15, "targetSeconds": 55,
            "at": self.event(0)["at"] + 1}), "published")
        # The transient state is displayed, but the last verified evidence is
        # retained for the manual correction gate.
        self.assertEqual(self.bridge.latest_snapshot.event_id, self.event(0)["eventID"])
        self.assertEqual(self.bridge.status()["latestEventID"], self.event(0)["eventID"])
        self.assertEqual(self.bridge.status()["latestState"], "collecting")
        calls = len(self.transport.calls)
        self.assertEqual(self.bridge.publish_evidence(A, "Fixture English One", dict(
            self.event(0), eventID="evt-older-than-state",
            at=self.event(0)["at"] - 0.5)), "stale")
        self.assertEqual(len(self.transport.calls), calls)

    def test_correction_is_retained_against_stale_state_and_duplicate_gesture(self):
        self.bridge.observe_channel(A, "Fixture English One")
        self.bridge.publish_evidence(A, "Fixture English One", self.event(0))
        corrected = dict(self.event(0), eventID=self.event(0)["eventID"] + ":corrected",
                         at=self.event(0)["at"] + 1, language="en", languageName="English",
                         phase="corrected", outcome="allowed", count=1)
        self.assertEqual(self.bridge.publish_evidence(A, "Fixture English One", corrected),
                         "published")
        self.bridge.confirm_correction(self.event(0)["eventID"])
        self.bridge.confirm_correction()
        before = len(self.transport.calls)
        self.assertEqual(self.bridge.publish_state(A, "Fixture English One", {
            "state": "collecting", "sampleSeconds": 4, "targetSeconds": 55,
            "at": corrected["at"] - 0.5}), "stale")
        self.assertEqual(len(self.transport.calls), before)
        self.assertIn("English", self.transport.calls[-1][1])
        self.assertIn("corrected", self.transport.calls[-1][1])
        self.assertEqual(self.bridge.latest_snapshot.event_id, corrected["eventID"])
        with self.assertRaises(ob.AlreadyCorrected):
            self.bridge.manual_correction(A, "Fixture English One", "nonce-after-correction")
        # A genuinely newer sampler state may update the display, but it never
        # replaces the verified correction snapshot.
        self.assertEqual(self.bridge.publish_state(A, "Fixture English One", {
            "state": "collecting", "sampleSeconds": 8, "targetSeconds": 55,
            "at": corrected["at"] + 2}), "published")
        self.assertEqual(self.bridge.latest_snapshot.event_id, corrected["eventID"])

    def test_manual_correction_requires_latest_evidence_and_current_channel(self):
        with self.assertRaises(ob.ManualCorrectionError):
            self.bridge.manual_correction(A, "Fixture English One", "nonce-1")
        self.bridge.observe_channel(A, "Fixture English One")
        self.bridge.publish_evidence(A, "Fixture English One", self.event(0))
        with self.assertRaises(ob.ManualCorrectionError):
            self.bridge.manual_correction(B, "Fixture English Two", "nonce-1")
        payload = self.bridge.manual_correction(A, "Fixture English One", "nonce-1")
        self.assertEqual(payload["schemaVersion"], 2)
        self.assertEqual(payload["action"], "mistaken_english")
        self.assertEqual(payload["channelID"], A)
        self.assertEqual(payload["channelName"], "Fixture English One")
        self.bridge.confirm_correction()
        with self.assertRaises(ob.AlreadyCorrected):
            self.bridge.manual_correction(A, "Fixture English One", "nonce-2")
        self.bridge.publish_evidence(A, "Fixture English One", self.event(1))
        self.assertEqual(self.bridge.manual_correction(A, "Fixture English One", "nonce-3")["nonce"], "nonce-3")
        self.bridge.observe_channel(B, "Fixture English Two")
        with self.assertRaises(ob.ManualCorrectionError):
            self.bridge.manual_correction(A, "Fixture English One", "nonce-4")

    def test_transport_failure_during_transition_reports_stale_label(self):
        self.bridge.observe_channel(A, "Fixture English One")
        self.bridge.publish_evidence(A, "Fixture English One", self.event(0))
        self.transport.failures = 10
        self.assertEqual(self.bridge.observe_channel(B, "Fixture English Two"), "retry")
        status = self.bridge.status()
        self.assertEqual(status["publishedChannelID"], A)
        self.assertTrue(status["staleLabel"])
        self.transport.failures = 0
        self.assertEqual(self.bridge.publish_evidence(B, "Fixture English Two", self.event(2)), "published")
        status = self.bridge.status()
        self.assertEqual(status["publishedChannelID"], B)
        self.assertFalse(status["staleLabel"])

    def test_clear_replaces_old_language_with_waiting_label(self):
        self.bridge.observe_channel(A, "Fixture English One")
        self.bridge.publish_evidence(A, "Fixture English One", self.event(0))
        self.assertIn("Spanish", self.transport.calls[-1][1])
        self.assertEqual(self.bridge.clear(), "published")
        self.assertIn("waiting for language evidence", self.transport.calls[-1][1])
        self.assertNotIn("Spanish", self.transport.calls[-1][1])


if __name__ == "__main__":
    unittest.main()
