"""Read-only per-source operational telemetry. Never changes policy or exports URLs."""
import argparse, collections, json, math, pathlib, re, sqlite3, statistics, time
from contextlib import closing

def distribution(values, positive=False):
    a=sorted(float(x) for x in values if type(x) in (int,float) and math.isfinite(x) and (x>0 if positive else x>=0))
    if not a:return {'n':0}
    return {'n':len(a),'p10':round(a[int((len(a)-1)*.1)],2),'median':round(statistics.median(a),2),'p90':round(a[int((len(a)-1)*.9)],2)}

def error_category(event):
    phase=event.get('phase')
    error=event.get('error','')
    if phase in ('policy','selector'):return 'gateway_infrastructure'
    if isinstance(error,str) and re.fullmatch(r'HTTP [1-5][0-9]{2}',error):return 'upstream_http'
    if error in ('URLError','TimeoutError','ConnectionError','ConnectionResetError','RemoteDisconnected'):return 'upstream_transport'
    return 'resolution_or_delivery_unknown'

def error_summary(root,since):
    counts=collections.Counter()
    # Two bounded rotated files. These are delivery events, not terminal channel failures.
    for filename in ('source-errors.previous.jsonl','source-errors.jsonl'):
        path=root/filename
        if not path.is_file():continue
        with path.open(encoding='utf-8',errors='replace') as f:
            for line in f:
                try:
                    row=json.loads(line)
                    if type(row.get('at')) in (int,float) and row['at']>=since:counts[error_category(row)]+=1
                except (ValueError,TypeError,AttributeError):continue
    return dict(counts)

def report(root,profile,hours=24,now=None):
    root=pathlib.Path(root);now=time.time() if now is None else now
    since=now-hours*3600
    info=json.loads((root/'trial-info.json').read_text())
    if info.get('sourceProfile','american')!=profile:raise ValueError('Profile mismatch')
    ids={x['id'] for x in info['channels']}
    with closing(sqlite3.connect((root/'policy.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        rows=db.execute('SELECT id,name,hiatus_until,banned FROM channel').fetchall()
        if not ids or not ids.issubset({r[0] for r in rows}):raise ValueError('Live catalogue is not fully represented in policy')
        live=[r for r in rows if r[0] in ids]
        quality=db.execute('SELECT buffer_ahead,frame_age,observed_bitrate,indicated_bitrate,dropped,stalls FROM playback_quality p JOIN channel c ON c.id=p.cid WHERE p.at>=? AND p.rate>0',(since,)).fetchall()
        startup=db.execute('SELECT confirmed-opened FROM sessions s JOIN channel c ON c.id=s.cid WHERE opened>=? AND confirmed>=opened AND confirmed>0',(since,)).fetchall()
        outcomes=dict(db.execute('SELECT outcome,count(*) FROM sessions s JOIN channel c ON c.id=s.cid WHERE opened>=? GROUP BY outcome',(since,)))
        # Invalid bitrate sentinels (-1, 0) are excluded; transfer and media bitrate remain distinct.
        fields=('bufferAheadSeconds','frameAgeSeconds','observedTransferBitsPerSecond','indicatedMediaBitsPerSecond','droppedFrames','stalls')
        result={'profile':profile,'windowHours':hours,'catalogue':len(live),'historicalPolicyRecords':len(rows),'eligible':sum(not r[3] and r[2]<=now for r in live),'hiatus':sum(not r[3] and r[2]>now for r in live),'banned':sum(bool(r[3]) for r in live),'historicalAbsent':sorted(r[1] for r in rows if r[0] not in ids),'startupSeconds':distribution([r[0] for r in startup]),'sessionOutcomes':outcomes,'quality':{key:distribution([r[i] for r in quality],positive=i in (2,3)) for i,key in enumerate(fields)},'deliveryEvents':error_summary(root,since)}
        columns={r[1] for r in db.execute('PRAGMA table_info(samples)')}
        if {'cid','source_profile'}<=columns:
            samples=db.execute('SELECT footprint,cpu FROM samples s JOIN channel c ON c.id=s.cid WHERE s.source_profile=? AND s.at>=?',(profile,since)).fetchall()
            result['ownedSystemSamples']={'memoryBytes':distribution([r[0] for r in samples]),'cpuOneCorePercent':distribution([r[1] for r in samples])}
        return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=pathlib.Path)
    parser.add_argument('--profile',required=True,choices=('american','malay'))
    parser.add_argument('--hours',type=int,default=24,choices=range(1,169),metavar='1..168')
    args=parser.parse_args()
    print(json.dumps(report(args.root,args.profile,args.hours),indent=2))
