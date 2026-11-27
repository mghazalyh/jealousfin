import time
import json
import urllib.error
import urllib.parse
import opencode_discovery as discovery
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import episode_audio as audio
import test_episode_audio as fixtures

class UnknownProgrammeTests(unittest.TestCase):
    def context(self, **kwargs):
        return dict(channelID=fixtures.CHANNEL, seriesTitle="", channelName="Example channel", **kwargs)

    def test_discovery_requires_audio_and_scopes_unknown_cache(self):
        calls=[]
        def provider(payload):
            calls.append(payload)
            return [fixtures.OFFICIAL_URL], [fixtures.REPORT_URL]
        discovery=audio.EpisodeSourceDiscovery(provider=provider)
        self.assertEqual(discovery.resolve(self.context()), ([], [], ""))
        clues=audio.extract_clues(fixtures.NXT_TEXT)
        official, reports, _=discovery.resolve(self.context(), clues)
        self.assertTrue(official and reports)
        self.assertEqual(calls[0]["seriesTitle"], "")
        self.assertEqual(calls[0]["channelName"], "Example channel")
        other=dict(self.context(), channelID="c"*32)
        discovery.resolve(other, clues)
        self.assertEqual(len(calls), 2)

    def test_unknown_requires_two_independent_matching_domains(self):
        req=fixtures.nxt_request()
        args=(req, [fixtures.nxt_official()])
        def promote(reports):
            return audio.promote_episode(*args, reports, fixtures.current(), fixtures.NOW, programme=self.context())
        reports=fixtures.nxt_reports()
        self.assertTrue(promote(reports).accepted)
        self.assertFalse(promote(reports[:1]).accepted)
        self.assertFalse(promote([reports[0], reports[0]]).accepted)
        self.assertFalse(promote([reports[0], audio.dataclasses.replace(reports[1],series_title="Unrelated")]).accepted)

    def test_no_epg_publication_must_have_playback_session(self):
        req=fixtures.nxt_request()
        promoted=audio.promote_episode(req,[fixtures.nxt_official()],fixtures.nxt_reports(),fixtures.current(),fixtures.NOW,programme=self.context())
        self.assertTrue(promoted.accepted)
        store=audio.PublicationStore(clock=lambda:fixtures.NOW)
        self.assertFalse(store.publish(promoted,self.context(),fixtures.NOW))
        self.assertTrue(store.publish(promoted,self.context(publicationSessionHash="test-session"),fixtures.NOW))
        self.assertEqual(store.get(fixtures.CHANNEL,fixtures.NOW)["audioSeriesTitle"],"WWE NXT")

class NoEPGGatewayTests(fixtures.GatewayAudioIntegrationTests):
    def test_new_row_only_for_same_active_session(self):
        now=time.time()
        req=fixtures.nxt_request(now)
        context={"channelID":fixtures.CHANNEL,"seriesTitle":"","publicationSessionHash":"s1"}
        promoted=audio.promote_episode(req,[fixtures.nxt_official()],fixtures.nxt_reports(),fixtures.current(now),now,programme=context)
        self.assertTrue(promoted.accepted)
        self.assertTrue(self.g.episode_publisher.publish(promoted,context,now))
        with patch.object(self.g.playback_activity,"views",return_value={fixtures.CHANNEL:SimpleNamespace(session_hash="s1")}):
            rows=self.g.merge_episode_publication({},now)
            row=rows[fixtures.CHANNEL]
            self.assertEqual(row["title"],"WWE NXT")
            self.assertNotIn("audioPlaybackSessionHash",row)
            item=self.g.programme_item(fixtures.CHANNEL,row)
            self.assertNotIn("StartDate",item)
            self.assertNotIn("EndDate",item)
            self.assertIn("WWE NXT",item["Name"])
            self.assertEqual(self.g.merge_episode_publication({fixtures.CHANNEL:{"title":"Different Show"}},now)[fixtures.CHANNEL]["title"],"Different Show")
        with patch.object(self.g.playback_activity,"views",return_value={fixtures.CHANNEL:SimpleNamespace(session_hash="s2")}):
            self.assertEqual(self.g.merge_episode_publication({},now),{})
        with patch.object(self.g.playback_activity,"views",return_value={}):
            self.assertEqual(self.g.merge_episode_publication({},now),{})


class PublicSearchTests(unittest.TestCase):
    def setUp(self):
        self.context={"seriesTitle":"","channelName":"WWE NXT","clues":["jevon evans","saquon shugars"]}
        self.official=fixtures.WWE_ARCHIVE_URL
        self.report=fixtures.WI_URL

    def test_search_page_accepts_only_real_result_links_and_approved_https(self):
        urls=[self.official,self.report,"https://127.0.0.1/private",
              "https://www.wwe.com:invalid/path","http://www.wwe.com/show",
              "https://www.wwe.com.evil.test/show","https://user@www.wwe.com/path"]
        markup="".join('<a class="result-link" href="//duckduckgo.com/l/?uddg='+urllib.parse.quote(u,safe="")+'">result</a>' for u in urls)
        markup+='<a href="https://www.wwe.com/not-a-result">unrelated</a>'
        self.assertEqual(discovery.parse_episode_search_page(markup.encode()),{"official":[self.official],"reports":[self.report]})

    def test_search_variants_are_bounded_and_clue_driven(self):
        queries=discovery.episode_search_queries(self.context)
        self.assertEqual(len(queries),2)
        self.assertIn("WWE NXT",queries[0])
        self.assertNotIn("WWE NXT",queries[1])
        self.assertEqual(discovery.episode_search_queries({"channelName":"WWE NXT"}),())

    def test_access_restriction_stops_second_query(self):
        calls=[];diag={}
        def fetch(q):
            calls.append(q)
            raise urllib.error.HTTPError("https://lite.duckduckgo.com",202,"challenge",None,None)
        result=discovery.public_episode_search(self.context,lambda x:x,fetch_page=fetch,diagnostics=diag)
        self.assertEqual(result,{"official":[],"reports":[]})
        self.assertEqual(len(calls),1)
        self.assertEqual(diag["faults"],{"http-202":1})

    def test_unknown_title_reports_need_explicit_air_date(self):
        self.assertEqual(audio.parse_report_page(fixtures.REPORT_PAGE,fixtures.REPORT_URL,{"seriesTitle":""}),[])
        report=fixtures.REPORT_PAGE.replace("WWE NXT results:", "WWE NXT results: June 16, 2026:")
        second="https://www.pwinsider.com/article/wwe-nxt-june-16"
        pages={fixtures.OFFICIAL_URL:fixtures.OFFICIAL_PAGE,fixtures.REPORT_URL:report,second:report}
        research=audio.PublicEvidenceResearch(pages.get,provider=lambda _:([fixtures.OFFICIAL_URL],[fixtures.REPORT_URL,second]))
        ctx={"channelID":fixtures.CHANNEL,"seriesTitle":"","channelName":"Channel"}
        req=fixtures.nxt_request()
        official,reports=research({"programme":ctx,"clues":list(req.clues)})
        self.assertEqual(len(official),1)
        self.assertEqual(len(reports),2)
        self.assertTrue(audio.promote_episode(req,official,reports,fixtures.current(),fixtures.NOW,programme=ctx).accepted)


class NoEPGDeadlineTests(unittest.TestCase):
    def test_discovery_uses_same_budget_as_page_fetch(self):
        context={"channelID":fixtures.CHANNEL,"seriesTitle":"","channelName":"Channel"}
        calls=[]
        def provider(_):
            time.sleep(1.01)
            return [fixtures.OFFICIAL_URL],[fixtures.REPORT_URL]
        def fetch(url):
            calls.append(url)
            return fixtures.OFFICIAL_PAGE
        research=audio.PublicEvidenceResearch(fetch,provider=provider,deadline_seconds=1)
        self.assertEqual(research({"programme":context,"clues":list(fixtures.nxt_request().clues)}),([],[]))
        self.assertEqual(calls,[])
