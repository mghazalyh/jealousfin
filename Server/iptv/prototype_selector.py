"""Opt-in compatibility selector for the installed ABI-1 UI.

The existing Movies & Series category is temporarily reserved for the Malay
pool on one explicitly configured iPad. All Channels retains the old American
scope. The gateway never accepts a client-supplied upstream host or port.
"""
import http.client
import ipaddress
import json
import re
from pathlib import Path

MALAY_CATEGORY = "Movies & Series"
MAX_JSON = 4 * 1024 * 1024
MAX_MEDIA = 32 * 1024 * 1024
ID = re.compile(r"^[a-f0-9]{32}$")

def load_selector(root):
    p = Path(root) / "prototype-source-selector.json"
    if not p.exists():
        return None
    if p.stat().st_size > 4096:
        raise ValueError("Prototype selector too large")
    data = json.loads(p.read_text())
    if set(data) != {"schemaVersion", "deviceIP", "backendPort"} or data["schemaVersion"] != 1:
        raise ValueError("Invalid prototype selector")
    address = ipaddress.IPv4Address(data["deviceIP"])
    if not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValueError("Expected explicit LAN device address")
    if data["backendPort"] != 18797:
        raise ValueError("Unexpected prototype backend port")
    return data

def read_response(host, port, path, method="GET", headers=None, body=None, limit=MAX_JSON, timeout=3):
    # No proxy, no redirects, no network address derived from HTTP input.
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Prototype response limit")
        return response.status, data, dict(response.getheaders())
    finally:
        conn.close()

def selected_profile(config, device_token=""):
    headers = {"Accept": "application/json"}
    if device_token:
        headers["Authorization"] = "Bearer " + device_token
    code, data, _ = read_response(config["deviceIP"], 8765, "/status", headers=headers)
    if code != 200:
        raise RuntimeError("Prototype device status unavailable")
    status = json.loads(data)
    category = status.get("preferences", {}).get("surfCategory")
    if category not in ("All Channels", "Favorites", "Sports", MALAY_CATEGORY):
        raise RuntimeError("Prototype device category unavailable")
    return "malay" if category == MALAY_CATEGORY else "american"

def source_ids(root):
    p = Path(root) / "profiles" / "malay" / "trial-info.json"
    if not p.exists() or p.stat().st_size > MAX_JSON:
        return set()
    info = json.loads(p.read_text())
    if info.get("sourceProfile") != "malay":
        raise ValueError("Incorrect prototype backend")
    result = {x["id"] for x in info["channels"]}
    if not result or not all(isinstance(x, str) and ID.fullmatch(x) for x in result):
        raise ValueError("Invalid prototype catalogue")
    return result

def channel_id(path, body=None):
    match = re.fullmatch(r"/Items/([a-f0-9]{32})/PlaybackInfo", path) or re.fullmatch(r"/Users/[^/]+/FavoriteItems/([a-f0-9]{32})", path)
    if match:
        return match[1]
    if path == "/Jealousfin/Policy/Event" and body:
        data = json.loads(body)
        value = data.get("channelID")
        if isinstance(value, str) and ID.fullmatch(value):
            return value
    return None

def tag_catalogue(data):
    # The tag is an ABI compatibility selector, not an actual programme genre.
    result = json.loads(data)
    items = result.get("Items")
    policy = result.get("JealousfinPolicy")
    if not isinstance(items, list):
        raise ValueError("Missing Malay catalogue")
    if policy is not None and {x["Id"] for x in items} != set(policy["channels"]):
        raise ValueError("Malay policy/catalogue mismatch")
    for item in items:
        tags = item.get("Tags", [])
        if not isinstance(tags, list):
            raise ValueError("Invalid channel tags")
        item["Tags"] = tags + ["Movies & Series"]
    return json.dumps(result, separators=(",", ":")).encode()
