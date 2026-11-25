#!/usr/bin/env python3
"""Audit promoted behavior before a native-only IPA release. No network or mutation."""
import argparse
import json
from pathlib import Path

KEYS=("runtimePatch","configurationRevision","uiRevision","jellyfinAPIRevision")

def audit(baseline,review,status=None,gateway=None):
    errors=[]
    if baseline.get("schemaVersion")!=1 or baseline.get("revision")!="native-baseline" or baseline.get("sources")!={}:
        errors.append("Release must start from the native-only baseline")
    if review.get("schemaVersion")!=1 or not isinstance(review.get("revisions"),dict):
        return errors+["Invalid promotion review manifest"]
    for key,entries in review["revisions"].items():
        if key not in KEYS or not isinstance(entries,dict):errors.append("Invalid review component: "+str(key));continue
        for revision,entry in entries.items():
            if not revision or not isinstance(entry,dict) or entry.get("disposition") not in ("native","gateway","retired") or not entry.get("evidence"):
                errors.append("Incomplete review: "+key+"/"+str(revision))
    if status is not None:
        for key in KEYS:
            revision=status.get(key)
            if not isinstance(revision,str) or not revision:
                errors.append("Missing live revision: "+key);continue
            if revision=="native-baseline":continue
            entry=review["revisions"].get(key,{}).get(revision)
            if not entry:
                errors.append("Unreviewed active revision: "+key+"/"+revision);continue
            if entry.get("disposition")=="gateway" and not (gateway and gateway.get("policyRevision")=="sqlite-policy-v2" and gateway.get("policyReady") is True and gateway.get("policyChannels",0)>0):
                errors.append("Gateway replacement must be deployed and policy-ready before upgrade: "+revision)
        modules=status.get("eventModuleRevisions",{})
        if not isinstance(modules,dict):errors.append("Invalid event module inventory")
        elif modules:errors.append("Active event modules must be promoted or explicitly retired before installation: "+", ".join(sorted(modules)))
        if status.get("stagedAPIRevision"):errors.append("Deferred API activation must be resolved before installation")
    return errors

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline",type=Path,default=Path("Promoted/baseline.json"))
    p.add_argument("--review",type=Path,default=Path("Promoted/native-review.json"))
    p.add_argument("--status",type=Path,help="Fresh device /status export, kept private")
    p.add_argument("--gateway-status",type=Path,help="Fresh gateway /trial-status export, kept private")
    args=p.parse_args()
    read=lambda path:json.loads(path.read_text()) if path else None
    errors=audit(read(args.baseline),read(args.review),read(args.status),read(args.gateway_status))
    if errors:
        for error in errors:print("BLOCKED:",error)
        raise SystemExit(1)
    print("Native release baseline and promotion review validated")
    if not args.status:print("Live revision comparison not performed; run with fresh status before installation.")

if __name__=="__main__":main()
