"""Offline generation of a Jellyfin tuner candidate; never changes a live server.
Map actual Jellyfin item IDs to stable M3U tvg-id values before using it.
"""
import argparse
import datetime as dt
import json
import re
from pathlib import Path

def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))

def blocked(ledger, now):
    states = {}
    seen = set()
    for e in sorted(ledger["events"], key=lambda e: (parse_time(e["at"]), e["id"])):
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        s = states.setdefault(e["channelID"], {"failures": 0, "until": None, "banned": False})
        t = parse_time(e["at"])
        eligible = not s["banned"] and (s["until"] is None or s["until"] <= t)
        if e["action"] == "failure" and eligible:
            s["failures"] += 1
            s["banned"] = s["failures"] >= 8
            s["until"] = None if s["banned"] else t + dt.timedelta(days=3*s["failures"])
        elif e["action"] == "success" and eligible:
            s.update(failures=0, until=None)
        elif e["action"] == "approve" and s["banned"]:
            s.update(failures=0, until=None, banned=False)
    return {k for k,s in states.items() if s["banned"] or (s["until"] is not None and s["until"] > now)}

def filter_m3u(source, ledger, mapping, now):
    excluded = blocked(ledger, now)
    missing = excluded - mapping.keys()
    if missing:
        raise ValueError("Missing verified item-ID mapping for excluded channels")
    ids = {mapping[k] for k in excluded}
    if any(not isinstance(x,str) or not x for x in ids):
        raise ValueError("Invalid channel mapping")
    lines = source.splitlines(keepends=True)
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("Not an M3U catalogue")
    result, block, present = [], [], set()
    def finish():
        if not block:
            return
        match = re.search(r'\btvg-id="([^"]*)"', block[0])
        identifier = match.group(1) if match else None
        if identifier:
            present.add(identifier)
        if identifier not in ids:
            result.extend(block)
    for line in lines:
        if line.startswith("#EXTINF:"):
            finish()
            block = [line]
        elif block:
            block.append(line)
        else:
            result.append(line)
    finish()
    if ids - present:
        raise ValueError("Excluded channel mapping does not match catalogue")
    return "".join(result)

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--ledger", type=Path, required=True)
    p.add_argument("--mapping", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args=p.parse_args()
    if args.output.resolve()==args.source.resolve():
        p.error("Output must be a separate candidate file")
    data=filter_m3u(args.source.read_text(),json.loads(args.ledger.read_text()),json.loads(args.mapping.read_text()),dt.datetime.now(dt.timezone.utc))
    tmp=args.output.with_suffix(args.output.suffix+".tmp")
    tmp.write_text(data)
    tmp.replace(args.output)
    print("Filtered candidate written; no production tuner changed.")
if __name__=="__main__":
    main()
