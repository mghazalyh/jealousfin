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

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("database",type=Path)
    p.add_argument("--build",required=True)
    p.add_argument("--launch",required=True)
    p.add_argument("--hours",type=float,default=6)
    p.add_argument("--max-gap",type=int,default=180)
    p.add_argument("--max-growth-mib",type=int,default=128)
    a=p.parse_args()
    if not 0<a.hours<=168 or not 60<=a.max_gap<=3600 or not 16<=a.max_growth_mib<=1024:p.error("Invalid acceptance thresholds")
    with sqlite3.connect(a.database.resolve(strict=True).as_uri()+"?mode=ro",uri=True) as db:
        rows=db.execute("SELECT at,footprint,policy_revision FROM samples WHERE build=? AND launch_id=? ORDER BY at",(a.build,a.launch)).fetchall()
    result=evaluate(rows,a.hours*3600,a.max_gap,a.max_growth_mib*1024*1024)
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result["passed"] else 1)

if __name__=="__main__":main()
