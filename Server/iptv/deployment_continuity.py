"""Narrow restart-correlated media continuity for the gateway.

The gateway's opaque media handles are process-local.  When the gateway is
restarted during a live physical visit the device keeps requesting the old
handles and sees local 404s until it obtains a new PlaybackInfo.  If it then
reports a startup stall, that is a deployment artefact, not a channel fault.

This module keeps only a private, bounded ledger of SHA-256 handle hashes and
their channel/kind.  It never stores upstream URLs, query strings, tokens or
credentials.  A failure is eligible for suppression only when all of these
are true:

* it is one of the fixed "after startup" reasons emitted by the native client;
* it arrives inside a short bounded window after a gateway generation start;
* the same channel produced at least two stale-handle 404 observations in the
  lookback window before the failure.

Anything else remains a real terminal failure.  Suppression never clears an
existing penalty, never changes a channel's counters and never issues a device
command.
"""
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import threading
import time

SCHEMA_VERSION = 1
RECOVERY_WINDOW_SECONDS = 180
STALE_HIT_LOOKBACK_SECONDS = 90
MIN_STALE_HITS = 2
CLOCK_SKEW_SECONDS = 5
MAX_HANDLE_AGE_SECONDS = 7 * 86400
MAX_HANDLE_RECORDS = 50000
MAX_JOURNAL_BYTES = 2 * 1024 * 1024
FAILURE_REASONS = (
    "video frames stalled after startup",
    "playback stalled after startup",
    "player failed after startup",
)
CHANNEL_RE = re.compile(r"[a-f0-9]{32}\Z")
RID_RE = re.compile(r"[a-f0-9]{32}\Z")
GENERATION_RE = re.compile(r"[a-f0-9]{32}\Z")
STATE_FILE = "deployment-generation.json"
HANDLE_FILE = "media-handles.jsonl"
STALE_FILE = "media-stale.jsonl"
IGNORED_FILE = "deployment-ignored.jsonl"
_LOCK = threading.RLock()


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _now(value=None):
    return float(time.time() if value is None else value)


def _paths(root):
    root = pathlib.Path(root)
    return root / STATE_FILE, root / HANDLE_FILE, root / STALE_FILE, root / IGNORED_FILE


def _atomic_write(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + secrets.token_hex(8))
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            handle = os.fdopen(fd, "w")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _append(path, entry):
    line = json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n"
    if len(line) > 2048:
        raise ValueError("Continuity record too large")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _read_json(path, limit=8192):
    try:
        stat = path.stat()
        if stat.st_size > limit:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None


def read_state(root):
    """Read the current gateway generation without creating or mutating it."""
    state_path = _paths(root)[0]
    data = _read_json(state_path, 4096)
    if not isinstance(data, dict) or set(data) != {"schemaVersion", "generation", "previousGeneration", "startedAt", "pid"}:
        return None
    if data.get("schemaVersion") != SCHEMA_VERSION:
        return None
    if not isinstance(data.get("generation"), str) or not GENERATION_RE.fullmatch(data["generation"]):
        return None
    previous = data.get("previousGeneration")
    if previous is not None and (not isinstance(previous, str) or not GENERATION_RE.fullmatch(previous)):
        return None
    if not _finite(data.get("startedAt")):
        return None
    if type(data.get("pid")) is not int or data["pid"] < 0:
        return None
    return data


def _read_handles(path, now):
    handles = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if len(line) > 1024:
                    continue
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(item, dict) or set(item) != {"schemaVersion", "h", "c", "k", "g", "at"}:
                    continue
                if item.get("schemaVersion") != SCHEMA_VERSION:
                    continue
                if not isinstance(item.get("h"), str) or not re.fullmatch(r"[a-f0-9]{64}", item["h"]):
                    continue
                if not isinstance(item.get("c"), str) or not CHANNEL_RE.fullmatch(item["c"]):
                    continue
                if not isinstance(item.get("k"), str) or not 1 <= len(item["k"]) <= 16:
                    continue
                if not isinstance(item.get("g"), str) or not GENERATION_RE.fullmatch(item["g"]):
                    continue
                if not _finite(item.get("at")):
                    continue
                age = now - float(item["at"])
                if age < -CLOCK_SKEW_SECONDS or age > MAX_HANDLE_AGE_SECONDS:
                    continue
                handles[item["h"]] = item
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    if len(handles) > MAX_HANDLE_RECORDS:
        ordered = sorted(handles.values(), key=lambda item: item["at"], reverse=True)[:MAX_HANDLE_RECORDS]
        handles = {item["h"]: item for item in ordered}
    return handles


def _compact_handles(path, handles):
    ordered = sorted(handles.values(), key=lambda item: item["at"], reverse=True)[:MAX_HANDLE_RECORDS]
    text = "".join(json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n" for item in ordered)
    _atomic_write(path, text)


def _prune_journal(path, now):
    try:
        if path.stat().st_size <= MAX_JOURNAL_BYTES:
            return
    except OSError:
        return
    kept = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if len(line) > 2048:
                    continue
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(item, dict) and _finite(item.get("at")) and 0 <= now - float(item["at"]) <= MAX_HANDLE_AGE_SECONDS:
                    kept.append(line)
    except OSError:
        return
    _atomic_write(path, "".join(kept[-4096:]))


def failure_reason_matches(reason):
    if not isinstance(reason, str) or len(reason) > 512:
        return False
    text = " ".join(reason.casefold().split())
    return any(phrase in text for phrase in FAILURE_REASONS)


def stale_hits(root, cid, started_at, failure_at):
    """Return bounded stale-handle observations for one channel/window."""
    if not isinstance(cid, str) or not CHANNEL_RE.fullmatch(cid):
        return []
    if not _finite(started_at) or not _finite(failure_at):
        return []
    lower = float(started_at) - CLOCK_SKEW_SECONDS
    upper = float(failure_at)
    hits = []
    try:
        with _paths(root)[2].open("r", encoding="utf-8") as handle:
            for line in handle:
                if len(line) > 2048:
                    continue
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("c") != cid or not _finite(item.get("at")):
                    continue
                at = float(item["at"])
                if lower <= at <= upper:
                    hits.append({"at": at, "h": item.get("h"), "k": item.get("k")})
    except (FileNotFoundError, OSError):
        return []
    hits.sort(key=lambda item: item["at"])
    return hits


def failure_ignores(root, cid, reason, at):
    """Return an audit record only for a narrowly correlated deployment stall."""
    state = read_state(root)
    if state is None or not failure_reason_matches(reason) or not _finite(at):
        return None
    started_at = float(state["startedAt"])
    failure_at = float(at)
    if failure_at < started_at - CLOCK_SKEW_SECONDS:
        return None
    if failure_at > started_at + RECOVERY_WINDOW_SECONDS + CLOCK_SKEW_SECONDS:
        return None
    hits = stale_hits(root, cid, started_at, failure_at)
    if len(hits) < MIN_STALE_HITS:
        return None
    if hits[-1]["at"] < failure_at - STALE_HIT_LOOKBACK_SECONDS:
        return None
    return {
        "kind": "deployment_restart_media_handles",
        "generation": state["generation"],
        "startedAt": started_at,
        "failureAt": failure_at,
        "recoveryWindowSeconds": RECOVERY_WINDOW_SECONDS,
        "staleHitCount": len(hits),
        "firstStaleAt": hits[0]["at"],
        "lastStaleAt": hits[-1]["at"],
    }


def record_ignored(root, source, code, trace, reason, audit):
    """Append a correction audit while leaving the original event identity intact."""
    entry = {
        "schemaVersion": SCHEMA_VERSION,
        "at": _now(),
        "source": source,
        "originalCode": str(code)[:80],
        "trace": str(trace)[:100] if trace is not None else "",
        "reason": str(reason)[:300],
        "correlation": audit,
    }
    try:
        _append(_paths(root)[3], entry)
    except OSError:
        pass


class Continuity:
    """Handle ledger owned by the gateway process."""

    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.generation = None
        self.started_at = None
        self._handles = None

    def begin_generation(self, now=None, pid=None):
        with _LOCK:
            if self.generation is not None and self._handles is not None:
                return read_state(self.root)
            moment = _now(now)
            state_path, handle_path, _, _ = _paths(self.root)
            previous = read_state(self.root)
            handles = _read_handles(handle_path, moment)
            generation = secrets.token_hex(16)
            state = {
                "schemaVersion": SCHEMA_VERSION,
                "generation": generation,
                "previousGeneration": previous.get("generation") if previous else None,
                "startedAt": moment,
                "pid": int(os.getpid() if pid is None else pid),
            }
            try:
                _compact_handles(handle_path, handles)
                _atomic_write(state_path, json.dumps(state, separators=(",", ":"), sort_keys=True) + "\n")
            except OSError:
                pass
            self.generation = generation
            self.started_at = moment
            self._handles = handles
            return state

    def _ensure(self):
        if self.generation is None or self._handles is None:
            self.begin_generation()

    def record_handle(self, rid, cid, kind, at=None):
        if not isinstance(rid, str) or not RID_RE.fullmatch(rid):
            return False
        if not isinstance(cid, str) or not CHANNEL_RE.fullmatch(cid):
            return False
        if not isinstance(kind, str) or not 1 <= len(kind) <= 16:
            return False
        with _LOCK:
            self._ensure()
            moment = _now(at)
            digest = hashlib.sha256(rid.encode("ascii")).hexdigest()
            entry = {
                "schemaVersion": SCHEMA_VERSION,
                "h": digest,
                "c": cid,
                "k": kind,
                "g": self.generation,
                "at": moment,
            }
            try:
                _append(_paths(self.root)[1], entry)
            except OSError:
                return False
            self._handles[digest] = entry
            if len(self._handles) > MAX_HANDLE_RECORDS:
                try:
                    _compact_handles(_paths(self.root)[1], self._handles)
                    self._handles = _read_handles(_paths(self.root)[1], moment)
                except OSError:
                    pass
            return True

    def stale_handle(self, rid, at=None):
        if not isinstance(rid, str) or not RID_RE.fullmatch(rid):
            return None
        with _LOCK:
            self._ensure()
            moment = _now(at)
            if moment < self.started_at - CLOCK_SKEW_SECONDS:
                return None
            if moment > self.started_at + RECOVERY_WINDOW_SECONDS + CLOCK_SKEW_SECONDS:
                return None
            record = self._handles.get(hashlib.sha256(rid.encode("ascii")).hexdigest())
            if not record or record.get("g") == self.generation:
                return None
            if moment - float(record["at"]) > MAX_HANDLE_AGE_SECONDS:
                return None
            hit = {
                "schemaVersion": SCHEMA_VERSION,
                "h": record["h"],
                "c": record["c"],
                "k": record["k"],
                "g": self.generation,
                "old": record["g"],
                "at": moment,
            }
            try:
                _append(_paths(self.root)[2], hit)
                _prune_journal(_paths(self.root)[2], moment)
            except OSError:
                return None
            return hit
