import json
import pathlib
import unittest

import overlay_bridge as ob
import programme_overlay as po

LAYOUT_PATH = pathlib.Path(__file__).resolve().parents[2] / "Examples" / "programme-language-combined-20261127.json"

A = "a" * 32
B = "b" * 32
C = "c" * 32
PROVIDER = "nltv:us:TinyHouseNation.us"
NOW = 1764201600.0


def scope(channel_id=A, provider=PROVIDER, name="Tiny House Nation", launch="launch-1",
          visit="visit-1", stream="stream-1", rotation=79):
    return po.OverlayScope(channel_id, provider, name, launch, visit, stream, rotation)


def programme(**overrides):
    row = {"title": "The Mentalist", "episodeTitle": "Forest Green",
           "seasonNumber": 6, "episodeNumber": 18, "seasonYear": 2014,
           "episodeOriginalAirDate": "2014-04-27", "scheduleVerification": "canonical-catalog"}
    row.update(overrides)
    return row


def language(**overrides):
    row = {"language": "en", "languageName": "English", "count": 1, "threshold": 25,
           "confidence": 0.9907, "at": NOW - 10, "phase": "recorded", "eventID": "e1"}
    row.update(overrides)
    return row


class LayoutContractTests(unittest.TestCase):
    def test_native_programme_title_slot_and_hidden_field_preserve_language_actions(self):
        layout = json.loads(LAYOUT_PATH.read_text())
        ob.validate_layout(layout)
        nodes = {}

        def walk(items):
            for item in items:
                nodes[item["id"]] = item
                walk(item.get("children") or [])

        for slot in layout["slots"]:
            walk(slot.get("nodes") or [])
        for page in layout.get("pages") or []:
            walk(page.get("nodes") or [])
        title = nodes["programme-language-title"]
        self.assertEqual(title["style"], "programme")
        self.assertEqual(title["valueKey"], "form.programmeOverlay")
        self.assertEqual(nodes["programme-overlay-value"]["kind"], "input")
        self.assertEqual(nodes["programme-overlay-value"]["valueKey"], "form.programmeOverlay")
        self.assertEqual(layout["revision"], "programme-language-combined-20261127")
        for node_id in ("prototype-source-switcher", "spanish-mark", "spanish-unmark",
                        "language-mistaken", "language-evidence"):
            self.assertIn(node_id, nodes)
        self.assertNotIn("static", json.dumps(layout).lower())


class FormatTests(unittest.TestCase):
    def test_exact_required_bottom_left_format(self):
        identity = po.programme_identity(programme())
        evidence = po.language_evidence(language())
        self.assertEqual(po.format_combined(identity, evidence, now=NOW),
                         "The Mentalist — S06E18: Forest Green (2014) English — 1/25 — 99.1%")

    def test_format_is_not_hardcoded(self):
        identity = po.programme_identity({"title": "Another Show", "episodeTitle": "Pilot",
                                          "seasonNumber": 2, "episodeNumber": 3, "seasonYear": 2020})
        evidence = po.language_evidence(language(language="es", languageName="Spanish", count=9,
                                                 threshold=25, confidence=0.8123))
        self.assertEqual(po.format_combined(identity, evidence, now=NOW),
                         "Another Show — S02E03: Pilot (2020) Spanish — 9/25 — 81.2%")

    def test_missing_or_inconclusive_language_never_fabricates(self):
        identity = po.programme_identity(programme())
        self.assertEqual(po.format_combined(identity, None, now=NOW),
                         "The Mentalist — S06E18: Forest Green (2014)")
        stale = po.LanguageEvidence("en", "English", 1, 25, 0.99, NOW - 10000)
        self.assertEqual(po.format_combined(identity, stale, now=NOW),
                         "The Mentalist — S06E18: Forest Green (2014) — language evidence pending")

    def test_no_episode_identity_is_not_invented(self):
        identity = po.programme_identity({"title": "Tiny House Nation"})
        self.assertEqual(identity.text(), "Tiny House Nation")
        self.assertEqual(identity.code(), "")


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.overlay = po.ProgrammeLanguageOverlay(clock=lambda: NOW)

    def test_observe_then_publish_language(self):
        self.assertEqual(self.overlay.observe(scope(), programme(), start=NOW - 600, end=NOW + 600), "observed")
        self.assertEqual(self.overlay.publish_language(scope(), language()), "published")
        self.assertEqual(self.overlay.label(NOW),
                         "The Mentalist — S06E18: Forest Green (2014) English — 1/25 — 99.1%")

    def test_channel_change_replaces_label_and_rejects_old_evidence(self):
        self.overlay.observe(scope(), programme(), start=NOW - 600, end=NOW + 600)
        self.overlay.publish_language(scope(), language())
        other = scope(channel_id=B, name="Another Channel")
        self.overlay.observe(other, {"title": "Another Show"}, start=NOW - 60, end=NOW + 600)
        self.assertEqual(self.overlay.label(NOW), "Another Show")
        self.assertEqual(self.overlay.publish_language(scope(), language()), "channel-mismatch")
        self.assertNotIn("Tiny House Nation", self.overlay.label(NOW))

    def test_same_name_channel_is_rejected_by_exact_scope(self):
        self.overlay.observe(scope(), programme(), start=NOW - 600, end=NOW + 600)
        same_name_other_id = scope(channel_id=C, name="Tiny House Nation", launch="launch-2", visit="visit-2")
        self.assertEqual(self.overlay.publish_language(same_name_other_id, language()), "channel-mismatch")

    def test_programme_change_drops_previous_language(self):
        self.overlay.observe(scope(), programme(), start=NOW - 600, end=NOW + 600)
        self.overlay.publish_language(scope(), language())
        self.overlay.observe(scope(), programme(episodeTitle="Red John", episodeNumber=8, seasonYear=2013),
                             start=NOW + 1, end=NOW + 600, now=NOW + 1)
        self.assertEqual(self.overlay.label(NOW + 1), "The Mentalist — S06E08: Red John (2013)")

    def test_stream_generation_change_clears_language_not_programme(self):
        self.overlay.observe(scope(), programme(), start=NOW - 600, end=NOW + 600)
        self.overlay.publish_language(scope(), language())
        reloaded = scope(stream="stream-2")
        self.assertEqual(self.overlay.observe(reloaded, programme(), start=NOW - 600, end=NOW + 600),
                         "stream-generation-changed")
        self.assertEqual(self.overlay.label(NOW), "The Mentalist — S06E18: Forest Green (2014)")
        self.assertEqual(self.overlay.publish_language(scope(stream="stream-1"), language()),
                         "stream-generation-mismatch")

    def test_same_visit_reload_keeps_programme_and_accepts_reverified_language(self):
        self.overlay.observe(scope(), programme(), start=NOW - 600, end=NOW + 600)
        self.overlay.publish_language(scope(), language())
        self.assertEqual(self.overlay.observe(scope(), programme(), start=NOW - 600, end=NOW + 600), "unchanged")
        self.assertEqual(self.overlay.publish_language(scope(), language(at=NOW)), "published")
        self.assertIn("99.1%", self.overlay.label(NOW))

    def test_provider_row_without_schedule_boundaries_does_not_clear_language(self):
        self.overlay.observe(scope(), programme())
        self.overlay.publish_language(scope(), language())
        self.overlay.observe(scope(), programme(), now=NOW + 5)
        self.assertEqual(self.overlay.label(NOW + 5),
                         "The Mentalist — S06E18: Forest Green (2014) English — 1/25 — 99.1%")

    def test_pending_sampling_keeps_programme_only(self):
        self.overlay.observe(scope(), programme(), start=NOW - 600, end=NOW + 600)
        self.assertEqual(self.overlay.publish_language(scope(), language(confidence=None)), "invalid-language-evidence")
        self.assertEqual(self.overlay.label(NOW), "The Mentalist — S06E18: Forest Green (2014)")

    def test_expiry_and_correction(self):
        self.overlay.observe(scope(), programme(), start=NOW - 600, end=NOW + 600)
        self.overlay.publish_language(scope(), language())
        self.assertEqual(self.overlay.correct(scope(), now=NOW + 1), "corrected")
        self.assertIn("English — 1/25 — 99.1%", self.overlay.label(NOW + 1))
        self.assertEqual(self.overlay.label(NOW + 4000), po.WAITING)

    def test_out_of_order_and_future_evidence_rejected(self):
        self.overlay.observe(scope(), programme(), start=NOW - 600, end=NOW + 600)
        self.overlay.publish_language(scope(), language(at=NOW), now=NOW)
        self.assertEqual(self.overlay.publish_language(scope(), language(at=NOW - 100)), "out-of-order-language-evidence")
        self.assertEqual(self.overlay.publish_language(scope(), language(at=NOW + 500)), "invalid-language-evidence")

    def test_stale_session_evidence_is_rejected(self):
        self.overlay.observe(scope(), programme(), start=NOW - 600, end=NOW + 600)
        self.assertEqual(self.overlay.publish_language(scope(), language(at=NOW - 4000)), "invalid-language-evidence")
        self.assertEqual(self.overlay.label(NOW), "The Mentalist — S06E18: Forest Green (2014)")

    def test_no_programme_means_waiting_not_static_mapping(self):
        self.assertEqual(self.overlay.observe(scope(), {}), "no-programme")
        self.assertEqual(self.overlay.label(NOW), po.WAITING)


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = NOW

    def now(self):
        return self.mono

    def time(self):
        return self.wall

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds


class Transport:
    def __init__(self, failures=0):
        self.calls = []
        self.failures = failures

    def set_value(self, field, value):
        self.calls.append((field, value))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("unavailable")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.transport = Transport()
        self.bridge = po.ProgrammeOverlayBridge(self.transport, min_interval=0.0,
                                                clock=self.clock.now, wallclock=self.clock.time)

    def test_exact_field_and_value(self):
        self.overlay = po.ProgrammeLanguageOverlay(clock=lambda: NOW)
        self.overlay.observe(scope(), programme(), start=NOW - 600, end=NOW + 600)
        self.overlay.publish_language(scope(), language())
        self.assertEqual(self.bridge.publish(self.overlay.label(NOW), self.overlay.event_id(NOW), scope()),
                         "published")
        field, value = self.transport.calls[-1]
        self.assertEqual(field, "programmeOverlay")
        self.assertEqual(value, "The Mentalist — S06E18: Forest Green (2014) English — 1/25 — 99.1%")

    def test_duplicate_and_scope_transition(self):
        self.bridge.publish("one", "e1", scope())
        self.assertEqual(self.bridge.publish("one", "e1", scope()), "duplicate")
        self.bridge.publish("two", "e2", scope(channel_id=B, name="Another Channel"))
        self.assertEqual(self.transport.calls[-1][1], "two")

    def test_retry_bound_and_recovery(self):
        self.transport.failures = 2
        self.assertEqual(self.bridge.publish("label", "e1", scope()), "retry")
        self.clock.advance(0.25)
        self.assertEqual(self.bridge.pump(), "retry")
        self.clock.advance(0.5)
        self.assertEqual(self.bridge.pump(), "published")

    def test_pending_expiry(self):
        self.transport.failures = 10
        self.bridge.publish("label", "e1", scope())
        self.clock.advance(31)
        self.assertEqual(self.bridge.pump(), "stale")


if __name__ == "__main__":
    unittest.main()
