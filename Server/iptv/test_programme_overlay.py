"""Existing-IPA combined programme/language overlay acceptance.

No device, socket or credential access. The authorized transport is a
recording fixture and the installed layout is the read-back hotpatch asset.
"""
import json
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import programme_overlay as po

A = "a" * 32
PROVIDER = "nltv:us:pluto-" + A


class Transport:
    def __init__(self):
        self.calls = []
        self.fail_fields = set()

    def set_value(self, field, value):
        self.calls.append((field, value))
        if field in self.fail_fields:
            raise RuntimeError("transport down")


def scope(**overrides):
    values = dict(channel_id=A, provider_id=PROVIDER, channel_name="Fixture One",
                  launch_id="launch-1", visit_id="visit-1",
                  stream_generation="stream-1")
    values.update(overrides)
    return po.OverlayScope(**values)


def programme(**overrides):
    values = dict(series_title="Nurse Jackie", episode_title="Pilot",
                  season_number=1, episode_number=1, episode_year=2009,
                  original_air_date="2009-06-08")
    values.update(overrides)
    return po.ProgrammeIdentity(**values)


def evidence(**overrides):
    values = dict(language="en", language_name="English", count=1, threshold=25,
                  confidence=0.9769, observed_at=1000.0, phase="allowed",
                  event_id="evt-1")
    values.update(overrides)
    return po.LanguageEvidence(**values)


class FormattingTests(unittest.TestCase):
    def test_exact_combined_label_uses_episode_year(self):
        self.assertEqual(
            po.format_combined(programme(), evidence(), now=1001.0),
            "Nurse Jackie — S01E01: Pilot (2009) English — 1/25 — 97.7%")

    def test_exact_episode_year_wins_over_season_year_and_series_year(self):
        row = {"title": "Nurse Jackie (2009)", "seasonNumber": 2,
               "episodeNumber": 3, "episodeTitle": "Candyland",
               "episodeYear": 2010, "seasonYear": 2009,
               "seriesPremiereYear": 2009, "episodeSource": "x"}
        identity = po.programme_identity(row)
        self.assertEqual(identity.episode_year, 2010)
        self.assertIn("Candyland (2010)", identity.text())
        self.assertNotIn("Candyland (2009)", identity.text())

    def test_missing_language_is_truthful_pending_not_invented(self):
        text = po.format_combined(programme(), None, now=1001.0)
        self.assertIn("language evidence pending", text)
        self.assertNotIn("/25", text)
        self.assertNotIn("%", text)

    def test_stale_language_is_pending(self):
        text = po.format_combined(programme(), evidence(observed_at=10.0), now=5000.0)
        self.assertIn("language evidence pending", text)
        self.assertNotIn("97.7%", text)


class PublisherTests(unittest.TestCase):
    def test_scope_transition_disables_ready_first_and_clears_value(self):
        transport = Transport()
        publisher = po.ProgrammeOverlayPublisher(transport)
        first = scope()
        self.assertEqual(publisher.publish(first, "label one", "e1"), "published")
        transport.calls.clear()
        second = scope(channel_id="b" * 32, channel_name="Fixture Two")
        self.assertEqual(publisher.transition(second), "transitioned")
        self.assertEqual(transport.calls, [
            (po.READY_FIELD, po.FALSE),
            (po.FALLBACK_FIELD, po.TRUE),
            (po.FIELD, ""),
        ])

    def test_valid_publication_writes_value_reprobes_then_switches_visibility(self):
        transport = Transport()
        publisher = po.ProgrammeOverlayPublisher(transport)
        current = scope()
        self.assertEqual(publisher.publish(
            current, "combined label", "e1",
            reprobe=lambda: {"channelID": A, "launchID": "launch-1",
                             "visitID": "visit-1", "streamGeneration": "stream-1"}),
            "published")
        self.assertEqual(transport.calls, [
            (po.READY_FIELD, po.FALSE),
            (po.FALLBACK_FIELD, po.TRUE),
            (po.FIELD, ""),
            (po.FIELD, "combined label"),
            (po.READY_FIELD, po.TRUE),
            (po.FALLBACK_FIELD, po.FALSE),
        ])

    def test_reprobe_scope_change_prefers_fallback_and_never_shows_stale(self):
        transport = Transport()
        publisher = po.ProgrammeOverlayPublisher(transport)
        result = publisher.publish(
            scope(), "stale label", "e1",
            reprobe=lambda: {"channelID": "b" * 32, "launchID": "launch-1",
                             "visitID": "visit-2", "streamGeneration": "stream-2"})
        self.assertEqual(result, "scope_changed")
        self.assertNotIn((po.READY_FIELD, po.TRUE), transport.calls)
        self.assertEqual(transport.calls[-2:], [(po.READY_FIELD, po.FALSE),
                                                (po.FALLBACK_FIELD, po.TRUE)])

    def test_transport_failure_keeps_safe_fallback(self):
        transport = Transport()
        transport.fail_fields.add(po.FIELD)
        publisher = po.ProgrammeOverlayPublisher(transport, max_retries=1)
        self.assertEqual(publisher.publish(scope(), "label", "e1"), "failed")
        self.assertIn("transport_unavailable", publisher.fault)
        self.assertNotIn((po.READY_FIELD, po.TRUE), transport.calls)
        # Ready must be explicitly disabled and Fallback enabled, even though
        # the combined value could not be written.
        self.assertIn((po.READY_FIELD, po.FALSE), transport.calls)
        self.assertIn((po.FALLBACK_FIELD, po.TRUE), transport.calls)

    def test_ready_failure_never_leaves_only_old_combined_label(self):
        transport = Transport()
        publisher = po.ProgrammeOverlayPublisher(transport)
        self.assertEqual(publisher.publish(scope(), "label", "e1"), "published")
        transport.calls.clear()
        transport.fail_fields.add(po.READY_FIELD)
        self.assertEqual(publisher.publish(scope(), "new label", "e2"), "failed")
        # The failed Ready write is followed by an explicit fallback.
        self.assertIn((po.FALLBACK_FIELD, po.TRUE), transport.calls)
        self.assertNotIn((po.FALLBACK_FIELD, po.FALSE), transport.calls)

    def test_published_label_is_deduplicated(self):
        transport = Transport()
        publisher = po.ProgrammeOverlayPublisher(transport)
        self.assertEqual(publisher.publish(scope(), "label", "e1"), "published")
        calls = list(transport.calls)
        self.assertEqual(publisher.publish(scope(), "label", "e1"), "duplicate")
        self.assertEqual(transport.calls, calls)


class InstalledLayoutTests(unittest.TestCase):
    def setUp(self):
        self.layout = json.loads((HERE / "fixtures" /
                                  "programme-language-existing-ipa-20261127.json").read_text())

    def test_installed_hotpatch_uses_existing_native_abi_only(self):
        po.validate_existing_ipa_layout(self.layout)
        nodes = []

        def walk(items):
            for item in items:
                nodes.append(item)
                walk(item.get("children", []))

        for slot in self.layout["slots"]:
            walk(slot["nodes"])
        by_id = {node["id"]: node for node in nodes if node.get("id")}
        native = by_id["programme-combined-native-v1"]
        self.assertEqual(native["style"], "headline")
        self.assertEqual(native["valueKey"], "form.programmeOverlay")
        self.assertEqual(native["visibleWhen"], "form.programmeOverlayReady")
        self.assertEqual(by_id["hidden-programmeOverlayReady"]["defaultValue"], "false")
        self.assertEqual(by_id["hidden-programmeOverlayFallback"]["defaultValue"], "true")
        self.assertEqual(by_id["programme-bottom-left-v1"]["visibleWhen"],
                         "form.programmeOverlayFallback")
        # The draft "programme" style is not in the installed ABI.
        self.assertNotIn("programme", {node.get("style") for node in nodes})


if __name__ == "__main__":
    unittest.main()
