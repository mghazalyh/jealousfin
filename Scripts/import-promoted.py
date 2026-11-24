#!/usr/bin/env python3
"""Import only the explicitly promoted baseline, not a device's experimental state."""
import base64
import json
import os
import pathlib
import urllib.request

target=pathlib.Path("Promoted/baseline.json")
token=os.environ.get("TORRIODB_RELEASE_TOKEN","")
if not token:
    print("::warning::TORRIODB_RELEASE_TOKEN not configured; using source-controlled promoted baseline. Automatic private import is disabled.")
else:
    url="https://api.github.com/repos/mghazalyh/torriodb/contents/jealousfin/engineering/releases/baseline.json?ref=main"
    req=urllib.request.Request(url,headers={"Authorization":"Bearer "+token,"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"})
    with urllib.request.urlopen(req,timeout=30) as f:
        item=json.load(f)
    if item.get("type")!="file" or item.get("encoding")!="base64":
        raise RuntimeError("Promoted baseline not available")
    data=base64.b64decode(item["content"],validate=False)
    if len(data)>700*1024:
        raise RuntimeError("Promoted baseline too large")
    target.write_bytes(data)
    print("Imported reviewed TorrioDB baseline blob:",item["sha"])
