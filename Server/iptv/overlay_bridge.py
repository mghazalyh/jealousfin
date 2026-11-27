"""Publish language evidence into the installed dynamic UI.

The installed app accepts a remote form value only when an editable node
declares the matching ``form.<field>`` key.  Text and banner nodes read
``model.dynamicValue(node.valueKey)``, so a hidden editable input and a native
label can share one key.  The bridge writes one atomic string to that field.

This module is deliberately additive.  It does not import ``gateway.py``, has
no scheduler, never reads credentials, and never issues playback, rotation,
queue or network-discovery commands.  The only device action is the existing
authorized ``ui_set_value`` command sent through ``prototype_selector``.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

import prototype_selector

OVERLAY_FIELD = "languageOverlay"
OVERLAY_KEY = "form." + OVERLAY_FIELD
OVERLAY_REVISION = "language-evidence-overlay-bridge-20261127"
MAX_TEXT = 4096
MAX_CHANNEL_NAME = 160
MAX_EVENT_ID = 128
MAX_LANGUAGE_NAME = 40
MAX_PHASE = 40
MAX_NONCE = 128
CHANNEL_ID = re.compile(r"^[a-f0-9]{32}$")
FIELD_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}$")
BINDING = re.compile(r"^(state|form)\.[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+){0,5}$")

LAYOUT_LOCATIONS = {
    "guide.body", "health.body", "insights.body", "more.body", "player.transport",
    "player.surf", "player.history", "player.fullscreen.header",
    "player.fullscreen.heading", "player.fullscreen.transport",
    "player.fullscreen.surf", "player.fullscreen.footer",
    "player.fullscreen.showControls", "player.fullscreen.hideControls",
    "player.fullscreen.exit", "player.fullscreen.programmeTitle",
    "player.fullscreen.countdown", "player.fullscreen.rotationToggle",
    "player.fullscreen.previous", "player.fullscreen.playPause",
    "player.fullscreen.next", "player.fullscreen.stop",
    "player.fullscreen.favorite", "player.fullscreen.category",
    "player.fullscreen.surfToggle", "player.fullscreen.brightness",
    "player.fullscreen.favoriteError", "player.fullscreen.sharedHint",
    "guide.top", "guide.bottom", "player.top", "player.controls",
    "player.bottom", "player.fullscreen.top", "player.fullscreen.controls",
    "player.fullscreen.bottom", "health.top", "health.bottom",
    "insights.top", "insights.bottom", "more.top", "more.bottom",
    "settings.top", "settings.bottom", "telemetry.top", "telemetry.bottom",
    "runtime.top", "runtime.bottom", "tests.top", "tests.bottom",
    "logs.top", "logs.bottom", "epg.top", "epg.bottom",
}
LAYOUT_KINDS = {
    "segmented", "menu", "disclosure", "scroll", "searchList", "table",
    "barChart", "banner", "liveBrightness", "text", "metric", "button",
    "input", "toggle", "picker", "slider", "progress", "card", "vstack",
    "hstack", "divider", "link", "spacer", "web",
}
LAYOUT_COMMANDS = {
    "query_native_data", "get_effective_configuration",
    "get_capability_registry", "list_event_modules", "get_media_tracks",
    "select_media_track", "seek_live", "seek_relative",
    "reload_current_stream", "get_hot_configuration",
    "get_native_capabilities", "get_preferences", "set_preferences",
    "set_player_audio", "set_video_brightness", "navigate",
    "refresh_catalogue", "probe_active_hls", "set_fullscreen",
    "set_fullscreen_controls", "get_player_snapshot", "get_status",
    "get_telemetry", "get_insights", "get_logs", "get_version",
    "get_capabilities", "list_channels", "get_channel_health",
    "list_banned", "list_hiatus", "open_channel", "open_server_surf", "play",
    "pause", "resume", "stop", "next_channel", "previous_channel",
    "start_surf", "stop_surf", "pause_surf", "resume_surf",
    "set_rotation_minutes", "favorite_current", "favorite_previous",
    "sync_now", "reconnect", "start_device_tests", "pause_device_tests",
    "resume_device_tests", "stop_device_tests", "get_device_tests",
    "get_runtime_module", "script",
}
EDITABLE_KINDS = {"input", "toggle", "picker", "slider", "segmented", "menu"}
CONTAINER_KINDS = {"card", "vstack", "hstack", "disclosure", "scroll", "searchList", "table", "barChart"}
STYLES = {"normal", "headline", "caption", "prominent", "destructive", "muted"}


class OverlayError(ValueError):
    """A bounded, user-visible overlay validation failure."""


class ManualCorrectionError(OverlayError):
    """A manual correction no longer matches the current visible evidence."""


class AlreadyCorrected(ManualCorrectionError):
    """The exact evidence event was already corrected by this bridge."""


class DeviceCommandTransport(Protocol):
    def set_value(self, field: str, value: str) -> None:
        ...


class AuthorizedDeviceTransport:
    """Use the gateway's existing authorized device command transport.

    ``config`` and ``token`` are owned by the gateway.  They are never logged
    or exposed by this wrapper, and the adapter never reads the environment.
    """

    def __init__(self, config: Mapping[str, Any], token: str = "",
                 command: Callable[..., str] = prototype_selector.device_command):
        self._config = dict(config)
        self._token = token
        self._command = command

    def __repr__(self) -> str:
        return "AuthorizedDeviceTransport(config=%r, token=<set>)" % (self._config,)

    def set_value(self, field: str, value: str) -> None:
        if not isinstance(field, str) or not FIELD_ID.fullmatch(field):
            raise OverlayError("Invalid overlay field")
        if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_TEXT:
            raise OverlayError("Invalid overlay value")
        self._command(self._config, self._token, "ui_set_value",
                      {"key": "form." + field, "value": value})


@dataclass(frozen=True)
class EvidenceSnapshot:
    channel_id: str
    channel_name: str
    event_id: str
    observed_at: float
    language: str = ""
    language_name: str = ""
    count: Optional[int] = None
    threshold: Optional[int] = None
    phase: str = ""
    confidence: Optional[float] = None
    outcome: str = ""
    detail: str = ""
    seconds: Optional[float] = None
    target: Optional[float] = None
    attempt: Optional[int] = None
    max_attempts: Optional[int] = None
    retry_seconds: Optional[float] = None


def _clip(value: str, limit: int = MAX_TEXT) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", "ignore")


def _iso_timestamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _validate_channel(channel_id: str, channel_name: str) -> None:
    if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
        raise OverlayError("Invalid current channel ID")
    if not isinstance(channel_name, str) or not channel_name or len(channel_name) > MAX_CHANNEL_NAME:
        raise OverlayError("Invalid current channel name")


def _optional_count(value: Any, name: str) -> Optional[int]:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise OverlayError("Invalid " + name)
    return value


def snapshot_from_mapping(channel_id: str, channel_name: str, evidence: Optional[Mapping[str, Any]],
                          *, observed_at: Optional[float] = None) -> Optional[EvidenceSnapshot]:
    """Normalize one Lane A evidence row into the small overlay contract.

    Accepted keys: eventID/event_id, at/observedAt, language, languageName,
    count, threshold, phase/outcome, confidence.  No other fields are read.
    """
    _validate_channel(channel_id, channel_name)
    if evidence is None:
        return None
    if not isinstance(evidence, Mapping):
        raise OverlayError("Invalid evidence row")
    at = evidence.get("at", evidence.get("observedAt", observed_at))
    if at is None:
        at = time.time()
    if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
        raise OverlayError("Invalid evidence timestamp")
    language = evidence.get("language", "") or ""
    if language and (not isinstance(language, str) or not LANGUAGE_CODE.fullmatch(language)):
        raise OverlayError("Invalid evidence language")
    language_name = evidence.get("languageName", "") or language
    if not isinstance(language_name, str) or len(language_name) > MAX_LANGUAGE_NAME:
        raise OverlayError("Invalid evidence language name")
    phase = (evidence.get("phase", "") or evidence.get("state", "")
             or evidence.get("outcome", "") or "")
    if not isinstance(phase, str) or len(phase) > MAX_PHASE:
        raise OverlayError("Invalid evidence phase")
    detail = evidence.get("detail", evidence.get("reason", "")) or ""
    if not isinstance(detail, str) or len(detail) > 160:
        raise OverlayError("Invalid evidence detail")
    event_id = evidence.get("eventID", evidence.get("event_id", "")) or ""
    if not isinstance(event_id, str) or len(event_id) > MAX_EVENT_ID:
        raise OverlayError("Invalid evidence event ID")
    if not event_id:
        event_id = "evt-%s-%s-%s" % (channel_id, int(float(at) * 1000), phase or language or "pending")
    confidence = evidence.get("confidence")
    if confidence is not None:
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise OverlayError("Invalid evidence confidence")
        confidence = float(confidence)
    count = _optional_count(evidence.get("count"), "evidence count")
    threshold = _optional_count(evidence.get("threshold"), "evidence threshold")
    if threshold == 0:
        raise OverlayError("Invalid evidence threshold")

    def bounded_number(name, source, limit=86400.0):
        value = evidence.get(name, evidence.get(source))
        if value is None:
            return None
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= limit:
            raise OverlayError("Invalid evidence " + name)
        return float(value)

    seconds = bounded_number("seconds", "sampleSeconds", 86400.0)
    target = bounded_number("target", "targetSeconds", 86400.0)
    attempt = _optional_count(evidence.get("attempt"), "evidence attempt")
    max_attempts = _optional_count(evidence.get("maxAttempts"), "evidence max attempts")
    retry_seconds = bounded_number("retrySeconds", "retrySeconds", 86400.0)
    return EvidenceSnapshot(
        channel_id=channel_id, channel_name=channel_name, event_id=event_id,
        observed_at=float(at), language=language, language_name=language_name,
        count=count, threshold=threshold, phase=phase,
        confidence=confidence, outcome=str(evidence.get("outcome", "") or "")[:MAX_PHASE],
        detail=detail, seconds=seconds, target=target, attempt=attempt,
        max_attempts=max_attempts, retry_seconds=retry_seconds,
    )


def _seconds_text(value: Optional[float]) -> str:
    return "%.1fs" % value if value is not None else "?"


def _state_label(snapshot: EvidenceSnapshot) -> str:
    phase = snapshot.phase or ""
    detail = (snapshot.detail or "").strip()
    label = ""
    if phase == "collecting":
        label = "collecting %s/%s" % (
            _seconds_text(snapshot.seconds),
            _seconds_text(snapshot.target))
        if snapshot.attempt is not None:
            bounded = "" if snapshot.max_attempts is None else "/%d" % snapshot.max_attempts
            label += " · attempt %d%s" % (snapshot.attempt, bounded)
    elif phase == "inference":
        label = "inference running"
    elif phase in ("inconclusive", "retry"):
        label = "inconclusive"
        if detail:
            label += ": " + detail
        if snapshot.seconds is not None or snapshot.target is not None:
            label += " (%s/%s)" % (_seconds_text(snapshot.seconds), _seconds_text(snapshot.target))
        if snapshot.retry_seconds is not None:
            label += " · retry in %.0fs" % snapshot.retry_seconds
        elif snapshot.attempt is not None and snapshot.max_attempts is not None and snapshot.attempt >= snapshot.max_attempts:
            label += " · retry limit reached"
    elif phase in ("deferred", "deferred_pause", "deferred_paused"):
        label = "deferred: " + (detail or "paused")
        if snapshot.language_name or snapshot.language:
            label += " · " + (snapshot.language_name or snapshot.language)
    elif phase in ("no_audio", "no-audio"):
        label = "no audio"
        if detail and detail != "no_audio":
            label += ": " + detail
    elif phase in ("disabled", "observation_disabled"):
        label = "language detection disabled"
    elif phase in ("waiting", "pending", ""):
        label = "language evidence pending"
    else:
        label = "language evidence pending"
    return label


def format_snapshot(snapshot: EvidenceSnapshot) -> str:
    """One atomic label.  Channel name and observation time are always present."""
    stamp = _iso_timestamp(snapshot.observed_at)
    if not snapshot.language:
        if snapshot.phase:
            return _clip("%s · %s · %s" % (stamp, snapshot.channel_name, _state_label(snapshot)))
        return _clip("%s · %s · language evidence pending" % (stamp, snapshot.channel_name))
    count = "-" if snapshot.count is None else str(snapshot.count)
    threshold = "-" if snapshot.threshold is None else str(snapshot.threshold)
    phase = snapshot.phase or "sampling"
    if snapshot.detail and phase in ("deferred", "deferred_pause", "deferred_paused"):
        phase += " (" + snapshot.detail + ")"
    confidence = "-" if snapshot.confidence is None else "%.1f%%" % (snapshot.confidence * 100)
    return _clip("%s · %s · %s %s/%s · %s · confidence %s" % (
        stamp, snapshot.channel_name, snapshot.language_name or snapshot.language,
        count, threshold, phase, confidence))


def format_waiting(channel_name: str, at: float) -> str:
    return _clip("%s · %s · waiting for language evidence" % (_iso_timestamp(at), channel_name))


def resolve_binding(key: str, values: Mapping[str, str]) -> Optional[str]:
    """Mirror the native form-binding lookup used by hidden overlay fields."""
    if isinstance(key, str) and key.startswith("form."):
        return values.get(key[5:])
    return None


def binding_truth(value: Optional[str]) -> bool:
    return value is not None and value.lower() in ("true", "1", "yes", "on")


class OverlayBridge:
    """Deduplicated, rate-bounded, retry-bounded overlay publisher.

    Call these methods from the gateway's existing catalogue/playback/poll
    boundaries.  There is no timer and no background worker.
    """

    def __init__(self, transport: DeviceCommandTransport, *, field: str = OVERLAY_FIELD,
                 min_interval: float = 0.5, max_retries: int = 2,
                 retry_base: float = 0.25, pending_ttl: float = 30.0,
                 clock: Callable[[], float] = time.monotonic,
                 wallclock: Callable[[], float] = time.time):
        if not isinstance(field, str) or not FIELD_ID.fullmatch(field):
            raise OverlayError("Invalid overlay field")
        if type(min_interval) not in (int, float) or not math.isfinite(min_interval) or min_interval < 0:
            raise OverlayError("Invalid overlay minimum interval")
        if type(max_retries) is not int or not 0 <= max_retries <= 5:
            raise OverlayError("Invalid overlay retry bound")
        if type(retry_base) not in (int, float) or not math.isfinite(retry_base) or retry_base < 0:
            raise OverlayError("Invalid overlay retry delay")
        if type(pending_ttl) not in (int, float) or not math.isfinite(pending_ttl) or pending_ttl <= 0:
            raise OverlayError("Invalid overlay pending lifetime")
        self._transport = transport
        self.field = field
        self.min_interval = float(min_interval)
        self.max_retries = max_retries
        self.retry_base = float(retry_base)
        self.pending_ttl = float(pending_ttl)
        self._clock = clock
        self._wallclock = wallclock
        self.current_channel_id = ""
        self.current_channel_name = ""
        self.generation = 0
        self.latest_event_id = ""
        self.latest_snapshot: Optional[EvidenceSnapshot] = None
        self.latest_state: Optional[EvidenceSnapshot] = None
        self.latest_display_at = float("-inf")
        self.last_published_text = ""
        self.last_published_event_id = ""
        self.last_published_channel_id = ""
        self.last_status = "idle"
        self.fault = ""
        self._pending: Optional[dict[str, Any]] = None
        self._last_attempt_at = float("-inf")
        self._corrected_events: set[str] = set()

    def status(self) -> dict[str, Any]:
        return {
            "revision": OVERLAY_REVISION,
            "channelID": self.current_channel_id,
            "channelName": self.current_channel_name,
            "generation": self.generation,
            "latestEventID": self.latest_event_id,
            "latestState": self.latest_state.phase if self.latest_state is not None else "",
            "publishedText": self.last_published_text,
            "publishedChannelID": self.last_published_channel_id,
            "staleLabel": bool(self.last_published_channel_id
                               and self.last_published_channel_id != self.current_channel_id),
            "pending": self._pending is not None,
            "status": self.last_status,
            "fault": self.fault,
        }

    def observe_channel(self, channel_id: str, channel_name: str, *, at: Optional[float] = None,
                        now: Optional[float] = None) -> str:
        """Replace any prior label immediately when the gateway channel changes."""
        _validate_channel(channel_id, channel_name)
        mono = self._clock() if now is None else now
        wall = self._wallclock() if at is None else at
        if channel_id == self.current_channel_id:
            self.current_channel_name = channel_name
            if self._pending is None and not self.last_published_text:
                self._enqueue(format_waiting(channel_name, wall), "", channel_id, mono, immediate=True)
                return self._pump(mono)
            return "unchanged"
        self.current_channel_id = channel_id
        self.current_channel_name = channel_name
        self.generation += 1
        self.latest_event_id = ""
        self.latest_snapshot = None
        self.latest_state = None
        self.latest_display_at = float("-inf")
        self._enqueue(format_waiting(channel_name, wall), "", channel_id, mono, immediate=True)
        return self._pump(mono)

    def clear(self, *, channel_id: Optional[str] = None, channel_name: Optional[str] = None,
              at: Optional[float] = None, now: Optional[float] = None) -> str:
        """Clear stale evidence while keeping the current channel label."""
        cid = self.current_channel_id if channel_id is None else channel_id
        name = self.current_channel_name if channel_name is None else channel_name
        _validate_channel(cid, name)
        mono = self._clock() if now is None else now
        wall = self._wallclock() if at is None else at
        if cid != self.current_channel_id or name != self.current_channel_name:
            self.current_channel_id = cid
            self.current_channel_name = name
            self.generation += 1
        self.latest_event_id = ""
        self.latest_snapshot = None
        self.latest_state = None
        self.latest_display_at = float("-inf")
        self._enqueue(format_waiting(name, wall), "", cid, mono, immediate=True)
        return self._pump(mono)

    def publish_evidence(self, channel_id: str, channel_name: str, evidence: Optional[Mapping[str, Any]],
                         *, at: Optional[float] = None, now: Optional[float] = None) -> str:
        """Publish or replace the current channel's evidence.

        Evidence for an older channel or an older event is rejected without
        touching the last published label.
        """
        mono = self._clock() if now is None else now
        wall = self._wallclock() if at is None else at
        if not self.current_channel_id:
            self.observe_channel(channel_id, channel_name, at=wall, now=mono)
        if channel_id != self.current_channel_id:
            self.last_status = "suppressed_channel"
            return self.last_status
        self.current_channel_name = channel_name
        snapshot = snapshot_from_mapping(channel_id, channel_name, evidence, observed_at=wall)
        if snapshot is None:
            if self.latest_event_id:
                self.latest_event_id = ""
                self.latest_snapshot = None
            self._enqueue(format_waiting(channel_name, wall), "", channel_id, mono, immediate=False)
            return self._pump(mono)
        if snapshot.event_id == self.latest_event_id:
            self.last_status = "duplicate"
            return self.last_status
        if snapshot.observed_at < self.latest_display_at:
            self.last_status = "stale"
            return self.last_status
        self.latest_event_id = snapshot.event_id
        self.latest_snapshot = snapshot
        self.latest_display_at = max(self.latest_display_at, snapshot.observed_at)
        self._enqueue(format_snapshot(snapshot), snapshot.event_id, channel_id, mono, immediate=False)
        return self._pump(mono)

    def publish_state(self, channel_id: str, channel_name: str, state: Optional[Mapping[str, Any]],
                      *, at: Optional[float] = None, now: Optional[float] = None) -> str:
        """Publish a transient sampler state without replacing evidence.

        A collecting/inference/inconclusive/retry/deferred/no-audio/disabled
        label is truthful for the current visit, but it is not evidence.  Keep
        the latest verified evidence snapshot separate so the manual correction
        control still targets the last real event.  A stale channel, visit or
        stream event must never overwrite a newer label.
        """
        mono = self._clock() if now is None else now
        wall = self._wallclock() if at is None else at
        if not self.current_channel_id:
            self.observe_channel(channel_id, channel_name, at=wall, now=mono)
        if channel_id != self.current_channel_id:
            self.last_status = "suppressed_channel"
            return self.last_status
        self.current_channel_name = channel_name
        snapshot = snapshot_from_mapping(channel_id, channel_name, state, observed_at=wall)
        if snapshot is None:
            self.last_status = "invalid"
            return self.last_status
        if self.latest_state is not None and snapshot.event_id == self.latest_state.event_id:
            self.last_status = "duplicate"
            return self.last_status
        if snapshot.observed_at < self.latest_display_at:
            self.last_status = "stale"
            return self.last_status
        self.latest_state = snapshot
        self.latest_display_at = max(self.latest_display_at, snapshot.observed_at)
        self._enqueue(format_snapshot(snapshot), snapshot.event_id, channel_id, mono, immediate=False)
        return self._pump(mono)

    def pump(self, now: Optional[float] = None) -> str:
        return self._pump(self._clock() if now is None else now)

    def _enqueue(self, text: str, event_id: str, channel_id: str, now: float, *, immediate: bool) -> None:
        text = _clip(text)
        if (self._pending is not None and self._pending["text"] == text
                and self._pending["event_id"] == event_id):
            return
        if self._pending is None and text == self.last_published_text and event_id == self.last_published_event_id:
            return
        self._pending = {
            "text": text, "event_id": event_id, "channel_id": channel_id,
            "generation": self.generation, "created_at": now, "attempts": 0,
            "retry_at": float("-inf"), "immediate": immediate,
        }

    def _pump(self, now: float) -> str:
        pending = self._pending
        if pending is None:
            self.last_status = "idle"
            return "idle"
        if pending["channel_id"] != self.current_channel_id or pending["generation"] != self.generation:
            self._pending = None
            self.last_status = "suppressed_channel"
            return self.last_status
        if now - pending["created_at"] > self.pending_ttl:
            self._pending = None
            self.fault = "stale_pending"
            self.last_status = "stale"
            return "stale"
        if now < pending["retry_at"]:
            self.last_status = "deferred"
            return "deferred"
        if not pending["immediate"] and now - self._last_attempt_at < self.min_interval:
            self.last_status = "rate_limited"
            return "rate_limited"
        pending["immediate"] = False
        self._last_attempt_at = now
        pending["attempts"] += 1
        try:
            self._transport.set_value(self.field, pending["text"])
        except Exception as error:  # transport is external and may fail freely
            self.fault = "transport_unavailable:" + type(error).__name__
            if pending["attempts"] > self.max_retries:
                self._pending = None
                self.last_status = "failed"
                return "failed"
            delay = self.retry_base * (2 ** (pending["attempts"] - 1))
            pending["retry_at"] = now + delay
            self.last_status = "retry"
            return "retry"
        self.last_published_text = pending["text"]
        self.last_published_event_id = pending["event_id"]
        self.last_published_channel_id = pending["channel_id"]
        self._pending = None
        self.fault = ""
        self.last_status = "published"
        return "published"

    def manual_correction(self, channel_id: str, channel_name: str, nonce: str, *,
                          now: Optional[float] = None) -> dict[str, Any]:
        """Build a Lane A v4 ``mistaken_english`` marker for the exact label.

        The caller must apply it through the existing gateway marker path and
        then call ``confirm_correction`` only after Lane A accepted it.  The
        native field cannot compare event IDs; this gate checks the adapter's
        own latest published event and current channel before emitting one.
        """
        _validate_channel(channel_id, channel_name)
        if channel_id != self.current_channel_id:
            raise ManualCorrectionError("Current channel changed")
        if channel_name != self.current_channel_name:
            raise ManualCorrectionError("Current channel name changed")
        snapshot = self.latest_snapshot
        if snapshot is None or not snapshot.event_id:
            raise ManualCorrectionError("No latest evidence to correct")
        if snapshot.channel_id != channel_id:
            raise ManualCorrectionError("Latest evidence belongs to another channel")
        if snapshot.event_id in self._corrected_events:
            raise AlreadyCorrected("Latest evidence was already corrected")
        if not isinstance(nonce, str) or not nonce or len(nonce) > MAX_NONCE:
            raise ManualCorrectionError("Invalid correction nonce")
        return {
            "schemaVersion": 2,
            "action": "mistaken_english",
            "channelID": channel_id,
            "channelName": channel_name,
            "nonce": nonce,
        }

    def confirm_correction(self, event_id: Optional[str] = None) -> None:
        target = self.latest_event_id if event_id is None else event_id
        if target:
            self._corrected_events.add(target)


def validate_layout(layout: Mapping[str, Any]) -> None:
    """Validate the same bounded UI-layout subset enforced by native ``checked()``.

    This is a deployment preflight for the JSON asset, not a replacement for
    the native validator.  It intentionally accepts a non-editable text node
    sharing a ``form.`` key with the hidden editable field.
    """
    if not isinstance(layout, Mapping):
        raise OverlayError("Layout is not an object")
    if layout.get("schemaVersion") != 1 or not isinstance(layout.get("revision"), str) or not FIELD_ID.fullmatch(layout["revision"]):
        raise OverlayError("Invalid layout header")
    slots = layout.get("slots")
    pages = layout.get("pages")
    if not isinstance(slots, list) or not isinstance(pages, list) or len(slots) > len(LAYOUT_LOCATIONS) or len(pages) > 12:
        raise OverlayError("Invalid layout slot/page count")
    locations = [slot.get("location") for slot in slots if isinstance(slot, Mapping)]
    if len(locations) != len(slots) or len(set(locations)) != len(locations) or not set(locations).issubset(LAYOUT_LOCATIONS):
        raise OverlayError("Invalid or duplicate slot location")
    page_ids = set()
    for page in pages:
        if not isinstance(page, Mapping) or not isinstance(page.get("id"), str) or not FIELD_ID.fullmatch(page["id"]):
            raise OverlayError("Invalid page ID")
        if page["id"] in page_ids:
            raise OverlayError("Duplicate page ID")
        page_ids.add(page["id"])
        if not isinstance(page.get("title"), str) or not page["title"] or len(page["title"]) > 100:
            raise OverlayError("Invalid page title")
    seen_ids: set[str] = set()
    editable_fields: set[str] = set()
    count = 0

    def walk(nodes: Any, depth: int) -> None:
        nonlocal count
        if not isinstance(nodes, list) or depth > 7 or len(nodes) > 60:
            raise OverlayError("Invalid node tree")
        for node in nodes:
            count += 1
            if count > 250 or not isinstance(node, Mapping):
                raise OverlayError("Too many UI nodes")
            node_id = node.get("id")
            kind = node.get("kind")
            if not isinstance(node_id, str) or not FIELD_ID.fullmatch(node_id) or node_id in seen_ids:
                raise OverlayError("Invalid or duplicate UI node")
            seen_ids.add(node_id)
            if kind not in LAYOUT_KINDS:
                raise OverlayError("Invalid UI kind")
            if not isinstance(node.get("title", ""), str) or len(node.get("title", "")) > 512:
                raise OverlayError("Invalid UI title")
            if not isinstance(node.get("detail", ""), str) or len(node.get("detail", "")) > 2048:
                raise OverlayError("Invalid UI detail")
            if node.get("style", "normal") not in STYLES:
                raise OverlayError("Invalid UI style")
            if len(str(node.get("defaultValue", "")).encode("utf-8")) > 4096:
                raise OverlayError("Invalid UI default value")
            for binding in (node.get("valueKey"), node.get("visibleWhen"), node.get("enabledWhen")):
                if binding is not None and (not isinstance(binding, str) or not BINDING.fullmatch(binding)):
                    raise OverlayError("Invalid UI binding")
            if kind in EDITABLE_KINDS:
                key = node.get("valueKey")
                if not isinstance(key, str) or not key.startswith("form.") or key.count(".") != 1:
                    raise OverlayError("Editable control requires one form key")
                if key in editable_fields:
                    raise OverlayError("Duplicate editable form field")
                editable_fields.add(key)
                if len(editable_fields) > 64:
                    raise OverlayError("Too many form fields")
            if kind in ("picker", "segmented", "menu"):
                options = node.get("options") or []
                if not options or len(options) > 40:
                    raise OverlayError("Invalid picker options")
                values = [option.get("value") for option in options if isinstance(option, Mapping)]
                if len(values) != len(options) or len(set(values)) != len(values):
                    raise OverlayError("Invalid picker options")
                for option in options:
                    if not isinstance(option.get("value"), str) or len(option["value"].encode("utf-8")) > 512:
                        raise OverlayError("Invalid picker option value")
                    if not isinstance(option.get("title"), str) or not option["title"] or len(option["title"]) > 100:
                        raise OverlayError("Invalid picker option title")
            if kind in ("slider", "progress"):
                low = node.get("minimum", 0)
                high = node.get("maximum", 100)
                step = node.get("step", 1)
                if (type(low) not in (int, float) or type(high) not in (int, float)
                        or not math.isfinite(low) or not math.isfinite(high) or high <= low
                        or high - low > 1e9 or type(step) not in (int, float) or step <= 0):
                    raise OverlayError("Invalid numeric range")
            html = node.get("html")
            height = node.get("height")
            if kind == "web":
                if not isinstance(html, str) or not html or len(html.encode("utf-8")) > 65536:
                    raise OverlayError("Invalid custom HTML")
                if type(height) not in (int, float) or not math.isfinite(height) or not 80 <= height <= 1200:
                    raise OverlayError("Invalid custom HTML height")
            elif html is not None or height is not None:
                raise OverlayError("HTML and height belong to web components")
            action = node.get("action")
            if action is not None:
                if kind != "button" or not isinstance(action, Mapping):
                    raise OverlayError("Invalid UI action")
                if action.get("command") not in LAYOUT_COMMANDS:
                    raise OverlayError("Invalid UI command")
                arguments = action.get("arguments") or {}
                if not isinstance(arguments, Mapping) or len(arguments) > 32:
                    raise OverlayError("Invalid UI arguments")
                for key, value in arguments.items():
                    if not isinstance(key, str) or len(key) > 128 or not isinstance(value, str) or len(value.encode("utf-8")) > 65536:
                        raise OverlayError("Invalid UI argument")
            if kind == "button" and action is None:
                raise OverlayError("Button requires an action")
            children = node.get("children")
            if children is not None and children:
                if kind not in CONTAINER_KINDS:
                    raise OverlayError("Only containers may have children")
                walk(children, depth + 1)

    for slot in slots:
        walk(slot.get("nodes"), 0)
    for page in pages:
        walk(page.get("nodes"), 0)
