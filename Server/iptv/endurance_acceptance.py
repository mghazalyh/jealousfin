#!/usr/bin/env python3
"""Read-only endurance acceptance for the installed build and launch."""
import argparse
import json
import sqlite3
from pathlib import Path

def evaluate(rows,min_seconds,max_gap,max_growth):
    if len(rows)<2:return {"passed":False,"reason":"Insufficient samples","samples":len(rows)}
    stamps=[row[0] for row in rows];memory=[row[1] for row in rows]
    span=stamps[-1]-stamps[0];gap=max(b-a for a,b in zip(stamps,stamps[1:]))
    growth=max(memory)-memory[0]
    reasons=[]
    if span<min_seconds:reasons.append("Observation window incomplete")
    if gap>max_gap:reasons.append("Telemetry gap exceeds limit")
    if min(memory)<=0:reasons.append("Missing memory samples")
    if growth>max_growth:reasons.append("Peak memory growth exceeds limit")
    if any(row[2]!="sqlite-policy-v2" for row in rows):reasons.append("Gateway policy contract not continuously verified")
    return {"passed":not reasons,"samples":len(rows),"spanSeconds":span,"maximumGapSeconds":gap,
            "peakGrowthBytes":growth,"reasons":reasons}

def evaluate_rotation(rows,max_gap):
    """Require an observed completed cycle and a following unpaused rollover.
    An observer attached mid-cycle cannot claim a complete cycle retrospectively.
    The ledger's attempted/remaining counts do not prove decoded playback;
    that evidence is reported separately from the session records."""
    reasons=[]
    if len(rows)<2:return {"passed":False,"samples":len(rows),"reasons":["Insufficient rotation evidence"]}
    groups={}
    for row in rows:groups.setdefault(row[1],[]).append(row)
    completed=[];rollovers=[]
    for a,b in zip(rows,rows[1:]):
        if b[1]==a[1]+1 and a[3]==0 and b[2]<=1 and not a[5] and not b[5] and 0<b[0]-a[0]<=max_gap:
            rollovers.append(a[1])
    for cycle,group in groups.items():
        # Allow pool changes caused by strict hiatus; do not use a fixed
        # channel count captured hours earlier as an acceptance denominator.
        if group[0][2]<=1 and group[-1][3]==0 and cycle in rollovers:
            if (all(0<b[0]-a[0]<=max_gap and b[2]>=a[2] for a,b in zip(group,group[1:]))
                    and all(not r[5] and r[2]+r[3]==r[4] for r in group)):
                completed.append(cycle)
    mismatches=sum(r[6]!=r[8] or r[7]!=r[9] for r in rows)
    if not completed:reasons.append("Complete observed eligible cycle and rollover not yet verified")
    if rows[-1][6]!=rows[-1][8] or rows[-1][7]!=rows[-1][9]:reasons.append("Latest server/device policy counts disagree")
    return {"passed":not reasons,"samples":len(rows),"completedCycles":completed,"observedRollovers":rollovers,"policyCountMismatchSamples":mismatches,"latestCycle":rows[-1][1],"latestAttempted":rows[-1][2],"latestRemaining":rows[-1][3],"latestEligible":rows[-1][4],"reasons":reasons}

def evaluate_cycle_playback(rotation, sessions, completed_cycles, max_gap):
    """Corroborate rotation counters against real decoded session evidence.
    Session rows: cid, opened, confirmed, closed, failed, outcome, samples,
    first_position, last_position, stalls. Rotation rows are at, cycle,
    attempted, remaining, eligible, paused, hiatus, banned, server hiatus,
    server banned, cid, playing. Fail closed on missing or ambiguous evidence."""
    results=[]
    for cycle in completed_cycles:
        group=[r for r in rotation if r[1]==cycle]
        if not group:continue
        start,end=group[0][0],group[-1][0]
        by_channel={}
        for r in group:
            by_channel.setdefault(r[10],[]).append(r)
        indices={r[2] for r in group if r[2]>0}
        stable=len({r[4] for r in group})==1
        expected=group[-1][4]
        reasons=[]
        if stable:
            if indices!=set(range(1,expected+1)):reasons.append("Missing rotation attempt indices")
            if len(by_channel)!=expected:reasons.append("Distinct observed channels do not match the stable eligible pool")
            per_index={}
            for r in group:
                if r[2]>0:per_index.setdefault(r[2],set()).add(r[10])
            if any(len(ids)!=1 for ids in per_index.values()):reasons.append("Ambiguous channel identity for rotation attempt")
        else:
            # Counts cannot independently certify membership of a changing pool.
            # Preserve the session evidence, but never silently pass this case.
            reasons.append("Eligible pool changed; exact historical membership evidence required")
        sustained=[];terminal=[];unresolved=[]
        for cid,observations in sorted(by_channel.items()):
            first,last=observations[0][0],observations[-1][0]
            matches=[s for s in sessions if s[0]==cid and first-max_gap<=s[1]<=last+1
                     and (not s[3] or s[3]>=first-1)]
            # Choose the latest session that began before the final observation;
            # an earlier healthy trace must not mask a failed replacement.
            session=max(matches,key=lambda s:s[1],default=None)
            if session is None:unresolved.append(cid);continue
            _,opened,confirmed,closed,failed,outcome,samples,pos1,pos2,stalls=session
            if (not failed and outcome=="sustained" and confirmed>=opened and confirmed>0
                and samples>=2 and pos1 is not None and pos2 is not None
                and pos2-pos1>=3 and not stalls and any(r[11] for r in observations)):
                sustained.append(cid)
            elif failed and outcome=="failed":terminal.append(cid)
            else:unresolved.append(cid)
        if unresolved:reasons.append("Unresolved or missing decoded playback evidence")
        if terminal:reasons.append("Terminal channel failures require policy attribution review")
        results.append({"cycle":cycle,"passed":not reasons,"observedChannels":len(by_channel),
                        "expectedEligible":expected,"stableEligiblePool":stable,
                        "sustainedChannels":len(sustained),"terminalChannels":terminal,
                        "unresolvedChannels":unresolved,"reasons":reasons})
    return {"passed":any(r["passed"] for r in results),"cycles":results,
            "reason":"" if results else "No independently completed cycle available"}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("database",type=Path)
    p.add_argument("--build",required=True)
    p.add_argument("--launch",required=True)
    p.add_argument("--hours",type=float,default=6)
    p.add_argument("--max-gap",type=int,default=180)
    p.add_argument("--max-growth-mib",type=int,default=128)
    p.add_argument("--require-rotation",action="store_true",help="Require complete observed cycle and rollover in addition to system endurance")
    a=p.parse_args()
    if not 0<a.hours<=168 or not 60<=a.max_gap<=3600 or not 16<=a.max_growth_mib<=1024:p.error("Invalid acceptance thresholds")
    with sqlite3.connect(a.database.resolve(strict=True).as_uri()+"?mode=ro",uri=True) as db:
        rows=db.execute("SELECT at,footprint,policy_revision FROM samples WHERE build=? AND launch_id=? ORDER BY at",(a.build,a.launch)).fetchall()
        rotation_rows=[]
        sessions=[]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='rotation_observations'").fetchone():
            rotation_rows=db.execute("SELECT at,cycle,attempted,remaining,eligible,paused,hiatus,banned,server_hiatus,server_banned,cid,playing FROM rotation_observations WHERE build=? AND launch_id=? ORDER BY at",(a.build,a.launch)).fetchall()
            if rotation_rows:
                sessions=db.execute("SELECT cid,opened,confirmed,closed,failed,outcome,sample_count,first_position,last_position,stalls FROM sessions WHERE opened BETWEEN ? AND ? ORDER BY opened",(rotation_rows[0][0]-a.max_gap,rotation_rows[-1][0]+1)).fetchall()
    result=evaluate(rows,a.hours*3600,a.max_gap,a.max_growth_mib*1024*1024)
    result["rotation"]=evaluate_rotation(rotation_rows,a.max_gap)
    result["cyclePlayback"]=evaluate_cycle_playback(rotation_rows,sessions,result["rotation"].get("completedCycles",[]),a.max_gap)
    if a.require_rotation:result["passed"]=result["passed"] and result["rotation"]["passed"] and result["cyclePlayback"]["passed"]
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result["passed"] else 1)

if __name__=="__main__":main()
