"""Durable physical-visit identity for automatic language evidence.

A physical visit is one continuous stay on a channel within one app launch.
The identity is assigned only from read-only device observations:

* the launch is the app process (``launchID`` from ``get_status``);
* the channel is the verified current ``channelID`` from
  ``get_player_snapshot``;
* a visit begins on the first observation of a channel in a launch, or when
  the device is observed on that channel again after being observed on a
  different channel (a verified departure and return).

A stream-generation change alone is deliberately not a new visit: a reload,
retry or re-seek changes the generation while the viewer never left.  Time is
never used as identity.  If the launch or channel cannot be verified, the
caller receives ``verified=False`` and the evidence store fails closed by
deduplicating inside a bounded window instead of inventing a new visit.

The ledger is bounded and written atomically (mode 0600).  It never issues a
device command, changes playback, rotation, health, Favorites or policy.
"""
import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 1
REVISION = "language-visits-v1"
FILE_NAME = "language-visits.json"
MAX_FILE = 512 * 1024
MAX_LAUNCHES = 8
MAX_CHANNELS = 512
MAX_TEXT = 200
PERSIST_SECONDS = 30.0
VERIFIED_PREFIX = "visit-"
CHANNEL_ID = re.compile(r"^[a-f0-9]{32}$")
VISIT_ID = re.compile(r"^visit-[a-f0-9]{32}$")
LAUNCH_KEYS = {
    "currentChannelID", "currentVisitID", "currentStreamGeneration",
    "currentSince", "lastObservedAt", "channels",
}
CHANNEL_KEYS = {"visitID", "state", "lastSeenAt", "streamGeneration", "departedAt"}
STATES = ("active", "departed")


def empty():
    return {
        "schemaVersion": SCHEMA_VERSION,
        "revision": REVISION,
        "updatedAt": 0,
        "launches": {},
    }


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_TEXT


def _empty_launch():
    return {
        "currentChannelID": "",
        "currentVisitID": "",
        "currentStreamGeneration": "",
        "currentSince": 0,
        "lastObservedAt": 0,
        "channels": {},
    }


def new_visit_id():
    return VERIFIED_PREFIX + uuid.uuid4().hex


def is_verified(visit_id):
    return isinstance(visit_id, str) and bool(VISIT_ID.fullmatch(visit_id))


def validate(data):
    if not isinstance(data, dict) or set(data) != {"schemaVersion", "revision", "updatedAt", "launches"}:
        raise ValueError("Invalid language visit state")
    if data["schemaVersion"] != SCHEMA_VERSION or data["revision"] != REVISION:
        raise ValueError("Unknown language visit schema")
    if not _number(data["updatedAt"]):
        raise ValueError("Invalid language visit timestamp")
    launches = data["launches"]
    if not isinstance(launches, dict) or len(launches) > MAX_LAUNCHES:
        raise ValueError("Invalid language visit launches")
    for launch_id, launch in launches.items():
        if not _text(launch_id):
            raise ValueError("Invalid language visit launch")
        if not isinstance(launch, dict) or set(launch) != LAUNCH_KEYS:
            raise ValueError("Invalid language visit launch record")
        for key in ("currentChannelID", "currentVisitID", "currentStreamGeneration"):
            value = launch[key]
            if not isinstance(value, str) or len(value) > MAX_TEXT:
                raise ValueError("Invalid language visit identity")
        if launch["currentChannelID"] and not CHANNEL_ID.fullmatch(launch["currentChannelID"]):
            raise ValueError("Invalid language visit current channel")
        if launch["currentVisitID"] and not is_verified(launch["currentVisitID"]):
            raise ValueError("Invalid language visit current identity")
        if not _number(launch["currentSince"]) or not _number(launch["lastObservedAt"]):
            raise ValueError("Invalid language visit time")
        channels = launch["channels"]
        if not isinstance(channels, dict) or len(channels) > MAX_CHANNELS:
            raise ValueError("Invalid language visit channels")
        for channel_id, row in channels.items():
            if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
                raise ValueError("Invalid language visit channel")
            if not isinstance(row, dict) or set(row) != CHANNEL_KEYS:
                raise ValueError("Invalid language visit channel record")
            if not is_verified(row["visitID"]):
                raise ValueError("Invalid language visit channel identity")
            if row["state"] not in STATES:
                raise ValueError("Invalid language visit channel state")
            if not _number(row["lastSeenAt"]):
                raise ValueError("Invalid language visit channel time")
            if not isinstance(row["streamGeneration"], str) or len(row["streamGeneration"]) > MAX_TEXT:
                raise ValueError("Invalid language visit stream generation")
            if row["departedAt"] is not None and not _number(row["departedAt"]):
                raise ValueError("Invalid language visit departure time")
    return data


def load(root):
    path = Path(root) / FILE_NAME
    if not path.exists():
        return empty()
    if path.stat().st_size > MAX_FILE:
        raise ValueError("Language visit store too large")
    return validate(json.loads(path.read_text(encoding="utf-8")))


def atomic_write(root, data):
    validate(data)
    path = Path(root) / FILE_NAME
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


def _prune_launches(state, now):
    launches = state["launches"]
    if len(launches) <= MAX_LAUNCHES:
        return
    ordered = sorted(launches, key=lambda key: (launches[key]["lastObservedAt"], key))
    for launch_id in ordered[:max(0, len(launches) - MAX_LAUNCHES)]:
        launches.pop(launch_id, None)


def _prune_channels(launch):
    channels = launch["channels"]
    if len(channels) <= MAX_CHANNELS:
        return
    current = launch["currentChannelID"]
    ordered = sorted(
        (cid for cid in channels if cid != current),
        key=lambda cid: (
            channels[cid]["state"] != "departed",
            channels[cid]["lastSeenAt"],
            cid,
        ),
    )
    for channel_id in ordered[:max(0, len(channels) - MAX_CHANNELS)]:
        channels.pop(channel_id, None)


class Ledger:
    """Bounded, thread-safe physical-visit ledger."""

    def __init__(self, root, clock=time.time):
        self.root = Path(root)
        self.clock = clock
        self._lock = threading.RLock()
        self._last_write = 0.0
        self.fault = None
        try:
            self.state = load(self.root)
        except (OSError, ValueError, TypeError) as error:
            self.state = empty()
            self.fault = type(error).__name__

    def _unverified(self, reason, channel_id=None, launch_id=None):
        return {
            "visitID": "", "verified": False, "newVisit": False, "reason": reason,
            "channelID": channel_id or "", "launchID": launch_id or "",
            "streamGeneration": "",
        }

    def observe(self, channel_id, launch_id, stream_generation=None, now=None):
        """Observe the current device channel and return its physical visit.

        ``channel_id`` is the verified device channel from the player snapshot,
        never the channel whose media produced a sample.  Passing the actual
        device channel lets a channel change be recognised as a departure even
        when it arrives while another sample is finalizing.
        """
        now = self.clock() if now is None else now
        if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
            return self._unverified("channel_unavailable", channel_id, launch_id)
        if not _text(launch_id):
            return self._unverified("launch_unavailable", channel_id, launch_id)
        if not isinstance(stream_generation, str) or len(stream_generation) > MAX_TEXT:
            stream_generation = ""
        with self._lock:
            state = json.loads(json.dumps(self.state))
            launch = state["launches"].get(launch_id)
            created = launch is None
            if launch is None:
                launch = _empty_launch()
                state["launches"][launch_id] = launch
            previous = launch["currentChannelID"]
            departed = False
            if previous and previous != channel_id:
                old = launch["channels"].get(previous)
                if old is not None and old["state"] == "active":
                    old["state"] = "departed"
                    old["departedAt"] = now
                    departed = True
            record = launch["channels"].get(channel_id)
            new_visit = False
            if previous == channel_id and record is not None and record["state"] == "active":
                visit_id = record["visitID"]
                reason = "same_visit"
            elif record is not None and record["state"] == "departed":
                visit_id = new_visit_id()
                new_visit = True
                reason = "returned_after_departure"
                record.update(visitID=visit_id, state="active", lastSeenAt=now,
                              streamGeneration=stream_generation, departedAt=None)
            else:
                visit_id = new_visit_id()
                new_visit = True
                reason = "initial_observation" if not previous else "channel_change"
                record = {
                    "visitID": visit_id, "state": "active", "lastSeenAt": now,
                    "streamGeneration": stream_generation, "departedAt": None,
                }
                launch["channels"][channel_id] = record
            launch["currentChannelID"] = channel_id
            launch["currentVisitID"] = visit_id
            launch["currentStreamGeneration"] = stream_generation
            if new_visit or departed or created:
                launch["currentSince"] = now
            launch["lastObservedAt"] = now
            record["lastSeenAt"] = now
            record["streamGeneration"] = stream_generation
            state["updatedAt"] = now
            _prune_channels(launch)
            _prune_launches(state, now)
            changed = bool(new_visit or departed or created)
            self.state = state
            if changed or now - self._last_write >= PERSIST_SECONDS:
                try:
                    atomic_write(self.root, state)
                    self._last_write = now
                    self.fault = None
                except (OSError, ValueError, TypeError) as error:
                    self.fault = type(error).__name__
            return {
                "visitID": visit_id, "verified": True, "newVisit": new_visit,
                "reason": reason, "channelID": channel_id, "launchID": launch_id,
                "streamGeneration": stream_generation,
            }

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self.state))
