"""Opt-in compatibility selector for the installed ABI-1 UI.

The existing Movies & Series category is temporarily reserved for the Malay
pool on one explicitly configured iPad. All Channels retains the old American
scope. The gateway never accepts a client-supplied upstream host or port.
"""
import hashlib
import http.client
import ipaddress
import json
import re
import threading
import time
import uuid
from pathlib import Path

MALAY_CATEGORY = "Movies & Series"
MAX_JSON = 4 * 1024 * 1024
MAX_MEDIA = 32 * 1024 * 1024
DEVICE_PORT = 8765
ID = re.compile(r"^[a-f0-9]{32}$")
NODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
ACTION_ID = re.compile(r"^[a-z_]{1,80}$")
STATUS_WAIT_SECONDS = 6.0
STATUS_BACKOFF_SECONDS = 2.0
STATUS_READ_TIMEOUT_SECONDS = 3.0
STATUS_CLOCK = time.monotonic
STATUS_LOCK = threading.Lock()
STATUS_FLIGHTS = {}
STATUS_BACKOFF = {}
STATUS_STATS = {"reads": 0, "shared": 0, "timeouts": 0, "failures": 0, "backoffRejected": 0}


class _StatusFlight:
    """One owner plus the exact callers that joined its in-flight read."""

    __slots__ = ("event", "status", "error", "waiters", "credential_scope")

    def __init__(self, credential_scope):
        self.credential_scope = credential_scope
        self.event = threading.Event()
        self.status = None
        self.error = None
        self.waiters = 0


def _status_key(config):
    return (str(config["deviceIP"]), DEVICE_PORT)

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

def read_response(host, port, path, method="GET", headers=None, body=None, limit=MAX_JSON, timeout=STATUS_READ_TIMEOUT_SECONDS):
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

def device_status(config, device_token=""):
    headers = {"Accept": "application/json"}
    if device_token:
        headers["Authorization"] = "Bearer " + device_token
    code, data, _ = read_response(config["deviceIP"], DEVICE_PORT, "/status", headers=headers)
    if code != 200:
        raise RuntimeError("Prototype device status unavailable")
    status = json.loads(data)
    if not isinstance(status, dict):
        raise RuntimeError("Prototype device status invalid")
    return status

def observe_device_status(config, device_token=""):
    """Return one fresh status observation, sharing only a genuinely in-flight read.

    The installed IPA is queried once per device while a read is running and
    callers that arrive during that window join the same flight.  A completed
    observation is never reused, so a later request always observes an explicit
    category transition.  After a failed read, new observations fail closed for
    a short per-device backoff instead of adding pressure to a slow device.
    """
    key = _status_key(config)
    now = STATUS_CLOCK()
    scope = hashlib.sha256(device_token.encode('utf-8')).digest()
    with STATUS_LOCK:
        until = STATUS_BACKOFF.get(key, 0.0)
        if now < until:
            STATUS_STATS["backoffRejected"] += 1
            raise RuntimeError("Prototype device status backoff")
        flight = STATUS_FLIGHTS.get(key)
        owner = flight is None
        if owner:
            flight = _StatusFlight(scope)
            STATUS_FLIGHTS[key] = flight
        elif flight.credential_scope != scope:
            raise RuntimeError('Prototype device status credential scope mismatch')
    if not owner:
        with STATUS_LOCK:
            flight.waiters += 1
        if not flight.event.wait(STATUS_WAIT_SECONDS):
            with STATUS_LOCK:
                STATUS_STATS["timeouts"] += 1
            raise RuntimeError("Prototype device status observation timeout")
        if flight.error is not None:
            raise RuntimeError(str(flight.error))
        with STATUS_LOCK:
            STATUS_STATS["shared"] += 1
        return dict(flight.status)
    try:
        status = device_status(config, device_token)
        if not isinstance(status, dict):
            raise RuntimeError("Prototype device status invalid")
    except BaseException as error:
        with STATUS_LOCK:
            if STATUS_FLIGHTS.get(key) is flight:
                STATUS_FLIGHTS.pop(key, None)
            if isinstance(error, Exception):
                STATUS_BACKOFF[key] = STATUS_CLOCK() + STATUS_BACKOFF_SECONDS
                STATUS_STATS["failures"] += 1
            flight.error = error
            flight.event.set()
        raise
    with STATUS_LOCK:
        if STATUS_FLIGHTS.get(key) is flight:
            STATUS_FLIGHTS.pop(key, None)
        STATUS_BACKOFF.pop(key, None)
        STATUS_STATS["reads"] += 1
        flight.status = status
        flight.event.set()
    return dict(status)


def status_observation_diagnostics():
    """Bounded counters for the selector status observation layer."""
    now = STATUS_CLOCK()
    with STATUS_LOCK:
        active = {"%s:%d" % key: max(0.0, until - now)
                  for key, until in STATUS_BACKOFF.items() if until > now}
        return dict(STATUS_STATS, activeBackoff=active,
                    inFlight=len(STATUS_FLIGHTS))


def status_observation_remaining(config):
    """Seconds until this device may issue another fresh status read.

    The gateway uses this only to bound a foreground recovery wait; it never
    turns an observation into a cached category.
    """
    key = _status_key(config)
    with STATUS_LOCK:
        return max(0.0, STATUS_BACKOFF.get(key, 0.0) - STATUS_CLOCK())


def reset_status_observations():
    """Reset observation state; intended for deterministic tests and operators."""
    with STATUS_LOCK:
        STATUS_FLIGHTS.clear()
        STATUS_BACKOFF.clear()
        for name in STATUS_STATS:
            STATUS_STATS[name] = 0


def selected_profile(config, device_token=""):
    status = observe_device_status(config, device_token)
    preferences = status.get("preferences")
    if not isinstance(preferences, dict):
        raise RuntimeError("Prototype device category unavailable")
    category = preferences.get("surfCategory")
    if not isinstance(category, str) or category not in (
        "All Channels", "Favorites", "Sports", MALAY_CATEGORY):
        raise RuntimeError("Prototype device category unavailable")
    return "malay" if category == MALAY_CATEGORY else "american"

def device_command(config, device_token, action, arguments=None):
    """Invoke one bounded device command over the fixed LAN endpoint.

    The action and arguments are supplied by gateway code only, never by an
    HTTP client. Media URLs and credentials are never logged or returned.
    """
    if not isinstance(action, str) or not ACTION_ID.fullmatch(action):
        raise ValueError("Invalid device action")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValueError("Invalid device arguments")
    command = {
        "schemaVersion": 1,
        "id": action + "-" + uuid.uuid4().hex,
        "action": action,
        "arguments": arguments,
    }
    body = json.dumps(command, separators=(",", ":")).encode()
    if len(body) > 32768:
        raise ValueError("Device command too large")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if device_token:
        headers["Authorization"] = "Bearer " + device_token
    code, data, _ = read_response(
        config["deviceIP"], DEVICE_PORT, "/command",
        method="POST", headers=headers, body=body, limit=MAX_JSON, timeout=8,
    )
    if code != 200:
        raise RuntimeError("Prototype device command unavailable")
    receipt = json.loads(data)
    if not isinstance(receipt, dict) or receipt.get("status") != "succeeded":
        raise RuntimeError("Prototype device command failed")
    result = receipt.get("result")
    if not isinstance(result, str):
        raise RuntimeError("Prototype device command result unavailable")
    return result

def device_ui_trigger(config, device_token, node_id):
    """Invoke one fixed, already-installed read-only UI action on the device.

    The node ID is fixed by the gateway, never supplied by an HTTP client.
    """
    if not isinstance(node_id, str) or not NODE_ID.fullmatch(node_id):
        raise ValueError("Invalid device UI node")
    return device_command(config, device_token, "ui_trigger", {"id": node_id})

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
