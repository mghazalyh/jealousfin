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
MARKER_NAME = "language-visits.seen"
RECOVERY_FILE = "language-visits.recovery.json"
MAX_FILE = 512 * 1024
MAX_RECOVERY = 64
MAX_LAUNCHES = 8
MAX_CHANNELS = 512
MAX_TEXT = 200
PERSIST_SECONDS = 30.0
VERIFIED_PREFIX = "visit-"
RECOVERY_PREFIX = "recover-"
CHANNEL_ID = re.compile(r"^[a-f0-9]{32}$")
VISIT_ID = re.compile(r"^visit-[a-f0-9]{32}$")
RECOVERY_ID = re.compile(r"^recover-[a-f0-9]{32}$")
IDENTITY_ID = re.compile(r"^(?:visit|recover)-[a-f0-9]{32}$")
EVIDENCE_FILES = ("language-exclusions.json", "spanish-classification.json")
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


def new_recovery_visit_id():
    """An identity that is observed but not proven across ledger recovery.

    A missing/corrupt ledger, or a failed durable write, means the physical
    history before this observation is unknown.  The evidence store treats
    these identities as fail-closed for the same launch until the ledger has
    directly observed a departure and return.
    """
    return RECOVERY_PREFIX + uuid.uuid4().hex


def is_verified(visit_id):
    return isinstance(visit_id, str) and bool(VISIT_ID.fullmatch(visit_id))


def is_recovery(visit_id):
    return isinstance(visit_id, str) and bool(RECOVERY_ID.fullmatch(visit_id))


def _is_identity(visit_id):
    return isinstance(visit_id, str) and bool(IDENTITY_ID.fullmatch(visit_id))


def _evidence_present(root):
    return any((Path(root) / name).exists() for name in EVIDENCE_FILES)


def _touch_marker(root):
    """Leave a tiny durable hint that this ledger existed before.

    Without it, deleting only ``language-visits.json`` looks exactly like a
    first run and the new process would issue trustworthy identities for a
    launch it never observed.  The marker contains no evidence or visit.
    """
    path = Path(root) / MARKER_NAME
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)
    except OSError:
        pass


def _load_recovery(root):
    path = Path(root) / RECOVERY_FILE
    try:
        if not path.exists() or path.stat().st_size > MAX_FILE:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schemaVersion") != 1:
            return None
        launches = data.get("launches")
        state = validate(data.get("state"))
        if not isinstance(launches, list) or len(launches) > MAX_RECOVERY:
            return None
    except (OSError, ValueError, TypeError):
        return None
    ids = {value for value in launches
           if isinstance(value, str) and 0 < len(value) <= MAX_TEXT}
    return ids, state


def _save_recovery(root, launches, state):
    path = Path(root) / RECOVERY_FILE
    values = sorted(set(launches))[:MAX_RECOVERY]
    if not values:
        try:
            path.unlink()
        except OSError:
            pass
        return
    snapshot = {"schemaVersion": 1, "launches": values, "state": state}
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if temporary.exists():
            temporary.unlink()


def _clear_recovery(root):
    try:
        (Path(root) / RECOVERY_FILE).unlink()
    except OSError:
        pass


def _evidence_visits(root):
    """Read existing automatic evidence identities without mutating anything.

    Recovery can only be recognised when the ledger's own history is missing
    or incomplete while the durable evidence store still has the visit.  This
    is a read-only reconciliation; the evidence store remains the authority.
    """
    path = Path(root) / "language-exclusions.json"
    try:
        if not path.exists() or path.stat().st_size > 8 * 1024 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    events = data.get("evidence", {}).get("events") if isinstance(data, dict) else None
    if not isinstance(events, dict):
        return {}
    result = {}
    for event in events.values():
        if not isinstance(event, dict) or event.get("source") != "auto":
            continue
        launch_id = event.get("launchID")
        visit_id = event.get("visitID")
        if isinstance(launch_id, str) and launch_id and isinstance(visit_id, str) and visit_id:
            result.setdefault(launch_id, set()).add(visit_id)
    return result


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
        if launch["currentVisitID"] and not _is_identity(launch["currentVisitID"]):
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
            if not _is_identity(row["visitID"]):
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
        self._recovery_launches = set()
        self._recovery_pending = False
        path = self.root / FILE_NAME
        marker = (self.root / MARKER_NAME).exists()
        try:
            present = path.exists()
            self.state = load(self.root)
            if not present and (marker or _evidence_present(self.root)):
                # The visits ledger was lost but this state directory (or its
                # durable evidence) shows it existed before.  Do not trust
                # fresh identities for the same launch until a departure is
                # directly observed.
                self._recovery_pending = True
        except (OSError, ValueError, TypeError) as error:
            self.state = empty()
            self.fault = type(error).__name__
            self._recovery_pending = bool(marker or _evidence_present(self.root))
        snapshot = _load_recovery(self.root)
        if snapshot is not None:
            recovered_launches, recovered_state = snapshot
            for launch_id in recovered_launches:
                row = recovered_state["launches"].get(launch_id)
                if row is not None:
                    # The snapshot was written after a full write failed, so it
                    # is newer than the durable main ledger for these launches.
                    self.state["launches"][launch_id] = row
            self._recovery_launches.update(recovered_launches)
        _touch_marker(self.root)
        # A failed durable write can leave the ledger behind the evidence
        # store even though the file itself still exists.
        try:
            for launch_id, visits in _evidence_visits(self.root).items():
                launch = self.state["launches"].get(launch_id)
                known = set()
                if launch is not None:
                    known.add(launch.get("currentVisitID", ""))
                    for row in launch.get("channels", {}).values():
                        known.add(row.get("visitID", ""))
                if launch is None or any(visit not in known for visit in visits):
                    self._recovery_launches.add(launch_id)
        except (OSError, ValueError, TypeError):
            pass

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
            transitioned = bool(previous and previous != channel_id)
            departed = False
            if transitioned:
                old = launch["channels"].get(previous)
                if old is not None and old["state"] == "active":
                    old["state"] = "departed"
                    old["departedAt"] = now
                    departed = True
            recovering = (launch_id in self._recovery_launches
                          or is_recovery(launch.get("currentVisitID", "")))
            if created and self._recovery_pending:
                recovering = True
                self._recovery_pending = False
                self._recovery_launches.add(launch_id)
            if recovering and transitioned:
                # A direct observation of a different channel is proof of a
                # departure even when the earlier ledger history is unknown.
                # The chain is coherent again from this visit onward.
                recovering = False
                self._recovery_launches.discard(launch_id)
            record = launch["channels"].get(channel_id)
            new_visit = False
            if previous == channel_id and record is not None and record["state"] == "active":
                visit_id = record["visitID"]
                reason = "same_visit"
            else:
                proven_return = record is not None and record["state"] == "departed"
                if proven_return:
                    # A departure was directly observed on an earlier call.
                    recovering = False
                    self._recovery_launches.discard(launch_id)
                visit_id = new_recovery_visit_id() if recovering else new_visit_id()
                new_visit = True
                if proven_return:
                    reason = "returned_after_departure"
                    record.update(visitID=visit_id, state="active", lastSeenAt=now,
                                  streamGeneration=stream_generation, departedAt=None)
                else:
                    reason = "initial_observation" if not previous else "channel_change"
                    record = {
                        "visitID": visit_id, "state": "active", "lastSeenAt": now,
                        "streamGeneration": stream_generation, "departedAt": None,
                    }
                    launch["channels"][channel_id] = record
            if recovering:
                self._recovery_launches.add(launch_id)
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
                    if changed:
                        # The main ledger now contains the full state.
                        _clear_recovery(self.root)
                except (OSError, ValueError, TypeError) as error:
                    self.fault = type(error).__name__
                    if changed:
                        # The new identity was not made durable.  Never let a
                        # later process trust an unrecorded visit: downgrade
                        # this visit and stay in recovery for the launch.
                        self._recovery_launches.add(launch_id)
                        if is_verified(visit_id):
                            recovery_id = new_recovery_visit_id()
                            record["visitID"] = recovery_id
                            launch["currentVisitID"] = recovery_id
                            visit_id = recovery_id
                            self.state = state
                        _save_recovery(self.root, self._recovery_launches, state)
            return {
                "visitID": visit_id, "verified": True, "newVisit": new_visit,
                "reason": reason, "channelID": channel_id, "launchID": launch_id,
                "streamGeneration": stream_generation,
            }

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self.state))
