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
