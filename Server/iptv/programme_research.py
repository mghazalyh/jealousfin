"""Active physical-viewing research scheduling.

This module is deliberately separate from the short resource-active protection
in gateway.py. Only a correlated, progressing playback session can promote a
channel to active research; a resource fetch, player pause, operator stop or a
stale report cannot. The coordinator is pure and injected with a clock so the
retry/escalation contract is testable without a device.
"""
from __future__ import annotations

import dataclasses
import hashlib
import math
import random
import re
import time
from collections import OrderedDict

REVISION = "programme-research-20261127"
DEFAULT_ACTIVE_SECONDS = 120
DEFAULT_STALE_SECONDS = 45
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_SECONDS = (45, 120, 300)
DEFAULT_DEDUP_SECONDS = 300
MAX_SESSIONS = 256
SESSION_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
CHANNEL_RE = re.compile(r"[a-f0-9]{32}")
TICKS = 10_000_000.0


def _session_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12] if isinstance(value, str) else ""


def parse_playback_report(body, action):
    """Validate the existing Jellyfin playback reports. Never retain raw bodies."""
    if not isinstance(body, dict):
        return None
    if action not in ("start", "progress", "stop"):
        return None
    cid = body.get("ItemId") or body.get("MediaSourceId")
    session = body.get("PlaySessionId")
    if not isinstance(cid, str) or not CHANNEL_RE.fullmatch(cid):
        return None
    if not isinstance(session, str) or not SESSION_RE.fullmatch(session):
        return None
    report = {"channelID": cid, "playSessionID": session, "action": action}
    ticks = body.get("PositionTicks")
    if ticks is not None:
        if type(ticks) is not int or not 0 <= ticks <= 10**15:
            return None
        report["position"] = ticks / TICKS
    paused = body.get("IsPaused")
    if paused is not None:
        if type(paused) is not bool:
            return None
        report["paused"] = paused
    return report


@dataclasses.dataclass(frozen=True)
class ActiveView:
    channel_id: str
    session_id: str
    session_hash: str
    window_key: str
    active_seconds: float
    started_at: float
    last_progress: float
    paused: bool
    verified: bool
    trace_present: bool
    position: float | None


class ActiveViewing:
    """Correlate server-issued playback sessions with real media progress."""

    def __init__(self, active_seconds=DEFAULT_ACTIVE_SECONDS, stale_seconds=DEFAULT_STALE_SECONDS,
                 max_sessions=MAX_SESSIONS, clock=None):
        self.active_seconds = max(1, int(active_seconds))
        self.stale_seconds = max(5, int(stale_seconds))
        self.max_sessions = max(8, int(max_sessions))
        self.clock = clock or time.monotonic
        self.issued = OrderedDict()
        self.touched = {}
        self.sessions = OrderedDict()

    def issue(self, channel_id, session_id, wall=None):
        if not isinstance(channel_id, str) or not CHANNEL_RE.fullmatch(channel_id):
            return False
        if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
            return False
        now = self.clock()
        self.issued[session_id] = {"channelID": channel_id, "at": now}
        self.issued.move_to_end(session_id)
        while len(self.issued) > self.max_sessions:
            self.issued.popitem(last=False)
        self._expire(now)
        return True

    def touch(self, channel_id):
        """Record a real media request for a channel (not a resource-only hint)."""
        if isinstance(channel_id, str) and CHANNEL_RE.fullmatch(channel_id):
            self.touched[channel_id] = self.clock()
            while len(self.touched) > self.max_sessions:
                self.touched.popitem(last=False)

    def _known_session(self, session_id, channel_id, now):
        issued = self.issued.get(session_id)
        if issued:
            return issued["channelID"] == channel_id
        touched = self.touched.get(channel_id)
        return touched is not None and now - touched <= max(120, self.stale_seconds * 2)

    def _expire(self, now):
        for session_id, row in list(self.sessions.items()):
            stale = now - row["last_mono"] > max(3600, self.stale_seconds * 8)
            if row["stopped"] or stale:
                self.sessions.pop(session_id, None)
        for channel_id, at in list(self.touched.items()):
            if now - at > 3600:
                self.touched.pop(channel_id, None)
        while len(self.sessions) > self.max_sessions:
            self.sessions.popitem(last=False)

    def observe(self, report, wall=None, now=None):
        if not isinstance(report, dict):
            return None
        cid = report.get("channelID"); session_id = report.get("playSessionID"); action = report.get("action")
        if not isinstance(cid, str) or not CHANNEL_RE.fullmatch(cid):
            return None
        if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
            return None
        now = self.clock() if now is None else float(now)
        wall = time.time() if wall is None else float(wall)
        row = self.sessions.get(session_id)
        if action == "stop":
            if row and row["cid"] == cid:
                row.update(stopped=True, paused=False, active_since=None, last_mono=now, last_wall=wall)
            return self._view(row, now) if row else None
        if row and row["cid"] != cid:
            # A reused session identity for another channel is not trusted.
            row.update(stopped=True, active_since=None)
            row = None
        if row is None:
            if not self._known_session(session_id, cid, now):
                return None
            row = {"cid": cid, "session": session_id, "opened": wall, "last_wall": wall,
                   "last_mono": now, "last_position": None, "active_since": None,
                   "paused": False, "stopped": False, "verified": True, "trace": ""}
            self.sessions[session_id] = row
        self.sessions.move_to_end(session_id)
        previous_mono = row["last_mono"]
        row["last_mono"] = now; row["last_wall"] = wall
        if action == "start":
            row["verified"] = True
            if report.get("paused") is True:
                row["paused"] = True; row["active_since"] = None
            elif row["active_since"] is None:
                # Start alone is not progress. A later advancing report opens
                # the evidence window.
                row["active_since"] = None
            self._expire(now)
            return self._view(row, now)
        paused = report.get("paused") is True
        if paused:
            row["paused"] = True; row["active_since"] = None
        else:
            row["paused"] = False
        position = report.get("position")
        if isinstance(position, (int, float)) and math.isfinite(position) and position >= 0:
            previous = row["last_position"]
            advancing = previous is not None and position > previous + 0.05
            row["last_position"] = position
            if paused:
                pass
            elif now - previous_mono > self.stale_seconds:
                row["active_since"] = now
            elif advancing:
                if row["active_since"] is None:
                    row["active_since"] = now
                row["last_progress"] = now
            elif previous is None:
                row["active_since"] = now
        self._expire(now)
        return self._view(row, now)

    def correlate_trace(self, channel_id, trace, now=None):
        if not isinstance(trace, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", trace):
            return False
        now = self.clock() if now is None else float(now)
        for row in reversed(self.sessions.values()):
            if row["cid"] == channel_id and not row["stopped"] and now - row["last_mono"] <= self.stale_seconds:
                row["trace"] = trace
                return True
        return False

    def _view(self, row, now):
        if not row or row["stopped"] or row["paused"]:
            return None
        if now - row["last_mono"] > self.stale_seconds:
            row["active_since"] = None
            return None
        if not row["verified"] or row["active_since"] is None:
            return None
        seconds = max(0.0, now - row["active_since"])
        if seconds <= 0:
            return None
        window = row["session"] + "@" + format(row["active_since"], ".3f")
        return ActiveView(channel_id=row["cid"], session_id=row["session"],
                          session_hash=_session_hash(row["session"]), window_key=window,
                          active_seconds=seconds, started_at=row["opened"],
                          last_progress=row.get("last_progress", row["last_mono"]),
                          paused=False, verified=row["verified"], trace_present=bool(row["trace"]),
                          position=row["last_position"])

    def views(self, now=None, minimum=0.0):
        now = self.clock() if now is None else float(now)
        out = {}
        for session_id in list(self.sessions):
            view = self._view(self.sessions[session_id], now)
            if view and view.active_seconds >= minimum:
                current = out.get(view.channel_id)
                if current is None or view.active_seconds > current.active_seconds:
                    out[view.channel_id] = view
        return out

    def diagnostics(self, now=None):
        now = self.clock() if now is None else float(now)
        views = self.views(now)
        return {"revision": REVISION, "activeCandidates": len(views),
                "activeChannels": [{"channelID": v.channel_id, "sessionHash": v.session_hash,
                                    "activeSeconds": round(v.active_seconds, 1),
                                    "traceCorrelated": v.trace_present}
                                   for v in sorted(views.values(), key=lambda v: v.channel_id)],
                "staleSeconds": self.stale_seconds, "activeThresholdSeconds": self.active_seconds}


@dataclasses.dataclass
class ResearchPlan:
    channel_id: str
    priority: str
    variant: str
    attempt: int
    window_key: str
    session_hash: str
    active_seconds: float
    due_at: float
    search_key: str
    note: str = ""


@dataclasses.dataclass
class WindowState:
    channel_id: str
    window_key: str
    session_hash: str
    attempts: list = dataclasses.field(default_factory=list)
    next_at: float = 0.0
    outcome: str = ""


class ResearchCoordinator:
    """Bounded active-viewing research; background cooldown stays in Discovery."""

    def __init__(self, active_seconds=DEFAULT_ACTIVE_SECONDS, max_attempts=DEFAULT_MAX_ATTEMPTS,
                 retry_seconds=DEFAULT_RETRY_SECONDS, dedup_seconds=DEFAULT_DEDUP_SECONDS,
                 clock=None, rng=None):
        self.active_seconds = max(1, int(active_seconds))
        self.max_attempts = max(1, min(6, int(max_attempts)))
        self.retry_seconds = tuple(max(1, int(v)) for v in retry_seconds)[:self.max_attempts]
        self.dedup_seconds = max(0, int(dedup_seconds))
        self.clock = clock or time.monotonic
        self.rng = rng or random.SystemRandom()
        self.windows = OrderedDict()
        self.failed_searches = OrderedDict()
        self.last_plan = None

    def _jitter(self, seconds):
        if seconds <= 0:
            return 0.0
        value = seconds * (1.0 + self.rng.uniform(-0.2, 0.2))
        return max(1.0, value)

    def _search_key(self, cid, window, variant, attempt):
        # Deduplicate substantially identical prompts across a pause/resume or
        # a quick channel return, not only within one contiguous window.
        return cid + "|" + str(variant) + "|" + str(attempt)

    def _window(self, view):
        row = self.windows.get(view.window_key)
        if row is None:
            row = WindowState(view.channel_id, view.window_key, view.session_hash)
            self.windows[view.window_key] = row
            while len(self.windows) > 512:
                self.windows.popitem(last=False)
        return row

    def choose_active(self, missing, views, now_wall, base_variant, records=None):
        """Return the next useful active plan, or None. At most one is returned."""
        records = records or {}
        now = self.clock()
        candidates = [v for v in views.values() if v.channel_id in missing and v.active_seconds >= self.active_seconds]
        for view in sorted(candidates, key=lambda v: (-v.active_seconds, v.channel_id)):
            record = records.get(view.channel_id) or {}
            if record.get("outcome") == "verified" and 0 <= now_wall - float(record.get("at", 0)) <= 1800:
                continue
            window = self._window(view)
            if window.outcome == "verified":
                continue
            attempt = len(window.attempts) + 1
            if attempt > self.max_attempts:
                continue
            if attempt > 1 and now < window.next_at:
                continue
            if base_variant in ("high", "max"):
                variant = base_variant
            else:
                variant = "low" if attempt == 1 else "high"
            key = self._search_key(view.channel_id, view.window_key, variant, attempt)
            failed_at = self.failed_searches.get(key)
            if failed_at is not None and now_wall - failed_at < self.dedup_seconds:
                continue
            note = ""
            if attempt > 1:
                note = "Previous attempt for this active viewing found no verifiable evidence; use a materially different official schedule."
            plan = ResearchPlan(view.channel_id, "active", variant, attempt, view.window_key,
                                view.session_hash, view.active_seconds, now_wall, key, note)
            self.last_plan = plan
            return plan
        return None

    def record(self, plan, outcome, now_wall=None):
        now_wall = time.time() if now_wall is None else float(now_wall)
        now = self.clock()
        window = self.windows.get(plan.window_key)
        if window is None:
            window = WindowState(plan.channel_id, plan.window_key, plan.session_hash)
            self.windows[plan.window_key] = window
        if any(row.get("searchKey") == plan.search_key for row in window.attempts):
            return False
        window.attempts.append({"at": now_wall, "variant": plan.variant, "attempt": plan.attempt,
                                "searchKey": plan.search_key, "outcome": str(outcome)[:32]})
        window.outcome = str(outcome)[:32]
        if outcome != "verified":
            self.failed_searches[plan.search_key] = now_wall
            while len(self.failed_searches) > 1024:
                self.failed_searches.popitem(last=False)
            index = min(len(window.attempts) - 1, len(self.retry_seconds) - 1)
            base = self.retry_seconds[index] if self.retry_seconds else 60
            window.next_at = now + self._jitter(base)
        return True

    def diagnostics(self, now_wall=None):
        now = self.clock()
        rows = []
        for window in list(self.windows.values())[-16:]:
            rows.append({"channelID": window.channel_id, "sessionHash": window.session_hash,
                         "attempts": len(window.attempts), "lastVariant": window.attempts[-1]["variant"] if window.attempts else None,
                         "lastOutcome": window.outcome or None,
                         "nextRetrySeconds": max(0, round(window.next_at - now, 1)) if window.next_at else 0})
        last = self.last_plan
        return {"revision": REVISION, "activeThresholdSeconds": self.active_seconds,
                "maxAttempts": self.max_attempts, "dedupSeconds": self.dedup_seconds,
                "retrySeconds": list(self.retry_seconds), "windows": rows,
                "lastPlan": None if last is None else {"channelID": last.channel_id, "priority": last.priority,
                                                       "variant": last.variant, "attempt": last.attempt,
                                                       "sessionHash": last.session_hash,
                                                       "activeSeconds": round(last.active_seconds, 1),
                                                       "dueAt": last.due_at}}
