import datetime as dt
import json
import unittest

import schedule_evidence as se
import schedule_episode as sp

NOW = dt.datetime(2026, 11, 27, 3, 0, tzinfo=dt.timezone.utc).timestamp()
PROVIDER = "nltv:us:TinyHouseNation.us"
CHANNEL = "Tiny House Nation"


def tvmaze_payload():
    return json.dumps([
        {"id": 1, "name": "172 Sq. Ft. Dream Castle", "season": 1, "number": 1,
         "airdate": "2014-07-09", "airtime": "22:00", "summary": "A Tennessee couple builds a pink castle."},
        {"id": 5, "name": "500 Sq. Ft. Rocky Mountain Mansion", "season": 1, "number": 5,
         "airdate": "2014-08-13", "airtime": "22:00",
         "summary": "A couple from Colorado builds 500 sq. foot Tiny House after their old home was destroyed by a wildfire. They fit two separate offices."},
        {"id": 6, "name": "493 Sq. Ft. Vermont Chalet", "season": 1, "number": 6,
         "airdate": "2014-08-20", "airtime": "22:00", "summary": "A Vermont family builds a chalet."},
    ])


def tvmaze_episodeguide():
    return """<html><body>
    <h3><a href="/episodes/129206/tiny-house-nation-1x05-500-sq-ft-rocky-mountain-mansion">500 Sq. Ft. Rocky Mountain Mansion</a></h3>
    <div>Episode: 1x05 | Airdate: Aug 13, 2014</div>
    <p>A couple from Colorado builds 500 sq. foot Tiny House after their old home was destroyed by a wildfire.</p>
    </body></html>"""


def tvdb_page():
    return """<html><body>
    <h3>Season 1</h3>
    <div>S01E04</div><div>220 Sq. Ft. Bohemian Escape</div><div>July 30, 2014</div>
    <div>S01E05</div><div>500 Sq. Ft. Rocky Mountain Mansion</div><div>August 13, 2014</div>
    <div>S01E06</div><div>493 Sq. Ft. Vermont Chalet</div><div>August 20, 2014</div>
    </body></html>"""


def apple_page():
    return """<html><head><title>500 Sq Ft Rocky Mountain Mansion - Tiny House Nation (Season 1, Episode 5)</title></head>
    <body><h1>500 Sq Ft Rocky Mountain Mansion</h1><p>S1, E5: A couple from Colorado builds 500 sq. foot Tiny House after a wildfire.</p>
    <p>Aug 13, 2014 · 39m</p></body></html>"""


def aetv_page():
    obj = {"@type": "TVEpisode", "name": "500 Sq. Ft. Rocky Mountain Mansion",
           "episodeNumber": 5, "datePublished": "2014-08-13",
           "partOfSeason": {"seasonNumber": 1}}
    return '<script type="application/ld+json">' + json.dumps(obj) + "</script>"


def event(start, end, title=CHANNEL, zone="America/New_York", source="tvguide.com",
          first_party=False, structured=False, episode_title="", season=None, number=None,
          air_date=""):
    return se.Event(title=title, start=start, end=end, station=CHANNEL, region="us",
                    zone=zone, source=source, format="json-ld" if structured else "html-table",
                    episode_title=episode_title, season_number=season, episode_number=number,
                    season_year=int(air_date[:4]) if air_date else None,
                    first_party=first_party, structured=structured)


def catalogs():
    return (sp.parse_tvmaze_episodes(tvmaze_payload(), CHANNEL) +
            sp.parse_tvdb_allseasons_html(tvdb_page(), CHANNEL) +
            sp.parse_apple_episode_html(apple_page(), CHANNEL, "https://tv.apple.com/us/episode/500-sq-ft-rocky-mountain-mansion/umc.cmc.3y50upj71pnyr52a5nt14kwzw"))


class CatalogParserTests(unittest.TestCase):
    def test_tvmaze_api_episode(self):
        rows = sp.parse_tvmaze_episodes(tvmaze_payload(), CHANNEL)
        rocky = next(row for row in rows if row.season_number == 1 and row.episode_number == 5)
        self.assertEqual(rocky.episode_title, "500 Sq. Ft. Rocky Mountain Mansion")
        self.assertEqual(rocky.original_air_date, "2014-08-13")
        self.assertEqual(rocky.episode_year(), 2014)
        self.assertTrue(rocky.checked())

    def test_tvmaze_html_episodeguide(self):
        rows = sp.parse_tvmaze_episodeguide_html(tvmaze_episodeguide(), CHANNEL)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].season_number, rows[0].episode_number), (1, 5))
        self.assertEqual(rows[0].original_air_date, "2014-08-13")

    def test_tvdb_html_and_special(self):
        rows = sp.parse_tvdb_allseasons_html(tvdb_page(), CHANNEL)
        self.assertEqual([(r.season_number, r.episode_number) for r in rows],
                         [(1, 4), (1, 5), (1, 6)])
        special = sp.parse_tvdb_allseasons_html(
            "<div>SPECIAL 0x2</div><div>Size Doesn't Matter</div><div>March 4, 2015</div>", CHANNEL)
        self.assertEqual(special[0].kind, "special")
        self.assertEqual(special[0].original_air_date, "2015-03-04")

    def test_apple_and_aetv_official(self):
        apple = sp.parse_apple_episode_html(
            apple_page(), CHANNEL,
            "https://tv.apple.com/us/episode/500-sq-ft-rocky-mountain-mansion/umc.cmc.3y50upj71pnyr52a5nt14kwzw")
        self.assertEqual((apple[0].season_number, apple[0].episode_number), (1, 5))
        self.assertEqual(apple[0].original_air_date, "2014-08-13")
        aetv = sp.parse_aetv_season_html(aetv_page(), CHANNEL, "https://www.aetv.com/shows/tiny-house-nation/season-1")
        self.assertEqual((aetv[0].season_number, aetv[0].episode_number), (1, 5))

    def test_catalog_url_is_allowlisted(self):
        self.assertTrue(sp.catalog_url("https://api.tvmaze.com/shows/1418/episodes"))
        self.assertFalse(sp.catalog_url("https://api.tvmaze.com.evil.example/shows/1418/episodes"))
        self.assertFalse(sp.catalog_url("http://thetvdb.com/series/tiny-house-nation"))
        self.assertFalse(sp.catalog_url("https://user:pass@thetvdb.com/series/tiny-house-nation"))


class CatalogConsensusTests(unittest.TestCase):
    def test_two_independent_catalogs_agree_on_episode_year(self):
        identity, reason = sp.resolve_catalog(catalogs(), series_title=CHANNEL,
                                              episode_title="500 Sq. Ft. Rocky Mountain Mansion",
                                              air_date="2014-08-13")
        self.assertIsNotNone(identity, reason)
        self.assertEqual((identity.season_number, identity.episode_number), (1, 5))
        self.assertEqual(identity.episode_year, 2014)
        self.assertEqual(identity.series_premiere_year, 2014)
        self.assertEqual(identity.season_premiere_year, 2014)
        self.assertGreaterEqual(len({s.split(":")[0] for s in identity.sources}), 2)

    def test_official_catalog_numbering_wins_different_numbering(self):
        tvmaze = sp.parse_tvmaze_episodes(tvmaze_payload(), CHANNEL)
        tvdb = sp.parse_tvdb_allseasons_html(
            "<div>S01E06</div><div>500 Sq. Ft. Rocky Mountain Mansion</div><div>August 13, 2014</div>", CHANNEL)
        apple = sp.parse_apple_episode_html(apple_page(), CHANNEL, "https://tv.apple.com/us/episode/x/y")
        identity, reason = sp.resolve_catalog(tvmaze + tvdb + apple, series_title=CHANNEL,
                                              episode_title="500 Sq. Ft. Rocky Mountain Mansion",
                                              air_date="2014-08-13")
        self.assertIsNotNone(identity, reason)
        self.assertEqual((identity.season_number, identity.episode_number), (1, 5))

    def test_two_database_numbering_conflict_is_rejected(self):
        tvmaze = sp.parse_tvmaze_episodes(tvmaze_payload(), CHANNEL)
        tvdb = sp.parse_tvdb_allseasons_html(
            "<div>S01E06</div><div>500 Sq. Ft. Rocky Mountain Mansion</div><div>August 13, 2014</div>", CHANNEL)
        identity, reason = sp.resolve_catalog(tvmaze + tvdb, series_title=CHANNEL,
                                              episode_title="500 Sq. Ft. Rocky Mountain Mansion",
                                              air_date="2014-08-13")
        self.assertIsNone(identity)
        self.assertEqual(reason, "numbering-conflict")

    def test_single_database_is_not_corroboration(self):
        identity, reason = sp.resolve_catalog(sp.parse_tvmaze_episodes(tvmaze_payload(), CHANNEL),
                                              series_title=CHANNEL,
                                              episode_title="500 Sq. Ft. Rocky Mountain Mansion",
                                              air_date="2014-08-13")
        self.assertIsNone(identity)
        self.assertEqual(reason, "catalog-not-corroborated")

    def test_advertisement_is_never_an_episode(self):
        rows = [sp.CatalogEpisode(CHANNEL, 1, 1, "Buy This", "2014-07-09",
                                  "https://api.tvmaze.com/episodes/1", "tvmaze-api",
                                  kind="advertisement")]
        identity, reason = sp.resolve_catalog(rows, series_title=CHANNEL,
                                              episode_title="Buy This", air_date="2014-07-09")
        self.assertIsNone(identity)
        self.assertEqual(reason, "catalog-no-match")


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.catalogs = catalogs()

    def test_first_party_structured_schedule_wins(self):
        event_row = event(NOW - 600, NOW + 1800, zone="America/New_York", source="pluto.tv",
                          first_party=True, structured=True,
                          episode_title="500 Sq. Ft. Rocky Mountain Mansion",
                          season=1, number=5, air_date="2026-11-27")
        result = sp.resolve_current(NOW, PROVIDER, CHANNEL, schedule_events=[event_row],
                                    catalogs=self.catalogs)
        self.assertTrue(result.accepted, result.reason)
        self.assertEqual(result.verification, "source-owned-schedule")
        self.assertEqual(result.row["episodeTitle"], "500 Sq. Ft. Rocky Mountain Mansion")
        self.assertEqual(result.row["seasonYear"], 2014)

    def test_two_public_schedules_corroborate(self):
        first = event(NOW - 600, NOW + 1800, source="tvguide.com")
        second = event(NOW - 590, NOW + 1810, source="tvpassport.com")
        result = sp.resolve_current(NOW, PROVIDER, CHANNEL,
                                    schedule_events=[first, second], catalogs=self.catalogs,
                                    audio_clues=("Colorado", "wildfire", "500 square feet", "two offices"))
        self.assertTrue(result.accepted, result.reason)
        self.assertEqual(result.verification, "audio-clue-catalog")
        self.assertEqual((result.row["seasonNumber"], result.row["episodeNumber"]), (1, 5))

    def test_corroborating_peer_can_supply_the_episode_subtitle(self):
        first = event(NOW - 60, NOW + 1800, source="tvguide.com")
        second = event(NOW - 50, NOW + 1810, source="tvpassport.com",
                       episode_title="500 Sq. Ft. Rocky Mountain Mansion", season=1, number=5)
        result = sp.resolve_current(NOW, PROVIDER, CHANNEL,
                                    schedule_events=[first, second], catalogs=self.catalogs)
        self.assertTrue(result.accepted, result.reason)
        self.assertEqual(result.row["episodeTitle"], "500 Sq. Ft. Rocky Mountain Mansion")

    def test_one_public_schedule_is_only_a_lead(self):
        result = sp.resolve_current(NOW, PROVIDER, CHANNEL,
                                    schedule_events=[event(NOW - 60, NOW + 1800)],
                                    catalogs=self.catalogs,
                                    audio_clues=("Colorado", "wildfire"))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "schedule-not-corroborated")

    def test_show_name_only_without_guide_does_not_invent_current_episode(self):
        result = sp.resolve_current(NOW, PROVIDER, CHANNEL, schedule_events=[], catalogs=self.catalogs)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "no-current-schedule")
        self.assertIsNone(result.row)

    def test_ambiguous_schedule_and_missing_timezone_reject(self):
        one = event(NOW - 60, NOW + 1800, title=CHANNEL, source="tvguide.com")
        other = event(NOW - 30, NOW + 1700, title=CHANNEL, source="tvpassport.com")
        result = sp.resolve_current(NOW, PROVIDER, CHANNEL, schedule_events=[one, other],
                                    catalogs=self.catalogs)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "ambiguous-current-schedule")
        no_zone = event(NOW - 60, NOW + 1800, zone="", source="tvguide.com")
        result = sp.resolve_current(NOW, PROVIDER, CHANNEL, schedule_events=[no_zone],
                                    catalogs=self.catalogs)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "no-current-schedule")
        self.assertTrue(any(item["reason"] == "missing-timezone" for item in result.diagnostics["rejected"]))

    def test_different_provider_same_channel_name_is_rejected(self):
        result = sp.resolve_current(NOW, "nltv:us:OtherTinyHouse.us", CHANNEL,
                                    schedule_events=[event(NOW - 60, NOW + 1800)],
                                    catalogs=self.catalogs)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "unreviewed-provider")

    def test_timezone_conflict_and_feed_mismatch_reject(self):
        result = sp.resolve_current(NOW, PROVIDER, CHANNEL,
                                    schedule_events=[
                                        event(NOW - 60, NOW + 1800, zone="America/Chicago", source="tvguide.com"),
                                        event(NOW - 50, NOW + 1810, zone="America/Chicago", source="tvpassport.com"),
                                    ],
                                    catalogs=self.catalogs,
                                    feed=sp.FeedIdentity(PROVIDER, CHANNEL, "us", station=CHANNEL,
                                                         zone="America/New_York"))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "timezone-conflict")

    def test_stale_schedule_observation_rejects(self):
        result = sp.resolve_current(NOW, PROVIDER, CHANNEL,
                                    schedule_events=[event(NOW - 60, NOW + 1800)],
                                    catalogs=self.catalogs, observed_at=NOW - 300)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "stale-schedule")

    def test_rerun_keeps_original_episode_year(self):
        first = event(NOW - 60, NOW + 1800, source="tvguide.com")
        second = event(NOW - 50, NOW + 1810, source="tvpassport.com")
        result = sp.resolve_current(NOW, PROVIDER, CHANNEL,
                                    schedule_events=[first, second], catalogs=self.catalogs,
                                    audio_clues=("Colorado", "wildfire", "two offices"))
        self.assertTrue(result.accepted, result.reason)
        self.assertEqual(result.row["seasonYear"], 2014)
        self.assertNotEqual(result.row["seasonYear"], 2026)
        self.assertEqual(result.row["seasonPremiereYear"], 2014)
        self.assertEqual(result.row["seriesPremiereYear"], 2014)

    def test_special_episode_is_catalogued_separately(self):
        special = sp.parse_tvdb_allseasons_html(
            "<div>SPECIAL 0x2</div><div>Size Doesn't Matter: The Best of Tiny Houses</div><div>March 4, 2015</div>", CHANNEL)
        tvmaze = sp.parse_tvmaze_episodes(json.dumps([
            {"name": "Size Doesn't Matter: The Best of Tiny Houses", "season": 0, "number": 2,
             "airdate": "2015-03-04"}]), CHANNEL)
        first = event(NOW - 60, NOW + 1800, source="tvguide.com",
                      episode_title="Size Doesn't Matter: The Best of Tiny Houses")
        second = event(NOW - 50, NOW + 1810, source="tvpassport.com",
                       episode_title="Size Doesn't Matter: The Best of Tiny Houses")
        result = sp.resolve_current(NOW, PROVIDER, CHANNEL, schedule_events=[first, second],
                                    catalogs=special + tvmaze)
        self.assertTrue(result.accepted, result.reason)
        self.assertEqual(result.row["episodeKind"], "special")
        self.assertEqual(result.row["seasonYear"], 2015)

    def test_source_conflict_with_schedule_numbering_and_catalog(self):
        first = event(NOW - 60, NOW + 1800, source="tvguide.com", episode_title="500 Sq. Ft. Rocky Mountain Mansion",
                      season=9, number=9, air_date="2026-11-27")
        second = event(NOW - 50, NOW + 1810, source="tvpassport.com", episode_title="500 Sq. Ft. Rocky Mountain Mansion",
                       season=9, number=9, air_date="2026-11-27")
        result = sp.resolve_current(NOW, PROVIDER, CHANNEL, schedule_events=[first, second],
                                    catalogs=self.catalogs)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "catalog-no-match")

    def test_audio_clues_ambiguous_in_catalog_reject(self):
        confusing = [
            sp.CatalogEpisode(CHANNEL, 1, 5, "Colorado Wildfire Home", "2014-08-13",
                              "https://api.tvmaze.com/episodes/1", "tvmaze-api"),
            sp.CatalogEpisode(CHANNEL, 1, 6, "Colorado Wildfire Cabin", "2014-08-20",
                              "https://thetvdb.com/series/tiny-house-nation", "tvdb-html"),
        ]
        first = event(NOW - 60, NOW + 1800, source="tvguide.com")
        second = event(NOW - 50, NOW + 1810, source="tvpassport.com")
        result = sp.resolve_current(NOW, PROVIDER, CHANNEL, schedule_events=[first, second],
                                    catalogs=confusing, audio_clues=("Colorado", "wildfire"))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "ambiguous-catalog-episode")


if __name__ == "__main__":
    unittest.main()
