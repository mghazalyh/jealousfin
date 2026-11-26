"""Evidence-first language store keyed by exact provider identity.

Schema v4 keeps the manual membership filter from v3 but no longer excludes a
channel on the first confident detection.  Instead, every distinct genuine
playback visit contributes one bounded evidence event.  Per-channel,
per-language counts persist independently of the evidence window so the
operator can inspect observation history after older event records have been
pruned.

Automatic exclusion is an explicit, independent enforcement gate: when
enforcement is enabled and the same unwanted language reaches the configured
threshold (25 by default), an *automatic* entry is added.  Observation-only
mode records counts and never touches membership.  Manual Spanish entries and
manual "not Spanish" allow decisions always win.  There is no automatic
whitelist: an English visit is evidence, not a permanent permission.

The store is still a membership filter only: it is never a policy failure, ban
or hiatus, and never mutates rotation, the visited set, Favorites or playback
history.
"""
import hashlib
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

import language_catalog

SCHEMA_VERSION = 4
REVISION = "language-exclusions-v4"
FILE_NAME = "language-exclusions.json"
LEGACY_FILE = "spanish-classification.json"
MAX_ENTRIES = 2000
MAX_DECISIONS = 4000
MAX_NONCE = 128
MAX_NONCES = 128
MAX_FILE = 8 * 1024 * 1024
MAX_REASON = 80
MAX_EVIDENCE_TEXT = 80
MAX_EVENTS = 5000
MAX_COUNT_CHANNELS = 2000
MAX_COUNT = 1000000
DEFAULT_THRESHOLD = 25
MIN_THRESHOLD = 1
MAX_THRESHOLD = 1000
DEFAULT_DEDUP_SECONDS = 600
PROVIDER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
CHANNEL_ID = re.compile(r"^[a-f0-9]{32}$")
LANG_CODE = re.compile(r"^[a-z]{2,3}$")
MARKER_KEYS = {"schemaVersion", "action", "channelID", "channelName", "nonce"}
STATE_KEYS = {"schemaVersion", "revision", "updatedAt", "lastNonce", "recentNonces", "entries", "decisions", "evidence"}
ENTRY_KEYS = {"channelID", "channelName", "markedAt", "nonce", "source", "language", "languageName", "confidence", "reason", "evidence"}
DECISION_KEYS = {"decision", "language", "languageName", "at", "nonce", "channelID", "channelName"}
EVIDENCE_KEYS = {
    "model", "windows", "speechSeconds", "sampleSeconds", "speechCoverage",
    "languages", "deviceVerified", "channelChangedBeforeResult", "lexicalMargin",
}
LANGUAGES_KEYS = {"language", "probability"}
EVIDENCE_STATE_KEYS = {"schemaVersion", "updatedAt", "events", "counts"}
EVIDENCE_EVENT_KEYS = {
    "eventID", "channelID", "channelName", "providerID", "language",
    "languageName", "confidence", "reason", "at", "source", "outcome",
    "visitID", "launchID", "streamGeneration", "deviceVerified",
    "channelChangedBeforeResult", "model", "windows", "speechSeconds",
    "sampleSeconds", "speechCoverage", "lexicalMargin", "retractedAt",
    "retractedBy", "retracts",
}
SOURCES = ("manual", "auto")
DECISIONS = ("spanish", "not_spanish")
OUTCOMES = ("excluded", "allowed", "unauthorized")
MARKER_ACTIONS = {"add", "remove", "mistaken_english"}


def empty():
    return {
        "schemaVersion": SCHEMA_VERSION,
        "revision": REVISION,
        "updatedAt": 0,
        "lastNonce": "",
        "recentNonces": [],
        "entries": {},
        "decisions": {},
        "evidence": {
            "schemaVersion": 1,
            "updatedAt": 0,
            "events": {},
            "counts": {},
        },
    }


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _probability(value):
    return value is None or (type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1)


def _validate_evidence(evidence):
    if evidence is None:
        return
    if not isinstance(evidence, dict) or not set(evidence).issubset(EVIDENCE_KEYS):
        raise ValueError("Invalid language auto evidence")
    model = evidence.get("model")
    if model is not None and (not isinstance(model, str) or not model or len(model) > MAX_EVIDENCE_TEXT):
        raise ValueError("Invalid language auto evidence model")
    windows = evidence.get("windows")
    if windows is not None and (type(windows) is not int or not 0 <= windows <= 32):
        raise ValueError("Invalid language auto window count")
    for key in ("speechSeconds", "sampleSeconds"):
        value = evidence.get(key)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 600):
            raise ValueError("Invalid language auto duration")
    coverage = evidence.get("speechCoverage")
    if coverage is not None and (type(coverage) not in (int, float) or not math.isfinite(coverage) or not 0 <= coverage <= 1):
        raise ValueError("Invalid language auto coverage")
    margin = evidence.get("lexicalMargin")
    if margin is not None and (type(margin) not in (int, float) or not math.isfinite(margin) or not -1 <= margin <= 1):
        raise ValueError("Invalid language lexical margin")
    for key in ("deviceVerified", "channelChangedBeforeResult"):
        value = evidence.get(key)
        if value is not None and type(value) is not bool:
            raise ValueError("Invalid language auto verification flag")
    languages = evidence.get("languages")
    if languages is not None:
        if not isinstance(languages, list) or len(languages) > 12:
            raise ValueError("Invalid language auto language votes")
        for vote in languages:
            if not isinstance(vote, dict) or set(vote) != LANGUAGES_KEYS:
                raise ValueError("Invalid language auto language vote")
            if not isinstance(vote["language"], str) or not LANG_CODE.fullmatch(vote["language"]):
                raise ValueError("Invalid language auto language")
            if not _probability(vote["probability"]) or vote["probability"] is None:
                raise ValueError("Invalid language auto language probability")


def _validate_event(event):
    if not isinstance(event, dict) or set(event) != EVIDENCE_EVENT_KEYS:
        raise ValueError("Invalid language evidence event")
    if not isinstance(event["eventID"], str) or not event["eventID"] or len(event["eventID"]) > 100:
        raise ValueError("Invalid language evidence identity")
    if not isinstance(event["channelID"], str) or not CHANNEL_ID.fullmatch(event["channelID"]):
        raise ValueError("Invalid language evidence channel")
    if not isinstance(event["channelName"], str) or len(event["channelName"]) > 160:
        raise ValueError("Invalid language evidence name")
    if not isinstance(event["providerID"], str) or not PROVIDER_ID.fullmatch(event["providerID"]):
        raise ValueError("Invalid language evidence provider")
    if not isinstance(event["language"], str) or not LANG_CODE.fullmatch(event["language"]):
        raise ValueError("Invalid language evidence language")
    if event["languageName"] != language_catalog.language_name(event["language"]):
        raise ValueError("Invalid language evidence language name")
    if not _probability(event["confidence"]):
        raise ValueError("Invalid language evidence confidence")
    if not isinstance(event["reason"], str) or not event["reason"] or len(event["reason"]) > MAX_REASON:
        raise ValueError("Invalid language evidence reason")
    if not _number(event["at"]):
        raise ValueError("Invalid language evidence timestamp")
    if event["source"] not in SOURCES:
        raise ValueError("Invalid language evidence provenance")
    if event["outcome"] not in OUTCOMES:
        raise ValueError("Invalid language evidence outcome")
    category = language_catalog.classify(event["language"])
    if event["outcome"] == "excluded" and category != "excluded":
        raise ValueError("Invalid excluded-language evidence")
    if event["outcome"] == "allowed" and category != "allowed":
        raise ValueError("Invalid allowed-language evidence")
    if event["outcome"] == "unauthorized" and category != "unauthorized":
        raise ValueError("Invalid unauthorized-language evidence")
    for key in ("visitID", "launchID", "streamGeneration", "retractedBy", "retracts"):
        value = event[key]
        if not isinstance(value, str) or len(value) > 200:
            raise ValueError("Invalid language evidence session identity")
    if event["retractedAt"] is not None and not _number(event["retractedAt"]):
        raise ValueError("Invalid language evidence retraction")
    for key in ("deviceVerified", "channelChangedBeforeResult"):
        value = event[key]
        if value is not None and type(value) is not bool:
            raise ValueError("Invalid language evidence verification flag")
    model = event["model"]
    if model is not None and (not isinstance(model, str) or len(model) > MAX_EVIDENCE_TEXT):
        raise ValueError("Invalid language evidence model")
    windows = event["windows"]
    if windows is not None and (type(windows) is not int or not 0 <= windows <= 32):
        raise ValueError("Invalid language evidence window count")
    for key in ("speechSeconds", "sampleSeconds"):
        value = event[key]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 600):
            raise ValueError("Invalid language evidence duration")
    coverage = event["speechCoverage"]
    if coverage is not None and (type(coverage) not in (int, float) or not math.isfinite(coverage) or not 0 <= coverage <= 1):
        raise ValueError("Invalid language evidence coverage")
    margin = event["lexicalMargin"]
    if margin is not None and (type(margin) not in (int, float) or not math.isfinite(margin) or not -1 <= margin <= 1):
        raise ValueError("Invalid language evidence lexical margin")


def _validate_evidence_state(evidence):
    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_STATE_KEYS:
        raise ValueError("Invalid language evidence state")
    if evidence["schemaVersion"] != 1 or not _number(evidence["updatedAt"]):
        raise ValueError("Unknown language evidence schema")
    events = evidence["events"]
    if not isinstance(events, dict) or len(events) > MAX_EVENTS:
        raise ValueError("Invalid language evidence events")
    for event_id, event in events.items():
        if not isinstance(event_id, str) or event_id != event.get("eventID"):
            raise ValueError("Invalid language evidence event key")
        _validate_event(event)
    counts = evidence["counts"]
    if not isinstance(counts, dict) or len(counts) > MAX_COUNT_CHANNELS:
        raise ValueError("Invalid language evidence counts")
    for channel_id, languages in counts.items():
        if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
            raise ValueError("Invalid language evidence count channel")
        if not isinstance(languages, dict) or len(languages) > len(language_catalog.SUPPORTED):
            raise ValueError("Invalid language evidence count languages")
        for code, count in languages.items():
            if not isinstance(code, str) or not LANG_CODE.fullmatch(code):
                raise ValueError("Invalid language evidence count language")
            if type(count) is not int or not 0 <= count <= MAX_COUNT:
                raise ValueError("Invalid language evidence count")


def validate(data):
    if not isinstance(data, dict) or set(data) != STATE_KEYS:
        raise ValueError("Invalid language exclusion state")
    if data["schemaVersion"] != SCHEMA_VERSION or data["revision"] != REVISION:
        raise ValueError("Unknown language exclusion schema")
    if not _number(data["updatedAt"]):
        raise ValueError("Invalid language exclusion timestamp")
    if not isinstance(data["lastNonce"], str) or len(data["lastNonce"]) > MAX_NONCE:
        raise ValueError("Invalid language exclusion nonce")
    nonces = data["recentNonces"]
    if not isinstance(nonces, list) or len(nonces) > MAX_NONCES:
        raise ValueError("Invalid language nonce history")
    seen = set()
    for nonce in nonces:
        if not isinstance(nonce, str) or not nonce or len(nonce) > MAX_NONCE or nonce in seen:
            raise ValueError("Invalid language nonce history entry")
        seen.add(nonce)
    entries = data["entries"]
    if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
        raise ValueError("Invalid language exclusion entries")
    for provider_id, row in entries.items():
        if not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id):
            raise ValueError("Invalid language provider identity")
        if not isinstance(row, dict) or set(row) != ENTRY_KEYS:
            raise ValueError("Invalid language exclusion row")
        if not isinstance(row["channelID"], str) or not CHANNEL_ID.fullmatch(row["channelID"]):
            raise ValueError("Invalid language channel identity")
        if not isinstance(row["channelName"], str) or len(row["channelName"]) > 160:
            raise ValueError("Invalid language channel name")
        if not _number(row["markedAt"]):
            raise ValueError("Invalid language mark timestamp")
        if not isinstance(row["nonce"], str) or not row["nonce"] or len(row["nonce"]) > MAX_NONCE:
            raise ValueError("Invalid language row nonce")
        if row["source"] not in SOURCES:
            raise ValueError("Invalid language provenance")
        if not isinstance(row["language"], str) or not language_catalog.is_excluded_code(row["language"]):
            raise ValueError("Invalid excluded language")
        if row["languageName"] != language_catalog.language_name(row["language"]):
            raise ValueError("Invalid language name")
        if not _probability(row["confidence"]):
            raise ValueError("Invalid language confidence")
        if row["source"] == "manual" and row["confidence"] is not None:
            raise ValueError("Manual language confidence must be empty")
        if not isinstance(row["reason"], str) or not row["reason"] or len(row["reason"]) > MAX_REASON:
            raise ValueError("Invalid language reason")
        _validate_evidence(row["evidence"])
    decisions = data["decisions"]
    if not isinstance(decisions, dict) or len(decisions) > MAX_DECISIONS:
        raise ValueError("Invalid language decisions")
    for provider_id, row in decisions.items():
        if not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id):
            raise ValueError("Invalid language decision provider")
        if not isinstance(row, dict) or set(row) != DECISION_KEYS:
            raise ValueError("Invalid language decision")
        if row["decision"] not in DECISIONS:
            raise ValueError("Invalid language decision value")
        if row["decision"] == "spanish":
            if row["language"] != "es" or row["languageName"] != "Spanish":
                raise ValueError("Invalid Spanish decision language")
        elif row["language"] != "all" or row["languageName"] != "Any language":
            raise ValueError("Invalid allow decision language")
        if not _number(row["at"]):
            raise ValueError("Invalid language decision timestamp")
        if not isinstance(row["nonce"], str) or not row["nonce"] or len(row["nonce"]) > MAX_NONCE:
            raise ValueError("Invalid language decision nonce")
        if not isinstance(row["channelID"], str) or not CHANNEL_ID.fullmatch(row["channelID"]):
            raise ValueError("Invalid language decision channel")
        if not isinstance(row["channelName"], str) or not row["channelName"] or len(row["channelName"]) > 160:
            raise ValueError("Invalid language decision name")
        if row["decision"] == "not_spanish" and provider_id in entries:
            raise ValueError("Language allow decision cannot coexist with a mark")
    _validate_evidence_state(data["evidence"])
    return data


def _legacy_entry(row):
    if "source" in row:
        source = row["source"]
        confidence = row["confidence"]
        reason = row["reason"]
        evidence = row["evidence"]
    else:
        source = "manual"
        confidence = None
        reason = "manual-legacy-v1"
        evidence = None
    return {
        "channelID": row["channelID"], "channelName": row["channelName"],
        "markedAt": row["markedAt"], "nonce": row["nonce"], "source": source,
        "language": "es", "languageName": "Spanish", "confidence": confidence,
        "reason": reason, "evidence": evidence,
    }


def _legacy_decision(row, allow=False):
    if allow:
        return {
            "decision": "not_spanish", "language": "all", "languageName": "Any language",
            "at": row.get("at"), "nonce": row.get("nonce"),
            "channelID": row.get("channelID"), "channelName": row.get("channelName"),
        }
    return {
        "decision": "spanish", "language": "es", "languageName": "Spanish",
        "at": row.get("at"), "nonce": row.get("nonce"),
        "channelID": row.get("channelID"), "channelName": row.get("channelName"),
    }


def _legacy_v3(data):
    """Validate the previous production schema before migrating it."""
    keys = {"schemaVersion", "revision", "updatedAt", "lastNonce", "recentNonces", "entries", "decisions"}
    if set(data) != keys or data.get("revision") != "language-exclusions-v3":
        raise ValueError("Invalid legacy language state")
    if not _number(data.get("updatedAt")):
        raise ValueError("Invalid legacy language timestamp")
    entries = data.get("entries")
    decisions = data.get("decisions")
    if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
        raise ValueError("Invalid legacy language entries")
    for provider_id, row in entries.items():
        if not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id):
            raise ValueError("Invalid legacy language provider")
        if not isinstance(row, dict) or set(row) != ENTRY_KEYS:
            raise ValueError("Invalid legacy language row")
        if not isinstance(row["channelID"], str) or not CHANNEL_ID.fullmatch(row["channelID"]):
            raise ValueError("Invalid legacy language channel")
        if not isinstance(row["channelName"], str) or len(row["channelName"]) > 160:
            raise ValueError("Invalid legacy language name")
        if not _number(row["markedAt"]):
            raise ValueError("Invalid legacy language mark timestamp")
        if not isinstance(row["nonce"], str) or not row["nonce"] or len(row["nonce"]) > MAX_NONCE:
            raise ValueError("Invalid legacy language nonce")
        if row["source"] not in SOURCES:
            raise ValueError("Invalid legacy language source")
        if not isinstance(row["language"], str) or not language_catalog.is_excluded_code(row["language"]):
            raise ValueError("Invalid legacy excluded language")
        if row["languageName"] != language_catalog.language_name(row["language"]):
            raise ValueError("Invalid legacy language name")
        if not _probability(row["confidence"]):
            raise ValueError("Invalid legacy language confidence")
        if not isinstance(row["reason"], str) or not row["reason"] or len(row["reason"]) > MAX_REASON:
            raise ValueError("Invalid legacy language reason")
        _validate_evidence(row["evidence"])
    if not isinstance(decisions, dict) or len(decisions) > MAX_DECISIONS:
        raise ValueError("Invalid legacy language decisions")
    for provider_id, row in decisions.items():
        if not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id):
            raise ValueError("Invalid legacy language decision provider")
        if not isinstance(row, dict) or set(row) != DECISION_KEYS:
            raise ValueError("Invalid legacy language decision")
        if row["decision"] not in DECISIONS:
            raise ValueError("Invalid legacy language decision value")
        if not _number(row["at"]):
            raise ValueError("Invalid legacy language decision timestamp")
        if not isinstance(row["nonce"], str) or not row["nonce"] or len(row["nonce"]) > MAX_NONCE:
            raise ValueError("Invalid legacy language decision nonce")
        if not isinstance(row["channelID"], str) or not CHANNEL_ID.fullmatch(row["channelID"]):
            raise ValueError("Invalid legacy language decision channel")
        if not isinstance(row["channelName"], str) or not row["channelName"] or len(row["channelName"]) > 160:
            raise ValueError("Invalid legacy language decision name")
    return data


def _event_from_legacy_entry(provider_id, row):
    evidence = row["evidence"] if isinstance(row["evidence"], dict) else {}
    confidence = row["confidence"] if _probability(row["confidence"]) else 0.0
    if confidence is None:
        confidence = 0.0
    event_id = "legacy-" + hashlib.sha256(
        (provider_id + "|" + row["channelID"] + "|" + row["language"] + "|" + row["nonce"]).encode("utf-8")
    ).hexdigest()[:32]
    return {
        "eventID": event_id,
        "channelID": row["channelID"], "channelName": row["channelName"],
        "providerID": provider_id, "language": row["language"],
        "languageName": row["languageName"], "confidence": confidence,
        "reason": row["reason"], "at": row["markedAt"], "source": "auto",
        "outcome": "excluded", "visitID": "legacy:" + row["nonce"],
        "launchID": "", "streamGeneration": "",
        "deviceVerified": evidence.get("deviceVerified"),
        "channelChangedBeforeResult": evidence.get("channelChangedBeforeResult"),
        "model": evidence.get("model"), "windows": evidence.get("windows"),
        "speechSeconds": evidence.get("speechSeconds"),
        "sampleSeconds": evidence.get("sampleSeconds"),
        "speechCoverage": evidence.get("speechCoverage"),
        "lexicalMargin": evidence.get("lexicalMargin"),
        "retractedAt": None, "retractedBy": "", "retracts": "",
    }


def _bump_count(state, channel_id, language):
    counts = state["evidence"]["counts"]
    if channel_id not in counts:
        if len(counts) >= MAX_COUNT_CHANNELS:
            oldest = min(counts, key=lambda cid: max(counts[cid].values() or [0])) if counts else None
            if oldest is not None:
                counts.pop(oldest, None)
        counts[channel_id] = {}
    row = counts[channel_id]
    row[language] = min(MAX_COUNT, row.get(language, 0) + 1)


def _drop_count(state, channel_id, language):
    counts = state["evidence"]["counts"]
    row = counts.get(channel_id)
    if not row:
        return
    value = row.get(language, 0)
    if value <= 1:
        row.pop(language, None)
    else:
        row[language] = value - 1
    if not row:
        counts.pop(channel_id, None)


def _remember_nonce(state, nonce):
    history = [value for value in state["recentNonces"] if value != nonce]
    history.insert(0, nonce)
    state["recentNonces"] = history[:MAX_NONCES]
    state["lastNonce"] = nonce


def _validate_identity(channel_id, channel_name, provider_id, nonce=None):
    if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
        raise ValueError("Invalid language classification channel")
    if not isinstance(channel_name, str) or not channel_name or len(channel_name) > 160:
        raise ValueError("Invalid language classification name")
    if nonce is not None and (not isinstance(nonce, str) or not nonce or len(nonce) > MAX_NONCE):
        raise ValueError("Invalid language classification nonce")
    if provider_id is not None and (not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id)):
        raise ValueError("Invalid language provider identity")


def apply_manual(state, action, channel_id, channel_name, provider_id, nonce, now=None):
    validate(state)
    if action not in ("add", "remove"):
        raise ValueError("Unknown language classification action")
    _validate_identity(channel_id, channel_name, provider_id, nonce)
    if nonce == state["lastNonce"] or nonce in state["recentNonces"]:
        return state, False, "replayed_nonce"
    copy = json.loads(json.dumps(state))
    copy["updatedAt"] = time.time() if now is None else now
    _remember_nonce(copy, nonce)
    if provider_id is None:
        return copy, True, "unknown_provider"
    if action == "add":
        copy["entries"][provider_id] = {
            "channelID": channel_id, "channelName": channel_name,
            "markedAt": copy["updatedAt"], "nonce": nonce, "source": "manual",
            "language": "es", "languageName": "Spanish", "confidence": None,
            "reason": "manual-spanish", "evidence": None,
        }
        copy["decisions"][provider_id] = {
            "decision": "spanish", "language": "es", "languageName": "Spanish",
            "at": copy["updatedAt"], "nonce": nonce,
            "channelID": channel_id, "channelName": channel_name,
        }
        if len(copy["entries"]) > MAX_ENTRIES:
            raise ValueError("Language exclusion capacity reached")
        return copy, True, "manual_add"
    copy["entries"].pop(provider_id, None)
    copy["decisions"][provider_id] = {
        "decision": "not_spanish", "language": "all", "languageName": "Any language",
        "at": copy["updatedAt"], "nonce": nonce,
        "channelID": channel_id, "channelName": channel_name,
    }
    if len(copy["decisions"]) > MAX_DECISIONS:
        raise ValueError("Language decision capacity reached")
    return copy, True, "manual_remove"


def _event_identity(provider_id, channel_id, language, visit_id, now):
    basis = "|".join((provider_id, channel_id, language, visit_id or "", "%.6f" % now))
    return "evt-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _is_duplicate(state, provider_id, channel_id, language, visit_id, launch_id, now, dedup_seconds):
    for event in state["evidence"]["events"].values():
        if event["providerID"] != provider_id or event["channelID"] != channel_id or event["language"] != language:
            continue
        if visit_id and event["visitID"] == visit_id:
            return True
        if dedup_seconds > 0 and 0 <= now - event["at"] < dedup_seconds:
            if not launch_id or not event["launchID"] or event["launchID"] == launch_id:
                return True
    return False


def _prune_events(events):
    if len(events) <= MAX_EVENTS:
        return
    ordered = sorted(events.values(), key=lambda row: (row["retractedAt"] is None, row["at"], row["eventID"]))
    for event in ordered[:max(0, len(events) - MAX_EVENTS)]:
        events.pop(event["eventID"], None)


def record_evidence(state, provider_id, channel_id, channel_name, language, language_name,
                    confidence, reason, evidence=None, outcome="excluded", visit_id=None,
                    launch_id=None, stream_generation=None, now=None,
                    threshold=DEFAULT_THRESHOLD, enforcement=True,
                    dedup_seconds=DEFAULT_DEDUP_SECONDS):
    """Record one conclusive visit and optionally enforce at the threshold.

    Returns (state, info).  Inconclusive samples must never call this function;
    weak, mixed, music and insufficient-speech decisions stay outside evidence.
    """
    validate(state)
    _validate_identity(channel_id, channel_name, provider_id)
    if outcome not in OUTCOMES:
        raise ValueError("Invalid language evidence outcome")
    if not isinstance(language, str) or not LANG_CODE.fullmatch(language):
        raise ValueError("Invalid evidence language")
    if language_name != language_catalog.language_name(language):
        raise ValueError("Invalid evidence language name")
    category = language_catalog.classify(language)
    expected = {"excluded": "excluded", "allowed": "allowed", "unauthorized": "unauthorized"}[outcome]
    if category != expected:
        raise ValueError("Evidence outcome does not match language policy")
    if not _number(confidence) or confidence > 1:
        raise ValueError("Invalid language auto confidence")
    if not isinstance(reason, str) or not reason or len(reason) > MAX_REASON:
        raise ValueError("Invalid language auto reason")
    payload = evidence if isinstance(evidence, dict) else {}
    bounded = {key: payload[key] for key in EVIDENCE_KEYS if key in payload}
    _validate_evidence(bounded)
    if type(threshold) is not int or not MIN_THRESHOLD <= threshold <= MAX_THRESHOLD:
        raise ValueError("Invalid language evidence threshold")
    if type(enforcement) is not bool:
        raise ValueError("Invalid language enforcement flag")
    if type(dedup_seconds) not in (int, float) or not math.isfinite(dedup_seconds) or not 0 <= dedup_seconds <= 86400:
        raise ValueError("Invalid language dedup window")
    for value in (visit_id, launch_id, stream_generation):
        if value is not None and (not isinstance(value, str) or len(value) > 200):
            raise ValueError("Invalid language session identity")

    decision = state["decisions"].get(provider_id)
    if decision and decision["decision"] == "not_spanish":
        return state, {"changed": False, "reason": "manual_not_spanish", "count": None,
                       "threshold": threshold, "enforced": False, "language": language,
                       "outcome": outcome, "eventID": None}
    existing = state["entries"].get(provider_id)
    if existing and existing["source"] == "manual":
        return state, {"changed": False, "reason": "manual_exclusion", "count": None,
                       "threshold": threshold, "enforced": False, "language": language,
                       "outcome": outcome, "eventID": None}

    at = time.time() if now is None else now
    if _is_duplicate(state, provider_id, channel_id, language, visit_id, launch_id, at, dedup_seconds):
        return state, {"changed": False, "reason": "duplicate_visit", "count": None,
                       "threshold": threshold, "enforced": False, "language": language,
                       "outcome": outcome, "eventID": None}

    copy = json.loads(json.dumps(state))
    event_id = _event_identity(provider_id, channel_id, language, visit_id, at)
    if event_id in copy["evidence"]["events"]:
        return state, {"changed": False, "reason": "duplicate_visit", "count": None,
                       "threshold": threshold, "enforced": False, "language": language,
                       "outcome": outcome, "eventID": None}
    payload = bounded
    event = {
        "eventID": event_id, "channelID": channel_id, "channelName": channel_name,
        "providerID": provider_id, "language": language, "languageName": language_name,
        "confidence": confidence, "reason": reason, "at": at, "source": "auto",
        "outcome": outcome, "visitID": visit_id or "", "launchID": launch_id or "",
        "streamGeneration": stream_generation or "",
        "deviceVerified": payload.get("deviceVerified"),
        "channelChangedBeforeResult": payload.get("channelChangedBeforeResult"),
        "model": payload.get("model"), "windows": payload.get("windows"),
        "speechSeconds": payload.get("speechSeconds"),
        "sampleSeconds": payload.get("sampleSeconds"),
        "speechCoverage": payload.get("speechCoverage"),
        "lexicalMargin": payload.get("lexicalMargin"),
        "retractedAt": None, "retractedBy": "", "retracts": "",
    }
    copy["evidence"]["events"][event_id] = event
    copy["evidence"]["updatedAt"] = at
    copy["updatedAt"] = at
    _bump_count(copy, channel_id, language)
    _prune_events(copy["evidence"]["events"])
    count = copy["evidence"]["counts"].get(channel_id, {}).get(language, 0)

    enforced = False
    result_reason = "evidence_recorded"
    if outcome == "excluded" and enforcement and count >= threshold:
        if provider_id not in copy["entries"]:
            copy["entries"][provider_id] = {
                "channelID": channel_id, "channelName": channel_name,
                "markedAt": at, "nonce": "auto-" + uuid.uuid4().hex, "source": "auto",
                "language": language, "languageName": language_name,
                "confidence": confidence, "reason": "auto_threshold",
                "evidence": dict(payload) if payload else None,
            }
            if len(copy["entries"]) > MAX_ENTRIES:
                raise ValueError("Language exclusion capacity reached")
            enforced = True
            result_reason = "auto_threshold"
        else:
            result_reason = "already_marked"
    return copy, {"changed": True, "reason": result_reason, "count": count,
                  "threshold": threshold, "enforced": enforced, "language": language,
                  "outcome": outcome, "eventID": event_id}


def apply_auto(state, provider_id, channel_id, channel_name, language, language_name,
               confidence, reason, evidence=None, now=None, **options):
    """Backward-compatible wrapper around record_evidence.

    A first confident detection records evidence; it does not exclude unless
    enforcement is on and the configured threshold has been reached.
    """
    outcome = options.pop("outcome", "excluded")
    state, info = record_evidence(
        state, provider_id, channel_id, channel_name, language, language_name,
        confidence, reason, evidence, outcome=outcome, now=now, **options)
    return state, info["changed"], info["reason"]


def retract_evidence(state, provider_id, channel_id, channel_name, nonce, now=None,
                     threshold=DEFAULT_THRESHOLD, enforcement=True):
    """Retract the exact latest automatic unwanted-language evidence once.

    The audit event is retained and marked retracted; the matching count is
    decremented.  A manual English evidence event is recorded, but it never
    creates a permanent allow decision, so later Spanish visits are tested
    normally.  No playback command is issued here.
    """
    validate(state)
    _validate_identity(channel_id, channel_name, provider_id, nonce)
    if type(threshold) is not int or not MIN_THRESHOLD <= threshold <= MAX_THRESHOLD:
        raise ValueError("Invalid language evidence threshold")
    if nonce == state["lastNonce"] or nonce in state["recentNonces"]:
        return state, {"changed": False, "reason": "replayed_nonce", "retractedEventID": None,
                       "count": None, "threshold": threshold}
    candidates = [event for event in state["evidence"]["events"].values()
                  if event["providerID"] == provider_id and event["channelID"] == channel_id
                  and event["outcome"] == "excluded"]
    if not candidates:
        return state, {"changed": False, "reason": "no_evidence", "retractedEventID": None,
                       "count": None, "threshold": threshold}
    latest = max(candidates, key=lambda row: (row["at"], row["eventID"]))
    if latest["retractedAt"] is not None:
        # A correction always targets the latest event only. Repeating the
        # gesture (even with a fresh nonce) must never walk backwards through
        # older evidence or retract a second event.
        return state, {"changed": False, "reason": "already_corrected",
                       "retractedEventID": None, "count": None,
                       "threshold": threshold}
    at = time.time() if now is None else now
    copy = json.loads(json.dumps(state))
    copy["updatedAt"] = at
    copy["evidence"]["updatedAt"] = at
    _remember_nonce(copy, nonce)
    target = copy["evidence"]["events"][latest["eventID"]]
    target["retractedAt"] = at
    target["retractedBy"] = nonce
    _drop_count(copy, channel_id, target["language"])

    manual_id = _event_identity(provider_id, channel_id, "en", "manual:" + nonce, at)
    copy["evidence"]["events"][manual_id] = {
        "eventID": manual_id, "channelID": channel_id, "channelName": channel_name,
        "providerID": provider_id, "language": "en", "languageName": "English",
        "confidence": None, "reason": "manual_mistaken_english", "at": at,
        "source": "manual", "outcome": "allowed", "visitID": "manual:" + nonce,
        "launchID": "", "streamGeneration": "", "deviceVerified": None,
        "channelChangedBeforeResult": None, "model": None, "windows": None,
        "speechSeconds": None, "sampleSeconds": None, "speechCoverage": None,
        "lexicalMargin": None, "retractedAt": None, "retractedBy": "",
        "retracts": latest["eventID"],
    }
    _bump_count(copy, channel_id, "en")
    _prune_events(copy["evidence"]["events"])

    auto_entry = copy["entries"].get(provider_id)
    if auto_entry and auto_entry["source"] == "auto":
        remaining = copy["evidence"]["counts"].get(channel_id, {}).get(auto_entry["language"], 0)
        if remaining < threshold:
            copy["entries"].pop(provider_id, None)
    try:
        validate(copy)
    except ValueError:
        raise
    return copy, {"changed": True, "reason": "retracted_english",
                  "retractedEventID": latest["eventID"],
                  "count": copy["evidence"]["counts"].get(channel_id, {}).get(target["language"], 0),
                  "threshold": threshold}


def excluded(data, include_auto=True):
    validate(data)
    return {provider_id for provider_id, row in data["entries"].items()
            if include_auto or row["source"] == "manual"}


def manual_excluded(data):
    return excluded(data, include_auto=False)


def auto_excluded(data):
    validate(data)
    return {provider_id for provider_id, row in data["entries"].items() if row["source"] == "auto"}


def manual_decisions(data):
    validate(data)
    return dict(data["decisions"])


def counts_for(data, channel_id, language=None):
    validate(data)
    row = data["evidence"]["counts"].get(channel_id, {})
    if language is None:
        return dict(row)
    return row.get(language, 0)


def latest_evidence(data, provider_id=None, channel_id=None, outcome=None, include_retracted=False):
    validate(data)
    rows = []
    for event in data["evidence"]["events"].values():
        if not include_retracted and event["retractedAt"] is not None:
            continue
        if provider_id is not None and event["providerID"] != provider_id:
            continue
        if channel_id is not None and event["channelID"] != channel_id:
            continue
        if outcome is not None and event["outcome"] != outcome:
            continue
        rows.append(event)
    return max(rows, key=lambda row: (row["at"], row["eventID"])) if rows else None


def describe(data):
    validate(data)
    languages = {}
    for row in data["entries"].values():
        languages[row["language"]] = languages.get(row["language"], 0) + 1
    counts = data["evidence"]["counts"]
    evidence_languages = {}
    for row in counts.values():
        for code, count in row.items():
            evidence_languages[code] = evidence_languages.get(code, 0) + count
    latest = latest_evidence(data)
    return {
        "schemaVersion": data["schemaVersion"],
        "revision": data["revision"],
        "updatedAt": data["updatedAt"],
        "entries": len(data["entries"]),
        "manualEntries": sum(1 for row in data["entries"].values() if row["source"] == "manual"),
        "autoEntries": sum(1 for row in data["entries"].values() if row["source"] == "auto"),
        "manualAllow": sum(1 for row in data["decisions"].values() if row["decision"] == "not_spanish"),
        "languages": languages,
        "languageNames": {code: language_catalog.language_name(code) for code in languages},
        "lastNonce": data["lastNonce"],
        "evidence": {
            "events": len(data["evidence"]["events"]),
            "eventLimit": MAX_EVENTS,
            "channels": len(counts),
            "languages": evidence_languages,
            "languageNames": {code: language_catalog.language_name(code) for code in evidence_languages},
            "latest": None if latest is None else {
                "channelID": latest["channelID"], "providerID": latest["providerID"],
                "language": latest["language"], "languageName": latest["languageName"],
                "outcome": latest["outcome"], "confidence": latest["confidence"],
                "at": latest["at"], "source": latest["source"],
            },
        },
    }


def parse_marker(raw):
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or set(data) != MARKER_KEYS:
        return None
    schema = data.get("schemaVersion")
    action = data.get("action")
    if schema == 1 and action in ("add", "remove"):
        pass
    elif schema == 2 and action == "mistaken_english":
        pass
    else:
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


def _read(path):
    if not path.exists():
        return None
    if path.stat().st_size > MAX_FILE:
        raise ValueError("Language exclusion store too large")
    return json.loads(path.read_text(encoding="utf-8"))


def migrate(data):
    """Accept v1/v2/v3 and return a v4 evidence-first state.

    Every legacy automatic entry becomes exactly one evidence event and is
    removed from the membership filter, restoring playback eligibility subject
    to the ordinary health policy.  Manual entries and allow decisions survive.
    """
    if isinstance(data, dict) and data.get("schemaVersion") == SCHEMA_VERSION:
        return validate(data)
    if not isinstance(data, dict) or data.get("schemaVersion") not in (1, 2, 3):
        raise ValueError("Unknown language exclusion schema")

    decisions = {}
    entries = {}
    recent = []
    updated_at = data.get("updatedAt", 0)
    if data.get("schemaVersion") == 3:
        _legacy_v3(data)
        recent = list(data.get("recentNonces", []))
        for provider_id, row in data.get("decisions", {}).items():
            decisions[provider_id] = dict(row)
        for provider_id, row in data.get("entries", {}).items():
            entries[provider_id] = dict(row)
    elif data.get("schemaVersion") == 1:
        if set(data) != {"schemaVersion", "revision", "updatedAt", "lastNonce", "entries"}:
            raise ValueError("Invalid legacy Spanish state")
        if data.get("revision") != "spanish-classification-v1":
            raise ValueError("Unknown legacy Spanish schema")
        if isinstance(data.get("lastNonce"), str) and data["lastNonce"]:
            recent = [data["lastNonce"]]
        for provider_id, row in data.get("entries", {}).items():
            entries[provider_id] = _legacy_entry(row)
    else:
        if set(data) != {"schemaVersion", "revision", "updatedAt", "lastNonce", "recentNonces", "entries", "decisions"}:
            raise ValueError("Invalid legacy Spanish state")
        if data.get("revision") != "spanish-classification-v2":
            raise ValueError("Unknown legacy Spanish schema")
        recent = list(data.get("recentNonces", []))
        raw_decisions = data.get("decisions", {})
        if not isinstance(raw_decisions, dict):
            raise ValueError("Invalid legacy decisions")
        for provider_id, row in raw_decisions.items():
            if not isinstance(row, dict):
                raise ValueError("Invalid legacy decision")
            if row.get("decision") == "spanish":
                decisions[provider_id] = _legacy_decision(row)
            elif row.get("decision") == "not_spanish":
                decisions[provider_id] = _legacy_decision(row, allow=True)
        for provider_id, row in data.get("entries", {}).items():
            entries[provider_id] = _legacy_entry(row)

    result = empty()
    result["updatedAt"] = updated_at
    result["lastNonce"] = data.get("lastNonce", "") if isinstance(data.get("lastNonce", ""), str) else ""
    result["recentNonces"] = [value for value in recent if isinstance(value, str)][:MAX_NONCES]
    result["decisions"] = decisions
    for provider_id, row in entries.items():
        if not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id):
            raise ValueError("Invalid legacy language provider")
        if row["source"] == "auto":
            decisions.pop(provider_id, None) if decisions.get(provider_id, {}).get("decision") == "spanish" else None
            event = _event_from_legacy_entry(provider_id, row)
            result["evidence"]["events"][event["eventID"]] = event
            _bump_count(result, row["channelID"], row["language"])
        else:
            result["entries"][provider_id] = row
            if provider_id not in decisions:
                decisions[provider_id] = {
                    "decision": "spanish", "language": "es", "languageName": "Spanish",
                    "at": row["markedAt"], "nonce": row["nonce"],
                    "channelID": row["channelID"], "channelName": row["channelName"],
                }
    result["evidence"]["updatedAt"] = updated_at
    return validate(result)


def load(root):
    root = Path(root)
    current = _read(root / FILE_NAME)
    if current is not None:
        return migrate(current)
    legacy = _read(root / LEGACY_FILE)
    if legacy is not None:
        return migrate(legacy)
    return empty()


def load_or_migrate(root):
    """Load v4, migrating any v1/v2/v3 store to the new file once."""
    root = Path(root)
    current_path = root / FILE_NAME
    legacy_path = root / LEGACY_FILE
    current = _read(current_path)
    if current is not None and current.get("schemaVersion") == SCHEMA_VERSION:
        return validate(current)
    legacy = _read(legacy_path)
    if current is None and legacy is None:
        return empty()
    state = migrate(current if current is not None else legacy)
    if current_path.exists() or legacy_path.exists():
        atomic_write(root, state)
    return state


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
        try:
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


def apply(state, action, channel_id, channel_name, provider_id, nonce, now=None):
    state, changed, _ = apply_manual(state, action, channel_id, channel_name, provider_id, nonce, now)
    return state, changed
