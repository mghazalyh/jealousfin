"""Observe Jealousfin events, persist rotation and strict channel health.
Never controls playback, changes credentials, or contacts an upstream media source."""
import os, collections, datetime as dt, json, pathlib, sqlite3, time, urllib.request, uuid, sys
ROOT=pathlib.Path(os.environ.get('JEALOUSFIN_STATE_DIR',str(pathlib.Path(__file__).resolve().parent))).expanduser().resolve()
DB=ROOT/'policy.sqlite3'
BASE=os.environ.get('JEALOUSFIN_DEVICE_URL','http://127.0.0.1:8765').rstrip('/')
AUTH=os.environ.get('JEALOUSFIN_DEVICE_TOKEN','')
DAY=86400
def epoch(v):
    return dt.datetime.fromisoformat(v.replace('Z','+00:00')).timestamp()
def connect(path=DB):
    c=sqlite3.connect(path,timeout=5)
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript("""
    CREATE TABLE IF NOT EXISTS channel(id TEXT PRIMARY KEY,name TEXT,last_attempt REAL DEFAULT 0,failures INTEGER DEFAULT 0,hiatus_until REAL DEFAULT 0,banned INTEGER DEFAULT 0,last_failure REAL DEFAULT 0,reason TEXT DEFAULT '',sustained INTEGER DEFAULT 0,startups INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS sessions(trace TEXT PRIMARY KEY,cid TEXT NOT NULL,opened REAL NOT NULL,confirmed REAL DEFAULT 0,closed REAL DEFAULT 0,failed INTEGER DEFAULT 0,outcome TEXT DEFAULT 'pending',sample_count INTEGER DEFAULT 0,first_position REAL,last_position REAL,stalls INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS observed(id TEXT PRIMARY KEY,at REAL NOT NULL,code TEXT NOT NULL,cid TEXT,trace TEXT,reason TEXT);
    CREATE TABLE IF NOT EXISTS samples(at INTEGER PRIMARY KEY,footprint INTEGER,cpu REAL,thermal TEXT,queued INTEGER,resources INTEGER,channel TEXT);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
    """)
    return c
def http(path):
    req=urllib.request.Request(BASE+path,headers={'Authorization':'Bearer '+AUTH} if AUTH else {})
    with urllib.request.urlopen(req,timeout=5) as f:return json.load(f)
def health():
    req=urllib.request.Request(BASE+'/command',data=json.dumps({'schemaVersion':1,'id':str(uuid.uuid4()),'action':'get_channel_health','arguments':{}}).encode(),headers=dict({'Content-Type':'application/json'},**({'Authorization':'Bearer '+AUTH} if AUTH else {})))
    with urllib.request.urlopen(req,timeout=6) as f:r=json.load(f)
    if r.get('status')!='succeeded':raise RuntimeError('Health query unsuccessful')
    return json.loads(r['result'])
def failure(c,cid,at,reason,trace=None):
    if trace:
        cur=c.execute("UPDATE sessions SET failed=1,outcome='failed' WHERE trace=? AND failed=0",(trace,))
        if not cur.rowcount:return
    row=c.execute('SELECT failures,hiatus_until,banned,last_failure FROM channel WHERE id=?',(cid,)).fetchone()
    if not row:return
    n,until,banned,last=row
    if banned or at<until or at<=last:return
    n+=1
    c.execute('UPDATE channel SET failures=?,hiatus_until=?,banned=?,last_failure=?,reason=? WHERE id=?',(n,0 if n>=8 else at+n*3*DAY,int(n>=8),at,reason[:300],cid))
def seed(c,info,h):
    for ch in info['channels']:
        c.execute('INSERT OR IGNORE INTO channel(id,name) VALUES(?,?)',(ch['id'],ch['name']))
        v=h.get(ch['id'],{})
        last=max([epoch(v[k]) for k in ('lastSuccess','lastFailure') if v.get(k)]+[0])
        c.execute('UPDATE channel SET last_attempt=max(last_attempt,?),startups=max(startups,?) WHERE id=?',(last,v.get('successes',0),ch['id']))
        if v.get('lastFailure') and (v.get('hiatusUntil') or v.get('banned')):
            stamp=epoch(v['lastFailure'])
            c.execute('UPDATE channel SET failures=?,hiatus_until=?,banned=?,last_failure=?,reason=? WHERE id=? AND last_failure<?',(v.get('failures',1),epoch(v['hiatusUntil']) if v.get('hiatusUntil') else 0,int(v.get('banned',False)),stamp,v.get('reason','native failure'),ch['id'],stamp))
    # Optional migration supplied locally; never bake provider IDs or user data into the app.
    seed_file=ROOT/'hiatus-seed.json'
    if seed_file.exists():
        for row in json.loads(seed_file.read_text()):
            failure(c,row['id'],epoch(row['at']),row['reason'])
    c.execute("INSERT OR REPLACE INTO meta VALUES('seeded','1')")
    c.execute("INSERT OR REPLACE INTO meta VALUES('seed_time',?)",(str(time.time()),))
def ingest(c,events,status):
    rotation=int(status.get('rotationMinutes',1))*60
    for e in sorted(events,key=lambda v:v.get('timestamp','')):
        code=e.get('code',''); trace=e.get('trace',''); fields=e.get('fields',{}); cid=fields.get('channelID'); at=epoch(e['timestamp'])
        if code not in ('OPEN','PLAYBACK_CONFIRMED','PLAYBACK_STALLED','FAILURE_UNCONFIRMED','CHANNEL_FAILED','PLAYBACK_SAMPLE','STOP','PAUSE','RESUME','ITEM_FAILED_TO_END','ITEM_STALLED'):continue
        if not trace and code not in ('STOP','PAUSE','RESUME'):continue
        if not cid and trace:
            row=c.execute('SELECT cid FROM sessions WHERE trace=?',(trace,)).fetchone()
            if row:cid=row[0]
        if not cid or not c.execute('SELECT 1 FROM channel WHERE id=?',(cid,)).fetchone():continue
        cur=c.execute('INSERT OR IGNORE INTO observed VALUES(?,?,?,?,?,?)',(e['id'],at,code,cid,trace,str(fields.get('reason',''))[:300]))
        if not cur.rowcount:continue
        if code=='OPEN':
            for old,oldcid,confirmed,count,first,last,stalls in c.execute("SELECT trace,cid,confirmed,sample_count,first_position,last_position,stalls FROM sessions WHERE closed=0 AND trace!=?",(trace,)).fetchall():
                good=confirmed>0 and at-confirmed>=rotation-1 and count>=2 and first is not None and last is not None and last-first>=max(3,rotation*.45) and not stalls
                c.execute("UPDATE sessions SET closed=?,outcome=CASE WHEN failed=1 THEN 'failed' ELSE ? END WHERE trace=?",(at,'sustained' if good else 'inconclusive',old))
                if good and c.execute("SELECT failed FROM sessions WHERE trace=?",(old,)).fetchone()[0]==0:
                    c.execute('UPDATE channel SET sustained=sustained+1,failures=0,hiatus_until=0 WHERE id=? AND banned=0 AND last_failure<?',(oldcid,confirmed))
            c.execute('INSERT OR IGNORE INTO sessions(trace,cid,opened) VALUES(?,?,?)',(trace,cid,at))
            c.execute('UPDATE channel SET last_attempt=max(last_attempt,?) WHERE id=?',(at,cid))
        elif code=='PLAYBACK_CONFIRMED':
            cur=c.execute('UPDATE sessions SET confirmed=? WHERE trace=? AND confirmed=0',(at,trace))
            if cur.rowcount and at>float((c.execute("SELECT value FROM meta WHERE key='seed_time'").fetchone() or ('0',))[0]):c.execute('UPDATE channel SET startups=startups+1 WHERE id=?',(cid,))
        elif code in ('PLAYBACK_STALLED','FAILURE_UNCONFIRMED','CHANNEL_FAILED'):
            # A terminal failure may occur before the observer saw OPEN.
            c.execute('INSERT OR IGNORE INTO sessions(trace,cid,opened) VALUES(?,?,?)',(trace,cid,at))
            failure(c,cid,at,fields.get('reason',code),trace)
            c.execute('UPDATE channel SET last_attempt=max(last_attempt,?) WHERE id=?',(at,cid))
        elif code in ('ITEM_STALLED','ITEM_FAILED_TO_END'):
            c.execute('UPDATE sessions SET stalls=max(stalls,1) WHERE trace=?',(trace,))
        elif code=='PLAYBACK_SAMPLE':
            try:
                pos=float(fields['position'])
                stalls=int(fields.get('stalls','0'))
                if float(fields.get('rate','0'))>0 and fields.get('displayReady')=='true':
                    c.execute('UPDATE sessions SET sample_count=sample_count+1,first_position=coalesce(first_position,?),last_position=?,stalls=max(stalls,?) WHERE trace=?',(pos,pos,stalls,trace))
            except (ValueError,KeyError,TypeError):pass
        elif code in ('STOP','PAUSE'):
            c.execute("UPDATE sessions SET closed=?,outcome=CASE WHEN failed=1 THEN 'failed' ELSE 'interrupted' END WHERE trace=? AND closed=0",(at,trace))
    cid=status.get('currentChannelID')
    if cid and status.get('surfing') and status.get('playing'):c.execute('UPDATE channel SET last_attempt=max(last_attempt,?) WHERE id=?',(time.time(),cid))
def cycle(c):
    status=http('/status')
    events=http('/logs')
    with c:
        if not c.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():
            seed(c,json.loads((ROOT/'trial-info.json').read_text()),health())
        ingest(c,events,status)
        now=int(time.time());m=status.get('systemMetrics',{})
        if not c.execute('SELECT 1 FROM samples WHERE at>=?',(now-60,)).fetchone():
            c.execute('INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?)',(now,int(m.get('processFootprintBytes',0)),float(m.get('processCPUPercentOneCore',0)),m.get('thermalState',''),int(m.get('queuedEvents',0)),None,status.get('currentChannel','')))
        c.execute('DELETE FROM observed WHERE at<?',(now-30*DAY,))
        c.execute('DELETE FROM samples WHERE at<?',(now-30*DAY,))
        c.execute('DELETE FROM sessions WHERE closed>0 AND closed<?',(now-30*DAY,))
        c.execute("INSERT OR REPLACE INTO meta VALUES('last_poll',?)",(str(now),))
def main():
    c=connect()
    while True:
        try:cycle(c)
        except Exception as e:
            print(dt.datetime.now().isoformat(),type(e).__name__,str(e)[:200],flush=True)
        if '--once' in sys.argv:break
        time.sleep(5)
if __name__=='__main__':main()
