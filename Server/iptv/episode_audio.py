"""Isolated, optional local audio-assisted episode identification contract.

Status: DEFAULT OFF and intentionally not wired to live playback. There is no
stable session-correlated audio handoff from the existing safe media sample
pipeline yet, so this module provides only a privacy-preserving pure ranking
adapter and an explicit deferred live-integration boundary. It never records
audio, keeps transcripts, uses a microphone, contacts a remote service, or
loads another Whisper model.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re

REVISION = "episode-audio-evidence-20261127"
DEFAULT_ENABLED = False
MAX_SAMPLE_BYTES = 2 * 1024 * 1024
MAX_CANDIDATES = 32
MAX_TERMS = 64
MIN_TERM_LENGTH = 4
CHANNEL_RE = re.compile(r"[a-f0-9]{32}")
SESSION_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
DIGEST_RE = re.compile(r"[a-f0-9]{32,64}")

GENERIC_TERMS = frozenset("""
about after again against almost alone along already also although always among another answer around
because before being below between both bring came come could does doing down during each either else
even ever every from give goes going good great have having here herself himself into just know last
like little long made make many might more most much must never next only other over people really
right said same should since some start still such take than that their them then there these they
thing think this those through time under until very want well were what when where which while will
with would your yours
""".split())


class DeferredIntegration(RuntimeError):
    """The live session-correlated audio contract is deliberately not active."""


def _tokens(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    out = []
    for item in value:
        if not isinstance(item, str):
            continue
        for token in re.findall(r"[a-z0-9']+", item.casefold()):
            token = token.strip("'")
            if len(token) < MIN_TERM_LENGTH or token in GENERIC_TERMS:
                continue
            if token not in out:
                out.append(token)
            if len(out) >= MAX_TERMS:
                return out
    return out


@dataclasses.dataclass(frozen=True)
class Candidate:
    title: str
    season_number: int
    episode_number: int
    series_title: str = ""
    release_year: int | None = None

    def checked(self):
        if not isinstance(self.title, str) or not 1 <= len(self.title) <= 300:
            return False
        if type(self.season_number) is not int or not 1 <= self.season_number <= 99:
            return False
        if type(self.episode_number) is not int or not 1 <= self.episode_number <= 9999:
            return False
        if self.release_year is not None and (type(self.release_year) is not int or not 1888 <= self.release_year <= 2100):
            return False
        return True


@dataclasses.dataclass(frozen=True)
class Match:
    channel_id: str
    play_session_id: str
    sample_digest: str
    candidate: Candidate
    confidence: float
    matched_terms: tuple

    def evidence(self):
        # Deliberately excludes spoken terms/transcript from UI/log output.
        return {"source": "local-audio-evidence", "revision": REVISION,
                "channelID": self.channel_id, "sampleDigest": self.sample_digest,
                "seasonNumber": self.candidate.season_number,
                "episodeNumber": self.candidate.episode_number,
                "episodeTitle": self.candidate.title,
                "confidence": round(self.confidence, 3)}


@dataclasses.dataclass(frozen=True)
class AudioSessionSample:
    channel_id: str
    play_session_id: str
    sample_digest: str
    seconds: float

    def checked(self):
        return (isinstance(self.channel_id, str) and bool(CHANNEL_RE.fullmatch(self.channel_id))
                and isinstance(self.play_session_id, str) and bool(SESSION_RE.fullmatch(self.play_session_id))
                and isinstance(self.sample_digest, str) and bool(DIGEST_RE.fullmatch(self.sample_digest))
                and isinstance(self.seconds, (int, float)) and 0 < self.seconds <= 30)


def digest_sample(sample):
    """Memory-only digest helper. The caller must not persist sample bytes."""
    if not isinstance(sample, (bytes, bytearray)) or not 0 < len(sample) <= MAX_SAMPLE_BYTES:
        raise ValueError("Unsafe audio sample")
    return hashlib.sha256(bytes(sample)).hexdigest()[:32]


def rank_episode_evidence(spoken_terms, candidates, min_hits=2, min_margin=0.12):
    """Rank independently verified local episode candidates from transient terms.

    Returns None unless one candidate is a clear, distinctive winner. Generic
    dialogue alone can never produce a title. The caller must discard spoken
    terms immediately; this function retains nothing.
    """
    terms = set(_tokens(spoken_terms))
    if len(terms) < min_hits:
        return None
    if not isinstance(candidates, (list, tuple)) or not 1 <= len(candidates) <= MAX_CANDIDATES:
        return None
    scored = []
    for candidate in candidates:
        if not isinstance(candidate, Candidate) or not candidate.checked():
            continue
        title = set(_tokens(candidate.title))
        if not title:
            continue
        matched = terms & title
        if len(matched) < min_hits:
            continue
        # Longer distinctive titles need more support; exact multi-word
        # matches rank above one incidental word.
        confidence = len(matched) / max(len(title), 2)
        scored.append((confidence, len(matched), candidate, tuple(sorted(matched))))
    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], row[1], -row[2].season_number, -row[2].episode_number), reverse=True)
    top = scored[0]
    if len(scored) > 1 and top[0] - scored[1][0] < min_margin:
        return None
    if top[1] < min_hits or top[0] < 0.2:
        return None
    return top


def identify_local(sample, spoken_terms, candidates):
    """Pure adapter entry point: no audio, file, network or transcript I/O."""
    if not isinstance(sample, AudioSessionSample) or not sample.checked():
        raise ValueError("Unsafe audio session correlation")
    ranked = rank_episode_evidence(spoken_terms, candidates)
    if ranked is None:
        return None
    _confidence, _hits, candidate, matched = ranked
    return Match(sample.channel_id, sample.play_session_id, sample.sample_digest,
                 candidate, _confidence, matched)


class LocalWhisperAdapter:
    """Explicit deferred boundary for a future in-process local Whisper handoff.

    A future implementation may transcribe an already-authorized, session
    correlated sample in memory and immediately call rank_episode_evidence().
    It must not persist audio or transcripts, use a microphone, call an external
    AI service, or load a second Whisper model.
    """
    enabled = DEFAULT_ENABLED

    def identify(self, sample, spoken_terms, candidates):
        raise DeferredIntegration("No stable session-correlated local audio handoff is enabled")


def status():
    return {"revision": REVISION, "enabled": DEFAULT_ENABLED, "active": False,
            "mode": "deferred-pure-local-proof", "liveIntegration": False,
            "reason": "Awaiting an explicit session-correlated sample contract; no audio is captured or retained",
            "maxCandidates": MAX_CANDIDATES, "maxSampleBytes": MAX_SAMPLE_BYTES}
