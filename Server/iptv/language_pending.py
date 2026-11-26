"""Bounded pending automatic language exclusions.

A confident automatic result is written here instead of the exclusion store
when the user has paused or stopped rotation on the sampled channel. The
pending record preserves the exact detected language and evidence without
touching playback, rotation, catalogue membership or policy. It is applied to
the exclusion store later, only after a fresh read-only device status shows
active rotation and no user pause. Manual decisions always win.
"""
import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path

import language_catalog
import language_exclusions

SCHEMA_VERSION = 1
REVISION = "language-pending-v1"
FILE_NAME = "language-exclusions-pending.json"
MAX_ENTRIES = 200
MAX_FILE = 512 * 1024
MAX_TEXT = 160
RECORD_KEYS = {
    "channelID", "channelName", "language", "languageName", "confidence",
    "reason", "evidence", "launchID", "streamGeneration", "createdAt",
}
_LOCK = threading.RLock()


def empty():
    return {
        "schemaVersion": SCHEMA_VERSION,
        "revision": REVISION,
        "updatedAt": 0,
        "entries": {},
    }


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate(data):
    if not isinstance(data, dict) or set(data) != {"schemaVersion", "revision", "updatedAt", "entries"}:
        raise ValueError("Invalid pending language state")
    if data["schemaVersion"] != SCHEMA_VERSION or data["revision"] != REVISION:
        raise ValueError("Unknown pending language schema")
    if not _number(data["updatedAt"]):
        raise ValueError("Invalid pending language timestamp")
    entries = data["entries"]
    if not isinstance(entries, dict) or len(entries) > MAX_ENTRIES:
        raise ValueError("Invalid pending language entries")
    for provider, row in entries.items():
        if not isinstance(provider, str) or not language_exclusions.PROVIDER_ID.fullmatch(provider):
            raise ValueError("Invalid pending language provider")
        if not isinstance(row, dict) or set(row) != RECORD_KEYS:
            raise ValueError("Invalid pending language record")
        if not isinstance(row["channelID"], str) or not language_exclusions.CHANNEL_ID.fullmatch(row["channelID"]):
            raise ValueError("Invalid pending language channel")
        if not isinstance(row["channelName"], str) or not row["channelName"] or len(row["channelName"]) > MAX_TEXT:
            raise ValueError("Invalid pending language name")
        if not isinstance(row["language"], str) or not language_catalog.is_excluded_code(row["language"]):
            raise ValueError("Invalid pending excluded language")
        if row["languageName"] != language_catalog.language_name(row["language"]):
            raise ValueError("Invalid pending language name")
        if not _number(row["confidence"]) or row["confidence"] > 1:
            raise ValueError("Invalid pending confidence")
        if not isinstance(row["reason"], str) or not row["reason"] or len(row["reason"]) > 80:
            raise ValueError("Invalid pending reason")
        evidence = row["evidence"]
        if not isinstance(evidence, dict):
            raise ValueError("Invalid pending evidence")
        allowed = language_exclusions.EVIDENCE_KEYS | {
            "outcome", "visitID", "launchID", "streamGeneration"}
        if not set(evidence).issubset(allowed):
            raise ValueError("Invalid pending evidence")
        language_exclusions._validate_evidence(
            {key: value for key, value in evidence.items()
             if key in language_exclusions.EVIDENCE_KEYS})
        if "outcome" in evidence and evidence["outcome"] not in language_exclusions.OUTCOMES:
            raise ValueError("Invalid pending evidence outcome")
        for key in ("visitID", "launchID", "streamGeneration"):
            value = evidence.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 200):
                raise ValueError("Invalid pending evidence identity")
        for key in ("launchID", "streamGeneration"):
            value = row[key]
            if value is not None and (not isinstance(value, str) or len(value) > MAX_TEXT):
                raise ValueError("Invalid pending session identity")
        if not _number(row["createdAt"]):
            raise ValueError("Invalid pending timestamp")
    return data


def load(root):
    path = Path(root) / FILE_NAME
    if not path.exists():
        return empty()
    if path.stat().st_size > MAX_FILE:
        raise ValueError("Pending language store too large")
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


def add(root, provider, record):
    if not isinstance(provider, str) or not language_exclusions.PROVIDER_ID.fullmatch(provider):
        raise ValueError("Invalid pending provider")
    with _LOCK:
        state = load(root)
        copy = json.loads(json.dumps(state))
        copy["entries"][provider] = dict(record)
        copy["updatedAt"] = time.time()
        atomic_write(root, copy)
        return copy


def remove(root, provider):
    with _LOCK:
        state = load(root)
        if provider not in state["entries"]:
            return state
        copy = json.loads(json.dumps(state))
        copy["entries"].pop(provider, None)
        copy["updatedAt"] = time.time()
        atomic_write(root, copy)
        return copy


def describe(root):
    state = load(root)
    languages = {}
    for row in state["entries"].values():
        languages[row["language"]] = languages.get(row["language"], 0) + 1
    return {
        "revision": REVISION,
        "entries": len(state["entries"]),
        "languages": languages,
        "languageNames": {code: language_catalog.language_name(code) for code in languages},
        "updatedAt": state["updatedAt"],
    }
