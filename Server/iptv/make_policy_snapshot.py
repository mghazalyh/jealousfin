#!/usr/bin/env python3
"""Read-only offline ABI-1 policy snapshot generator. Does not install patches."""
import argparse
import json
import math
import re
import sqlite3
import time
from pathlib import Path


def build_snapshot(rows, now):
    if not math.isfinite(now):
        raise ValueError("Invalid snapshot clock")
    expiry, attempts = {}, {}
    for cid, attempted, until, banned in rows:
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", cid):
            raise ValueError("Invalid channel ID")
        if cid in attempts:
            raise ValueError("Duplicate channel ID")
        attempted, until = float(attempted or 0), float(until or 0)
        if not all(math.isfinite(v) and v >= 0 for v in (attempted, until)):
            raise ValueError("Invalid timestamp")
        if banned not in (0, 1):
            raise ValueError("Invalid ban flag")
        attempts[cid] = attempted
        if banned or until > now:
            expiry[cid] = 0 if banned else until
    if not attempts or len(expiry) == len(attempts):
        raise ValueError("Snapshot must leave at least one eligible known channel")
    return expiry, attempts


def adapter_source(expiry, attempts):
    def encode(value):
        return json.dumps(value, separators=(",", ":"), allow_nan=False)
    return ("const expiry=" + encode(expiry) + ";\n"
            "const lastAttempt=" + encode(attempts) + ";\n" +
            """const hooks={response:function(input){
if(input.operation!=="channels")return input.response;
const r=input.response;
if(!r||!Array.isArray(r.Items)||r.JealousfinPolicyRevision==="sqlite-policy-v1")return r;
const now=Date.now()/1000;
const items=r.Items.filter(x=>!Object.prototype.hasOwnProperty.call(expiry,x.Id)||(expiry[x.Id]!==0&&expiry[x.Id]<=now));
items.sort((a,b)=>(lastAttempt[a.Id]||0)-(lastAttempt[b.Id]||0)||String(a.Id).localeCompare(String(b.Id)));
return Object.assign({},r,{Items:items,TotalRecordCount:items.length});
}};""")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path, help="New file outside repository")
    args = parser.parse_args()
    db = args.database.resolve(strict=True)
    repo = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if output == db or repo in output.parents:
        parser.error("Output must be outside repository and not the database")
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT id,last_attempt,hiatus_until,banned FROM channel").fetchall()
    expiry, attempts = build_snapshot(rows, time.time())
    source = adapter_source(expiry, attempts)
    if len(source.encode()) > 192 * 1024:
        raise ValueError("Adapter exceeds native source limit")
    with output.open("x", encoding="utf-8") as handle:
        handle.write(source)
    print("Snapshot generated:", len(attempts), "channels;", len(expiry), "excluded")


if __name__ == "__main__":
    main()
