import datetime as dt,json,pathlib,tempfile,time,unittest
from unittest.mock import patch
import opencode_discovery as d
class ResearchTests(unittest.TestCase):
    def test_research_variant_is_explicit_and_scoped(self):
        args=d.model_args('fixture',d.DEFAULT_MODEL,'/tmp/fixture')
        self.assertEqual(args[args.index('--variant')+1],'low')
        self.assertNotIn('--variant',d.model_args('fixture',d.DEFAULT_MODEL,'/tmp/fixture',None))
        with self.assertRaises(ValueError):d.model_args('fixture',d.DEFAULT_MODEL,'/tmp/fixture','medium')
    def test_restricted_environment(self):
        with tempfile.TemporaryDirectory() as work,patch.dict("os.environ",{"JEALOUSFIN_TOKEN":"secret","GITHUB_TOKEN":"secret"}):
            env=d.model_environment(work)
            self.assertNotIn("JEALOUSFIN_TOKEN",env);self.assertNotIn("GITHUB_TOKEN",env)
            self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"])["permission"],{"*":"deny","websearch":"allow"})
    def test_output_requires_search(self):
        result={"title":"Example","year":2020,"region":"us","sources":[]}
        events=[{"type":"tool_use","part":{"tool":"websearch","state":{"status":"completed"}}},{"type":"text","part":{"text":json.dumps(result)}}]
        encode=lambda rows:"\n".join(json.dumps(r) for r in rows).encode()
        self.assertEqual(d.parse_events(encode(events)),result)
        with self.assertRaises(ValueError):d.parse_events(encode(events[1:]))
        with self.assertRaises(ValueError):d.parse_events(encode([{"type":"tool_use","part":{"tool":"bash"}}]+events))
    def test_evidence(self):
        now=time.time();fmt=lambda t:dt.datetime.fromtimestamp(t,dt.timezone.utc).isoformat()
        def page(name,title,start,end,region="US"):
            obj={"@type":"BroadcastService","name":name,"areaServed":region,"hasBroadcastEvent":[{"@type":"BroadcastEvent","name":title,"startDate":start,"endDate":end}]}
            return '<script type="application/ld+json">'+json.dumps(obj)+'</script>'
        candidate=d.normalize_result({"title":"Example","region":"us","year":2020,"sources":["https://www.tvpassport.com/example","https://www.tvguide.com/example","http://localhost/secret","https://www.tvguide.com/path?token=secret"]})
        self.assertEqual(len(candidate["sources"]),2)
        good=page("Fixture TV","Example",fmt(now-60),fmt(now+600))
        self.assertEqual(d.verify_candidate(candidate,"Fixture TV","nltv:us:Fixture.us",now,lambda u:good)["title"],"Example")
        changed=page("Fixture TV","Next show",fmt(now-60),fmt(now+600))
        self.assertEqual(d.verify_candidate(candidate,"Fixture TV","nltv:us:Fixture.us",now,lambda u:changed,match_title=False)["title"],"Next show")
        self.assertIsNone(d.verify_candidate(candidate,"Fixture TV","nltv:us:Fixture.us",now,lambda u:changed))
        self.assertIsNone(d.verify_candidate(candidate,"Other","nltv:us:Fixture.us",now,lambda u:good))
        self.assertIsNone(d.verify_candidate(candidate,"Fixture TV","nltv:uk:Fixture.uk",now,lambda u:good))
        self.assertIsNone(d.verify_candidate(candidate,"Fixture TV","nltv:us:Fixture.us",now+601,lambda u:good))
        self.assertIsNone(d.verify_candidate(dict(candidate,sources=["https://tvguide.com/a","https://www.tvguide.com/b"]),"Fixture TV","nltv:us:Fixture.us",now,lambda u:good))
        self.assertIsNone(d.verify_candidate(candidate,"Fixture TV","nltv:us:Fixture.us",now,lambda u:page("Fixture TV","Example",fmt(now-60),fmt(now+600),"Canada")))
        self.assertIsNone(d.verify_candidate(candidate,"Fixture TV","nltv:us:Fixture.us",now,lambda u:page("Fixture TV","Example","2026-11-26T06:00:00","2026-11-26T07:00:00")))
        self.assertIsNone(d.verify_candidate(candidate,"Fixture TV","nltv:us:Fixture.us",now,lambda u:good if "tvguide" in u else page("Fixture TV","Other",fmt(now-60),fmt(now+600))))
    def test_supported_region(self):
        self.assertTrue(d.supported_region("nltv:us:Fixture.us"))
        self.assertTrue(d.supported_region("nltv:uk:Fixture.uk"))
        self.assertFalse(d.supported_region("nltv:sg:Fixture.sg"))
        self.assertFalse(d.supported_region("nltv:my:Fixture.my"))
        self.assertFalse(d.supported_region("nltv:id:Fixture.id"))
    def test_persistent_cooldown_without_daily_quota(self):
        with tempfile.TemporaryDirectory() as work:
            p=pathlib.Path(work)/"research.json";now=time.time();a,b="a"*32,"b"*32
            state=d.Discovery(p);self.assertEqual(state.choose({a,b},{b},now,12),b)
            state.calls.append(now);self.assertIsNone(state.choose({a,b},{b},now+60,12))
            state.records[b]={"at":now,"outcome":"missing","candidate":d.normalize_result({})}
            state.save(lambda path,data:path.write_text(json.dumps(data)))
            restored=d.Discovery(p)
            self.assertEqual(restored.choose({a,b},{b},now+901,12),a)
            self.assertEqual(restored.choose({a,b},{b},now+901,1),a)
            restored.calls=[now-1800]*100
            self.assertEqual(restored.choose({a,b},{b},now+901,12),a)
            self.assertIsNone(restored.status(now+901,12)['dailyBudget'])
            p.write_text("corrupt");corrupt=d.Discovery(p)
            self.assertIsNone(corrupt.choose({a,b},{b},time.time()+60,12))
            self.assertEqual(corrupt.choose({a,b},{b},time.time()+901,12),b)
    def test_unverified_does_not_publish(self):
        with tempfile.TemporaryDirectory() as work:
            s=d.Discovery(pathlib.Path(work)/"research.json")
            s.records["a"*32]={"at":time.time(),"outcome":"unverified","candidate":{"title":"Invented"}}
            self.assertEqual(s.snapshot(time.time()),{})
if __name__=="__main__":unittest.main()
