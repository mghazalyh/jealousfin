import tempfile, pathlib, datetime, time
import policy_observer as p
def event(i,code,at,trace,cid=None,**kw):
    if cid:kw['channelID']=cid
    return dict(id=str(i),code=code,timestamp=datetime.datetime.fromtimestamp(at,datetime.timezone.utc).isoformat(),trace=trace,fields=kw)
with tempfile.TemporaryDirectory() as d:
    c=p.connect(pathlib.Path(d)/'test.sqlite3')
    c.executemany('INSERT INTO channel(id,name) VALUES(?,?)',[('a','A'),('b','B'),('c','C')])
    t=int(time.time())-1000
    status={'rotationMinutes':1}
    rows=[event(1,'OPEN',t,'a1','a'),event(2,'PLAYBACK_CONFIRMED',t+5,'a1','a'),event(3,'PLAYBACK_STALLED',t+20,'a1'),event(4,'OPEN',t+21,'b1','b')]
    p.ingest(c,rows,status);p.ingest(c,rows,status)
    assert c.execute('SELECT failures,hiatus_until FROM channel WHERE id="a"').fetchone()==(1,t+20+3*p.DAY)
    assert c.execute('SELECT outcome FROM sessions WHERE trace="a1"').fetchone()[0]=='failed'
    assert c.execute('SELECT count(*) FROM observed').fetchone()[0]==4
    # After expiry, a second failure incurs six days, not another three.
    p.failure(c,'a',t+20+3*p.DAY+1,'second')
    assert c.execute('SELECT failures,hiatus_until FROM channel WHERE id="a"').fetchone()==(2,t+21+9*p.DAY)
    p.ingest(c,[event(5,'PLAYBACK_CONFIRMED',t+25,'b1','b'),event(6,'PLAYBACK_SAMPLE',t+40,'b1',position='15',rate='1',displayReady='true',stalls='0'),event(7,'PLAYBACK_SAMPLE',t+70,'b1',position='45',rate='1',displayReady='true',stalls='0'),event(8,'OPEN',t+85,'c1','c')],status)
    assert c.execute('SELECT outcome FROM sessions WHERE trace="b1"').fetchone()[0]=='sustained'
    assert c.execute('SELECT sustained FROM channel WHERE id="b"').fetchone()[0]==1
    p.ingest(c,[event(9,'FAILURE_UNCONFIRMED',t+90,'c1','c',reason='Network player error -1008')],status)
    assert c.execute('SELECT failures FROM channel WHERE id="c"').fetchone()[0]==1
    # Permanent ban after eight eligible failures.
    at=t+21+9*p.DAY+1
    for n in range(3,9):
        p.failure(c,'a',at,'retry failed')
        at+=n*3*p.DAY+1
    assert c.execute('SELECT failures,banned,hiatus_until FROM channel WHERE id="a"').fetchone()==(8,1,0)
print('PASS: deduplication, stall attribution, strict failure, progressive hiatus, sustained playback, permanent ban')


# A replacement trace can start before a prior stall is ingested. A later
# sustained active stream must be credited without closing or reopening it.
with tempfile.TemporaryDirectory() as d:
    c=p.connect(pathlib.Path(d)/'active.sqlite3')
    c.executemany('INSERT INTO channel(id,name) VALUES(?,?)',[(x,x) for x in ('a','b','c','d','e')])
    now=time.time()
    def setup(cid,trace,opened,confirmed,at,first=10,last=80,failed=0,stalls=0,closed=0):
        c.execute('INSERT INTO sessions(trace,cid,opened,confirmed,closed,failed,sample_count,first_position,last_position,stalls) VALUES(?,?,?,?,?,?,?,?,?,?)',(trace,cid,opened,confirmed,closed,failed,4,first,last,stalls))
        p.retain_quality(c,{'id':trace+'-sample'},cid,trace,at,{'position':str(last),'rate':'1','stalls':str(stalls)}, {})
    def status(cid,pos=80):
        return {'currentChannelID':cid,'playing':True,'metrics':{'displayReady':'true','position':str(pos),'rate':'1','stalls':'0'}}
    p.failure(c,'a',now-100,'previous trace stalled')
    setup('a','a2',now-99,now-94,now-5)
    assert p.credit_active_session(c,status('a'),now)
    assert c.execute('SELECT failures,hiatus_until,sustained,last_failure,reason FROM channel WHERE id="a"').fetchone()==(0,0,1,now-100,'previous trace stalled')
    assert c.execute('SELECT outcome,closed FROM sessions WHERE trace="a2"').fetchone()==('sustained',0)
    assert not p.credit_active_session(c,status('a'),now)
    p.failure(c,'a',now+1,'new actual stall','a2')
    assert c.execute('SELECT failures,hiatus_until FROM channel WHERE id="a"').fetchone()==(1,now+1+3*p.DAY)
    p.failure(c,'b',now-100,'newer failure')
    setup('b','b1',now-120,now-110,now-5)
    assert not p.credit_active_session(c,status('b'),now)
    c.execute('UPDATE channel SET banned=1 WHERE id="c"')
    setup('c','c1',now-100,now-94,now-5)
    assert not p.credit_active_session(c,status('c'),now)
    setup('d','d1',now-100,now-94,now-5,stalls=1)
    assert not p.credit_active_session(c,status('d'),now)
    setup('e','e1',now-300,now-290,now-120)
    assert not p.credit_active_session(c,status('e'),now)
    assert not p.credit_active_session(c,{'currentChannelID':'e','playing':True,'metrics':{}},now)
print('PASS: active sustained evidence, idempotence, newer failure, permanent ban, stale evidence and stalled trace guards')
