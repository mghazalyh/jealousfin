import json
import time
import unittest
import urllib.error

import episode_audio as audio

CHANNEL = "a" * 32
OFFICIAL_ROOT = "https://www.wwe.com/shows/wwenxt"
OFFICIAL_DATE = OFFICIAL_ROOT + "/2025-11-04"
OFFICIAL_RESULTS = OFFICIAL_DATE + "/results"
REPORT_ONE = "https://www.f4wonline.com/news/wwe-nxt-results-november-4"
REPORT_TWO = "https://www.wrestlinginc.com/123456/wwe-nxt-results-november-4"
NXT_TEXT = "Zaria becomes the first-ever women's North American Champion after the match."

OFFICIAL_HTML = (
    "<html><head><title>WWE NXT: Nov. 4, 2025 | WWE</title>"
    '<meta property="og:title" content="WWE NXT results, Nov. 4, 2025: Zaria wins '
    'the first-ever women\'s North American Championship"/>'
    '<meta property="og:description" content="Zaria becomes the first-ever '
    'women\'s North American Champion on Nov. 4, 2025."/>'
    "</head><body><p>Full results</p></body></html>"
)
REPORT_HTML = (
    "<html><head><title>WWE NXT Results 11/4 - Zaria Makes History</title>"
    '<script type="application/ld+json">'
    + json.dumps({
        "@type": "NewsArticle",
        "headline": "WWE NXT results, Nov. 4, 2025: Zaria becomes the first-ever women's North American Champion",
        "description": "Zaria became the first-ever women's North American Champion.",
        "datePublished": "2025-11-05T00:00:00+00:00",
    })
    + "</script></head><body></body></html>"
)
REPORT_TWO_HTML = (
    "<html><head><title>WWE NXT 11/4 Report</title>"
    '<script type="application/ld+json">'
    + json.dumps({
        "@type": "NewsArticle",
        "headline": "WWE NXT 11/4 report: Zaria wins the women's North American Championship",
        "description": "Zaria became the first-ever women's North American Champion.",
        "datePublished": "2025-11-04T00:00:00+00:00",
    })
    + "</script></head><body></body></html>"
)


def context(**overrides):
    values = {"seriesTitle": "WWE NXT", "channelID": CHANNEL,
              "originalAirDate": "", "episodeTitle": ""}
    values.update(overrides)
    return values


class FakeClock:
    def __init__(self, value=1_000.0):
        self.value = value

    def __call__(self):
        return self.value


class SourceDiscoveryTests(unittest.TestCase):
    def test_registry_override_suppresses_automatic_and_provider(self):
        provider = lambda payload: (_ for _ in ()).throw(AssertionError("provider used"))
        discovery = audio.EpisodeSourceDiscovery(
            lambda url: None, source_registry={"wwenxt": {
                "official": [OFFICIAL_DATE], "reports": [REPORT_ONE]}},
            provider=provider)
        official, reports, source = discovery.resolve(context(originalAirDate="2025-11-04"))
        self.assertEqual(source, "registry")
        self.assertEqual(official, [OFFICIAL_DATE])
        self.assertEqual(reports, [REPORT_ONE])
        self.assertEqual(discovery.provider_calls, 0)

    def test_known_date_uses_reviewed_official_roots_and_validates_provider(self):
        calls = []
        provider = lambda payload: (calls.append(payload) or {
            "official": [OFFICIAL_DATE, "https://example.com/nope",
                         "http://www.wwe.com/shows/wwenxt/2025-11-04",
                         "https://127.0.0.1/secret", "https://www.wwe.com/shows/wwenxt/2025-11-04#x"],
            "reports": [REPORT_ONE, REPORT_TWO, "https://www.wwe.com/not-a-report"]})
        discovery = audio.EpisodeSourceDiscovery(lambda url: None, provider=provider)
        official, reports, source = discovery.resolve(context(originalAirDate="2025-11-04"))
        self.assertEqual(source, "automatic+provider")
        self.assertIn(OFFICIAL_DATE, official)
        self.assertIn(OFFICIAL_RESULTS, official)
        self.assertNotIn("https://example.com/nope", official)
        self.assertNotIn("http://www.wwe.com/shows/wwenxt/2025-11-04", official)
        self.assertNotIn("https://127.0.0.1/secret", official)
        self.assertEqual(reports, [REPORT_ONE, REPORT_TWO])
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]), {"seriesTitle", "originalAirDate", "channelID", "clues"})

    def test_unknown_date_scans_a_bounded_reviewed_index(self):
        links = "".join('<a href="%s/2025-11-%02d">%d</a>' % (OFFICIAL_ROOT, day, day)
                        for day in range(1, 13))
        index = ("<html><body>" + links +
                 '<a href="https://example.com/2025-11-04">bad</a>'
                 '<a href="' + OFFICIAL_ROOT + '/other-page">not dated</a>'
                 '<a href="' + OFFICIAL_ROOT + '/2025-10-29/results">results</a>'
                 "</body></html>")
        provider = lambda payload: {"official": [], "reports": [REPORT_ONE]}
        discovery = audio.EpisodeSourceDiscovery(
            lambda url: index if url == OFFICIAL_ROOT else None, provider=provider)
        official, reports, _source = discovery.resolve(context())
        self.assertTrue(official)
        self.assertLessEqual(len(official), audio.MAX_INDEX_PAGES)
        self.assertTrue(all(url.startswith(OFFICIAL_ROOT + "/") for url in official))
        self.assertTrue(all(audio._date(url.rsplit("/", 1)[-1]) for url in official))
        self.assertEqual(reports, [REPORT_ONE])

    def test_candidate_cache_is_bounded_and_expires(self):
        clock = FakeClock()
        calls = []
        discovery = audio.EpisodeSourceDiscovery(
            lambda url: None, provider=lambda payload: calls.append(payload) or {"reports": [REPORT_ONE]},
            cache_ttl=30, clock=clock)
        first = discovery.resolve(context(originalAirDate="2025-11-04"))
        second = discovery.resolve(context(originalAirDate="2025-11-04"))
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        clock.value += 31
        discovery.resolve(context(originalAirDate="2025-11-04"))
        self.assertEqual(len(calls), 2)
        for day in range(1, 80):
            discovery.resolve(context(originalAirDate="2025-12-%02d" % (day % 28 + 1)))
        self.assertLessEqual(discovery.diagnostics()["cacheEntries"], audio.MAX_SOURCE_CACHE)

    def test_prefetch_warm_cache_avoids_refetch_and_never_uses_transcript(self):
        clock = FakeClock(time.time())
        fetches = []
        pages = {OFFICIAL_DATE: OFFICIAL_HTML, OFFICIAL_RESULTS: OFFICIAL_HTML,
                 REPORT_ONE: REPORT_HTML, REPORT_TWO: REPORT_TWO_HTML}

        def fetch(url):
            fetches.append(url)
            if url not in pages:
                raise OSError("missing")
            return pages[url]

        discovery = audio.EpisodeSourceDiscovery(
            fetch, provider=lambda payload: {"official": [OFFICIAL_DATE],
                                             "reports": [REPORT_ONE, REPORT_TWO]},
            clock=clock)
        research = audio.PublicEvidenceResearch(fetch, discovery=discovery)
        self.assertTrue(research.prefetch(context(originalAirDate="2025-11-04")))
        before = len(fetches)
        official, reports = research({"clues": list(audio.extract_clues(NXT_TEXT)),
                                      "programme": context(originalAirDate="2025-11-04")})
        self.assertEqual(len(fetches), before)
        self.assertTrue(official)
        self.assertEqual(len(reports), 2)
        self.assertNotIn("zaria", json.dumps(discovery.diagnostics()).casefold())
        self.assertNotIn("transcript", json.dumps(discovery.diagnostics()).casefold())

    def test_blocked_report_host_is_not_retried_or_bypassed(self):
        calls = []

        def fetch(url):
            calls.append(url)
            if url == OFFICIAL_DATE:
                return OFFICIAL_HTML
            if url.startswith("https://www.f4wonline.com/"):
                raise urllib.error.HTTPError(url, 403, "blocked", {}, None)
            return REPORT_TWO_HTML

        discovery = audio.EpisodeSourceDiscovery(
            fetch, provider=lambda payload: {"official": [OFFICIAL_DATE],
                                             "reports": [REPORT_ONE, REPORT_TWO]})
        research = audio.PublicEvidenceResearch(fetch, discovery=discovery)
        official, reports = research({"clues": list(audio.extract_clues(NXT_TEXT)),
                                      "programme": context(originalAirDate="2025-11-04")})
        self.assertTrue(official)
        self.assertEqual([report.source_url for report in reports], [REPORT_TWO])
        self.assertEqual(sum(url.startswith("https://www.f4wonline.com/") for url in calls), 1)

    def test_automatic_date_only_identity_never_invents_numbering(self):
        pages = {OFFICIAL_DATE: OFFICIAL_HTML, REPORT_ONE: REPORT_HTML,
                 REPORT_TWO: REPORT_TWO_HTML}
        discovery = audio.EpisodeSourceDiscovery(
            pages.get, provider=lambda payload: {"official": [OFFICIAL_DATE],
                                                 "reports": [REPORT_ONE, REPORT_TWO]})
        research = audio.PublicEvidenceResearch(pages.get, discovery=discovery)
        official, reports = research({"clues": list(audio.extract_clues(NXT_TEXT)),
                                      "programme": context(originalAirDate="2025-11-04")})
        verification = audio.corroborate_episode(audio.extract_clues(NXT_TEXT), official,
                                                 reports, now=1_000.0)
        self.assertTrue(verification.accepted)
        self.assertEqual(verification.identity.original_air_date, "2025-11-04")
        self.assertIsNone(verification.identity.season_number)
        self.assertIsNone(verification.identity.episode_number)
        self.assertEqual(verification.identity.broadcaster_code, "")

    def test_historical_date_and_unknown_date_fail_closed_without_evidence(self):
        discovery = audio.EpisodeSourceDiscovery(lambda url: None, provider=None)
        official, reports, _source = discovery.resolve(context(originalAirDate="2019-02-14"))
        self.assertIn("https://www.wwe.com/shows/wwenxt/2019-02-14", official)
        self.assertEqual(reports, [])
        unknown, unknown_reports, _ = discovery.resolve(context())
        self.assertEqual(unknown, [])
        self.assertEqual(unknown_reports, [])


if __name__ == "__main__":
    unittest.main()


class OpenCodeEpisodeSourceTests(unittest.TestCase):
    def test_prompt_is_bounded_and_never_contains_a_transcript(self):
        import opencode_discovery as discovery
        prompt = discovery.episode_prompt_for(
            {"seriesTitle": "WWE NXT", "originalAirDate": "2025-11-04",
             "channelName": "CW Presents WWE NXT"},
            ["zaria", "womens north american champion"])
        self.assertIn('"official"', prompt)
        self.assertIn("websearch only", prompt)
        self.assertIn("zaria", prompt)
        self.assertNotIn("Zaria becomes", prompt)
        self.assertNotIn("transcript", prompt.casefold())

    def test_answer_urls_are_allowlisted_and_lead_only(self):
        import opencode_discovery as discovery
        result = discovery.normalize_episode_sources({
            "official": [OFFICIAL_DATE, "https://example.com/fake",
                         "http://www.wwe.com/shows/wwenxt/2025-11-04",
                         "https://127.0.0.1/x"],
            "reports": [REPORT_ONE, REPORT_TWO, "https://www.wwe.com/report"]})
        self.assertEqual(result["official"], [OFFICIAL_DATE])
        self.assertEqual(result["reports"], [REPORT_ONE, REPORT_TWO])
        with self.assertRaises(ValueError):
            discovery.normalize_episode_sources(["not", "a", "dict"])

    def test_unknown_date_clue_prioritizes_a_bounded_older_index_link(self):
        recent = "".join('<a href="%s/2026-11-%02d">Episode %d</a>' % (OFFICIAL_ROOT, day, day)
                         for day in range(10, 25))
        index = (recent + '<a href="%s/2019-02-14">Zaria wins the women\'s North '
                 'American Championship</a>' % OFFICIAL_ROOT)
        discovery = audio.EpisodeSourceDiscovery(
            lambda url: index if url == OFFICIAL_ROOT else None,
            provider=lambda payload: {"reports": [REPORT_ONE]})
        official, _reports, _source = discovery.resolve(
            context(), audio.extract_clues("Zaria wins the women's North American Championship"))
        self.assertIn("https://www.wwe.com/shows/wwenxt/2019-02-14", official)
        self.assertLessEqual(len(official), audio.MAX_INDEX_PAGES)
