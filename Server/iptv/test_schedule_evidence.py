import datetime as dt
import json
import unittest

import opencode_discovery as discovery
import schedule_evidence as se


def epoch(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def jsonld(document):
    return '<script type="application/ld+json">' + json.dumps(document) + "</script>"


def station_page(name, region, title, start, end, extra=None):
    event = {"@type": "BroadcastEvent", "name": title, "startDate": start, "endDate": end}
    if extra:
        event.update(extra)
    return jsonld({"@type": "BroadcastService", "name": name, "areaServed": region,
                   "hasBroadcastEvent": [event]})


class ScheduleParserTests(unittest.TestCase):
    def test_jsonld_timezone_and_station(self):
        now = epoch("2026-11-26T22:30:00-05:00")
        page = station_page("Fixture TV", "United States", "Example",
                            "2026-11-26T22:00:00-05:00", "2026-11-26T23:00:00-05:00")
        result = se.parse_schedule(page, "Fixture TV", "us", now, "tvguide.com")
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.title, "Example")
        self.assertTrue(event.zone.endswith("-05:00") or event.zone == "America/New_York")
        self.assertEqual(event.start, now - 1800)
        self.assertIn("json-ld", result.formats)

    def test_jsonld_graph_reference(self):
        now = epoch("2026-11-26T17:00:00+00:00")
        page = jsonld({"@graph": [
            {"@type": "TelevisionChannel", "@id": "#station", "name": "Fixture TV", "areaServed": {"name": "US"}},
            {"@type": "BroadcastEvent", "@id": "#event", "name": "Example",
             "startDate": "2026-11-26T16:00:00Z", "endDate": "2026-11-26T18:00:00Z"},
            {"@type": "BroadcastService", "name": "Fixture TV", "areaServed": "US", "hasBroadcastEvent": ["#event"]},
        ]})
        result = se.parse_schedule(page, "Fixture TV", "us", now, "tvpassport.com")
        self.assertEqual([event.title for event in result.events], ["Example"])

    def test_episode_identity_is_not_series_year(self):
        now = epoch("2026-11-27T01:30:00+00:00")
        page = station_page("JAG", "US", "JAG",
                            "2026-11-27T01:00:00+00:00", "2026-11-27T02:00:00+00:00", {
                                "partOfSeries": {"@type": "TVSeries", "name": "JAG", "copyrightYear": 1995},
                                "partOfEpisode": {"@type": "TVEpisode", "name": "Silent Service",
                                                  "episodeNumber": 16,
                                                  "partOfSeason": {"seasonNumber": 4},
                                                  "datePublished": "1998-02-24"},
                            })
        result = se.parse_schedule(page, "JAG", "us", now, "tvguide.com")
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.title, "JAG")
        self.assertEqual(event.episode_title, "Silent Service")
        self.assertEqual((event.season_number, event.episode_number), (4, 16))
        self.assertEqual(event.season_year, 1998)
        self.assertIsNone(event.release_year)

    def test_region_spoof_and_naive_time_rejected(self):
        now = epoch("2026-11-26T17:00:00+00:00")
        canada = station_page("JAG", "CA", "JAG", "2026-11-26T16:00:00Z", "2026-11-26T18:00:00Z")
        result = se.parse_schedule(canada, "JAG", "us", now, "tvguide.com")
        self.assertEqual(result.events, [])
        self.assertTrue(any(item["reason"] == "region-or-station-identity-missing" for item in result.rejections))
        naive = station_page("Fixture TV", "US", "Example", "2026-11-26T16:00:00", "2026-11-26T18:00:00")
        result = se.parse_schedule(naive, "Fixture TV", "us", now, "tvguide.com")
        self.assertEqual(result.events, [])

    def test_multiple_active_events_are_ambiguous(self):
        now = epoch("2026-11-26T17:00:00+00:00")
        document = {"@type": "BroadcastService", "name": "Fixture TV", "areaServed": "US", "hasBroadcastEvent": [
            {"@type": "BroadcastEvent", "name": "One", "startDate": "2026-11-26T16:00:00Z", "endDate": "2026-11-26T18:00:00Z"},
            {"@type": "BroadcastEvent", "name": "Two", "startDate": "2026-11-26T17:00:00Z", "endDate": "2026-11-26T19:00:00Z"},
        ]}
        result = se.parse_schedule(jsonld(document), "Fixture TV", "us", now, "tvguide.com")
        self.assertEqual(len(result.events), 2)

    def test_html_table_with_explicit_station_timezone_and_episode(self):
        now = epoch("2026-11-26T16:30:00-05:00")
        page = """<html><head><title>JAG schedule</title>
        <meta name="timezone" content="America/New_York"><meta name="date" content="2026-11-26"></head>
        <body><h1>JAG</h1><table>
        <tr><td><time datetime="16:00">4:00 PM</time></td><td class="title">Silent Service S04E16</td></tr>
        <tr><td><time datetime="17:00">5:00 PM</time></td><td class="title">King of the Greenie Board S04E17</td></tr>
        </table></body></html>"""
        result = se.parse_schedule(page, "JAG", "us", now, "ontvtonight.com")
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.title, "Silent Service")
        self.assertEqual((event.season_number, event.episode_number), (4, 16))
        self.assertEqual(event.end, epoch("2026-11-26T17:00:00-05:00"))

    def test_html_wrong_station_and_dst_ambiguity(self):
        now = epoch("2026-11-01T01:30:00-04:00")
        page = """<html><head><meta name="timezone" content="America/New_York">
        <meta name="date" content="2026-11-01"></head><body><h1>Other</h1><table>
        <tr><td><time datetime="01:30">1:30</time></td><td>Example</td></tr></table></body></html>"""
        result = se.parse_schedule(page, "Fixture TV", "us", now, "tvguide.com")
        self.assertEqual(result.events, [])
        page = page.replace("<h1>Other</h1>", "<h1>Fixture TV</h1>")
        result = se.parse_schedule(page, "Fixture TV", "us", now, "tvguide.com")
        self.assertEqual(result.events, [])
        self.assertTrue(any(item["reason"] == "ambiguous-dst-time" for item in result.rejections))
        page = page.replace('<meta name="timezone"', '<meta name="region" content="CA"><meta name="timezone"')
        result = se.parse_schedule(page, "Fixture TV", "us", now, "tvguide.com")
        self.assertTrue(any(item["reason"] == "region-mismatch" for item in result.rejections))

    def test_xmltv_offset_and_episode_numbers(self):
        now = epoch("2026-11-26T21:30:00+00:00")
        page = b"""<?xml version="1.0"?><tv>
        <channel id="jag.us"><display-name>JAG</display-name></channel>
        <programme channel="jag.us" start="20261126160000 -0500" stop="20261126170000 -0500">
          <title>JAG</title><sub-title>Silent Service</sub-title>
          <episode-num system="xmltv_ns">3.15.0</episode-num><date>19980224</date>
        </programme></tv>""".decode()
        result = se.parse_schedule(page, "JAG", "us", now, "pluto.tv", first_party=True)
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.episode_title, "Silent Service")
        self.assertEqual((event.season_number, event.episode_number), (4, 16))
        self.assertEqual(event.season_year, 1998)

    def test_schedule_boundaries_and_malformed(self):
        now = epoch("2026-11-26T16:00:00+00:00")
        page = station_page("Fixture TV", "US", "Example", "2026-11-26T16:00:00Z", "2026-11-26T17:00:00Z")
        self.assertEqual(len(se.parse_schedule(page, "Fixture TV", "us", now, "tvguide.com").events), 1)
        self.assertEqual(se.parse_schedule(page, "Fixture TV", "us", now + 3600, "tvguide.com").events, [])
        broken = "<html><script type='application/ld+json'>{bad</script>"
        result = se.parse_schedule(broken, "Fixture TV", "us", now, "tvguide.com")
        self.assertEqual(result.events, [])
        self.assertTrue(result.rejections)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.now = epoch("2026-11-26T17:00:00+00:00")
        self.candidate = discovery.normalize_result({"title": "Example", "region": "us", "sources": [
            "https://www.tvpassport.com/fixture", "https://www.tvguide.com/fixture"]})
        self.good = station_page("Fixture TV", "US", "Example",
                                 "2026-11-26T16:00:00Z", "2026-11-26T18:00:00Z")

    def test_independent_corroboration_and_episode(self):
        page = station_page("Fixture TV", "US", "Fixture TV",
                            "2026-11-26T16:00:00Z", "2026-11-26T18:00:00Z", {
                                "partOfEpisode": {"@type": "TVEpisode", "name": "Slipping Away",
                                                  "episodeNumber": 17,
                                                  "partOfSeason": {"seasonNumber": 9},
                                                  "datePublished": "1999-03-24"}})
        self.candidate = discovery.normalize_result({"title": "Fixture TV", "region": "us", "sources": [
            "https://www.tvpassport.com/fixture", "https://www.tvguide.com/fixture"]})
        row, report = discovery.verify_candidate_detailed(self.candidate, "Fixture TV", "nltv:us:Fixture.us",
                                                          self.now, lambda _: page)
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "Fixture TV")
        self.assertEqual(row["episodeTitle"], "Slipping Away")
        self.assertEqual((row["seasonNumber"], row["episodeNumber"]), (9, 17))
        self.assertEqual(row["seasonYear"], 1999)
        self.assertEqual(report["verification"], "corroborated")

    def test_first_party_structured_source(self):
        page = station_page("Fixture TV", "US", "Example",
                            "2026-11-26T16:00:00Z", "2026-11-26T18:00:00Z")
        candidate = discovery.normalize_result({"title": "Example", "region": "us",
                                                "sources": ["https://pluto.tv/us/fixture"]})
        row, report = discovery.verify_candidate_detailed(candidate, "Fixture TV", "nltv:us:Fixture.us",
                                                          self.now, lambda _: page)
        self.assertIsNotNone(row)
        self.assertEqual(report["verification"], "first-party")

    def test_rejects_unapproved_duplicate_conflicting_and_stale(self):
        candidate = discovery.normalize_result({"title": "Example", "region": "us", "sources": [
            "https://evil.example/fixture", "https://www.tvguide.com/a", "https://tvguide.com/b"]})
        row, report = discovery.verify_candidate_detailed(candidate, "Fixture TV", "nltv:us:Fixture.us",
                                                          self.now, lambda _: self.good)
        self.assertIsNone(row)
        self.assertTrue(any(r["reason"] == "host-not-approved" for r in report["rejected"]))
        self.assertTrue(any(r["reason"] == "duplicate-domain" for r in report["rejected"]))
        page = station_page("Fixture TV", "US", "Other",
                            "2026-11-26T16:00:00Z", "2026-11-26T18:00:00Z")
        self.assertIsNone(discovery.verify_candidate(self.candidate, "Fixture TV", "nltv:us:Fixture.us",
                                                     self.now, lambda _: page))
        self.assertIsNone(discovery.verify_candidate(self.candidate, "Fixture TV", "nltv:us:Fixture.us",
                                                     self.now + 3600, lambda _: self.good))

    def test_jag_region_mismatch_is_diagnosed_not_guessed(self):
        page = station_page("JAG", "CA", "JAG",
                            "2026-11-26T16:00:00Z", "2026-11-26T17:00:00Z")
        candidate = discovery.normalize_result({"title": "JAG", "region": "us",
                                                "sources": ["https://www.tvguide.com/jag"]})
        row, report = discovery.verify_candidate_detailed(candidate, "JAG", "nltv:us:JAG.us",
                                                          self.now, lambda _: page)
        self.assertIsNone(row)
        self.assertTrue(any(r["reason"] == "region-or-station-identity-missing" for r in report["rejected"]))

    def test_null_research_answer_and_tool_diagnostics(self):
        events = [{"type": "tool_use", "part": {"tool": "websearch", "state": {"status": "completed"}}}] * 4
        events.append({"type": "text", "part": {"text": json.dumps({"title": None, "year": None, "region": "us", "sources": []})}})
        data = "\n".join(json.dumps(row) for row in events).encode()
        raw, diagnostics = discovery.parse_events_detailed(data)
        self.assertEqual(raw["title"], None)
        self.assertEqual(diagnostics["completedSearches"], 4)
        self.assertIsNone(diagnostics["error"])
        self.assertEqual(discovery.normalize_result(raw)["title"], "")
        failed = [{"type": "tool_use", "part": {"tool": "websearch", "state": {"status": "error"}}}]
        failed.append({"type": "text", "part": {"text": "{}"}})
        data = "\n".join(json.dumps(row) for row in failed).encode()
        raw, diagnostics = discovery.parse_events_detailed(data)
        self.assertIsNone(raw)
        self.assertEqual(diagnostics["failedSearches"], 1)
        self.assertEqual(diagnostics["error"], "no-completed-public-search")


if __name__ == "__main__":
    unittest.main()
