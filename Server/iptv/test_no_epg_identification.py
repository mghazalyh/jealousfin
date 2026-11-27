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

def fixture_scoped_report():
    headline="WWE NXT results: June 16, 2026: Zaria wins first-ever women's North American Championship"
    obj={"@type":"NewsArticle","headline":headline,"description":"Results","datePublished":"2026-06-17"}
    return ('<meta property="og:title" content="'+headline+'">'
            +'<script type="application/ld+json">'+json.dumps(obj)+'</script>'
            +'<article><h1>'+headline+'</h1><time datetime="2026-06-17"></time>'
            +"<p>Zaria became the first-ever women's North American Champion.</p></article>")


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
        report=fixture_scoped_report()
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

class ReviewHardeningTests(unittest.TestCase):
    def test_report_date_not_incidental_or_ambiguous(self):
        self.assertEqual(audio._unknown_report_air_date("WWE NXT results: Zaria remembers June 16, 2026", ""), "")
        self.assertEqual(audio._unknown_report_air_date("WWE NXT results: June 16, 2026: Zaria", ""), "2026-06-16")
        self.assertEqual(audio._unknown_report_air_date("WWE NXT results: June 16, 2026: Zaria", "Original air date: June 17, 2026"), "")
        self.assertEqual(audio._unknown_report_air_date("WWE NXT results", "Published June 16, 2026"), "")
        self.assertEqual(audio._unknown_report_air_date("WWE NXT results", "Originally aired on June 16, 2026"), "2026-06-16")

    def test_unknown_session_cache_isolated_and_not_sent_to_provider(self):
        calls=[];fetches=[]
        def provider(payload):
            self.assertNotIn("researchSession",payload)
            calls.append(payload)
            return [fixtures.OFFICIAL_URL], [fixtures.REPORT_URL]
        def fetch(url):
            fetches.append(url)
            return fixtures.OFFICIAL_PAGE if url==fixtures.OFFICIAL_URL else fixtures.REPORT_PAGE
        research=audio.PublicEvidenceResearch(fetch,provider=provider)
        ctx={"channelID":fixtures.CHANNEL,"seriesTitle":"","researchSession":"a"*64}
        clues=list(fixtures.nxt_request().clues)
        def read(c):return research({"programme":c,"clues":clues})
        read(ctx);n=len(fetches)
        read(ctx)
        self.assertEqual(len(fetches),n)
        read(dict(ctx,researchSession="b"*64))
        self.assertGreater(len(fetches),n)
        self.assertEqual(len(calls),2)
        read(dict(ctx,researchSession=""));read(dict(ctx,researchSession=""))
        self.assertEqual(len(calls),4)

class ReviewTimestampTests(fixtures.GatewayAudioIntegrationTests):
    def test_invalid_timestamps_do_not_break_programme(self):
        for start,end in [(float("nan"),1),(0,float("inf")),(-1e100,1e100),(2,1),(False,10),(0,0)]:
            row={"title":"WWE NXT","episodeTitle":"Test episode","start":start,"end":end}
            item=self.g.programme_item(fixtures.CHANNEL,row)
            self.assertNotIn("StartDate",item)
            self.assertNotIn("EndDate",item)
        row={"title":"WWE NXT","episodeTitle":"Test episode","start":100,"end":200}
        self.assertIn("StartDate",self.g.programme_item(fixtures.CHANNEL,row))

class ReviewObserverTests(unittest.TestCase):
    def test_failed_publication_does_not_deduplicate_and_session_hash_is_valid(self):
        calls=[]; publications=[];wall=time.time()
        def research(payload):
            calls.append(payload)
            return [fixtures.nxt_official()],fixtures.nxt_reports()
        def publish(*args):
            publications.append(args)
            return len(publications)>1
        observer=audio.EpisodeObserver(lambda:fixtures.current(now=wall),research,enabled=True,
            programme=lambda cid:{"channelID":cid,"seriesTitle":""},
            activity=lambda cid:{"channel_id":cid,"active_seconds":150,"session_hash":"actual-session"},
            publish=publish)
        job={"cid":fixtures.CHANNEL,"name":"CW Presents WWE NXT","deviceVerified":True,
             "changed":False,"launchID":"launch-1","streamGeneration":"generation-1",
             "visitID":"visit-1","observedAt":wall-5,"expiresAt":wall+50,"sampleDigest":"b"*32}
        decision={"outcome":"allowed","language":"en","confidence":0.97,"speechSeconds":21.0,"sampleSeconds":32.0}
        self.assertEqual(observer(job,decision,fixtures.NXT_TEXT).reason,"publication-failed")
        self.assertTrue(observer(job,decision,fixtures.NXT_TEXT).accepted)
        self.assertEqual(observer(job,decision,fixtures.NXT_TEXT).reason,"duplicate-publication")
        self.assertEqual(len(publications),2)
        self.assertRegex(calls[0]["programme"]["researchSession"],r"^[0-9a-f]{64}$")
        self.assertEqual(audio._source_context(calls[0]["programme"])["researchSession"],calls[0]["programme"]["researchSession"])

    def test_session_change_during_callback_discards_only_failed_publication(self):
        wall=time.time();session=["session-a"];calls=[]
        store=audio.PublicationStore()
        def delivery(*args):
            calls.append(args)
            if len(calls)==1:session[0]="session-b"
            return True
        observer=audio.EpisodeObserver(lambda:fixtures.current(now=wall),
            lambda _:([fixtures.nxt_official()],fixtures.nxt_reports()),enabled=True,
            programme=lambda cid:{"channelID":cid,"seriesTitle":""},
            activity=lambda cid:{"channel_id":cid,"active_seconds":150,"session_hash":session[0]},
            publisher=store,publish=delivery)
        job={"cid":fixtures.CHANNEL,"name":"CW Presents WWE NXT","deviceVerified":True,
             "changed":False,"launchID":"launch-1","streamGeneration":"generation-1",
             "visitID":"visit-1","observedAt":wall-5,"expiresAt":wall+50,"sampleDigest":"b"*32}
        decision={"outcome":"allowed","language":"en","confidence":0.97,"speechSeconds":21.0,"sampleSeconds":32.0}
        self.assertEqual(observer(job,decision,fixtures.NXT_TEXT).reason,"publication-failed")
        self.assertIsNone(store.get(fixtures.CHANNEL))
        self.assertTrue(observer(job,decision,fixtures.NXT_TEXT).accepted)
        self.assertEqual(store.get(fixtures.CHANNEL)["audioPlaybackSessionHash"],"session-b")
        session[0]="session-c"
        self.assertTrue(observer(job,decision,fixtures.NXT_TEXT).accepted)
        self.assertEqual(store.get(fixtures.CHANNEL)["audioPlaybackSessionHash"],"session-c")

    def test_primary_store_success_does_not_hide_delivery_failure(self):
        wall=time.time();store=audio.PublicationStore()
        request=fixtures.nxt_request(wall)
        context={"channelID":fixtures.CHANNEL,"seriesTitle":"","publicationSessionHash":"session-a"}
        promotion=audio.promote_episode(request,[fixtures.nxt_official()],fixtures.nxt_reports(),fixtures.current(wall),wall,programme=context)
        self.assertTrue(promotion.accepted)
        observer=audio.EpisodeObserver(None,None,activity=lambda cid:{"channel_id":cid,"active_seconds":150,"session_hash":"session-a"},publisher=store,publish=lambda *args:False)
        self.assertFalse(observer._publish(promotion,context,wall))
        self.assertIsNone(store.get(fixtures.CHANNEL))
        observer.publish=lambda *args:True
        self.assertTrue(observer._publish(promotion,context,wall))
        self.assertIsNotNone(store.get(fixtures.CHANNEL))

    def test_exact_series_description_air_date(self):
        headline="WWE NXT Results 11/4 - Six Woman Tag Team Match"
        self.assertEqual(audio._unknown_report_air_date(headline,"This is Wrestling Inc.’s results for “WWE NXT” on November 4, 2025, with a singles match"),"2025-11-04")
        self.assertEqual(audio._unknown_report_air_date(headline,"Results for WWE Raw on November 4, 2025"),"")

class ResearchTimingTests(unittest.TestCase):
    def test_remaining_deadline_is_passed_to_page_fetch(self):
        timeouts=[]
        def fetch(url,timeout):
            timeouts.append(timeout)
            return fixtures.OFFICIAL_PAGE
        research=audio.PublicEvidenceResearch(lambda _:None,provider=lambda _:([fixtures.OFFICIAL_URL],[]),
            deadline_seconds=1,unknown_deadline_seconds=2,fetch_with_timeout=fetch)
        ctx={"channelID":fixtures.CHANNEL,"seriesTitle":""}
        research({"programme":ctx,"clues":list(fixtures.nxt_request().clues)})
        self.assertEqual(len(timeouts),1)
        self.assertGreater(timeouts[0],0)
        self.assertLessEqual(timeouts[0],2)

class GatewayResearchTimingTests(fixtures.GatewayAudioIntegrationTests):
    def test_unknown_provider_uses_expanded_budget_without_changing_known_lane(self):
        ctx={"seriesTitle":"","clues":list(fixtures.nxt_request().clues)}
        with patch.dict(self.g.gateway_config,{"episodeAudioEnabled":True,"metadataResearchTimeoutSeconds":180}), \
             patch.object(self.g.opencode_discovery,"public_episode_search",return_value={"official":[],"reports":[]}), \
             patch.object(self.g.opencode_discovery,"available",return_value=True), \
             patch.object(self.g.opencode_discovery,"run_model_detailed",return_value=({"official":[],"reports":[]},{})) as model:
            self.g.episode_source_provider(ctx)
            self.assertEqual(model.call_args.args[2],audio.UNKNOWN_PROVIDER_SECONDS)
            self.g.episode_source_provider(dict(ctx,seriesTitle="WWE NXT"))
            self.assertEqual(model.call_args.args[2],8)

class ConfirmedProgressTests(unittest.TestCase):
    def test_lead_budget_does_not_relax_known_title_or_short_sample(self):
        budget=audio.EpisodeBudget()
        view={"channel_id":fixtures.CHANNEL,"session_hash":"s1","active_seconds":15}
        args=dict(has_episode_identity=False,profile="american",language="en",language_outcome="allowed")
        self.assertEqual(budget.eligible(view,sample_seconds=55,**args),(False,"inactive"))
        self.assertEqual(budget.eligible(view,sample_seconds=55,unknown_lead=True,**args),(True,""))
        self.assertEqual(budget.eligible(view,sample_seconds=39,unknown_lead=True,**args),(False,"inactive"))
        self.assertEqual(budget.eligible(dict(view,active_seconds=11),sample_seconds=55,unknown_lead=True,**args),(False,"inactive"))

    def test_research_may_start_early_but_cannot_publish_before_confirmed_floor(self):
        for final, expected in [(20,False),(40,True)]:
            with self.subTest(final=final):
                wall=time.time();progress=[15];calls=[];store=audio.PublicationStore()
                def research(payload):
                    calls.append(payload)
                    progress[0]=final
                    return [fixtures.nxt_official()],fixtures.nxt_reports()
                observer=audio.EpisodeObserver(lambda:fixtures.current(now=wall),research,enabled=True,
                    budget=audio.EpisodeBudget(),programme=lambda cid:{"channelID":cid,"seriesTitle":""},
                    activity=lambda cid:{"channel_id":cid,"active_seconds":60,"confirmed_seconds":progress[0],"session_hash":"s1"},
                    publisher=store)
                job={"cid":fixtures.CHANNEL,"name":"CW Presents WWE NXT","deviceVerified":True,
                     "changed":False,"launchID":"launch-1","streamGeneration":"generation-1",
                     "visitID":"visit-1","observedAt":wall-5,"expiresAt":wall+50,"sampleDigest":"b"*32}
                decision={"outcome":"allowed","language":"en","confidence":0.97,"speechSeconds":21.0,"sampleSeconds":55.0}
                with patch.object(audio,"UNKNOWN_CONFIRM_WAIT_SECONDS",0):
                    result=observer(job,decision,fixtures.NXT_TEXT)
                self.assertEqual(len(calls),1)
                self.assertEqual(result.accepted,expected)
                self.assertEqual(store.get(fixtures.CHANNEL) is not None,expected)
                if not expected:self.assertEqual(result.reason,"insufficient-confirmed-progress")

class PublicArticleAndPartialSearchTests(unittest.TestCase):
    def test_completed_search_survives_model_timeout_but_only_as_leads(self):
        official=fixtures.WWE_ARCHIVE_URL
        report=fixtures.WI_URL
        def event(status,output,tool="websearch"):
            return {"type":"tool_use","part":{"tool":tool,"state":{"status":status,"output":output}}}
        encode=lambda rows: "\n".join(json.dumps(r) for r in rows).encode()
        events=[event("completed",f"Result: {official} and {report} and https://www.wwe.com.evil.test/fake"),
                event("error","https://www.pwinsider.com/fake")]
        data=encode(events)+b"\n{truncated"
        with patch.object(discovery,"_run_model_process",return_value=(data,True)):
            found,diag=discovery.run_model_detailed("fixture",discovery.DEFAULT_MODEL,1,episode_leads=True)
        self.assertEqual(found,{"official":[official],"reports":[report]})
        self.assertTrue(diag["timedOut"])
        self.assertEqual(diag["completedSearchLeadCounts"],{"official":1,"reports":1})
        self.assertEqual(discovery.completed_episode_search_leads(encode(events+[event("completed",official,"bash")])),{"official":[],"reports":[]})
        self.assertEqual(discovery.completed_episode_search_leads(encode([event("error",official)])),{"official":[],"reports":[]})
        self.assertEqual(discovery.completed_episode_search_leads(encode(events+[{"type":"error"}])),{"official":[],"reports":[]})
        with patch.object(discovery,"_run_model_process",return_value=(data,True)):
            with self.assertRaises(discovery.ResearchError):
                discovery.run_model_detailed("fixture",discovery.DEFAULT_MODEL,1)

    def test_article_body_matches_only_scoped_exact_disjoint_clues(self):
        headline="WWE NXT Results - November 4, 2025"
        base=f'<meta property="og:title" content="{headline}"><meta property="og:description" content="Six Woman Tag Team Match">'
        body=f'<article><h1>{headline}</h1><time datetime="2025-11-05T04:31:21-05:00"></time><p>Je&apos;Von Evans takes on Saquon Shugars.</p></article>'
        url="https://www.pwmania.com/wwe-nxt-results-november-4-2025"
        clues=["jevon evans","saquon shugars"]
        rows=audio.parse_report_page(base+body,url,{"seriesTitle":""},clues=clues)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0].original_air_date,"2025-11-04")
        self.assertEqual(rows[0].published_at,"2025-11-05")
        self.assertEqual(set(rows[0].terms),set(clues))
        for bad in [body.replace("<p>","<aside><p>").replace("</p>","</p></aside>"),
                    body.replace(headline,"Unrelated article"),
                    body+"<article><p>other</p></article>",
                    body.replace("Saquon Shugars","someone else"),
                    body.replace("Saquon Shugars","SaquonShugars"),
                    body.replace('<time datetime="2025-11-05T04:31:21-05:00"></time>',"")]:
            self.assertEqual(audio.parse_report_page(base+bad,url,{"seriesTitle":""},clues=clues),[])
        self.assertEqual(audio.parse_report_page(base+body,url,{"seriesTitle":""},clues=["jevon evans","jevon","evans"]),[])
        # Metadata contains both clues and explicit dates, but a mismatched
        # page body must still reject the entire source.
        rich=base.replace("Six Woman Tag Team Match","Je'Von Evans takes on Saquon Shugars.")
        unrelated=body.replace(headline,"Other report")
        self.assertEqual(audio.parse_report_page(rich+unrelated,url,{"seriesTitle":""},clues=clues),[])
        self.assertEqual(audio.parse_report_page(rich+body.replace(headline,"WWE NXT Results"),url,{"seriesTitle":""},clues=clues),[])
        self.assertEqual(audio.parse_report_page(rich+body+"<article><h1>Other</h1></article>",url,{"seriesTitle":""},clues=clues),[])


    def test_unknown_evidence_cache_also_scoped_to_clues_within_session(self):
        calls=[]
        report=fixture_scoped_report()
        def fetch(url):
            calls.append(url)
            return fixtures.OFFICIAL_PAGE if url==fixtures.OFFICIAL_URL else report
        research=audio.PublicEvidenceResearch(fetch,provider=lambda _:([fixtures.OFFICIAL_URL],[fixtures.REPORT_URL]))
        ctx={"channelID":fixtures.CHANNEL,"seriesTitle":"","researchSession":"a"*64}
        first={"programme":ctx,"clues":list(fixtures.nxt_request().clues)}
        _,rows=research(first)
        self.assertTrue(rows)
        n=len(calls);research(first);self.assertEqual(len(calls),n)
        _,other=research(dict(first,clues=["different person","unrelated plot"]))
        self.assertGreater(len(calls),n)
        self.assertEqual(other,[])

    def test_verified_content_after_rotation_is_not_a_live_publication(self):
        wall=time.time();switched=[False];store=audio.PublicationStore();delivered=[]
        def research(payload):
            switched[0]=True
            return [fixtures.nxt_official()],fixtures.nxt_reports()
        observer=audio.EpisodeObserver(lambda:fixtures.current(now=wall,visitID="visit-other" if switched[0] else "visit-1"),research,enabled=True,
            programme=lambda cid:{"channelID":cid,"seriesTitle":""},
            activity=lambda cid:{"channel_id":cid,"active_seconds":150,"session_hash":"s1"},
            publisher=store,publish=lambda *args:delivered.append(args) or True)
        job={"cid":fixtures.CHANNEL,"name":"CW Presents WWE NXT","deviceVerified":True,
             "changed":False,"launchID":"launch-1","streamGeneration":"generation-1",
             "visitID":"visit-1","observedAt":wall-5,"expiresAt":wall+50,"sampleDigest":"b"*32}
        decision={"outcome":"allowed","language":"en","confidence":0.97,"speechSeconds":21.0,"sampleSeconds":55.0}
        result=observer(job,decision,fixtures.NXT_TEXT)
        self.assertFalse(result.accepted)
        self.assertIsNone(store.get(fixtures.CHANNEL))
        self.assertEqual(delivered,[])
        diag=observer.diagnostics()["contentResearch"]
        self.assertEqual((diag["completed"],diag["verified"],diag["verifiedAfterVisit"]),(1,1,1))
        self.assertEqual(diag["lastOutcome"],"verified")

    def test_publication_dates_must_agree_and_json_body_is_not_unscoped_evidence(self):
        h="WWE NXT Results - November 4, 2025"
        clues=["jevon evans","saquon shugars"]
        url="https://www.pwmania.com/wwe-nxt-results-november-4-2025"
        meta=f'<meta property="og:title" content="{h}"><meta property="og:description" content="Results"><meta property="article:published_time" content="2025-11-05">'
        body=f'<article><h1>{h}</h1><time datetime="2025-11-05T04:31:21-05:00"></time><p>Je&apos;Von Evans takes on Saquon Shugars.</p></article>'
        parse=lambda page:audio.parse_report_page(page,url,{"seriesTitle":""},clues=clues)
        self.assertTrue(parse(meta+body))
        self.assertEqual(parse(meta+body.replace("2025-11-05T","2025-11-06T")),[])
        self.assertEqual(parse(meta+body.replace("</article>",'<time datetime="2025-11-06"></time></article>')),[])
        obj={"@type":"NewsArticle","headline":h,"description":"Results","datePublished":"2025-11-05","articleBody":"Je'Von Evans takes on Saquon Shugars"}
        structured=lambda x:'<script type="application/ld+json">'+json.dumps(x)+'</script>'
        self.assertEqual(parse(structured(obj)),[])
        self.assertEqual(parse(meta+body.replace("Je&apos;Von Evans takes on Saquon Shugars.","Unrelated details.")+structured(obj)),[])
        self.assertEqual(parse(meta+body+structured(dict(obj,datePublished="2025-11-06"))),[])
        self.assertTrue(parse(meta+body+structured(obj)))

    def test_timeout_diagnostics_distinguish_no_search_and_no_approved_leads(self):
        for events,reason in [([], "no-completed-public-search"),([{"type":"tool_use","part":{"tool":"websearch","state":{"status":"completed","output":"https://example.org/no"}}}],"timeout-no-approved-leads")]:
            raw="\n".join(json.dumps(x) for x in events).encode()
            with patch.object(discovery,"_run_model_process",return_value=(raw,True)):
                with self.assertRaises(discovery.ResearchError) as caught:
                    discovery.run_model_detailed("fixture",discovery.DEFAULT_MODEL,1,episode_leads=True)
                self.assertEqual(caught.exception.diagnostics["error"],reason)
        # An unexpected tool must fail closed even if earlier results are useful.
        raw="\n".join(json.dumps(x) for x in [{"type":"tool_use","part":{"tool":"websearch","state":{"status":"completed","output":fixtures.OFFICIAL_URL}}},{"type":"tool_use","part":{"tool":"bash","state":{"status":"error"}}}]).encode()
        with patch.object(discovery,"_run_model_process",return_value=(raw,True)):
            with self.assertRaises(discovery.ResearchError):
                discovery.run_model_detailed("fixture",discovery.DEFAULT_MODEL,1,episode_leads=True)

    def test_model_final_json_cannot_add_unobserved_urls(self):
        official=fixtures.OFFICIAL_URL
        report=fixtures.REPORT_URL
        invented="https://www.wwe.com/invented"
        final={"official":[invented],"reports":["https://www.pwinsider.com/invented"]}
        encode=lambda rows: "\n".join(json.dumps(r) for r in rows).encode()
        completed=lambda output:{"type":"tool_use","part":{"tool":"websearch","state":{"status":"completed","output":output}}}
        answer={"type":"text","part":{"text":json.dumps(final)}}
        with patch.object(discovery,"_run_model_process",return_value=(encode([completed(official+" "+report),answer]),False)):
            result,diag=discovery.run_model_detailed("fixture",discovery.DEFAULT_MODEL,1,episode_leads=True)
        self.assertEqual(result,{"official":[official],"reports":[report]})
        self.assertFalse(diag["partialResult"])
        with patch.object(discovery,"_run_model_process",return_value=(encode([completed("https://example.org/no"),answer]),False)):
            with self.assertRaises(discovery.ResearchError) as caught:
                discovery.run_model_detailed("fixture",discovery.DEFAULT_MODEL,1,episode_leads=True)
        self.assertEqual(caught.exception.diagnostics["error"],"no-approved-search-leads")
        with patch.object(discovery,"_run_model_process",return_value=(encode([completed(official),answer,{"type":"tool_use","part":{"tool":"bash"}}]),False)):
            with self.assertRaises(discovery.ResearchError):
                discovery.run_model_detailed("fixture",discovery.DEFAULT_MODEL,1,episode_leads=True)
        with patch.object(discovery,"_run_model_process",return_value=(encode([completed(official),answer]),False)):
            result,diag=discovery.run_model_detailed("fixture",discovery.DEFAULT_MODEL,1)
        self.assertEqual(result,final)

    def test_strict_event_rejection_is_only_enabled_in_unknown_title_lane(self):
        encode=lambda rows: "\n".join(json.dumps(r) for r in rows).encode()
        completed={"type":"tool_use","part":{"tool":"websearch","state":{"status":"completed","output":fixtures.OFFICIAL_URL}}}
        malformed={"type":"tool_use","part":{}}
        answer={"type":"text","part":{"text":'{"official":[],"reports":[]}'}}
        data=encode([completed,malformed,answer])
        result,diag=discovery.parse_events_detailed(data)
        self.assertEqual(result,{"official":[],"reports":[]})
        self.assertIsNone(diag["error"])
        result,diag=discovery.parse_events_detailed(data,strict_episode=True)
        self.assertIsNone(result)
        self.assertEqual(diag["error"],"unexpected-tool")
        other={"type":"tool_use","part":{"tool":"bash"}}
        data=encode([completed,other])
        self.assertEqual(discovery.parse_events_detailed(data)[1]["error"],"invalid-answer")
        self.assertEqual(discovery.parse_events_detailed(data,strict_episode=True)[1]["error"],"unexpected-tool")

    def test_metadata_without_matching_visible_body_cannot_supply_clues(self):
        h="WWE NXT Results - November 4, 2025"
        clues=["jevon evans","saquon shugars"]
        url="https://www.pwmania.com/wwe-nxt-results-november-4-2025"
        meta=f'<meta property="og:title" content="{h}"><meta property="og:description" content="Jevon Evans and Saquon Shugars"><meta property="article:published_time" content="2025-11-05">'
        body=f'<article><h1>{h}</h1><time datetime="2025-11-05"></time><p>Unrelated recap.</p></article>'
        self.assertEqual(audio.parse_report_page(meta+body,url,{"seriesTitle":""},clues=clues),[])
        self.assertEqual(audio.parse_report_page(meta,url,{"seriesTitle":""},clues=clues),[])
        self.assertTrue(audio.parse_report_page(meta+body.replace("Unrelated recap.","Jevon Evans and Saquon Shugars"),url,{"seriesTitle":""},clues=clues))

    def test_canonical_publisher_count_and_confidence_ignore_alias_duplicates(self):
        req=fixtures.nxt_request()
        reports=fixtures.nxt_reports()
        first=audio.dataclasses.replace(reports[0],source_url="https://www.pwmania.com/a")
        alias=audio.dataclasses.replace(first,source_url="https://pwmania.com/b")
        second=audio.dataclasses.replace(reports[1],source_url="https://www.wrestlinginc.com/c")
        ctx={"seriesTitle":""}
        verify=lambda rows:audio.verify_episode_content(req,[fixtures.nxt_official()],rows,fixtures.NOW,ctx)
        self.assertFalse(verify([first,alias]).accepted)
        normal=verify([first,second]);duplicate=verify([first,alias,second])
        self.assertTrue(normal.accepted)
        self.assertEqual(duplicate.corroborating_sources,2)
        self.assertEqual(normal.matched_clue_count,duplicate.matched_clue_count)
        self.assertEqual(audio.independent_publisher("https://www.wrestlingobserver.com/foo"),audio.independent_publisher("https://f4wonline.com/bar"))

class FairSourceReadTests(unittest.TestCase):
    def test_distinct_reports_and_matching_archive_prioritized_under_deadline(self):
        ctx={"channelID":fixtures.CHANNEL,"seriesTitle":""}
        first="https://www.pwmania.com/first"
        duplicate="https://pwmania.com/second"
        second="https://www.wrestlinginc.com/other"
        wrong="https://www.wwe.com/shows/wwenxt/2025-11-04"
        right="https://www.wwe.com/shows/wwenxt/2026-06-16"
        calls=[]
        def fetch(url,timeout):
            calls.append((url,timeout))
            return fixture_scoped_report() if url in (first,duplicate,second) else fixtures.OFFICIAL_PAGE
        research=audio.PublicEvidenceResearch(lambda u:fetch(u,8),provider=lambda _:([wrong,right],[first,duplicate,second]),
                   unknown_deadline_seconds=30,fetch_with_timeout=fetch)
        official,reports=research({"programme":ctx,"clues":list(fixtures.nxt_request().clues)})
        self.assertEqual([u for u,_ in calls],[first,right,second,wrong,duplicate])
        self.assertTrue(all(0<t<=3 for _,t in calls))
        self.assertTrue(official)
        self.assertTrue(reports)

    def test_access_denial_is_not_retried_through_publisher_alias(self):
        import urllib.error
        first="https://www.pwmania.com/a";alias="https://pwmania.com/b"
        calls=[]
        def fetch(url,timeout):
            calls.append(url)
            if url==first:raise urllib.error.HTTPError(url,403,"denied",None,None)
            return None
        research=audio.PublicEvidenceResearch(lambda u:fetch(u,8),provider=lambda _:([fixtures.OFFICIAL_URL],[first,alias]),fetch_with_timeout=fetch)
        research({"programme":{"channelID":fixtures.CHANNEL,"seriesTitle":""},"clues":list(fixtures.nxt_request().clues)})
        self.assertEqual(calls,[first,fixtures.OFFICIAL_URL])
