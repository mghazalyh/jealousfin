"""Local HTTP gateway acceptance. No provider or live device access."""
import importlib
import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch


class GatewayAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env={"JEALOUSFIN_STATE_DIR":self.temp.name,"JEALOUSFIN_USERNAME":"fixture",
             "JEALOUSFIN_PASSWORD":"fixture","JEALOUSFIN_TOKEN":"fixture-token",
             "JEALOUSFIN_SERVER_ID":"fixture-server","JEALOUSFIN_BIND":"127.0.0.1"}
        with patch.dict(os.environ,env):
            import gateway
            self.g=importlib.reload(gateway)
        self.a,self.b,self.c=("a"*32,"b"*32,"c"*32)
        for cid in (self.a,self.b,self.c):
            self.g.channels[cid]={"Id":cid,"Name":cid,"Type":"TvChannel"}
            self.g.sources[cid]=cid
        self.db=pathlib.Path(self.temp.name)/"policy.sqlite3"
        with self.g.policy_connect(self.db) as db:
            db.executemany("INSERT INTO channel(id,name) VALUES(?,?)",[(cid,cid) for cid in (self.a,self.b,self.c)])
            db.execute("INSERT INTO meta VALUES('seeded','1')")
            db.execute("UPDATE channel SET hiatus_until=?,failures=1 WHERE id=?",(time.time()+86400,self.a))
            db.execute("UPDATE channel SET banned=1,failures=8,last_failure=? WHERE id=?",(time.time()-100,self.b))
        self.server=self.g.Server(("127.0.0.1",0),self.g.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.base="http://127.0.0.1:"+str(self.server.server_port)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def req(self,path,body=None,auth=True):
        headers={"X-Emby-Token":"fixture-token"} if auth else {}
        if body is not None:headers["Content-Type"]="application/json"
        request=urllib.request.Request(self.base+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
        try:
            with urllib.request.urlopen(request,timeout=3) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as error:
            with error:return error.code,json.load(error)

    def test_catalogue_visibility_and_final_gate(self):
        status,legacy=self.req("/LiveTv/Channels")
        self.assertEqual(status,200)
        self.assertEqual([x["Id"] for x in legacy["Items"]],[self.c])
        status,full=self.req("/LiveTv/Channels?JealousfinIncludePolicy=true")
        self.assertEqual(status,200)
        self.assertEqual(len(full["Items"]),3)
        self.assertEqual(full["JealousfinPolicy"]["channels"][self.b]["banned"],True)
        with patch.object(self.g,"resolve",side_effect=AssertionError("excluded stream must not resolve")):
            self.assertEqual(self.req("/Items/"+self.a+"/PlaybackInfo",{})[0],409)
            self.assertEqual(self.req("/Items/"+self.b+"/PlaybackInfo",{})[0],409)
        with patch.object(self.g,"resolve",return_value="https://fixture.invalid/live.m3u8"),patch.object(self.g,"register",return_value="/media/fixture-token/test.m3u8"):
            self.assertEqual(self.req("/Items/"+self.c+"/PlaybackInfo",{})[0],200)

    def test_failure_dedup_and_reinstatement(self):
        event={"channelID":self.c,"trace":"trace-one","action":"failure","reason":"fixture"}
        self.assertEqual(self.req("/Jealousfin/Policy/Event",event)[0],200)
        self.assertEqual(self.req("/Jealousfin/Policy/Event",event)[0],200)
        status,policy=self.req("/Jealousfin/Policy")
        self.assertEqual(status,200)
        self.assertEqual(policy["channels"][self.c]["failures"],1)
        approve={"channelID":self.b,"trace":"approve-one","action":"approve"}
        self.assertEqual(self.req("/Jealousfin/Policy/Event",approve)[0],403)
        approve["approval"]="explicit"
        self.assertEqual(self.req("/Jealousfin/Policy/Event",approve)[0],200)
        self.assertEqual(self.req("/Jealousfin/Policy")[1]["channels"][self.b]["banned"],False)
        self.assertEqual(self.req("/Jealousfin/Policy",auth=False)[0],401)

    def test_policy_unavailable_fails_closed(self):
        with self.g.policy_connect(self.db) as db:db.execute("DELETE FROM meta WHERE key='seeded'")
        self.assertEqual(self.req("/LiveTv/Channels")[0],503)
        self.assertEqual(self.req("/Items/"+self.c+"/PlaybackInfo",{})[0],503)

    def test_validated_runtime_reload(self):
        g=self.g
        g.CONFIG_FILE.write_text(json.dumps({"revision":"fixture-v2","resourceLimit":600,"resourceTTLSeconds":300,"activeProtectionSeconds":60,"upstreamTimeoutSeconds":5,"sourceSelection":"first"}))
        self.assertTrue(g.reload_gateway_config(force=True))
        self.assertEqual(g.RESOURCE_LIMIT,600)
        self.assertEqual(g.gateway_config["upstreamTimeoutSeconds"],5)
        self.assertEqual(self.req("/trial-status")[1]["configurationRevision"],"fixture-v2")
        g.CONFIG_FILE.write_text(json.dumps({"revision":"invalid","upstreamTimeoutSeconds":1}))
        self.assertFalse(g.reload_gateway_config(force=True))
        self.assertEqual(g.gateway_config["revision"],"fixture-v2")
        self.assertIsNotNone(g.config_fault)
        g.CONFIG_FILE.write_text(json.dumps({"revision":"fixture-v3","resourceLimit":700}))
        self.assertTrue(g.reload_gateway_config(force=True))
        self.assertEqual(g.RESOURCE_LIMIT,700)
        self.assertIsNone(g.config_fault)

    def test_favorites_persist(self):
        g=self.g
        g.set_favorite(self.c,True)
        g.favorites.clear();g.load_favorites()
        self.assertIn(self.c,g.favorites)
        g.set_favorite(self.c,False)
        g.favorites.clear();g.load_favorites()
        self.assertNotIn(self.c,g.favorites)

    def test_sequence_segment_cache(self):
        g=self.g
        playlist=b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:100\n#EXTINF:5,\nsegment.ts\n"
        with patch.object(g,"validate",side_effect=lambda u:u):
            first=g.rewrite(playlist,"https://fixture.invalid/live.m3u8",self.c)
            second=g.rewrite(playlist.replace(b":100",b":101"),"https://fixture.invalid/live.m3u8",self.c)
            self.assertNotEqual(first,second)
        rid=next(iter(g.segment_resources))
        with patch.object(g,"fetch",return_value=(b"fixture",{},200,"https://fixture.invalid/segment.ts")) as fetch:
            self.assertFalse(g.fetch_media(rid,"https://fixture.invalid/segment.ts",None)[1])
            self.assertTrue(g.fetch_media(rid,"https://fixture.invalid/segment.ts",None)[1])
            self.assertEqual(fetch.call_count,1)
            self.assertFalse(g.fetch_media(rid,"https://fixture.invalid/segment.ts","bytes=0-2")[1])
        self.assertLessEqual(g.segment_cache_bytes,g.CACHE_BYTES)

    def test_resume_policy(self):
        with self.g.policy_connect(self.db) as db:
            db.execute("INSERT INTO meta VALUES('surf_resume',?)",(json.dumps({"channelID":self.c,"category":"All Channels","paused":True,"remainingSeconds":50,"at":time.time()}),))
        self.assertEqual(self.req("/Jealousfin/Policy")[1]["resume"]["channelID"],self.c)

    def test_metadata_cache_on_provider_failure(self):
        g=self.g
        fresh={"nltv:"+str(i):{"name":"fixture "+str(i),"genres":["Drama"]} for i in range(100)}
        with patch.object(g,"catalogue",return_value=fresh):g.initialize_catalogue()
        self.assertEqual(g.catalogue_source,"provider")
        with patch.object(g,"catalogue",side_effect=TimeoutError):g.initialize_catalogue()
        self.assertEqual(g.catalogue_source,"cached")
        self.assertEqual(g.catalogue_fault,"TimeoutError")

    def test_current_programme(self):
        g=self.g
        g.sources[self.c]="nltv:movie"
        cid=__import__("hashlib").md5(b"nltv:movie").hexdigest()
        g.channels[cid]=g.channels.pop(self.c)
        g.channels[cid]["Id"]=cid
        del g.sources[self.c];g.sources[cid]="nltv:movie"
        with g.policy_connect(self.db) as db:db.execute("UPDATE channel SET id=? WHERE id=?",(cid,self.c))
        raw={"nltv:movie":{"name":"MovieSphere Gold","description":"Now: Love the Coopers (until 23:16)\nNext: Killers at 23:16","releaseInfo":"Now · Love the Coopers"}}
        g.update_programmes(raw)
        items=self.req("/LiveTv/Channels")[1]["Items"]
        self.assertEqual(items[0]["CurrentProgram"]["Name"],"Love the Coopers")
        self.assertEqual(items[0]["CurrentProgram"]["JealousfinProgramme"]["nextTitle"],"Killers")
        self.assertNotIn("EndDate",items[0]["CurrentProgram"])
        self.assertEqual(self.req("/LiveTv/Programs?ChannelIds="+cid)[1]["Items"][0]["Name"],"Love the Coopers")
        self.assertEqual(self.req("/LiveTv/Programs",auth=False)[0],401)
        self.assertEqual(g.programme_snapshot(g.programme_observed_at+151),{})
        g.update_programmes({"nltv:movie":{"name":"MovieSphere Gold"}})
        self.assertNotIn("CurrentProgram",self.req("/LiveTv/Channels")[1]["Items"][0])
        self.assertIsNone(g.parse_programme({"name":"Example","description":"Now: Show","releaseInfo":"Now · Different"}))
        self.assertIsNone(g.parse_programme({"name":"Example","description":"Now: https://fixture.invalid"}))

    def test_external_schedule_corroboration_and_precedence(self):
        g=self.g
        now=1795620275
        g.channels[self.c]["Name"]="OuterSphere"
        a,b=g.GUIDE_CHANNELS["OuterSphere"]
        g.guide_rows=({a:[(now-60,now+600,"You're Next")]},{b:[(now-59,now+599,"You're Next")]})
        g.guide_observed_at=now
        rows=g.programme_snapshot(now)
        self.assertEqual(rows[self.c]["title"],"You're Next")
        item=g.programme_item(self.c,rows[self.c])
        self.assertEqual(item["Name"],"Scheduled: You're Next")
        self.assertIn("EndDate",item)
        g.programme_rows={self.c:{"title":"Provider actual"}}
        g.programme_observed_at=now
        self.assertEqual(g.programme_snapshot(now)[self.c]["title"],"Provider actual")
        g.programme_rows={}
        g.guide_rows=({a:[(now-60,now+600,"You're Next")]},{b:[(now-59,now+599,"Different")]})
        self.assertNotIn(self.c,g.programme_snapshot(now))
        g.guide_rows=({a:[(now-60,now+600,"You're Next")]},{b:[(now-59,now+599,"You're Next")]})
        self.assertNotIn(self.c,g.programme_snapshot(now+601))
        self.assertNotIn(self.c,g.programme_snapshot(now+1801))
        raw=b'<tv><programme channel="fixture" start="20261125140000 +0000" stop="20261125160000 +0000"><title>Fixture</title></programme></tv>'
        self.assertEqual(g.parse_guide(raw,{"fixture"},now)["fixture"][0][2],"Fixture")
        with self.assertRaises(ValueError):g.parse_guide(b'<!DOCTYPE tv><tv/>',{"fixture"},now)

    def test_active_resource_protection(self):
        g=self.g
        g.RESOURCE_LIMIT=2
        now=time.monotonic()
        g.resources["active"]=(self.c,"active")
        g.resources["idle"]=(self.a,"idle")
        g.reverse[(self.c,"active")]="active";g.reverse[(self.a,"idle")]="idle"
        g.resource_touched.update(active=now,idle=now-1000)
        g.active_channels[self.c]=now+120
        g.evict_resources(now,1)
        self.assertIn("active",g.resources)
        self.assertNotIn("idle",g.resources)
        self.assertNotIn((self.a,"idle"),g.reverse)


if __name__=="__main__":
    unittest.main()
