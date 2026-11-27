"""Combined programme + verified-language label for the installed IPA.

The installed hotpatch (revision ``programme-language-existing-ipa-20261127``)
already contains the native binding surface.  It is:

* hidden editable ``form.programmeOverlay`` (empty by default)
* hidden editable ``form.programmeOverlayReady`` (``false`` by default)
* hidden editable ``form.programmeOverlayFallback`` (``true`` by default)
* native text node ``programme-combined-native-v1`` using the existing
  ``headline`` style, ``valueKey: form.programmeOverlay`` and
  ``visibleWhen: form.programmeOverlayReady``
* the existing 22 px web fallback showing live programme metadata only,
  ``visibleWhen: form.programmeOverlayFallback``

This module deliberately does not introduce a new native style, layout or
provenance.  It writes only those three existing form fields through the
existing authorized ``ui_set_value`` transport.

Visibility is switched in a safe order:

1. scope transition / clear: disable Ready, enable Fallback, clear the value
2. valid current-scope publication: write the value, re-probe the exact scope
3. only then enable Ready and disable Fallback

A transport failure leaves the user with the existing live-metadata fallback
rather than a stale combined label.  Rotation pause is not a presentation
scope change: the caller may publish while rotation is paused as long as the
same channel/launch/visit/stream is still current and playback is progressing.
"""
from __future__ import annotations

import dataclasses
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

REVISION = "programme-language-existing-ipa-20261127-v1"
FIELD = "programmeOverlay"
READY_FIELD = "programmeOverlayReady"
FALLBACK_FIELD = "programmeOverlayFallback"
KEY = "form." + FIELD
READY_KEY = "form." + READY_FIELD
FALLBACK_KEY = "form." + FALLBACK_FIELD
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
TRUE = "true"
FALSE = "false"
WAITING = "Waiting for verified programme and language evidence"
SAFE_NATIVE_STYLES = frozenset({"normal", "headline", "caption", "prominent", "destructive", "muted"})
ALLOWED_EPISODE_SOURCES = frozenset({
    "pluto-xmltv", "catalog-schedule", "local-audio-episode-identity",
})


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
    if not text or re.search(r"(?i)https?://|api_key=|token=|[<>]", text):
        return ""
    return text[:limit]


def _int(value: Any, limit: int) -> Optional[int]:
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

    def checked(self) -> bool:
        return (isinstance(self.channel_id, str) and CHANNEL_ID.fullmatch(self.channel_id) is not None
                and isinstance(self.provider_id, str) and PROVIDER_ID.fullmatch(self.provider_id) is not None
                and isinstance(self.channel_name, str) and 0 < len(self.channel_name) <= MAX_CHANNEL_NAME
                and (not self.launch_id or IDENTITY_TEXT.fullmatch(self.launch_id) is not None)
                and (not self.visit_id or IDENTITY_TEXT.fullmatch(self.visit_id) is not None)
                and (not self.stream_generation or IDENTITY_TEXT.fullmatch(self.stream_generation) is not None))

    def identity(self) -> tuple:
        return (self.channel_id, self.provider_id, self.channel_name,
                self.launch_id, self.visit_id)

    def fingerprint(self) -> str:
        return "|".join(str(part) for part in self.identity() + (self.stream_generation,))

    def matches(self, current: Any) -> bool:
        if not isinstance(current, Mapping):
            return False
        if current.get("channelID") != self.channel_id:
            return False
        if self.launch_id and current.get("launchID") != self.launch_id:
            return False
        if self.stream_generation and current.get("streamGeneration") != self.stream_generation:
            return False
        if self.visit_id and current.get("visitID") != self.visit_id:
            return False
        return True


@dataclass(frozen=True)
class ProgrammeIdentity:
    series_title: str
    episode_title: str = ""
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    episode_year: Optional[int] = None
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
        if type(self.count) is not int or type(self.threshold) is not int or not 0 <= self.count <= 10 ** 6 or not 0 < self.threshold <= 10 ** 6:
            return False
        if type(self.confidence) not in (int, float) or not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            return False
        if type(self.observed_at) not in (int, float) or not math.isfinite(self.observed_at) or self.observed_at < 0:
            return False
        if self.observed_at > now + 30:
            return False
        return 0 <= now - self.observed_at <= ttl

    def text(self) -> str:
        return "%s — %d/%d — %.1f%%" % (
            self.language_name, self.count, self.threshold, self.confidence * 100)


def format_combined(programme: ProgrammeIdentity, language: Optional[LanguageEvidence] = None,
                    *, fallback: str = WAITING, now: Optional[float] = None) -> str:
    """Render the installed single-line contract when evidence is verified."""
    if not isinstance(programme, ProgrammeIdentity) or not programme.checked():
        return _clip(fallback)
    if language is None:
        return _clip(programme.text() + " — language evidence pending")
    stamp = time.time() if now is None else float(now)
    if not isinstance(language, LanguageEvidence) or not language.checked(stamp):
        return _clip(programme.text() + " — language evidence pending")
    return _clip(programme.text() + " " + language.text())


def programme_identity(row: Mapping[str, Any]) -> Optional[ProgrammeIdentity]:
    """Build presentation identity from the existing programme metadata row.

    The year is the episode-scoped original-air year where available.  A
    season year or the provider title year is only used when no episode year
    exists.  This keeps ``Nurse Jackie (2009)`` from replacing the exact
    episode year.
    """
    if not isinstance(row, Mapping):
        return None
    series = _clean(row.get("seriesTitle") or row.get("title") or row.get("name"), MAX_SERIES)
    if not series:
        return None
    season = _int(row.get("seasonNumber", row.get("season_number")), 99)
    number = _int(row.get("episodeNumber", row.get("episode_number")), 9999)
    if (season is None) != (number is None):
        season = number = None
    episode = _clean(row.get("episodeTitle") or row.get("episode_title"), MAX_EPISODE)
    source = _clean(row.get("episodeSource"), 80)
    if source not in ALLOWED_EPISODE_SOURCES:
        # Match the native ProgrammeEvidence allowlist: an unknown provenance
        # must not display episode identity the client parser would drop.
        season = number = None
        episode = None
    year = row.get("episodeYear", row.get("episode_year"))
    if type(year) is not int or not 1888 <= year <= time.gmtime().tm_year + 1:
        year = row.get("seasonYear", row.get("season_year"))
    if type(year) is not int or not 1888 <= year <= time.gmtime().tm_year + 1:
        year = None
    if episode is None and season is None:
        year = None
    identity = ProgrammeIdentity(
        series_title=series, episode_title=episode,
        season_number=season, episode_number=number, episode_year=year,
        original_air_date=_clean(row.get("episodeOriginalAirDate") or row.get("originalAirDate"), 10),
        verification=_clean(row.get("scheduleVerification") or row.get("episodeVerification"), 80),
        source=source or _clean(row.get("source"), 80))
    return identity if identity.checked() else None


def language_evidence(evidence: Mapping[str, Any]) -> Optional[LanguageEvidence]:
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
    # Structural validation only; freshness is enforced against the caller's
    # clock so a delayed label publication stays coherent.
    return row if row.checked(row.observed_at) else None


class ProgrammeOverlayPublisher:
    """Ordered, deduplicated publisher for the three installed form fields."""

    def __init__(self, transport: DeviceTransport, *, max_retries: int = 2,
                 retry_base: float = 0.25, pending_ttl: float = 30.0,
                 clock: Callable[[], float] = time.monotonic,
                 wallclock: Callable[[], float] = time.time):
        if not isinstance(max_retries, int) or not 0 <= max_retries <= 5:
            raise OverlayError("Invalid overlay retry bound")
        self._transport = transport
        self.max_retries = max_retries
        self.retry_base = max(0.0, float(retry_base))
        self.pending_ttl = max(1.0, float(pending_ttl))
        self._clock = clock
        self._wallclock = wallclock
        self.current_scope = ""
        self.last_text = ""
        self.last_event_id = ""
        self.last_status = "idle"
        self.fault = ""
        self.last_attempt_at = float("-inf")
        self.published_count = 0
        self.fallback_count = 0

    def _now(self, now: Optional[float]) -> float:
        return self._clock() if now is None else float(now)

    def _write(self, field: str, value: str, now: float) -> bool:
        attempts = 0
        while attempts <= self.max_retries:
            attempts += 1
            self.last_attempt_at = now
            try:
                self._transport.set_value(field, value)
                return True
            except Exception as error:
                self.fault = "transport_unavailable:" + type(error).__name__
        return False

    def _fallback(self, now: float, *, clear_value: bool = False) -> str:
        # Disable the combined node first, then show the live-metadata fallback.
        ready = self._write(READY_FIELD, FALSE, now)
        fallback = self._write(FALLBACK_FIELD, TRUE, now)
        cleared = True if not clear_value else self._write(FIELD, "", now)
        if ready and fallback and cleared:
            self.current_scope = ""
            self.last_text = ""
            self.last_event_id = ""
            self.last_status = "fallback"
            self.fallback_count += 1
            return self.last_status
        self.fault = self.fault or "fallback_failed"
        self.last_status = "failed"
        return self.last_status

    def transition(self, scope: OverlayScope, now: Optional[float] = None) -> str:
        if not isinstance(scope, OverlayScope) or not scope.checked():
            self.last_status = "invalid-scope"
            return self.last_status
        now = self._now(now)
        fingerprint = scope.fingerprint()
        if fingerprint == self.current_scope:
            self.last_status = "unchanged"
            return self.last_status
        self.fault = ""
        result = self._fallback(now, clear_value=True)
        if result == "fallback":
            self.current_scope = fingerprint
            self.last_status = "transitioned"
        return self.last_status

    def publish(self, scope: OverlayScope, text: str, event_id: str = "",
                *, reprobe: Optional[Callable[[], Any]] = None,
                now: Optional[float] = None) -> str:
        if not isinstance(scope, OverlayScope) or not scope.checked():
            self.last_status = "invalid-scope"
            return self.last_status
        text = _clip(text)
        if not isinstance(text, str) or not text:
            self.last_status = "empty"
            return self.last_status
        now = self._now(now)
        fingerprint = scope.fingerprint()
        if fingerprint != self.current_scope:
            if self.transition(scope, now) != "transitioned":
                return self.last_status
        event_id = _clip(str(event_id or ""), MAX_EVENT_ID)
        if text == self.last_text and event_id == self.last_event_id and fingerprint == self.current_scope:
            self.last_status = "duplicate"
            return self.last_status
        if not self._write(FIELD, text, now):
            self._fallback(now)
            self.last_status = "failed"
            return self.last_status
        if callable(reprobe):
            try:
                current = reprobe()
            except Exception:
                current = None
            if not scope.matches(current):
                self.fault = "scope_changed"
                self._fallback(now)
                self.last_status = "scope_changed"
                return self.last_status
        if not self._write(READY_FIELD, TRUE, now):
            self._fallback(now)
            self.last_status = "failed"
            return self.last_status
        if not self._write(FALLBACK_FIELD, FALSE, now):
            # Both nodes would be visible if fallback stayed true; revert to
            # the safe fallback rather than duplicate the label.
            self.fault = "fallback_disable_failed"
            self.last_status = "fallback"
            self._fallback(now)
            return self.last_status
        self.last_text = text
        self.last_event_id = _clip(str(event_id or ""), MAX_EVENT_ID)
        self.current_scope = fingerprint
        self.fault = ""
        self.last_status = "published"
        self.published_count += 1
        return self.last_status

    def clear(self, now: Optional[float] = None) -> str:
        now = self._now(now)
        self.fault = ""
        return self._fallback(now, clear_value=True)

    def status(self) -> dict:
        return {"revision": REVISION, "field": KEY, "readyField": READY_KEY,
                "fallbackField": FALLBACK_KEY, "publishedText": self.last_text,
                "publishedEventID": self.last_event_id,
                "status": self.last_status, "fault": self.fault,
                "published": self.published_count, "fallbacks": self.fallback_count,
                "scope": self.current_scope}


def _walk(nodes):
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        yield node
        for child in _walk(node.get("children") or []):
            yield child


def validate_existing_ipa_layout(layout: Mapping[str, Any]) -> None:
    """Validate the already-installed ABI without requiring a native rebuild.

    The draft ``style: programme`` layout is intentionally not accepted: the
    installed IPA only knows the existing native styles.  The exact hidden
    fields and the native ``headline`` binding are required.
    """
    if not isinstance(layout, Mapping):
        raise OverlayError("Layout is not an object")
    if layout.get("schemaVersion") != 1 or not isinstance(layout.get("revision"), str):
        raise OverlayError("Invalid layout header")
    slots = layout.get("slots")
    if not isinstance(slots, list):
        raise OverlayError("Invalid layout slots")
    nodes = []
    for slot in slots:
        if not isinstance(slot, Mapping):
            raise OverlayError("Invalid layout slot")
        nodes.extend(_walk(slot.get("nodes") or []))
    by_id = {node.get("id"): node for node in nodes if isinstance(node.get("id"), str)}
    required = {
        "hidden-programmeOverlay": (FIELD, ""),
        "hidden-programmeOverlayReady": (READY_FIELD, FALSE),
        "hidden-programmeOverlayFallback": (FALLBACK_FIELD, TRUE),
    }
    for node_id, (field, default) in required.items():
        node = by_id.get(node_id)
        if not isinstance(node, Mapping):
            raise OverlayError("Missing installed field " + node_id)
        if node.get("kind") != "input" or node.get("valueKey") != "form." + field:
            raise OverlayError("Installed field binding mismatch " + node_id)
        if str(node.get("defaultValue")) != default:
            raise OverlayError("Installed default mismatch " + node_id)
    native = by_id.get("programme-combined-native-v1")
    if not isinstance(native, Mapping):
        raise OverlayError("Missing installed native binding")
    if native.get("kind") != "text" or native.get("valueKey") != KEY:
        raise OverlayError("Invalid installed native binding")
    if native.get("visibleWhen") != READY_KEY:
        raise OverlayError("Invalid installed native visibility")
    style = native.get("style", "normal")
    if style not in SAFE_NATIVE_STYLES or style == "programme":
        raise OverlayError("Installed native ABI does not support style " + str(style))
    fallback = by_id.get("programme-bottom-left-v1")
    if not isinstance(fallback, Mapping):
        raise OverlayError("Missing installed web fallback")
    if fallback.get("kind") != "web" or fallback.get("visibleWhen") != FALLBACK_KEY:
        raise OverlayError("Invalid installed web fallback visibility")


def status() -> dict:
    return {"revision": REVISION, "field": KEY, "readyField": READY_KEY,
            "fallbackField": FALLBACK_KEY, "maxTextBytes": MAX_TEXT,
            "defaultTTLSeconds": DEFAULT_TTL,
            "format": "series — SxxExx: episode (episode year) language — count/threshold — confidence%",
            "staticEpisodeMapping": False,
            "nativeStyle": "existing headline (installed IPA)",
            "nativeAbi": "programme-language-existing-ipa-20261127",
            "scope": ["channelID", "providerID", "channelName", "launchID",
                      "visitID", "streamGeneration"]}
