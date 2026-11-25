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
    CREATE TABLE IF NOT EXISTS playback_quality(event_id TEXT PRIMARY KEY,at REAL NOT NULL,cid TEXT NOT NULL,trace TEXT NOT NULL,launch_id TEXT,build TEXT,position REAL,rate REAL,buffer_total REAL,buffer_ahead REAL,live_edge REAL,frame_age REAL,dropped INTEGER,stalls INTEGER,observed_bitrate REAL,indicated_bitrate REAL);
    CREATE INDEX IF NOT EXISTS playback_quality_at ON playback_quality(at);
    CREATE INDEX IF NOT EXISTS playback_quality_channel_at ON playback_quality(cid,at);
    CREATE INDEX IF NOT EXISTS playback_quality_trace_at ON playback_quality(trace,at);
    """)
    for column in ("approved_at","last_success"):
        if column not in {row[1] for row in c.execute("PRAGMA table_info(channel)")}:
            try:c.execute("ALTER TABLE channel ADD COLUMN "+column+" REAL DEFAULT 0")
            except sqlite3.OperationalError:
                if column not in {row[1] for row in c.execute("PRAGMA table_info(channel)")}:raise
    for column in ("launch_id","build","policy_revision"):
        if column not in {row[1] for row in c.execute("PRAGMA table_info(samples)")}:
            try:c.execute("ALTER TABLE samples ADD COLUMN "+column+" TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                if column not in {row[1] for row in c.execute("PRAGMA table_info(samples)")}:raise
    c.execute('UPDATE channel SET last_attempt=max(last_attempt,last_failure) WHERE last_attempt<last_failure')
    c.commit()
    return c
def http(path):
    req=urllib.request.Request(BASE+path,headers={'Authorization':'Bearer '+AUTH} if AUTH else {})
    with urllib.request.urlopen(req,timeout=5) as f:return json.load(f)
def health():
    for action in ('get_native_channel_health','get_channel_health'):
        req=urllib.request.Request(BASE+'/command',data=json.dumps({'schemaVersion':1,'id':str(uuid.uuid4()),'action':action,'arguments':{}}).encode(),headers=dict({'Content-Type':'application/json'},**({'Authorization':'Bearer '+AUTH} if AUTH else {})))
        with urllib.request.urlopen(req,timeout=6) as f:r=json.load(f)
        if r.get('status')=='succeeded':return json.loads(r['result'])
    raise RuntimeError('Health query unsuccessful')
def penalty(c):
    values=dict(c.execute("SELECT key,value FROM meta WHERE key IN ('hiatus_base_days','hiatus_ban_after')"))
    days=int(values.get('hiatus_base_days','3'));ban=int(values.get('hiatus_ban_after','8'))
    if not 1<=days<=30 or not 2<=ban<=20:raise ValueError('Invalid progressive hiatus policy')
    return days,ban
def failure(c,cid,at,reason,trace=None):
    if trace:
        cur=c.execute("UPDATE sessions SET failed=1,outcome='failed' WHERE trace=? AND failed=0",(trace,))
        if not cur.rowcount:return
    row=c.execute('SELECT failures,hiatus_until,banned,last_failure,approved_at,last_success FROM channel WHERE id=?',(cid,)).fetchone()
    if not row:return
    n,until,banned,last,approved,success=row
    if banned or at<until or at<=max(last,approved or 0,success or 0):return
    n+=1
    days,ban=penalty(c)
    c.execute('UPDATE channel SET failures=?,hiatus_until=?,banned=?,last_failure=?,reason=? WHERE id=?',(n,0 if n>=ban else at+n*days*DAY,int(n>=ban),at,reason[:300],cid))
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
def finite_number(value):
    import math
    try:
        n=float(value)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError,OverflowError):return None

def retain_quality(c,e,cid,trace,at,fields,status):
    # Store scalar playback evidence, not stream URLs or raw event bodies.
    metrics=status.get('systemMetrics',{})
    keys=('position','rate','bufferSeconds','bufferAheadSeconds','liveEdgeDistanceSeconds','videoSampleAgeSeconds','itemDroppedFramesTotal','itemStallsTotal','observedBitrate','indicatedBitrate')
    values=[finite_number(fields.get(key)) for key in keys]
    if values[6] is None:values[6]=finite_number(fields.get('droppedFrames'))
    if values[7] is None:values[7]=finite_number(fields.get('stalls'))
    # Old log samples can cross a launch boundary. Use explicit event identity
    # when provided; unknown historical launch/build stays unknown.
    c.execute("INSERT OR IGNORE INTO playback_quality VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (e['id'],at,cid,trace,fields.get('launchID'),fields.get('buildNumber'),*values))

def credit_active_session(c,status,now=None):
    """Credit sustained evidence while an already-open stream is still playing.
    Never initiate a retry, forgive a newer failure, or reinstate a banned channel.
    Use recorded samples plus live device state, not the playing flag alone."""
    now=time.time() if now is None else now
    cid=status.get('currentChannelID')
    metrics=status.get('metrics',{})
    if not cid or status.get('playing') is not True or metrics.get('displayReady') not in ('true',True):return False
    rate=finite_number(metrics.get('rate'))
    position=finite_number(metrics.get('position'))
    if rate is None or rate<=0 or position is None or finite_number(metrics.get('stalls'))!=0:return False
    # Do not use an older trace if the most recent attempt is still pending,
    # failed, or has already been closed.
    row=c.execute("""SELECT trace,opened,confirmed,closed,failed,outcome,sample_count,first_position,last_position,stalls
                     FROM sessions WHERE cid=? ORDER BY opened DESC LIMIT 1""",(cid,)).fetchone()
    if not row:return False
    trace,opened,confirmed,closed,failed,outcome,count,first,last,stalls=row
    if closed or failed or outcome!='pending' or not confirmed or confirmed<opened or count<2 or stalls:return False
    policy=c.execute('SELECT banned,last_failure FROM channel WHERE id=?',(cid,)).fetchone()
    if not policy or policy[0] or opened<policy[1] or confirmed<=policy[1]:return False
    sample=c.execute("""SELECT at,position,rate,stalls FROM playback_quality
                        WHERE trace=? AND cid=? ORDER BY at DESC LIMIT 1""",(trace,cid)).fetchone()
    if not sample:return False
    at,sample_position,sample_rate,sample_stalls=sample
    if not 0<=now-at<=90 or sample_rate is None or sample_rate<=0 or sample_stalls!=0:return False
    if any(finite_number(v) is None for v in (first,last,sample_position)):return False
    # A healthy open stream must have progressed for at least one minute.
    # A larger configured dwell does not require delaying recovery indefinitely.
    if at-confirmed<60 or last-first<45 or abs(last-sample_position)>2 or not -2<=position-sample_position<=90:return False
    updated=c.execute("""UPDATE channel SET sustained=sustained+1,failures=0,hiatus_until=0,last_success=max(last_success,?)
                         WHERE id=? AND banned=0 AND last_failure<=? AND last_failure<?""",(at,cid,opened,confirmed))
    if not updated.rowcount:return False
    c.execute("UPDATE sessions SET outcome='sustained' WHERE trace=? AND failed=0 AND outcome='pending'",(trace,))
    return True

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
            for old,oldcid,confirmed,count,first,last,stalls,outcome in c.execute("SELECT trace,cid,confirmed,sample_count,first_position,last_position,stalls,outcome FROM sessions WHERE closed=0 AND trace!=? AND opened<=?",(trace,at)).fetchall():
                good=confirmed>0 and at-confirmed>=rotation-1 and count>=2 and first is not None and last is not None and last-first>=max(3,rotation*.45) and not stalls
                c.execute("UPDATE sessions SET closed=?,outcome=CASE WHEN failed=1 THEN 'failed' ELSE ? END WHERE trace=?",(at,'sustained' if good or outcome=='sustained' else 'inconclusive',old))
                if good and outcome!="sustained" and c.execute("SELECT failed FROM sessions WHERE trace=?",(old,)).fetchone()[0]==0:
                    c.execute('UPDATE channel SET sustained=sustained+1,failures=0,hiatus_until=0,last_success=max(last_success,?) WHERE id=? AND banned=0 AND last_failure<?',(at,oldcid,confirmed))
            c.execute('INSERT OR IGNORE INTO sessions(trace,cid,opened) VALUES(?,?,?)',(trace,cid,at))
            c.execute('UPDATE channel SET last_attempt=max(last_attempt,?) WHERE id=?',(at,cid))
        elif code=='PLAYBACK_CONFIRMED':
            cur=c.execute('UPDATE sessions SET confirmed=? WHERE trace=? AND confirmed=0',(at,trace))
            if cur.rowcount and at>float((c.execute("SELECT value FROM meta WHERE key='seed_time'").fetchone() or ('0',))[0]):c.execute('UPDATE channel SET startups=startups+1 WHERE id=?',(cid,))
        elif code in ('PLAYBACK_STALLED','FAILURE_UNCONFIRMED','CHANNEL_FAILED'):
            if fields.get('terminal') in ('false',False):continue
            # A terminal failure may occur before the observer saw OPEN.
            c.execute('INSERT OR IGNORE INTO sessions(trace,cid,opened) VALUES(?,?,?)',(trace,cid,at))
            failure(c,cid,at,fields.get('reason',code),trace)
            c.execute('UPDATE channel SET last_attempt=max(last_attempt,?) WHERE id=?',(at,cid))
        elif code in ('ITEM_STALLED','ITEM_FAILED_TO_END'):
            c.execute('UPDATE sessions SET stalls=max(stalls,1) WHERE trace=?',(trace,))
        elif code=='PLAYBACK_SAMPLE':
            retain_quality(c,e,cid,trace,at,fields,status)
            try:
                pos=float(fields['position'])
                stalls=int(fields.get('stalls','0'))
                if float(fields.get('rate','0'))>0 and fields.get('displayReady')=='true':
                    c.execute('UPDATE sessions SET sample_count=sample_count+1,first_position=coalesce(first_position,?),last_position=?,stalls=max(stalls,?) WHERE trace=?',(pos,pos,stalls,trace))
            except (ValueError,KeyError,TypeError):pass
        elif code in ('STOP','PAUSE'):
            c.execute("UPDATE sessions SET closed=?,outcome=CASE WHEN failed=1 THEN 'failed' ELSE 'interrupted' END WHERE trace=? AND closed=0",(at,trace))
    credit_active_session(c,status)
    cid=status.get('currentChannelID')
    # Blank startup snapshots cannot overwrite the last actual Surf channel.
    if cid and status.get('surfing') and c.execute('SELECT 1 FROM channel WHERE id=?',(cid,)).fetchone():
        if status.get('playing'):
            c.execute('UPDATE channel SET last_attempt=max(last_attempt,?) WHERE id=?',(time.time(),cid))
        resume={'channelID':cid,'category':status.get('preferences',{}).get('surfCategory','All Channels'),
                'paused':bool(status.get('surfPaused')),'remainingSeconds':max(0,min(86400,int(status.get('remainingSeconds',0)))),
                'at':time.time()}
        c.execute("INSERT OR REPLACE INTO meta VALUES('surf_resume',?)",(json.dumps(resume,separators=(',',':')),))
def cycle(c):
    status=http('/status')
    events=http('/logs')
    with c:
        if not c.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():
            seed(c,json.loads((ROOT/'trial-info.json').read_text()),health())
        ingest(c,events,status)
        now=int(time.time());m=status.get('systemMetrics',{})
        if not c.execute('SELECT 1 FROM samples WHERE at>=?',(now-60,)).fetchone():
            c.execute('INSERT OR REPLACE INTO samples(at,footprint,cpu,thermal,queued,resources,channel,launch_id,build,policy_revision) VALUES(?,?,?,?,?,?,?,?,?,?)',(now,int(m.get('processFootprintBytes',0)),float(m.get('processCPUPercentOneCore',0)),m.get('thermalState',''),int(m.get('queuedEvents',0)),None,status.get('currentChannel',''),status.get('launchID',''),m.get('buildNumber',''),status.get('gatewayPolicyRevision','')))
        c.execute('DELETE FROM observed WHERE at<?',(now-30*DAY,))
        c.execute('DELETE FROM playback_quality WHERE at<?',(now-30*DAY,))
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
