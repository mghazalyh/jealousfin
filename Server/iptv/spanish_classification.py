"""Manual Spanish classification keyed by exact provider identity.

This store is deliberately separate from the SQLite health policy. A manual
Spanish mark must never be represented as a failure, ban or hiatus, and it must
not reset rotation, Favorites or playback history. The gateway filters marked
provider identities out of the English catalogue/policy membership only.
"""
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 1
REVISION = "spanish-classification-v1"
MAX_ENTRIES = 2000
MAX_NONCE = 128
MAX_FILE = 256 * 1024
# Provider identities are already validated by the catalogue/profile layers.
# Keep this permissive enough for every existing nltv:* identity and fixtures.
PROVIDER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
CHANNEL_ID = re.compile(r"^[a-f0-9]{32}$")
MARKER_KEYS = {"schemaVersion", "action", "channelID", "channelName", "nonce"}


def empty():
    return {
        "schemaVersion": SCHEMA_VERSION,
        "revision": REVISION,
        "updatedAt": 0,
        "lastNonce": "",
        "entries": {},
    }


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate(data):
    if not isinstance(data, dict) or set(data) != {
        "schemaVersion", "revision", "updatedAt", "lastNonce", "entries"
    }:
        raise ValueError("Invalid Spanish classification state")
    if data["schemaVersion"] != SCHEMA_VERSION or data["revision"] != REVISION:
        raise ValueError("Unknown Spanish classification schema")
    if not _number(data["updatedAt"]):
        raise ValueError("Invalid Spanish classification timestamp")
    if not isinstance(data["lastNonce"], str) or len(data["lastNonce"]) > MAX_NONCE:
        raise ValueError("Invalid Spanish classification nonce")
    entries = data["entries"]
    if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
        raise ValueError("Invalid Spanish classification entries")
    for provider_id, row in entries.items():
        if not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id):
            raise ValueError("Invalid Spanish provider identity")
        if not isinstance(row, dict) or set(row) != {"channelID", "channelName", "markedAt", "nonce"}:
            raise ValueError("Invalid Spanish classification row")
        if not isinstance(row["channelID"], str) or not CHANNEL_ID.fullmatch(row["channelID"]):
            raise ValueError("Invalid Spanish channel identity")
        if not isinstance(row["channelName"], str) or len(row["channelName"]) > 160:
            raise ValueError("Invalid Spanish channel name")
        if not _number(row["markedAt"]):
            raise ValueError("Invalid Spanish mark timestamp")
        if not isinstance(row["nonce"], str) or not row["nonce"] or len(row["nonce"]) > MAX_NONCE:
            raise ValueError("Invalid Spanish row nonce")
    return data


def parse_marker(raw):
    """Accept only the exact bounded marker shape emitted by the iPad UI."""
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or set(data) != MARKER_KEYS:
        return None
    if data.get("schemaVersion") != 1 or data.get("action") not in ("add", "remove"):
        return None
    channel_id = data.get("channelID")
    channel_name = data.get("channelName")
    nonce = data.get("nonce")
    if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
        return None
    if not isinstance(channel_name, str) or not channel_name or len(channel_name) > 160:
        return None
    if not isinstance(nonce, str) or not nonce or len(nonce) > MAX_NONCE:
        return None
    return data


def load(root):
    path = Path(root) / "spanish-classification.json"
    if not path.exists():
        return empty()
    if path.stat().st_size > MAX_FILE:
        raise ValueError("Spanish classification too large")
    return validate(json.loads(path.read_text(encoding="utf-8")))


def atomic_write(root, data):
    validate(data)
    path = Path(root) / "spanish-classification.json"
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def excluded(data):
    validate(data)
    return set(data["entries"])


def apply(data, action, channel_id, channel_name, provider_id, nonce, now=None):
    """Return (nextState, changed). A repeated nonce is a no-op.

    Unknown provider identities still advance the nonce so a synthetic or stale
    fixture is not retried, but they can never exclude a real catalogue row.
    """
    validate(data)
    if action not in ("add", "remove"):
        raise ValueError("Unknown Spanish classification action")
    if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
        raise ValueError("Invalid Spanish classification channel")
    if not isinstance(channel_name, str) or not channel_name or len(channel_name) > 160:
        raise ValueError("Invalid Spanish classification name")
    if not isinstance(nonce, str) or not nonce or len(nonce) > MAX_NONCE:
        raise ValueError("Invalid Spanish classification nonce")
    if provider_id is not None and (not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id)):
        raise ValueError("Invalid Spanish provider identity")
    if nonce == data["lastNonce"]:
        return data, False
    copy = json.loads(json.dumps(data))
    copy["lastNonce"] = nonce
    copy["updatedAt"] = time.time() if now is None else now
    if action == "add" and provider_id is not None:
        copy["entries"][provider_id] = {
            "channelID": channel_id,
            "channelName": channel_name,
            "markedAt": copy["updatedAt"],
            "nonce": nonce,
        }
        if len(copy["entries"]) > MAX_ENTRIES:
            raise ValueError("Spanish classification capacity reached")
    elif action == "remove" and provider_id is not None:
        copy["entries"].pop(provider_id, None)
    return copy, True
