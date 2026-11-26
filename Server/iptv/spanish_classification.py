"""Spanish classification keyed by exact provider identity.

The store separates user/manual decisions from conservative automatic
audio-language decisions. A mark is always a membership filter only: it is
never a policy failure, ban or hiatus, and it never mutates rotation, the
visited set, Favorites or playback history.

Schema v2 adds provenance and manual overrides while transparently migrating
the v1 manual-only file. A manual "Not Spanish" decision is an explicit
allow decision: it removes any mark and permanently blocks automatic
re-marking for that exact provider identity. Automatic decisions can add a
Spanish mark but can never remove a manual mark or override a manual allow.
"""
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 2
REVISION = "spanish-classification-v2"
MAX_ENTRIES = 2000
MAX_DECISIONS = 4000
MAX_NONCE = 128
MAX_NONCES = 128
MAX_FILE = 512 * 1024
MAX_REASON = 80
MAX_EVIDENCE_TEXT = 80
# Provider identities are already validated by the catalogue/profile layers.
# Keep this permissive enough for every existing nltv:* identity and fixtures.
PROVIDER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
CHANNEL_ID = re.compile(r"^[a-f0-9]{32}$")
MARKER_KEYS = {"schemaVersion", "action", "channelID", "channelName", "nonce"}
STATE_KEYS = {"schemaVersion", "revision", "updatedAt", "lastNonce", "recentNonces", "entries", "decisions"}
ENTRY_KEYS = {"channelID", "channelName", "markedAt", "nonce", "source", "confidence", "reason", "evidence"}
DECISION_KEYS = {"decision", "at", "nonce", "channelID", "channelName"}
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
        raise ValueError("Invalid Spanish auto evidence")
    model = evidence.get("model")
    if model is not None and (not isinstance(model, str) or not model or len(model) > MAX_EVIDENCE_TEXT):
        raise ValueError("Invalid Spanish auto evidence model")
    windows = evidence.get("windows")
    if windows is not None and (type(windows) is not int or not 0 <= windows <= 32):
        raise ValueError("Invalid Spanish auto window count")
    for key in ("speechSeconds", "sampleSeconds"):
        value = evidence.get(key)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 600):
            raise ValueError("Invalid Spanish auto duration")
    coverage = evidence.get("speechCoverage")
    if coverage is not None and (type(coverage) not in (int, float) or not math.isfinite(coverage) or not 0 <= coverage <= 1):
        raise ValueError("Invalid Spanish auto coverage")
    margin = evidence.get("lexicalMargin")
    if margin is not None and (type(margin) not in (int, float) or not math.isfinite(margin) or not -1 <= margin <= 1):
        raise ValueError("Invalid Spanish lexical margin")
    for key in ("deviceVerified", "channelChangedBeforeResult"):
        value = evidence.get(key)
        if value is not None and type(value) is not bool:
            raise ValueError("Invalid Spanish auto verification flag")
    languages = evidence.get("languages")
    if languages is not None:
        if not isinstance(languages, list) or len(languages) > 12:
            raise ValueError("Invalid Spanish auto language votes")
        for vote in languages:
            if not isinstance(vote, dict) or set(vote) != LANGUAGES_KEYS:
                raise ValueError("Invalid Spanish auto language vote")
            if not isinstance(vote["language"], str) or not re.fullmatch(r"[a-z]{2,3}", vote["language"]):
                raise ValueError("Invalid Spanish auto language")
            if not _probability(vote["probability"]) or vote["probability"] is None:
                raise ValueError("Invalid Spanish auto language probability")


def validate(data):
    if not isinstance(data, dict) or set(data) != STATE_KEYS:
        raise ValueError("Invalid Spanish classification state")
    if data["schemaVersion"] != SCHEMA_VERSION or data["revision"] != REVISION:
        raise ValueError("Unknown Spanish classification schema")
    if not _number(data["updatedAt"]):
        raise ValueError("Invalid Spanish classification timestamp")
    if not isinstance(data["lastNonce"], str) or len(data["lastNonce"]) > MAX_NONCE:
        raise ValueError("Invalid Spanish classification nonce")
    nonces = data["recentNonces"]
    if not isinstance(nonces, list) or len(nonces) > MAX_NONCES:
        raise ValueError("Invalid Spanish nonce history")
    seen = set()
    for nonce in nonces:
        if not isinstance(nonce, str) or not nonce or len(nonce) > MAX_NONCE or nonce in seen:
            raise ValueError("Invalid Spanish nonce history entry")
        seen.add(nonce)
    entries = data["entries"]
    if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
        raise ValueError("Invalid Spanish classification entries")
    for provider_id, row in entries.items():
        if not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id):
            raise ValueError("Invalid Spanish provider identity")
        if not isinstance(row, dict) or set(row) != ENTRY_KEYS:
            raise ValueError("Invalid Spanish classification row")
        if not isinstance(row["channelID"], str) or not CHANNEL_ID.fullmatch(row["channelID"]):
            raise ValueError("Invalid Spanish channel identity")
        if not isinstance(row["channelName"], str) or len(row["channelName"]) > 160:
            raise ValueError("Invalid Spanish channel name")
        if not _number(row["markedAt"]):
            raise ValueError("Invalid Spanish mark timestamp")
        if not isinstance(row["nonce"], str) or not row["nonce"] or len(row["nonce"]) > MAX_NONCE:
            raise ValueError("Invalid Spanish row nonce")
        if row["source"] not in SOURCES:
            raise ValueError("Invalid Spanish provenance")
        if not _probability(row["confidence"]):
            raise ValueError("Invalid Spanish confidence")
        if row["source"] == "manual" and row["confidence"] is not None:
            raise ValueError("Manual Spanish confidence must be empty")
        if not isinstance(row["reason"], str) or not row["reason"] or len(row["reason"]) > MAX_REASON:
            raise ValueError("Invalid Spanish reason")
        _validate_evidence(row["evidence"])
    decisions = data["decisions"]
    if not isinstance(decisions, dict) or len(decisions) > MAX_DECISIONS:
        raise ValueError("Invalid Spanish decisions")
    for provider_id, row in decisions.items():
        if not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id):
            raise ValueError("Invalid Spanish decision provider")
        if not isinstance(row, dict) or set(row) != DECISION_KEYS:
            raise ValueError("Invalid Spanish decision")
        if row["decision"] not in DECISIONS:
            raise ValueError("Invalid Spanish decision value")
        if not _number(row["at"]):
            raise ValueError("Invalid Spanish decision timestamp")
        if not isinstance(row["nonce"], str) or not row["nonce"] or len(row["nonce"]) > MAX_NONCE:
            raise ValueError("Invalid Spanish decision nonce")
        if not isinstance(row["channelID"], str) or not CHANNEL_ID.fullmatch(row["channelID"]):
            raise ValueError("Invalid Spanish decision channel")
        if not isinstance(row["channelName"], str) or not row["channelName"] or len(row["channelName"]) > 160:
            raise ValueError("Invalid Spanish decision name")
        if row["decision"] == "not_spanish" and provider_id in entries:
            raise ValueError("Spanish allow decision cannot coexist with a mark")
    return data


def migrate(data):
    """Accept the deployed v1 manual-only state and return a v2 state."""
    if not isinstance(data, dict):
        raise ValueError("Invalid Spanish classification state")
    if data.get("schemaVersion") == SCHEMA_VERSION:
        return validate(data)
    if set(data) != {"schemaVersion", "revision", "updatedAt", "lastNonce", "entries"}:
        raise ValueError("Unknown Spanish classification schema")
    if data.get("schemaVersion") != 1 or data.get("revision") != "spanish-classification-v1":
        raise ValueError("Unknown Spanish classification schema")
    if not _number(data["updatedAt"]) or not isinstance(data["lastNonce"], str) or len(data["lastNonce"]) > MAX_NONCE:
        raise ValueError("Invalid Spanish classification state")
    if not isinstance(data["entries"], dict) or len(data["entries"]) > MAX_ENTRIES:
        raise ValueError("Invalid Spanish classification entries")
    result = empty()
    result["updatedAt"] = data["updatedAt"]
    result["lastNonce"] = data["lastNonce"]
    if data["lastNonce"]:
        result["recentNonces"] = [data["lastNonce"]]
    for provider_id, row in data["entries"].items():
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
        result["entries"][provider_id] = {
            "channelID": row["channelID"], "channelName": row["channelName"],
            "markedAt": row["markedAt"], "nonce": row["nonce"], "source": "manual",
            "confidence": None, "reason": "manual-legacy-v1", "evidence": None,
        }
        result["decisions"][provider_id] = {
            "decision": "spanish", "at": row["markedAt"], "nonce": row["nonce"],
            "channelID": row["channelID"], "channelName": row["channelName"],
        }
    return validate(result)


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
    return migrate(json.loads(path.read_text(encoding="utf-8")))


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
    return {
        "schemaVersion": data["schemaVersion"],
        "revision": data["revision"],
        "updatedAt": data["updatedAt"],
        "entries": len(data["entries"]),
        "manualEntries": sum(1 for row in data["entries"].values() if row["source"] == "manual"),
        "autoEntries": sum(1 for row in data["entries"].values() if row["source"] == "auto"),
        "manualAllow": sum(1 for row in data["decisions"].values() if row["decision"] == "not_spanish"),
        "lastNonce": data["lastNonce"],
    }


def _remember_nonce(state, nonce):
    history = [value for value in state["recentNonces"] if value != nonce]
    history.insert(0, nonce)
    state["recentNonces"] = history[:MAX_NONCES]
    state["lastNonce"] = nonce


def _validate_identity(channel_id, channel_name, provider_id, nonce):
    if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
        raise ValueError("Invalid Spanish classification channel")
    if not isinstance(channel_name, str) or not channel_name or len(channel_name) > 160:
        raise ValueError("Invalid Spanish classification name")
    if not isinstance(nonce, str) or not nonce or len(nonce) > MAX_NONCE:
        raise ValueError("Invalid Spanish classification nonce")
    if provider_id is not None and (not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id)):
        raise ValueError("Invalid Spanish provider identity")


def apply_manual(state, action, channel_id, channel_name, provider_id, nonce, now=None):
    """Apply one explicit user decision.

    Returns (nextState, changed, reason). A repeated or replayed nonce is a
    no-op. Unknown provider identities still consume the nonce so a stale or
    synthetic marker is never retried, but they can never exclude a real row.
    """
    validate(state)
    if action not in ("add", "remove"):
        raise ValueError("Unknown Spanish classification action")
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
            "confidence": None, "reason": "manual", "evidence": None,
        }
        copy["decisions"][provider_id] = {
            "decision": "spanish", "at": copy["updatedAt"], "nonce": nonce,
            "channelID": channel_id, "channelName": channel_name,
        }
        if len(copy["entries"]) > MAX_ENTRIES:
            raise ValueError("Spanish classification capacity reached")
        return copy, True, "manual_add"
    copy["entries"].pop(provider_id, None)
    copy["decisions"][provider_id] = {
        "decision": "not_spanish", "at": copy["updatedAt"], "nonce": nonce,
        "channelID": channel_id, "channelName": channel_name,
    }
    if len(copy["decisions"]) > MAX_DECISIONS:
        raise ValueError("Spanish decision capacity reached")
    return copy, True, "manual_remove"


def apply_auto(state, provider_id, channel_id, channel_name, confidence, reason, evidence=None, now=None):
    """Add a conservative automatic Spanish mark.

    Never removes or rewrites a mark, never overrides a manual decision and
    never changes the manual nonce state. Returns (nextState, changed, reason).
    """
    validate(state)
    if not isinstance(provider_id, str) or not PROVIDER_ID.fullmatch(provider_id):
        raise ValueError("Invalid Spanish provider identity")
    if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
        raise ValueError("Invalid Spanish classification channel")
    if not isinstance(channel_name, str) or not channel_name or len(channel_name) > 160:
        raise ValueError("Invalid Spanish classification name")
    if not _number(confidence) or confidence > 1:
        raise ValueError("Invalid Spanish auto confidence")
    if not isinstance(reason, str) or not reason or len(reason) > MAX_REASON:
        raise ValueError("Invalid Spanish auto reason")
    _validate_evidence(evidence)
    decision = state["decisions"].get(provider_id)
    if decision and decision["decision"] == "not_spanish":
        return state, False, "manual_not_spanish"
    existing = state["entries"].get(provider_id)
    if existing:
        if existing["source"] == "manual":
            return state, False, "manual_spanish"
        return state, False, "already_marked"
    copy = json.loads(json.dumps(state))
    copy["updatedAt"] = time.time() if now is None else now
    copy["entries"][provider_id] = {
        "channelID": channel_id, "channelName": channel_name,
        "markedAt": copy["updatedAt"], "nonce": "auto-" + uuid.uuid4().hex,
        "source": "auto", "confidence": confidence, "reason": reason,
        "evidence": evidence,
    }
    if len(copy["entries"]) > MAX_ENTRIES:
        raise ValueError("Spanish classification capacity reached")
    return copy, True, "auto_add"


def apply(state, action, channel_id, channel_name, provider_id, nonce, now=None):
    """Backward-compatible manual application entry point."""
    state, changed, _ = apply_manual(state, action, channel_id, channel_name, provider_id, nonce, now)
    return state, changed
