"""Durable combined programme + verified-language overlay.

The permanent bottom-left contract is one line::

    The Mentalist — S06E18: Forest Green (2014) English — 1/25 — 99.1%

Nothing in this module hard-codes a series, episode, language, count or
confidence.  It accepts a programme row and a language-evidence row only while
they belong to the exact same channel, provider, launch and physical visit.
A channel change, corrected evidence, expired evidence or a conflicting update
replaces the label with a truthful waiting/fallback state.

The actual programme metadata (``Name`` / ``JealousfinProgramme``) is never
rewritten with language text.  This module only formats a presentation label
for the gateway-managed ``form.programmeOverlay`` field.
"""
from __future__ import annotations

import dataclasses
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

REVISION = "programme-language-overlay-20261127"
FIELD = "programmeOverlay"
KEY = "form." + FIELD
MAX_TEXT = 4096
MAX_CHANNEL_NAME = 160
MAX_SERIES = 200
MAX_EPISODE = 300
MAX_LANGUAGE_NAME = 40
MAX_EVENT_ID = 160
DEFAULT_TTL = 1800.0
CHANNEL_ID = re.compile(r"^[a-f0-9]{32}$")
PROVIDER_ID = re.compile(r"^nltv:[A-Za-z0-9_-]+:[A-Za-z0-9_.-]{1,160}$")
LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}$")
IDENTITY_TEXT = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
WAITING = "Waiting for verified programme and language evidence"


class OverlayError(ValueError):
    """Bounded presentation rejection with a stable reason."""


class DeviceTransport(Protocol):
    def set_value(self, field: str, value: str) -> None:
        ...


def _clip(value: str, limit: int = MAX_TEXT) -> str:
    encoded = value.encode("utf-8")
    return value if len(encoded) <= limit else encoded[:limit].decode("utf-8", "ignore")


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:limit] if text and not re.search(r"(?i)https?://|api_key=|token=|[<>]", text) else ""


def _int(value: Any, limit: int) -> int | None:
    return value if type(value) is int and 0 < value <= limit else None


def _norm(value: Any) -> str:
    return "".join(c for c in str(value or "").casefold() if c.isalnum())


@dataclass(frozen=True)
class OverlayScope:
    """The exact physical viewing scope the label is allowed to describe."""

    channel_id: str
    provider_id: str
    channel_name: str
    launch_id: str = ""
    visit_id: str = ""
    stream_generation: str = ""
    rotation_attempt: int | None = None

    def checked(self) -> bool:
        return (isinstance(self.channel_id, str) and CHANNEL_ID.fullmatch(self.channel_id) is not None
                and isinstance(self.provider_id, str) and PROVIDER_ID.fullmatch(self.provider_id) is not None
                and isinstance(self.channel_name, str) and 0 < len(self.channel_name) <= MAX_CHANNEL_NAME
                and (not self.launch_id or IDENTITY_TEXT.fullmatch(self.launch_id) is not None)
                and (not self.visit_id or IDENTITY_TEXT.fullmatch(self.visit_id) is not None)
                and (not self.stream_generation or IDENTITY_TEXT.fullmatch(self.stream_generation) is not None)
                and (self.rotation_attempt is None or type(self.rotation_attempt) is int and self.rotation_attempt >= 0))

    def identity(self) -> tuple:
        return (self.channel_id, self.provider_id, self.channel_name,
                self.launch_id, self.visit_id, self.rotation_attempt)

    def fingerprint(self) -> str:
        return "|".join(str(part) for part in self.identity() + (self.stream_generation,))


@dataclass(frozen=True)
class ProgrammeIdentity:
    series_title: str
    episode_title: str = ""
    season_number: int | None = None
    episode_number: int | None = None
    episode_year: int | None = None
    original_air_date: str = ""
    verification: str = ""
    source: str = ""

    def checked(self) -> bool:
        if not isinstance(self.series_title, str) or not 0 < len(self.series_title) <= MAX_SERIES:
            return False
        if self.episode_title and (not isinstance(self.episode_title, str) or len(self.episode_title) > MAX_EPISODE):
            return False
        if (self.season_number is None) != (self.episode_number is None):
            return False
        if self.season_number is not None and (type(self.season_number) is not int or not 1 <= self.season_number <= 99):
            return False
        if self.episode_number is not None and (type(self.episode_number) is not int or not 1 <= self.episode_number <= 9999):
            return False
        if self.episode_year is not None and (type(self.episode_year) is not int or not 1888 <= self.episode_year <= time.gmtime().tm_year + 1):
            return False
        return True

    def code(self) -> str:
        if self.season_number is None or self.episode_number is None:
            return ""
        return "S%02dE%02d" % (self.season_number, self.episode_number)

    def text(self) -> str:
        head = self.series_title
        code = self.code()
        if code and self.episode_title:
            head += " — " + code + ": " + self.episode_title
        elif code:
            head += " — " + code
        elif self.episode_title:
            head += " — " + self.episode_title
        if self.episode_year:
            head += " (%d)" % self.episode_year
        return head


@dataclass(frozen=True)
class LanguageEvidence:
    language: str
    language_name: str
    count: int
    threshold: int
    confidence: float
    observed_at: float
    phase: str = "recorded"
    event_id: str = ""

    def checked(self, now: float, ttl: float = DEFAULT_TTL) -> bool:
        if not isinstance(self.language, str) or not LANGUAGE_CODE.fullmatch(self.language):
            return False
        if not isinstance(self.language_name, str) or not 0 < len(self.language_name) <= MAX_LANGUAGE_NAME:
            return False
        if type(self.count) is not int or type(self.threshold) is not int or not 0 <= self.count <= 10**6 or not 0 < self.threshold <= 10**6:
            return False
        if type(self.confidence) not in (int, float) or not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            return False
        if type(self.observed_at) not in (int, float) or not math.isfinite(self.observed_at) or self.observed_at < 0:
            return False
        if self.observed_at > now + 30:
            return False
        return 0 <= now - self.observed_at <= ttl

    def text(self) -> str:
        return "%s — %d/%d — %.1f%%" % (self.language_name, self.count, self.threshold, self.confidence * 100)


def format_combined(programme: ProgrammeIdentity, language: LanguageEvidence | None = None,
                    *, fallback: str = WAITING, now: float | None = None) -> str:
    """Render the exact permanent bottom-left format when evidence is verified."""
    if not isinstance(programme, ProgrammeIdentity) or not programme.checked():
        return _clip(fallback)
    if language is None:
        return _clip(programme.text())
    stamp = time.time() if now is None else float(now)
    if not isinstance(language, LanguageEvidence) or not language.checked(stamp):
        return _clip(programme.text() + " — language evidence pending")
    return _clip(programme.text() + " " + language.text())


def programme_identity(row: Mapping[str, Any]) -> ProgrammeIdentity | None:
    """Build presentation identity from the existing programme metadata row."""
    if not isinstance(row, Mapping):
        return None
    series = _clean(row.get("seriesTitle") or row.get("title") or row.get("name"), MAX_SERIES)
    if not series:
        return None
    season = _int(row.get("seasonNumber", row.get("season_number")), 99)
    number = _int(row.get("episodeNumber", row.get("episode_number")), 9999)
    if (season is None) != (number is None):
        season = number = None
    year = row.get("seasonYear", row.get("episodeYear", row.get("episode_year")))
    if type(year) is not int or not 1888 <= year <= time.gmtime().tm_year + 1:
        year = None
    episode = _clean(row.get("episodeTitle") or row.get("episode_title"), MAX_EPISODE)
    identity = ProgrammeIdentity(
        series_title=series, episode_title=episode,
        season_number=season, episode_number=number, episode_year=year,
        original_air_date=_clean(row.get("episodeOriginalAirDate") or row.get("originalAirDate"), 10),
        verification=_clean(row.get("scheduleVerification") or row.get("episodeVerification"), 80),
        source=_clean(row.get("episodeSource") or row.get("source"), 80))
    return identity if identity.checked() else None


def language_evidence(evidence: Mapping[str, Any]) -> LanguageEvidence | None:
    if not isinstance(evidence, Mapping):
        return None
    try:
        row = LanguageEvidence(
            language=str(evidence.get("language") or ""),
            language_name=_clean(evidence.get("languageName") or evidence.get("language"), MAX_LANGUAGE_NAME),
            count=evidence.get("count"), threshold=evidence.get("threshold"),
            confidence=evidence.get("confidence"),
            observed_at=float(evidence.get("at", evidence.get("observedAt", 0)) or 0),
            phase=_clean(evidence.get("phase") or evidence.get("outcome"), 40) or "recorded",
            event_id=_clean(evidence.get("eventID") or evidence.get("event_id"), MAX_EVENT_ID))
    except (TypeError, ValueError):
        return None
    # Structural validation only; freshness is enforced by the caller against
    # its injected clock so tests and delayed label publication stay coherent.
    return row if row.checked(row.observed_at) else None


@dataclass
class _State:
    scope: OverlayScope
    programme: ProgrammeIdentity
    programme_start: float
    programme_end: float
    observed_at: float
    expires_at: float
    language: LanguageEvidence | None = None
    correction: bool = False


class ProgrammeLanguageOverlay:
    """Pure state machine for one combined label; no transport or timers."""

    def __init__(self, *, ttl: float = DEFAULT_TTL, clock: Callable[[], float] = time.time):
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0:
            raise OverlayError("Invalid overlay TTL")
        self.ttl = float(ttl)
        self.clock = clock
        self.generation = 0
        self._state: _State | None = None
        self.last_reason = "idle"

    def _now(self, now: Optional[float]) -> float:
        return float(self.clock() if now is None else now)

    def observe(self, scope: OverlayScope, row: Mapping[str, Any], *, start: float | None = None,
                end: float | None = None, now: Optional[float] = None) -> str:
        if not isinstance(scope, OverlayScope) or not scope.checked():
            self.last_reason = "invalid-scope"
            return self.last_reason
        programme = programme_identity(row) if isinstance(row, Mapping) else None
        if programme is None:
            self.clear(scope, reason="no-programme", now=now)
            return self.last_reason
        now = self._now(now)
        same_programme = bool(self._state is not None
                              and self._state.scope.identity() == scope.identity()
                              and self._state.programme == programme)
        if start is None:
            start = self._state.programme_start if same_programme else now
        else:
            start = float(start)
        if end is None:
            end = self._state.programme_end if same_programme else now + self.ttl
        else:
            end = float(end)
        if not (start <= now < end):
            self.last_reason = "not-current"
            return self.last_reason
        if self._state is not None and self._state.scope.identity() == scope.identity():
            # Only the stream generation changed: keep the programme, drop the
            # old language label until evidence is re-verified for this stream.
            if self._state.scope.stream_generation != scope.stream_generation:
                self._state.scope = scope
                self._state.language = None
                self._state.correction = False
                self.generation += 1
                self.last_reason = "stream-generation-changed"
                return self.last_reason
            if self._state.programme == programme and self._state.programme_start == start and self._state.programme_end == end:
                self.last_reason = "unchanged"
                return self.last_reason
        if self._state is None or self._state.scope.identity() != scope.identity():
            self.generation += 1
        if self._state is not None and self._state.programme != programme and self._state.language is not None:
            # A different episode must not inherit the previous episode's label.
            pass
        self._state = _State(scope=scope, programme=programme, programme_start=start,
                             programme_end=end, observed_at=now, expires_at=now + self.ttl,
                             language=None, correction=False)
        self.last_reason = "observed"
        return self.last_reason

    def publish_language(self, scope: OverlayScope, evidence: Mapping[str, Any], *,
                         now: Optional[float] = None) -> str:
        if not isinstance(scope, OverlayScope) or not scope.checked():
            self.last_reason = "invalid-scope"
            return self.last_reason
        now = self._now(now)
        state = self._state
        if state is None:
            self.last_reason = "no-programme"
            return self.last_reason
        if state.expires_at <= now or state.programme_end <= now:
            self.last_reason = "expired"
            return self.last_reason
        if state.scope.identity() != scope.identity():
            self.last_reason = "channel-mismatch"
            return self.last_reason
        if state.scope.stream_generation and scope.stream_generation and state.scope.stream_generation != scope.stream_generation:
            self.last_reason = "stream-generation-mismatch"
            return self.last_reason
        row = language_evidence(evidence)
        if row is None or not row.checked(now, self.ttl):
            self.last_reason = "invalid-language-evidence"
            return self.last_reason
        # Freshness and exact visit/channel scope are the stale controls here.
        # A sample may legitimately start a few seconds before the gateway
        # observes the schedule row; it is still bounded by the evidence TTL.
        if state.language is not None and row.observed_at < state.language.observed_at:
            self.last_reason = "out-of-order-language-evidence"
            return self.last_reason
        if state.correction and row.phase not in ("corrected", "english", "allowed"):
            self.last_reason = "correction-conflict"
            return self.last_reason
        state.language = row
        state.correction = row.phase in ("corrected", "mistaken", "english")
        state.scope = scope
        self.last_reason = "published"
        return self.last_reason

    def correct(self, scope: OverlayScope, *, language_name: str = "English", now: Optional[float] = None) -> str:
        state = self._state
        now = self._now(now)
        if state is None or state.scope.identity() != getattr(scope, "identity", lambda: None)():
            self.last_reason = "channel-mismatch"
            return self.last_reason
        if state.language is None:
            self.last_reason = "no-evidence"
            return self.last_reason
        row = LanguageEvidence(
            language="en", language_name=_clean(language_name, MAX_LANGUAGE_NAME) or "English",
            count=state.language.count, threshold=state.language.threshold,
            confidence=state.language.confidence, observed_at=now, phase="corrected",
            event_id=state.language.event_id + ":corrected")
        state.language = row
        state.correction = True
        self.last_reason = "corrected"
        return self.last_reason

    def clear(self, scope: OverlayScope | None = None, *, reason: str = "cleared",
              now: Optional[float] = None) -> str:
        if scope is not None and self._state is not None and self._state.scope.identity() != scope.identity():
            self.last_reason = "channel-mismatch"
            return self.last_reason
        self._state = None
        self.generation += 1
        self.last_reason = reason
        return self.last_reason

    def label(self, now: Optional[float] = None, fallback: str = WAITING) -> str:
        now = self._now(now)
        state = self._state
        if state is None or state.expires_at <= now or state.programme_end <= now:
            return _clip(fallback)
        if not (state.programme_start <= now < state.programme_end):
            return _clip(fallback)
        return format_combined(state.programme, state.language, fallback=fallback, now=now)

    def event_id(self, now: Optional[float] = None) -> str:
        state = self._state
        if state is None:
            return ""
        language = state.language.event_id if state.language else "pending"
        return _clip("%s|%s|%s" % (state.scope.fingerprint(), state.programme.original_air_date or state.programme.text(),
                                   language), MAX_EVENT_ID)

    def status(self, now: Optional[float] = None) -> dict:
        now = self._now(now)
        state = self._state
        return {"revision": REVISION, "generation": self.generation,
                "current": bool(state and state.expires_at > now and state.programme_end > now),
                "channelID": state.scope.channel_id if state else "",
                "providerID": state.scope.provider_id if state else "",
                "channelName": state.scope.channel_name if state else "",
                "label": self.label(now), "reason": self.last_reason,
                "hasLanguage": bool(state and state.language),
                "expiresAt": state.expires_at if state else 0,
                "programmeExpiresAt": state.programme_end if state else 0,
                "streamGeneration": state.scope.stream_generation if state else "",
                "rotationAttempt": state.scope.rotation_attempt if state else None}


class ProgrammeOverlayBridge:
    """Deduplicated, bounded publisher for the combined form field."""

    def __init__(self, transport: DeviceTransport, *, field: str = FIELD,
                 min_interval: float = 0.5, max_retries: int = 2,
                 retry_base: float = 0.25, pending_ttl: float = 30.0,
                 clock: Callable[[], float] = time.monotonic,
                 wallclock: Callable[[], float] = time.time):
        if not isinstance(field, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", field):
            raise OverlayError("Invalid overlay field")
        self._transport = transport
        self.field = field
        self.min_interval = float(min_interval)
        self.max_retries = int(max_retries)
        self.retry_base = float(retry_base)
        self.pending_ttl = float(pending_ttl)
        self._clock = clock
        self._wallclock = wallclock
        self.last_text = ""
        self.last_event_id = ""
        self.last_scope = ""
        self.status_text = "idle"
        self.fault = ""
        self._pending: dict | None = None
        self._last_attempt = float("-inf")

    def publish(self, text: str, event_id: str, scope: OverlayScope, *,
                now: Optional[float] = None) -> str:
        if not isinstance(scope, OverlayScope) or not scope.checked():
            self.status_text = "invalid-scope"
            return self.status_text
        text = _clip(text)
        if not isinstance(text, str) or not text:
            self.status_text = "empty"
            return self.status_text
        now = self._clock() if now is None else float(now)
        fingerprint = scope.fingerprint()
        if self.last_scope and self.last_scope != fingerprint:
            # A stale queued value can never survive a scope transition.
            self._pending = None
        if self._pending is None and text == self.last_text and event_id == self.last_event_id and fingerprint == self.last_scope:
            self.status_text = "duplicate"
            return self.status_text
        self._pending = {"text": text, "event_id": _clip(str(event_id or ""), MAX_EVENT_ID),
                         "scope": fingerprint, "created": now, "attempts": 0,
                         "retry_at": float("-inf")}
        return self._pump(now)

    def pump(self, now: Optional[float] = None) -> str:
        return self._pump(self._clock() if now is None else float(now))

    def _pump(self, now: float) -> str:
        pending = self._pending
        if pending is None:
            self.status_text = "idle"
            return self.status_text
        if now - pending["created"] > self.pending_ttl:
            self._pending = None
            self.fault = "stale_pending"
            self.status_text = "stale"
            return self.status_text
        if now < pending["retry_at"]:
            self.status_text = "deferred"
            return self.status_text
        if pending["attempts"] and now - self._last_attempt < self.min_interval:
            self.status_text = "rate_limited"
            return self.status_text
        self._last_attempt = now
        pending["attempts"] += 1
        try:
            self._transport.set_value(self.field, pending["text"])
        except Exception as error:
            self.fault = "transport_unavailable:" + type(error).__name__
            if pending["attempts"] > self.max_retries:
                self._pending = None
                self.status_text = "failed"
                return self.status_text
            pending["retry_at"] = now + self.retry_base * (2 ** (pending["attempts"] - 1))
            self.status_text = "retry"
            return self.status_text
        self.last_text = pending["text"]
        self.last_event_id = pending["event_id"]
        self.last_scope = pending["scope"]
        self._pending = None
        self.fault = ""
        self.status_text = "published"
        return self.status_text

    def status(self) -> dict:
        return {"revision": REVISION, "field": KEY, "publishedText": self.last_text,
                "publishedEventID": self.last_event_id, "pending": self._pending is not None,
                "status": self.status_text, "fault": self.fault,
                "staleLabel": bool(self._pending is None and self.last_scope
                                   and self._pending and self._pending["scope"] != self.last_scope)}


def status() -> dict:
    return {"revision": REVISION, "field": KEY, "maxTextBytes": MAX_TEXT,
            "defaultTTLSeconds": DEFAULT_TTL,
            "format": "series — SxxExx: episode (year) language — count/threshold — confidence%",
            "staticEpisodeMapping": False,
            "scope": ["channelID", "providerID", "channelName", "launchID", "visitID",
                      "streamGeneration", "rotationAttempt"]}
