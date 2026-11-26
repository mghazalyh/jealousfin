"""Generalized language exclusion store keyed by exact provider identity.

The store is a membership filter only: it is never a policy failure, ban or
hiatus, and never mutates rotation, the visited set, Favorites or playback
history. Schema v3 stores the exact detected language code and name, source
(manual/auto), confidence, reason and bounded evidence.

Manual Spanish keeps its existing behavior: adding a manual marker stores
language "es". The existing "Not Spanish" action removes any language
exclusion and records an allow decision that blocks all future automatic
re-marking for that provider. Automatic exclusions can add but never remove
or override a manual decision.
"""
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

import language_catalog

SCHEMA_VERSION = 3
REVISION = "language-exclusions-v3"
FILE_NAME = "language-exclusions.json"
LEGACY_FILE = "spanish-classification.json"
MAX_ENTRIES = 2000
MAX_DECISIONS = 4000
MAX_NONCE = 128
MAX_NONCES = 128
MAX_FILE = 1024 * 1024
MAX_REASON = 80
MAX_EVIDENCE_TEXT = 80
PROVIDER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
CHANNEL_ID = re.compile(r"^[a-f0-9]{32}$")
LANG_CODE = re.compile(r"^[a-z]{2,3}$")
MARKER_KEYS = {"schemaVersion", "action", "channelID", "channelName", "nonce"}
STATE_KEYS = {"schemaVersion", "revision", "updatedAt", "lastNonce", "recentNonces", "entries", "decisions"}
ENTRY_KEYS = {"channelID", "channelName", "markedAt", "nonce", "source", "language", "languageName", "confidence", "reason", "evidence"}
DECISION_KEYS = {"decision", "language", "languageName", "at", "nonce", "channelID", "channelName"}
EVIDENCE_KEYS = {
    "model", "windows", "speechSeconds", "sampleSeconds", "speechCoverage",
    "languages", "deviceVerified", "channelChangedBeforeResult", "lexicalMargin",
}
LANGUAGES_KEYS = {"language", "probability"}
SOURCES = ("manual", "auto")
DECISIONS = ("spanish", "not_spanish")


def empty():
    return {
        "schemaVersion": SCHEMA_VERSION,
        "revision": REVISION,
        "updatedAt": 0,
        "lastNonce": "",
        "recentNonces": [],
        "entries": {},
        "decisions": {},
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
    return data


def _legacy_entry(row, now):
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


def migrate(data):
    """Accept v1/v2 Spanish state and return a v3 generalized state."""
    if isinstance(data, dict) and data.get("schemaVersion") == SCHEMA_VERSION:
        return validate(data)
    if not isinstance(data, dict) or data.get("schemaVersion") not in (1, 2):
        raise ValueError("Unknown language exclusion schema")
    if data.get("schemaVersion") == 1:
        if set(data) != {"schemaVersion", "revision", "updatedAt", "lastNonce", "entries"}:
            raise ValueError("Invalid legacy Spanish state")
        if data.get("revision") != "spanish-classification-v1":
            raise ValueError("Unknown legacy Spanish schema")
        recent = [data["lastNonce"]] if isinstance(data.get("lastNonce"), str) and data["lastNonce"] else []
        decisions = {}
    else:
        if set(data) != {"schemaVersion", "revision", "updatedAt", "lastNonce", "recentNonces", "entries", "decisions"}:
            raise ValueError("Invalid legacy Spanish state")
        if data.get("revision") != "spanish-classification-v2":
            raise ValueError("Unknown legacy Spanish schema")
        recent = list(data.get("recentNonces", []))
        decisions = {}
        for provider_id, row in data.get("decisions", {}).items():
            if not isinstance(row, dict):
                raise ValueError("Invalid legacy decision")
            if row.get("decision") == "spanish":
                decisions[provider_id] = {
                    "decision": "spanish", "language": "es", "languageName": "Spanish",
                    "at": row.get("at"), "nonce": row.get("nonce"),
                    "channelID": row.get("channelID"), "channelName": row.get("channelName"),
                }
            elif row.get("decision") == "not_spanish":
                decisions[provider_id] = {
                    "decision": "not_spanish", "language": "all", "languageName": "Any language",
                    "at": row.get("at"), "nonce": row.get("nonce"),
                    "channelID": row.get("channelID"), "channelName": row.get("channelName"),
                }
    result = empty()
    result["updatedAt"] = data.get("updatedAt", 0)
    result["lastNonce"] = data.get("lastNonce", "")
    result["recentNonces"] = [value for value in recent if isinstance(value, str)][:MAX_NONCES]
    for provider_id, row in data.get("entries", {}).items():
        result["entries"][provider_id] = _legacy_entry(row, result["updatedAt"])
        if provider_id not in decisions:
            decisions[provider_id] = {
                "decision": "spanish", "language": "es", "languageName": "Spanish",
                "at": row.get("markedAt", 0), "nonce": row.get("nonce", ""),
                "channelID": row.get("channelID"), "channelName": row.get("channelName"),
            }
    result["decisions"] = decisions
    return validate(result)


def _read(path):
    if not path.exists():
        return None
    if path.stat().st_size > MAX_FILE:
        raise ValueError("Language exclusion store too large")
    return json.loads(path.read_text(encoding="utf-8"))


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
    """Load v3, migrating the legacy Spanish store to the new file once."""
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


def excluded(data):
    validate(data)
    return set(data["entries"])


def manual_decisions(data):
    validate(data)
    return dict(data["decisions"])


def describe(data):
    validate(data)
    languages = {}
    for row in data["entries"].values():
        languages[row["language"]] = languages.get(row["language"], 0) + 1
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


def _remember_nonce(state, nonce):
    history = [value for value in state["recentNonces"] if value != nonce]
    history.insert(0, nonce)
    state["recentNonces"] = history[:MAX_NONCES]
    state["lastNonce"] = nonce


def _validate_identity(channel_id, channel_name, provider_id, nonce):
    if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
        raise ValueError("Invalid language classification channel")
    if not isinstance(channel_name, str) or not channel_name or len(channel_name) > 160:
        raise ValueError("Invalid language classification name")
    if not isinstance(nonce, str) or not nonce or len(nonce) > MAX_NONCE:
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


def apply_auto(state, provider_id, channel_id, channel_name, language, language_name,
               confidence, reason, evidence=None, now=None):
    validate(state)
    if not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id):
        raise ValueError("Invalid language provider identity")
    if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
        raise ValueError("Invalid language classification channel")
    if not isinstance(channel_name, str) or not channel_name or len(channel_name) > 160:
        raise ValueError("Invalid language classification name")
    if not isinstance(language, str) or not language_catalog.is_excluded_code(language):
        raise ValueError("Invalid automatic exclusion language")
    if language_name != language_catalog.language_name(language):
        raise ValueError("Invalid automatic exclusion language name")
    if not _number(confidence) or confidence > 1:
        raise ValueError("Invalid language auto confidence")
    if not isinstance(reason, str) or not reason or len(reason) > MAX_REASON:
        raise ValueError("Invalid language auto reason")
    _validate_evidence(evidence)
    decision = state["decisions"].get(provider_id)
    if decision and decision["decision"] == "not_spanish":
        return state, False, "manual_not_spanish"
    existing = state["entries"].get(provider_id)
    if existing:
        if existing["source"] == "manual":
            return state, False, "manual_exclusion"
        return state, False, "already_marked"
    copy = json.loads(json.dumps(state))
    copy["updatedAt"] = time.time() if now is None else now
    copy["entries"][provider_id] = {
        "channelID": channel_id, "channelName": channel_name,
        "markedAt": copy["updatedAt"], "nonce": "auto-" + uuid.uuid4().hex,
        "source": "auto", "language": language, "languageName": language_name,
        "confidence": confidence, "reason": reason, "evidence": evidence,
    }
    if len(copy["entries"]) > MAX_ENTRIES:
        raise ValueError("Language exclusion capacity reached")
    return copy, True, "auto_add"


def apply(state, action, channel_id, channel_name, provider_id, nonce, now=None):
    state, changed, _ = apply_manual(state, action, channel_id, channel_name, provider_id, nonce, now)
    return state, changed
