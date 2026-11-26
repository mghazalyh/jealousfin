"""Local HTTP gateway acceptance. No provider or live device access."""
import importlib
import json
import os
import pathlib
import tempfile
import sqlite3
from contextlib import closing
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

    def test_http_audit_redacts_secrets_and_records_login_status(self):
        with patch.object(self.g,'log') as audit:
            self.assertEqual(self.req('/Users/AuthenticateByName',{'Username':'fixture','Pw':'fixture'})[0],200)
            self.assertEqual(self.req('/Users/AuthenticateByName',{'Username':'fixture','Pw':'wrong-secret'})[0],401)
            self.assertEqual(self.req('/media/sensitive-token/unknown.bin?api_key=secret-query')[0],401)
        rows=[json.loads(c.args[0].split(' ',1)[1]) for c in audit.call_args_list if c.args[0].startswith('HTTP_AUDIT ')]
        self.assertEqual([r['status'] for r in rows],[200,401,401])
        self.assertEqual(rows[-1]['route'],'/media/[redacted]')
        for secret in ('wrong-secret','sensitive-token','secret-query','fixture-token'):
            self.assertNotIn(secret,str(rows))

    def test_lan_static_asset_route_and_origin(self):
        from unittest.mock import MagicMock
        self.assertTrue(self.g.web_asset('/assets/fonts/MaterialIcons-Regular.otf'))
        self.assertFalse(self.g.web_asset('/assets/%2e%2e/gateway-runtime.json'))
        self.assertFalse(self.g.web_asset('/assets/a%2fb'))
        self.assertFalse(self.g.web_asset('/gateway-runtime.json'))
        upstream=MagicMock()
        upstream.status=200
        upstream.read.return_value=b'<html>Jealouswin</html>'
        upstream.getheader.return_value='text/html; charset=utf-8'
        with patch.object(self.g.http.client,'HTTPSConnection') as connection:
            connection.return_value.getresponse.return_value=upstream
            req=urllib.request.Request(self.base+'/?api_key=never-forward',headers={'Cookie':'never-forward','Authorization':'never-forward'})
            with urllib.request.urlopen(req,timeout=3) as response:
                self.assertEqual(response.read(),b'<html>Jealouswin</html>')
            connection.assert_called_once_with('jealouswin.pages.dev',timeout=20)
            connection.return_value.request.assert_called_once_with('GET','/',headers={'Accept-Encoding':'identity','User-Agent':'Jealousfin-LAN-assets/1'})
        origin=self.g.local_web_origin()
        req=urllib.request.Request(self.base+'/Users/AuthenticateByName',data=b'{"Username":"fixture","Pw":"fixture"}',headers={'Origin':origin,'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=3) as response:
            self.assertEqual(response.status,200)
            self.assertEqual(response.headers['Access-Control-Allow-Origin'],origin)

    def test_browser_origin_cors_preflight_and_authentication(self):
        origin='https://jealouswin.pages.dev'
        def send(path,method='GET',headers=None,body=None):
            req=urllib.request.Request(self.base+path,method=method,headers=headers or {},data=body)
            try:
                with urllib.request.urlopen(req,timeout=3) as response:
                    return response.status,dict(response.headers),response.read()
            except urllib.error.HTTPError as error:
                with error:return error.code,dict(error.headers),error.read()
        pre={'Origin':origin,'Access-Control-Request-Method':'POST',
             'Access-Control-Request-Headers':'content-type, x-emby-token',
             'Access-Control-Request-Private-Network':'true'}
        code,h,body=send('/Users/AuthenticateByName','OPTIONS',pre)
        self.assertEqual(code,204);self.assertEqual(body,b'')
        self.assertEqual(h['Access-Control-Allow-Origin'],origin)
        self.assertEqual(h['Access-Control-Allow-Private-Network'],'true')
        self.assertNotIn('Access-Control-Allow-Credentials',h)
        self.assertEqual(send('/Users/AuthenticateByName','OPTIONS',dict(pre,Origin='https://evil.example'))[0],403)
        self.assertEqual(send('/Users/AuthenticateByName','OPTIONS',dict(pre,**{'Access-Control-Request-Headers':'x-arbitrary'}))[0],403)
        self.assertEqual(send('/Jealousfin/Policy/Event','OPTIONS',pre)[0],403)
        self.assertEqual(send('/LiveTv/Channels',headers={'Origin':origin})[0],401)
        code,h,body=send('/Users/AuthenticateByName','POST',{'Origin':origin,'Content-Type':'application/json'},b'{"Username":"fixture","Pw":"fixture"}')
        self.assertEqual(code,200);self.assertEqual(h['Access-Control-Allow-Origin'],origin)
        self.assertEqual(json.loads(body)['AccessToken'],'fixture-token')
        code,h,body=send('/LiveTv/Channels?JealousfinIncludePolicy=true',headers={'Origin':origin,'X-Emby-Token':'fixture-token'})
        self.assertEqual(code,200);self.assertEqual(h['Access-Control-Allow-Origin'],origin)
        self.assertEqual(len(json.loads(body)['Items']),3)
        code,h,_=send('/media/wrong/unknown.bin',headers={'Origin':origin,'Range':'bytes=0-99'})
        self.assertEqual(code,401);self.assertEqual(h['Access-Control-Allow-Origin'],origin)
        code,h,_=send('/LiveTv/Channels',headers={'Origin':'https://evil.example','X-Emby-Token':'fixture-token'})
        self.assertEqual(code,403);self.assertNotIn('Access-Control-Allow-Origin',h)
        code,h,_=send('/System/Info/Public')
        self.assertEqual(code,200);self.assertNotIn('Access-Control-Allow-Origin',h)
        self.assertEqual(send('/Jealousfin/Policy/Event','POST',{'Origin':origin,'X-Emby-Token':'fixture-token'},b'{}')[0],403)
    def test_uk_trial_identity_and_resolution(self):
        g=self.g
        sid='nltv:uk:Fixture.uk'
        manifest='https://ptv3-livetv-addon.pages.dev/fixture/manifest.json'
        config=g.validate_gateway_config({'ukManifest':manifest,'ukTrialProviderIds':[sid],'ukTrialEnabled':True})
        self.assertEqual(config['metadataResearchVariant'],'low')
        g.sources[self.c]=sid
        rows=[{'Id':self.a,'Genres':['Sports'],'Tags':['Sports']},{'Id':self.c,'Genres':['Movies'],'Tags':[]}]
        projected=g.uk_trial_projection(rows,config)
        self.assertEqual(projected[0]['Genres'],[])
        self.assertEqual(projected[0]['Tags'],[])
        self.assertEqual(projected[1]['Tags'],['Sports'])
        self.assertEqual(rows[0]['Genres'],['Sports'])
        g.gateway_config=config
        with patch.object(g,'provider_json',return_value={'streams':[{'url':'https://example.com/a.m3u8'}]}) as provider,patch.object(g,'validate',side_effect=lambda url,**kwargs:url):
            self.assertEqual(g.resolve(self.c),'https://example.com/a.m3u8')
            provider.assert_called_once_with('/stream/tv/nltv:uk:Fixture.uk.json',manifest.rsplit('/',1)[0])
        with patch.object(g,'provider_json',side_effect=TimeoutError) as provider:
            with self.assertRaises(TimeoutError):g.resolve(self.c)
            self.assertEqual(provider.call_count,1)
        g.gateway_config=dict(config,ukTrialEnabled=False)
        with patch.object(g,'provider_json',side_effect=[TimeoutError,{'streams':[{'url':'https://example.com/b.m3u8'}]}]) as provider,patch.object(g,'validate',side_effect=lambda url,**kwargs:url):
            self.assertEqual(g.resolve(self.c),'https://example.com/b.m3u8')
            self.assertEqual(provider.call_count,2)
        with patch.object(g,'catalogue',side_effect=TimeoutError):
            self.assertEqual(g.uk_programmes(config),{})
            self.assertEqual(g.uk_manifest_status['fault'],'TimeoutError')
        with self.assertRaises(ValueError):g.validate_gateway_config({'ukTrialEnabled':True})
        with self.assertRaises(ValueError):g.validate_gateway_config({'metadataResearchVariant':'medium'})
    def test_page_refresh_preserves_independent_freshness_and_backoff(self):
        g=self.g
        a='nltv:us:A.us';b='nltv:us:B.us'
        aid=g.source_profiles.channel_id(g.SOURCE_PROFILE,a);bid=g.source_profiles.channel_id(g.SOURCE_PROFILE,b)
        g.sources={aid:a,bid:b};g.primary_source_ids={a,b}
        good=lambda sid,title:{'metas':[{'id':sid,'name':'Fixture','description':'Now: '+title,'releaseInfo':'Now · '+title}]}
        with patch.object(g,'provider_json',side_effect=[good(a,'First'),urllib.error.HTTPError('https://fixture.invalid',503,'fixture',{},None),good(b,'Second')]):
            self.assertTrue(g.refresh_programme_page('primary',None,0,1000))
            self.assertFalse(g.refresh_programme_page('primary',None,1,1001))
            self.assertEqual(g.paged_faults[('primary',1)],'HTTP 503')
            self.assertEqual(g.paged_due[('primary',1)],1016)
            self.assertEqual(g.programme_snapshot(1002)[aid]['title'],'First')
            self.assertNotIn(bid,g.programme_snapshot(1002))
            self.assertTrue(g.refresh_programme_page('primary',None,1,1100))
            self.assertEqual(g.programme_snapshot(1140)[bid]['title'],'Second')
            self.assertNotIn(aid,g.programme_snapshot(1151))
            self.assertEqual(g.programme_snapshot(1151)[bid]['observedAt'],1100)
        with patch.object(g,'provider_json',return_value={'metas':[{'id':b,'name':'Fixture'}]}):
            self.assertTrue(g.refresh_programme_page('primary',None,1,1152))
            self.assertNotIn(bid,g.programme_snapshot(1153))
        with patch.object(g,'provider_json',side_effect=urllib.error.HTTPError('https://fixture.invalid',429,'fixture',{'Retry-After':'180'},None)):
            self.assertFalse(g.refresh_programme_page('primary',None,0,1200))
            self.assertEqual(g.paged_origin_block_until,1380)
    def test_expanded_trial_catalogue_and_origin(self):
        g=self.g
        sid='nltv:us:Extra.us'
        primary='nltv:us:Original.us'
        manifest='https://ptv3-livetv-addon.pages.dev/fixture/manifest.json'
        cfg=g.validate_gateway_config({'expandedManifest':manifest,'expandedTrialProviderIds':[sid]})
        g.gateway_config=cfg
        row={'name':'Extra','genres':['Movies']}
        g.sources[self.c]=sid
        g.primary_source_ids.add(primary)
        with patch.object(g,'catalogue',return_value={sid:row,primary:{'name':'Original'}}):
            self.assertEqual(g.expanded_catalogue(cfg,True),{sid:row})
        with patch.object(g,'catalogue',side_effect=TimeoutError):
            self.assertEqual(g.expanded_catalogue(cfg,True),{sid:row})
            self.assertEqual(g.expanded_catalogue(cfg),{})
        with patch.object(g,'provider_json',return_value={'streams':[{'url':'https://example.com/new.m3u8'}]}) as provider,patch.object(g,'validate',side_effect=lambda url,**kwargs:url):
            self.assertEqual(g.resolve(self.c),'https://example.com/new.m3u8')
            provider.assert_called_once_with('/stream/tv/nltv:us:Extra.us.json',manifest.rsplit('/',1)[0])
        projected=g.uk_trial_projection([{'Id':self.c,'Genres':[],'Tags':[]}],dict(cfg,ukTrialEnabled=True))
        self.assertEqual(projected[0]['Tags'],['Sports'])
        with self.assertRaises(ValueError):g.validate_gateway_config({'expandedManifest':manifest,'expandedTrialProviderIds':[sid,sid]})
        with self.assertRaises(ValueError):g.validate_gateway_config({'expandedManifest':manifest,'expandedTrialProviderIds':['nltv:my:Other.my']})
    def test_exact_media_endpoint_and_local_deferral(self):
        g=self.g
        g.gateway_config=g.validate_gateway_config({'mediaEndpointAllowlist':{'fixture.example':[8000]}})
        public=[(2,1,6,'',('93.184.216.34',8000))]
        real_dns=g.socket.getaddrinfo
        def fixture_dns(host,*args,**kwargs):
            return real_dns(host,*args,**kwargs) if host=='127.0.0.1' else public
        with patch.object(g.socket,'getaddrinfo',side_effect=fixture_dns):
            self.assertEqual(g.validate('http://fixture.example:8000/live',media=True),'http://fixture.example:8000/live')
            with self.assertRaises(ValueError):g.validate('http://fixture.example:8000/live')
            with self.assertRaises(g.MediaCompatibilityError):g.validate('http://other.example:8000/live',media=True)
            with self.assertRaises(g.MediaCompatibilityError):g.validate('http://fixture.example:9000/live',media=True)
            self.assertEqual(g.validate('https://other.example/live',media=True),'https://other.example/live')
            with self.assertRaises(ValueError):g.validate('http://user:pass@fixture.example:8000/live',media=True)
            with patch.object(g,'provider_json',return_value={'streams':[{'url':'http://other.example:8000/live'}]}):
                self.assertEqual(self.req('/Items/'+self.c+'/PlaybackInfo',{})[0],409)
                self.assertEqual(g.policy_rows()[self.c]['failures'],0)
            with patch.object(g,'provider_json',return_value={'streams':[{'url':'http://other.example:8000/live'},{'url':'rtmp://other.example/live'}]}):
                self.assertEqual(self.req('/Items/'+self.c+'/PlaybackInfo',{})[0],502)
        with patch.object(g.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',8000))]):
            with self.assertRaises(ValueError):g.validate('http://fixture.example:8000/live',media=True)
        with self.assertRaises(ValueError):g.validate_gateway_config({'mediaEndpointAllowlist':{'127.0.0.1':[8000]}})
    def test_policy_connections_closed_on_success_and_failure(self):
        # Hold strong references to every opened connection so GC cannot hide
        # leaked descriptors. Exercise both read and transactional write paths.
        original=sqlite3.connect
        opened=[]
        def tracked(*args, **kwargs):
            db=original(*args, **kwargs)
            opened.append(db)
            return db
        try:
            with patch.object(sqlite3, "connect", side_effect=tracked):
                for n in range(100):
                    snapshot=self.g.policy_rows()
                    self.assertEqual(len(self.g.policy_payload(snapshot)["channels"]),3)
                    self.g.record_attempt(self.c)
                    self.g.policy_event(self.c, "close-test-"+str(n), "success", "")
                with self.assertRaises(ValueError):
                    self.g.policy_event(self.c,"invalid-action","invalid","")
                with closing(original(self.db)) as db, db:
                    db.execute("DELETE FROM meta WHERE key='seeded'")
                with self.assertRaises(RuntimeError):
                    self.g.policy_rows()
                self.assertGreaterEqual(len(opened),402)
                for db in opened:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        db.execute("SELECT 1")
        finally:
            for db in opened:
                db.close()

    def test_isolated_source_profiles_and_policy(self):
        import source_profiles as sp
        import hashlib
        g=self.g
        root=pathlib.Path(self.temp.name).resolve()
        sid='nltv:my:fixture'
        american=sp.channel_id('american',sid)
        malay=sp.channel_id('malay',sid)
        self.assertEqual(american,hashlib.md5(sid.encode()).hexdigest())
        self.assertNotEqual(american,malay)
        self.assertEqual(len(malay),32)
        self.assertEqual(sp.state_root(root,'american'),root)
        self.assertEqual(sp.state_root(root,'malay'),root/'profiles'/'malay')
        self.assertEqual(sp.gateway_port({},'american'),18796)
        self.assertEqual(sp.gateway_port({},'malay'),18797)
        with self.assertRaises(ValueError):sp.gateway_port({'JEALOUSFIN_PORT':'18796'},'malay')
        with self.assertRaises(ValueError):sp.minimum_channels({},'malay')
        with self.assertRaises(ValueError):sp.profile_name({'JEALOUSFIN_SOURCE_PROFILE':'unknown'})
        pilot=sp.state_root(root,'malay');pilot.mkdir(parents=True)
        with patch.object(g,'SOURCE_PROFILE','malay'),patch.object(g,'ROOT',pilot),patch.object(g,'MIN_CHANNELS',1),patch.object(g,'channels',{}),patch.object(g,'sources',{}),patch.object(g,'catalogue',return_value={sid:{'name':'Fixture','genres':['series']}}):
            g.initialize_catalogue()
            self.assertEqual(set(g.channels),{malay})
            self.assertEqual(g.sources[malay],sid)
            with g.policy_connect(pilot/'policy.sqlite3') as db:
                self.assertEqual(g.policy_penalty(db),(3,8))
                self.assertEqual(db.execute('SELECT count(*) FROM channel').fetchone()[0],1)
                now=time.time()
                g.policy_failure(db,malay,now,'fixture')
                self.assertEqual(db.execute('SELECT failures,hiatus_until FROM channel WHERE id=?',(malay,)).fetchone(),(1,now+3*86400))
            self.assertFalse(g.policy_eligible(g.policy_rows()[malay],time.time()))
        with g.policy_connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM channel').fetchone()[0],3)
            self.assertIsNone(db.execute('SELECT id FROM channel WHERE id=?',(malay,)).fetchone())

    def test_spanish_classification_is_separate_membership_filter(self):
        g=self.g
        sc=g.spanish_classification
        before=g.policy_rows()
        state=sc.empty()
        state,changed=sc.apply(state,'add',self.c,'Fixture C',g.sources[self.c],'nonce-add')
        self.assertTrue(changed)
        self.assertEqual(sc.excluded(state),{self.c})
        # A repeated nonce is a no-op; an unknown provider advances the nonce
        # without ever excluding a real catalogue row.
        same,changed=sc.apply(state,'add',self.c,'Fixture C',g.sources[self.c],'nonce-add')
        self.assertFalse(changed)
        self.assertEqual(sc.excluded(same),{self.c})
        state=sc.apply(state,'add','d'*32,'Synthetic',None,'nonce-unknown')[0]
        self.assertEqual(sc.excluded(state),{self.c})
        sc.atomic_write(self.temp.name,state)
        g.maybe_reload_spanish(force=True)
        code,body=self.req('/LiveTv/Channels?JealousfinIncludePolicy=true')
        self.assertEqual(code,200)
        ids={row['Id'] for row in body['Items']}
        self.assertNotIn(self.c,ids)
        self.assertIn(self.a,ids)
        self.assertIn(self.b,ids)
        self.assertNotIn(self.c,body['JealousfinPolicy']['channels'])
        self.assertIn(self.a,body['JealousfinPolicy']['channels'])
        self.assertEqual(self.req('/Items/'+self.c+'/PlaybackInfo',{})[0],409)
        # Classification must not touch the health policy at all.
        self.assertEqual(g.policy_rows()[self.c],before[self.c])
        self.assertIn('spanishClassification',self.req('/trial-status')[1])
        # Explicit removal restores membership without resetting counters.
        removed,changed=sc.apply(g.spanish_state,'remove',self.c,'Fixture C',g.sources[self.c],'nonce-remove')
        self.assertTrue(changed)
        sc.atomic_write(self.temp.name,removed)
        g.maybe_reload_spanish(force=True)
        code,body=self.req('/LiveTv/Channels?JealousfinIncludePolicy=true')
        self.assertIn(self.c,{row['Id'] for row in body['Items']})
        self.assertIn(self.c,body['JealousfinPolicy']['channels'])
        self.assertEqual(g.policy_rows()[self.c],before[self.c])

    def test_spanish_marker_parser_rejects_unbounded_shapes(self):
        sc=self.g.spanish_classification
        good=json.dumps({'schemaVersion':1,'action':'add','channelID':'a'*32,'channelName':'A','nonce':'n-1'})
        self.assertEqual(sc.parse_marker(good)['action'],'add')
        self.assertEqual(sc.parse_marker(json.dumps({'schemaVersion':1,'action':'remove','channelID':'a'*32,'channelName':'A','nonce':'n-2'}))['action'],'remove')
        for raw in (None,'','{}','not json','[]',json.dumps({'schemaVersion':2,'action':'add','channelID':'a'*32,'channelName':'A','nonce':'n'}),
                    json.dumps({'schemaVersion':1,'action':'toggle','channelID':'a'*32,'channelName':'A','nonce':'n'}),
                    json.dumps({'schemaVersion':1,'action':'add','channelID':'z'*32,'channelName':'A','nonce':'n'}),
                    json.dumps({'schemaVersion':1,'action':'add','channelID':'a'*32,'channelName':'A','nonce':''}),
                    json.dumps({'schemaVersion':1,'action':'add','channelID':'a'*32,'channelName':'A','nonce':'n','extra':1})):
            self.assertIsNone(sc.parse_marker(raw))

    def test_prototype_selector_contract(self):
        import prototype_selector as ps
        from unittest.mock import patch
        root=pathlib.Path(self.temp.name).resolve()
        self.assertIsNone(ps.load_selector(root))
        cfg={'schemaVersion':1,'deviceIP':'192.168.31.82','backendPort':18797}
        (root/'prototype-source-selector.json').write_text(json.dumps(cfg))
        self.assertEqual(ps.load_selector(root),cfg)
        for category,expected in [('All Channels','american'),('Movies & Series','malay')]:
            with patch.object(ps,'read_response',return_value=(200,json.dumps({'preferences':{'surfCategory':category}}).encode(),{})):
                self.assertEqual(ps.selected_profile(cfg),expected)
        with patch.object(ps,'read_response',return_value=(503,b'{}',{})):
            with self.assertRaises(RuntimeError):ps.selected_profile(cfg)
        seen={}
        def fake_trigger(host,port,path,method="GET",headers=None,body=None,limit=None,timeout=None):
            seen.update(host=host,port=port,path=path,method=method,headers=headers,body=body)
            return 200,json.dumps({'status':'succeeded','result':'{"schemaVersion":1}'}).encode(),{}
        with patch.object(ps,'read_response',side_effect=fake_trigger):
            self.assertEqual(ps.device_ui_trigger(cfg,'device-token','spanish-read'),'{"schemaVersion":1}')
        self.assertEqual((seen['host'],seen['port'],seen['path'],seen['method']),('192.168.31.82',8765,'/command','POST'))
        command=json.loads(seen['body'])
        self.assertEqual(command['action'],'ui_trigger')
        self.assertEqual(command['arguments'],{'id':'spanish-read'})
        self.assertEqual(seen['headers']['Authorization'],'Bearer device-token')
        with self.assertRaises(ValueError):ps.device_ui_trigger(cfg,'','bad node')
        with patch.object(ps,'read_response',return_value=(200,json.dumps({'status':'failed'}).encode(),{})):
            with self.assertRaises(RuntimeError):ps.device_ui_trigger(cfg,'','spanish-read')
        row={'Id':'a'*32,'Name':'Fixture','Tags':[]}
        payload={'Items':[row],'JealousfinPolicy':{'channels':{'a'*32:{'failures':1}}}}
        result=json.loads(ps.tag_catalogue(json.dumps(payload).encode()))
        self.assertEqual(result['JealousfinPolicy'],payload['JealousfinPolicy'])
        self.assertEqual(result['Items'][0]['Tags'],['Movies & Series'])
        self.assertEqual(ps.channel_id('/Items/'+'a'*32+'/PlaybackInfo'),'a'*32)
        self.assertEqual(ps.channel_id('/Jealousfin/Policy/Event',json.dumps({'channelID':'b'*32}).encode()),'b'*32)
        with self.assertRaises(ValueError):ps.tag_catalogue(json.dumps({'Items':[row],'JealousfinPolicy':{'channels':{}}}).encode())
        (root/'prototype-source-selector.json').unlink()
        self.assertIsNone(ps.load_selector(root))

    def test_prototype_catalogue_denial_fails_closed(self):
        g=self.g
        pilot=pathlib.Path(self.temp.name)/'blocked-pilot';pilot.mkdir()
        with patch.object(g,'SOURCE_PROFILE','malay'),patch.object(g,'ROOT',pilot),patch.object(g,'MIN_CHANNELS',1),patch.object(g,'channels',{}),patch.object(g,'sources',{}),patch.object(g,'catalogue',side_effect=urllib.error.HTTPError('https://fixture.invalid',403,'Forbidden',None,None)):
            with self.assertRaises(urllib.error.HTTPError):g.initialize_catalogue()
            self.assertFalse((pilot/'catalogue-metadata.json').exists())
            self.assertFalse((pilot/'policy.sqlite3').exists())
            self.assertEqual(g.channels,{})

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
        self.assertEqual(g.validate_gateway_config({})['metadataResearchTimeoutSeconds'],180)
        self.assertEqual(g.validate_gateway_config({'metadataResearchTimeoutSeconds':180})['metadataResearchTimeoutSeconds'],180)
        with self.assertRaises(ValueError):g.validate_gateway_config({'metadataResearchTimeoutSeconds':301})
        self.assertIsNone(g.validate_gateway_config({'metadataResearchDailyBudget':12})['metadataResearchDailyBudget'])
        self.assertEqual(self.req('/trial-status')[1]['metadataResearch']['timeoutSeconds'],180)

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
        with patch.object(g,"validate",side_effect=lambda u,**kwargs:u):
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

    def test_catalogue_partial_last_page_and_refresh_retry(self):
        g=self.g
        def page(start,count):
            return {"metas":[{"id":"nltv:"+str(i),"name":"Channel "+str(i)} for i in range(start,start+count)]}
        with patch.object(g,"provider_json",side_effect=[page(0,100),page(100,100),page(200,99)]) as request:
            self.assertEqual(len(g.catalogue()),299)
            self.assertEqual(request.call_count,3)
        with patch.object(g,"provider_json",return_value={"metas":"invalid"}):
            with self.assertRaises(ValueError):g.catalogue()
        def http(code,headers=None):
            return urllib.error.HTTPError("https://fixture.invalid",code,"fixture",headers or {},None)
        self.assertEqual(g.programme_retry_delay(http(503),1,1000),("HTTP 503",15))
        self.assertEqual(g.programme_retry_delay(http(503),2,1000),("HTTP 503",30))
        self.assertEqual(g.programme_retry_delay(http(503),3,1000),("HTTP 503",60))
        self.assertEqual(g.programme_retry_delay(http(429),1,1000),("HTTP 429",120))
        self.assertEqual(g.programme_retry_delay(http(429,{"Retry-After":"180"}),1,1000),("HTTP 429",180))
        self.assertEqual(g.programme_retry_delay(http(403),1,1000),("HTTP 403",300))
        self.assertLess(g.programme_retry_delay(http(429,{"Retry-After":"999999999999"}),1,1000)[1],threading.TIMEOUT_MAX)
        self.assertEqual(g.programme_retry_delay(TimeoutError(),1,1000),("TimeoutError",15))
        self.assertEqual(g.programme_retry_delay(ValueError("incomplete"),1,1000),("ValueError",60))
        self.assertEqual(g.programme_retry_delay(http(503,{"Retry-After":"Thu, 01 Jan 1970 00:18:20 GMT"}),1,1000),("HTTP 503",100))

    def test_regional_axn_crime_guide(self):
        import regional_guide as r
        import datetime as dt
        day=dt.date(2026,11,25)
        first=[(r.epoch(day,"22:00"),"Országúti bosszú","Országúti bosszú",2014),(r.epoch(day+dt.timedelta(days=1),"00:20"),"Next","Next",None)]
        second=[(r.epoch(day,"22:00"),"Országúti bosszú","The Rover",2014),(r.epoch(day+dt.timedelta(days=1),"02:05"),"Later","Later",None)]
        now=r.epoch(day,"23:18")
        row=r.corroborate(first,second,now)
        self.assertEqual(row["title"],"The Rover (2014)")
        self.assertEqual(row["end"],first[1][0])
        self.assertIsNone(r.corroborate(first,second,first[1][0]))
        self.assertIsNone(r.corroborate(first,[(second[0][0],"Different","Different",2014),second[1]],now))
        document={"name":"AXN Crime","broadcastDisplayName":"AXN Crime","hasBroadcastEvent":[{"@type":"BroadcastEvent","name":"Országúti bosszú","startDate":"2026-11-25T22:00:00","description":"(2014.) fixture"}]}
        page="currentDateStr: '2026-11-25', <script type=\"application/ld+json\">"+json.dumps(document)+"</script>"
        for clock,title in (("22:00","Országúti bosszú"),("00:20","Next")):
            page+=f"<div onclick=\"loadDetailsVertical(this, 'viasat_film', 1, '{clock}')\"><div class=\"v-time\">{clock}</div><div class=\"v-title\">{title}</div></div>"
        self.assertEqual(r.mustra(page,day),first)
        with self.assertRaises(ValueError):r.mustra(page,day+dt.timedelta(days=1))
        film="<TITLE>TV műsor (AXN Crime - 2026. november 25., szerda)</TITLE><TR><TD VALIGN='TOP'>21:05</TD><TD VALIGN=TOP><B>Before</B></TD></TR><TR><TD VALIGN='TOP'>22:00</TD><TD VALIGN=TOP><B><a href='/fixture'>Országúti bosszú</a></B><BR>(The Rover, 2014)<BR></TD></TR>"
        self.assertEqual(r.filmkatalogus(film,day)[-1],second[0])
        g=self.g
        g.channels[self.c]["Name"]="AXN Crime";g.sources[self.c]=r.PROVIDER_ID
        g.regional_programme=row
        self.assertEqual(g.programme_snapshot(now)[self.c]["title"],"The Rover (2014)")
        g.sources[self.c]="nltv:other:AXNCrime"
        self.assertNotIn(self.c,g.programme_snapshot(now))
        g.sources[self.c]=r.PROVIDER_ID
        self.assertNotIn(self.c,g.programme_snapshot(now+1801))

    def test_dedicated_series_title_requires_explicit_schedule(self):
        g=self.g
        raw={"name":"Rawhide","description":"Now: Rawhide (until 05:00)\nNext: Rawhide at 05:00","releaseInfo":"Now · Rawhide"}
        item=g.parse_programme(raw)
        self.assertEqual(item,{"title":"Rawhide","nextTitle":"Rawhide","providerEndClock":"05:00","providerNextClock":"05:00"})
        for row in (
            {"name":"Rawhide"},
            {"name":"Rawhide","releaseInfo":"Now · Rawhide"},
            {"name":"Rawhide","description":"Now: Rawhide"},
            dict(raw,releaseInfo="Now · Another programme"),
            dict(raw,description="Now: Rawhide (until 05:00)\nNext: Rawhide at 06:00"),
            dict(raw,description="Now: Rawhide (until 05:00)"),
            dict(raw,releaseInfo=""),
        ):
            self.assertIsNone(g.parse_programme(row),row)
        self.assertEqual(g.parse_programme(dict(raw,name="Another channel"))["title"],"Rawhide")
        self.assertIsNone(g.parse_programme({"name":"Unknown","description":"Now: Unknown (until 05:00)\nNext: Unknown at 05:00","releaseInfo":"Now · Unknown"}))

    def test_external_schedule_corroboration_and_precedence(self):
        g=self.g
        now=1795620275
        g.channels[self.c]["Name"]="OuterSphere"
        g.sources[self.c]=g.GUIDE_PROVIDER_IDS["OuterSphere"]
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
        self.assertEqual(g.parse_guide(b'<!DOCTYPE tv SYSTEM "xmltv.dtd">'+raw,{"fixture"},now)["fixture"][0][2],"Fixture")
        with self.assertRaises(ValueError):g.parse_guide(b'<!DOCTYPE tv><tv/>',{"fixture"},now)
        with self.assertRaises(ValueError):g.parse_guide(b'<!DOCTYPE tv SYSTEM "other.dtd"><tv/>',{"fixture"},now)

    def test_extended_guide_exact_provider_identity(self):
        g=self.g
        now=1795620275
        self.assertEqual(set(g.GUIDE_CHANNELS),set(g.GUIDE_PROVIDER_IDS))
        self.assertEqual(len(g.GUIDE_CHANNELS),30)
        name="Designated Survivor"
        g.channels[self.c]["Name"]=name
        g.sources[self.c]=g.GUIDE_PROVIDER_IDS[name]
        a,b=g.GUIDE_CHANNELS[name]
        g.guide_rows=({a:[(now-60,now+600,name)]},{b:[(now-59,now+599,name)]})
        g.guide_observed_at=now
        self.assertEqual(g.guide_snapshot(now)[self.c]["title"],name)
        g.sources[self.c]="nltv:uk:DesignatedSurvivor.uk"
        self.assertNotIn(self.c,g.guide_snapshot(now))
        g.sources[self.c]=g.GUIDE_PROVIDER_IDS[name]
        g.guide_rows=({a:[(now-60,now+600,name)]},{b:[(now-59,now+599,"Different")]})
        self.assertNotIn(self.c,g.guide_snapshot(now))
        g.guide_rows=({a:[(now-60,now+600,name)]},{b:[(now-59,now+599,name)]})
        self.assertNotIn(self.c,g.guide_snapshot(now+601))

    def test_programme_year_matching_and_display(self):
        g=self.g
        row={"title":"Example"}
        with patch.object(g,"lookup_json",side_effect=[{"metas":[{"name":"Example","id":"tt1234567","releaseInfo":"2022"}]},{"metas":[]}]):
            self.assertEqual(g.lookup_year(row),2022)
        with patch.object(g,"lookup_json",side_effect=[{"metas":[{"name":"Example","id":"tt1234567","releaseInfo":"2022"},{"name":"Example","id":"tt7654321","releaseInfo":"1995"}]},{"metas":[]}]):
            self.assertIsNone(g.lookup_year(row))
        row={"title":"Example","guideSynopsis":"A mysterious family harbors a secret talent for fighting back"}
        search=[{"metas":[{"name":"Example","id":"tt1234567","releaseInfo":"2011"},{"name":"Example","id":"tt7654321","releaseInfo":"2023"}]},{"metas":[]},{"meta":{"description":"The family harbors a secret talent for fighting back."}},{"meta":{"description":"An unrelated story."}}]
        with patch.object(g,"lookup_json",side_effect=search):self.assertEqual(g.lookup_year(row),2011)
        g.year_cache[g.year_key(row)]={"year":2011,"expires":time.time()+60}
        self.assertEqual(g.programme_item(self.c,row)["Name"],"Example (2011)")
        self.assertEqual(g.programme_item(self.c,row)["ProductionYear"],2011)
        self.assertEqual(g.programme_item(self.c,{"title":"Example (2022)"})["Name"],"Example (2022)")
        self.assertEqual(g.programme_item(self.c,{"title":"Unknown title"})["Name"],"Unknown title")
        self.assertIsNone(g.release_year("20261125"))
        self.assertNotIn("guideSynopsis",g.programme_item(self.c,row)["JealousfinProgramme"])

    def test_year_lookup_denied_alternative_and_queue_isolation(self):
        g=self.g
        row={"title":"Example","guideSynopsis":"A mysterious family harbors a secret talent for fighting back"}
        denied=urllib.error.HTTPError("https://fixture.invalid",403,"Forbidden",{},None)
        results=[{"metas":[{"name":"Example","id":"tt1234567","releaseInfo":"2011"},{"name":"Example","id":"tt7654321","releaseInfo":"2023"}]},{"metas":[]},{"meta":{"description":"The family harbors a secret talent for fighting back."}},denied]
        with patch.object(g,"lookup_json",side_effect=results):self.assertEqual(g.lookup_year(row),2011)
        self.assertEqual(g.year_error_code(denied),"HTTP 403")
        stop=threading.Event()
        def wait(seconds):
            if g.year_last_lookup and len(g.year_cache)>=1:return True
            return False
        with patch.object(g,"programme_snapshot",return_value={self.a:{"title":"Broken"},self.c:{"title":"Working"}}),patch.object(g,"lookup_year",side_effect=[ValueError("Metadata response too large"),2022]),patch.object(stop,"wait",side_effect=wait):
            g.year_worker(stop)
        self.assertEqual(g.cached_year({"title":"Working"}),2022)
        self.assertEqual(g.year_errors,1)
        self.assertEqual(g.year_last_error["reason"],"Metadata response too large")
        self.assertIn(g.year_key({"title":"Broken"}),g.year_retry)
        self.assertIsNone(g.year_fault)

    def test_permanent_episode_matching_and_display(self):
        g=self.g;now=time.time()
        sid='nltv:us:BeverlyHills90210.us';name,gid=g.EPISODE_CHANNELS[sid]
        g.sources[self.c]=sid;g.channels[self.c]['Name']=name
        g.programme_rows={self.c:{'title':'Beverly Hills, 90210'}}
        g.programme_observed_at=now
        g.episode_rows={gid:[(now-30,now+300,'Beverly Hills, 90210','','Slipping Away',(9,17))]}
        g.episode_observed_at=now
        row=g.programme_snapshot(now)[self.c]
        self.assertEqual(row['episodeTitle'],'Slipping Away')
        self.assertEqual(g.programme_item(self.c,row)['Name'],'Slipping Away')
        with patch.object(g,'fetch_episode_series',return_value=('Beverly Hills, 90210',[(9,1,'The Morning After',1998),(9,17,'Slipping Away',1999)])):
            value=g.resolve_episode(row)
        self.assertEqual(value,{'seasonNumber':9,'episodeNumber':17,'seasonYear':1998})
        key=g.episode_key(row)
        g.episode_cache[key]={'expires':now+60,'value':value}
        row=g.programme_snapshot(now)[self.c]
        item=g.programme_item(self.c,row)
        self.assertEqual(item['Name'],'S09E17: Slipping Away (1998)')
        self.assertEqual(item['JealousfinProgramme']['seasonYear'],1998)
        self.assertIn('EndDate',item)
        g.programme_observed_at=now+301
        self.assertNotIn('episodeTitle',g.programme_snapshot(now+301).get(self.c,{}))
        g.programme_observed_at=now
        g.programme_rows[self.c]['title']='Other series'
        self.assertNotIn('episodeTitle',g.programme_snapshot(now)[self.c])

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
