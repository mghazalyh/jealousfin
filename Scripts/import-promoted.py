#!/usr/bin/env python3
"""Import the explicitly reviewed release baseline from private JealousDB."""
import base64
import hashlib
import json
import os
import pathlib
import urllib.error
import urllib.request

target = pathlib.Path("Promoted/baseline.json")
token = os.environ.get("JEALOUSDB_RELEASE_TOKEN", "").strip()
if not token:
    raise SystemExit("::error::JEALOUSDB_RELEASE_TOKEN is required; refusing to build with a potentially stale baseline.")

url = "https://api.github.com/repos/mghazalyh/jealousdb/contents/jealousfin/engineering/releases/baseline.json?ref=main"
req = urllib.request.Request(url, headers={
    "Authorization": "Bearer " + token,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})
try:
    with urllib.request.urlopen(req, timeout=30) as response:
        item = json.load(response)
except urllib.error.HTTPError as exc:
    raise SystemExit(f"::error::JealousDB release baseline import failed (HTTP {exc.code}). Check the secret's repository scope and Contents read permission.") from None
except urllib.error.URLError:
    raise SystemExit("::error::JealousDB release baseline connection failed.") from None
if item.get("type") != "file" or item.get("encoding") != "base64":
    raise SystemExit("::error::JealousDB release baseline is not an inline file.")
data = base64.b64decode("".join(item["content"].split()), validate=True)
if len(data) > 700 * 1024:
    raise SystemExit("::error::JealousDB release baseline exceeds size limit.")
blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
if blob != item.get("sha"):
    raise SystemExit("::error::JealousDB baseline blob integrity check failed.")
manifest = json.loads(data)
if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
    raise SystemExit("::error::Unknown JealousDB release baseline schema.")
temp = target.with_suffix(".tmp")
temp.write_bytes(data)
temp.replace(target)
print("Imported reviewed JealousDB baseline blob:", blob)
print("JealousDB release baseline import verified.")
