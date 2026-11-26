"""Isolated, optional local audio-assisted episode identification contract.

Status: DEFAULT OFF, wired to the existing language callback but inactive
unless explicitly enabled. This module defines the handoff from the existing
same-session bounded media sampling pipeline (the language/audio lane) *and*
the privacy-preserving pure ranking/corroboration adapter. It never records
audio, keeps transcripts, uses a microphone, contacts a remote service, or
loads another Whisper model.

The handoff is deliberately split into small, independently testable pieces:

* ``build_request`` accepts a transient transcript from the existing decode and
  transcribe pass, extracts a small bounded set of public-programme clues, and
  immediately drops the transcript.
* ``SessionBinding`` carries the exact channel/launch and durable physical
  ``visitID`` plus a short expiry. Promotion is rejected unless that exact
  visit is still the current device session. A stream reload inside one visit
  is accepted; a visit that ended is stale and rejected.
* ``corroborate_episode`` separates audio-derived content identity from
  scheduled-airing verification. Only official broadcaster archives and
  independent episode reports are accepted. Ads, reused promos, generic
  references and ambiguous matches are rejected.
* ``EpisodeBudget`` adds active-viewing priority plus bounded attempts,
  cooldowns, deduplication and a daily cap.
* ``EpisodeObserver`` is the callable adapter for
  ``language_audio.Manager(on_speech=...)``. It is disabled by default and its
  research search function is injected, so tests never perform a live call.

Nothing in this module mutates language exclusions, policy, rotation, health or
the Malay profile. The existing pure ``rank_episode_evidence`` surface is kept
unchanged for the historical deferred proof.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
from collections import OrderedDict

REVISION = "episode-audio-evidence-20261127-v2"
DEFAULT_ENABLED = False
MAX_SAMPLE_BYTES = 2 * 1024 * 1024
MAX_CANDIDATES = 32
MAX_TERMS = 64
MIN_TERM_LENGTH = 4
CHANNEL_RE = re.compile(r"[a-f0-9]{32}")
SESSION_RE = re.compile(r"[A-Za-z0-9_-]{1,160}")
DIGEST_RE = re.compile(r"[a-f0-9]{32,64}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
CODE_RE = re.compile(r"S(\d{2})E(\d{2,4})", re.IGNORECASE)

MAX_CLUES = 8
MAX_CLUE_TOKENS = 4
MAX_REPORTS = 64
MAX_OFFICIAL_EPISODES = 32
MIN_DISTINCTIVE_CLUES = 2
DEFAULT_SESSION_TTL = 60.0
MAX_SESSION_TTL = 120.0
MAX_OBSERVATION_AGE = 30.0
ACTIVE_VIEWING_SECONDS = 120.0
MAX_ATTEMPTS = 2
COOLDOWN_SECONDS = 300.0
DEDUP_SECONDS = 300.0
MAX_DAILY_EPISODE_CALLS = 24

ALLOWED_PROFILES = frozenset({"american"})
# Preserve the generalized-language gate: only an allowed English stream may
# produce programme clues. Excluded/unauthorized languages never reach research.
ALLOWED_LANGUAGES = frozenset({"en"})

# Bounded public source families. Extend only after an explicit source review.
OFFICIAL_ARCHIVE_HOSTS = frozenset({
    "cwtv.com", "www.cwtv.com",
    "wwe.com", "www.wwe.com",
})
INDEPENDENT_REPORT_HOSTS = frozenset({
    "f4wonline.com", "www.f4wonline.com",
    "pwinsider.com", "www.pwinsider.com",
    "wrestlinginc.com", "www.wrestlinginc.com",
    "sescoops.com", "www.sescoops.com",
    "wrestlingobserver.com", "www.wrestlingobserver.com",
})

GENERIC_TERMS = frozenset("""
about after again against almost alone along already also although always among another answer around
because before being below between both bring came come could does doing down during each either else
even ever every from give goes going good great have having here herself himself into just know last
like little long made make many might more most much must never next only other over people really
right said same should since some start still such take than that their them then there these they
thing think this those through time under until very want well were what when where which while will
with would your yours
""".split())

# Words that are programme-generic in isolation but useful inside a distinctive
# multi-word phrase (for example "women's North American champion").
WEAK_SINGLE_TERMS = frozenset("""
ad advertisement ads air airing broadcast champion championship clip clips demo event exclusive
first footage full highlight highlights live match matches night now official premiere preview
promo replay result results show special stream streaming teaser title tonight watch week
women womens mens north american south latin world heavyweight tag
""".split())

AD_PROMO_TERMS = frozenset("""
ad advertisement ads clip clips demo footage highlight highlights preview previews promo promos
replay replays teaser teasers trailer trailers watch
""".split())

CLUE_STOPWORDS = GENERIC_TERMS | frozenset("""
back becomes become becoming gets get got takes take taken makes make made wins win won
now today tonight week welcome
""".split())

_CLUE_TOKEN = re.compile(r"[a-z0-9]+(?:['\u2019][a-z0-9]+)?", re.IGNORECASE)


class DeferredIntegration(RuntimeError):
    """The live session-correlated audio contract is deliberately not active."""


def _digest(value):
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:32]


def _host(url):
    if not isinstance(url, str) or len(url) > 500:
        return ""
    match = re.match(r"https://([^/?#]+)/?", url.strip(), re.IGNORECASE)
    if not match:
        return ""
    host = match[1].lower()
    return host if re.fullmatch(r"[a-z0-9.-]+", host) and "." in host else ""


def _date(value):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value.strip()):
        return ""
    import datetime as dt
    try:
        dt.date.fromisoformat(value.strip())
    except ValueError:
        return ""
    return value.strip()


def _year(value):
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1888 <= year <= 2100 else None


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


def _clue_token(value):
    token = value.casefold().replace("\u2019", "'").replace("'", "")
    return token if token.isalnum() else ""


def _clue_candidates(value):
    """Return bounded distinctive clue strings from one transient string.

    The caller must never persist ``value``. Output is lowercase, short and
    contains only tokens/phrases, not surrounding dialogue.
    """
    if not isinstance(value, str) or not value:
        return []
    tokens = [token for token in (_clue_token(item) for item in _CLUE_TOKEN.findall(value)) if token]
    kept = []
    for token in tokens:
        if len(token) < MIN_TERM_LENGTH or token in CLUE_STOPWORDS:
            kept.append(None)
        else:
            # Weak single terms are allowed inside a distinctive phrase but
            # are never emitted on their own.
            kept.append(token)
    clues = []
    run = []
    runs = []
    for token in kept + [None]:
        if token:
            run.append(token)
        else:
            if run:
                runs.append(run)
            run = []
    for run in runs:
        if len(run) == 1:
            token = run[0]
            if len(token) >= 5 and token not in WEAK_SINGLE_TERMS and token not in AD_PROMO_TERMS:
                clues.append(token)
            continue
        if all(token in AD_PROMO_TERMS or token in CLUE_STOPWORDS for token in run):
            continue
        if len(run) <= MAX_CLUE_TOKENS:
            clues.append(" ".join(run))
        else:
            for start in range(len(run) - MAX_CLUE_TOKENS + 1):
                clues.append(" ".join(run[start:start + MAX_CLUE_TOKENS]))
        for token in run:
            if len(token) >= 5 and token not in WEAK_SINGLE_TERMS and token not in AD_PROMO_TERMS and token not in CLUE_STOPWORDS:
                clues.append(token)
    ordered = []
    for clue in sorted(set(clues), key=lambda item: (-len(item.split()), -len(item), item)):
        if clue not in ordered:
            ordered.append(clue)
        if len(ordered) >= MAX_CLUES:
            break
    return ordered


def extract_clues(text):
    """Extract minimal public-programme clues from a transient transcript."""
    return tuple(_clue_candidates(text))


def normalize_clues(values):
    """Normalize a clue string/list/set into bounded distinct clue strings."""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return ()
    out = []
    for value in values:
        for clue in _clue_candidates(value):
            if clue not in out:
                out.append(clue)
            if len(out) >= MAX_CLUES:
                return tuple(out)
    return tuple(out)


def _clue_token_set(clue):
    return frozenset(token for token in clue.split() if token)


def clue_matches(clues, terms):
    """Return the clues with a distinctive overlap against candidate terms."""
    left = normalize_clues(clues)
    right = normalize_clues(terms)
    matched = []
    for clue in left:
        a = _clue_token_set(clue)
        for other in right:
            b = _clue_token_set(other)
            overlap = a & b
            if clue == other:
                matched.append(clue)
                break
            if len(a) == 1 and len(overlap) == 1 and next(iter(a)) not in WEAK_SINGLE_TERMS:
                matched.append(clue)
                break
            if len(a) >= 2 and len(overlap) >= 2:
                matched.append(clue)
                break
    return tuple(matched)


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


@dataclasses.dataclass(frozen=True)
class SessionBinding:
    """Exact device playback session plus a bounded validity window.

    ``launch_id`` identifies the app launch, ``stream_generation`` records the
    stream generation observed with the sample, and ``visit_id`` is the durable
    physical-visit identity from the language visit ledger. When a visit
    identity is present it is the authority: a stream-generation change inside
    the same physical visit is a reload, not a new session, so the exact visit
    ID must still match. ``expires_at`` is a short local validity bound for the
    observation. A promotion is rejected if the binding is expired or the
    current probe no longer matches the same visit.
    """

    channel_id: str
    launch_id: str
    stream_generation: str
    observed_at: float
    expires_at: float
    visit_id: str = ""
    profile: str = "american"

    def checked(self, now=None):
        now = time.time() if now is None else float(now)
        if self.profile not in ALLOWED_PROFILES:
            return False
        if not isinstance(self.channel_id, str) or not CHANNEL_RE.fullmatch(self.channel_id):
            return False
        if not isinstance(self.launch_id, str) or not SESSION_RE.fullmatch(self.launch_id):
            return False
        if not isinstance(self.stream_generation, str) or not SESSION_RE.fullmatch(self.stream_generation):
            return False
        if self.visit_id and (not isinstance(self.visit_id, str) or not SESSION_RE.fullmatch(self.visit_id)):
            return False
        if type(self.observed_at) not in (int, float) or type(self.expires_at) not in (int, float):
            return False
        if not self.observed_at <= now < self.expires_at:
            return False
        if now - self.observed_at > MAX_OBSERVATION_AGE:
            return False
        return 0 < self.expires_at - self.observed_at <= MAX_SESSION_TTL

    def matches(self, current, now=None):
        now = time.time() if now is None else float(now)
        if not self.checked(now) or not isinstance(current, dict):
            return False
        if current.get("channelID") != self.channel_id:
            return False
        if current.get("launchID") != self.launch_id:
            return False
        if self.visit_id:
            # The durable physical visit is authoritative. A reload may change
            # the stream generation without ending the visit, so the exact
            # visit identity must match instead of the exact generation. A
            # missing current visit means the session cannot be proven and is
            # rejected as stale.
            other = current.get("visitID")
            return bool(other) and other == self.visit_id
        if current.get("streamGeneration") != self.stream_generation:
            return False
        return True

    def digest(self):
        # Visit-scoped so a same-visit reload cannot masquerade as a second
        # physical session or bypass duplicate suppression. Fall back to the
        # exact generation when no visit identity is available.
        if self.visit_id:
            return _digest("|".join((self.channel_id, self.launch_id, self.visit_id)))
        return _digest("|".join((self.channel_id, self.launch_id, self.stream_generation)))


def make_binding(job, snapshot, now=None, ttl=DEFAULT_SESSION_TTL):
    """Build a binding only when the job and current probe agree exactly.

    The language lane is expected to populate ``launchID`` and
    ``streamGeneration`` (the separate 8abac98 evidence branch does) plus the
    durable ``visitID``. Without launch/generation no binding is possible. When
    the job carries a visit identity, the probe must carry the same exact visit
    and a stream-generation change is accepted as a reload inside that visit.
    Without a visit identity the old exact-generation rule is retained and the
    observer will not promote.
    """
    if not isinstance(job, dict) or not isinstance(snapshot, dict):
        return None
    now = time.time() if now is None else float(now)
    cid = job.get("cid")
    launch = job.get("launchID")
    generation = job.get("streamGeneration")
    if not all(isinstance(value, str) and value for value in (cid, launch, generation)):
        return None
    if snapshot.get("channelID") != cid or snapshot.get("launchID") != launch:
        return None
    visit_id = str(job.get("visitID", "") or "")
    if visit_id:
        current_visit = snapshot.get("visitID")
        if not isinstance(current_visit, str) or current_visit != visit_id:
            return None
    elif snapshot.get("streamGeneration") != generation:
        return None
    observed = job.get("observedAt", now)
    expires = job.get("expiresAt", None)
    try:
        observed = float(observed)
        expires = float(expires) if expires is not None else observed + min(max(1.0, float(ttl)), MAX_SESSION_TTL)
    except (TypeError, ValueError):
        return None
    binding = SessionBinding(cid, launch, generation, observed, expires,
                             visit_id, str(job.get("profile", "american")))
    return binding if binding.checked(now) else None


@dataclasses.dataclass(frozen=True)
class ClueRequest:
    clues: tuple
    binding: SessionBinding
    channel_name: str
    language: str
    language_outcome: str
    language_probability: float
    speech_seconds: float
    sample_seconds: float
    sample_digest: str

    def research_payload(self):
        """Minimal public-programme clues only. Never includes the transcript."""
        return {"clues": list(self.clues), "channelName": self.channel_name,
                "language": self.language,
                "sampleSeconds": round(float(self.sample_seconds), 3),
                "speechSeconds": round(float(self.speech_seconds), 3)}


def build_request(text, job, decision, binding, now=None):
    """Convert a transient transcript into a bounded clue request, then drop it.

    No part of ``text`` is stored on the returned object. The caller must not
    log or persist ``text``.
    """
    now = time.time() if now is None else float(now)
    if not isinstance(binding, SessionBinding) or not binding.checked(now):
        return None
    if not isinstance(job, dict) or job.get("cid") != binding.channel_id:
        return None
    if job.get("deviceVerified") is not True or job.get("changed") is True:
        return None
    job_launch = job.get("launchID")
    if job_launch and job_launch != binding.launch_id:
        return None
    job_generation = job.get("streamGeneration")
    if job_generation and job_generation != binding.stream_generation:
        return None
    if not isinstance(decision, dict):
        return None
    if decision.get("outcome") != "allowed":
        return None
    language = decision.get("language")
    if language not in ALLOWED_LANGUAGES:
        return None
    clues = extract_clues(text)
    if len(clues) < MIN_DISTINCTIVE_CLUES:
        return None
    sample_digest = job.get("sampleDigest") or binding.digest()
    if not isinstance(sample_digest, str) or not DIGEST_RE.fullmatch(sample_digest):
        return None
    channel_name = job.get("name") if isinstance(job.get("name"), str) else ""
    channel_name = " ".join(channel_name.split())[:120]
    try:
        probability = float(decision.get("confidence", 0.0))
        speech = float(decision.get("speechSeconds", 0.0))
        sample = float(decision.get("sampleSeconds", 0.0))
    except (TypeError, ValueError):
        return None
    return ClueRequest(clues, binding, channel_name, language, "allowed",
                       probability, speech, sample, sample_digest)


@dataclasses.dataclass(frozen=True)
class OfficialEpisode:
    series_title: str
    season_number: int
    episode_number: int
    episode_title: str
    original_air_date: str
    broadcaster_code: str = ""
    series_premiere_year: int | None = None
    source_url: str = ""
    kind: str = "official-archive"

    def code(self):
        if self.broadcaster_code:
            match = CODE_RE.fullmatch(self.broadcaster_code.strip())
            if match:
                return "S%02dE%02d" % (int(match[1]), int(match[2])) if len(match[2]) == 2 else "S%02dE%s" % (int(match[1]), match[2])
        return "S%02dE%02d" % (self.season_number, self.episode_number)

    def episode_year(self):
        date = _date(self.original_air_date)
        return int(date[:4]) if date else None

    def checked(self):
        if not isinstance(self.series_title, str) or not 1 <= len(self.series_title) <= 200:
            return False
        if not isinstance(self.episode_title, str) or len(self.episode_title) > 300:
            return False
        if type(self.season_number) is not int or not 1 <= self.season_number <= 99:
            return False
        if type(self.episode_number) is not int or not 1 <= self.episode_number <= 9999:
            return False
        if not _date(self.original_air_date):
            return False
        if self.series_premiere_year is not None and _year(self.series_premiere_year) is None:
            return False
        if self.kind != "official-archive":
            return False
        host = _host(self.source_url)
        if host not in OFFICIAL_ARCHIVE_HOSTS:
            return False
        if self.broadcaster_code:
            match = CODE_RE.fullmatch(self.broadcaster_code.strip())
            if not match:
                return False
            if (int(match[1]), int(match[2])) != (self.season_number, self.episode_number):
                return False
        return True


@dataclasses.dataclass(frozen=True)
class IndependentReport:
    source_url: str
    published_at: str
    terms: tuple
    original_air_date: str
    series_title: str = ""
    season_number: int | None = None
    episode_number: int | None = None
    broadcaster_code: str = ""
    kind: str = "independent-report"
    advertisement: bool = False
    reused_promo: bool = False
    generic: bool = False

    def host(self):
        return _host(self.source_url)

    def rejection(self):
        if self.kind != "independent-report":
            return "advertisement" if "ad" in self.kind or "promo" in self.kind else "non-report"
        if self.advertisement:
            return "advertisement"
        if self.reused_promo:
            return "reused-promo"
        if self.generic:
            return "generic-reference"
        if self.host() not in INDEPENDENT_REPORT_HOSTS:
            return "unapproved-source"
        if not _date(self.published_at) or not _date(self.original_air_date):
            return "missing-date"
        if not normalize_clues(self.terms):
            return "generic-reference"
        if self.series_title and (not isinstance(self.series_title, str) or len(self.series_title) > 200):
            return "invalid-series"
        for value, limit in ((self.season_number, 99), (self.episode_number, 9999)):
            if value is not None and (type(value) is not int or not 0 < value <= limit):
                return "invalid-numbering"
        return ""


@dataclasses.dataclass(frozen=True)
class ContentIdentity:
    series_title: str
    season_number: int
    episode_number: int
    episode_title: str
    original_air_date: str
    broadcaster_code: str
    episode_year: int | None
    series_premiere_year: int | None = None

    def evidence(self):
        return {"seriesTitle": self.series_title,
                "seasonNumber": self.season_number,
                "episodeNumber": self.episode_number,
                "episodeTitle": self.episode_title,
                "officialCode": self.broadcaster_code,
                "originalAirDate": self.original_air_date,
                "episodeYear": self.episode_year,
                "seriesPremiereYear": self.series_premiere_year}


@dataclasses.dataclass(frozen=True)
class Verification:
    accepted: bool
    reason: str
    identity: ContentIdentity | None = None
    matched_clue_count: int = 0
    corroborating_sources: int = 0
    numbering_conflict: bool = False
    verification: str = ""


def _numbering_differs(report, official):
    if report.broadcaster_code and report.broadcaster_code.strip().upper() != official.code():
        return True
    if report.season_number is not None and report.season_number != official.season_number:
        return True
    if report.episode_number is not None and report.episode_number != official.episode_number:
        return True
    return False


def _identity_from_official(official):
    return ContentIdentity(
        official.series_title, official.season_number, official.episode_number,
        official.episode_title, _date(official.original_air_date),
        official.code(), official.episode_year(), official.series_premiere_year,
    )


def corroborate_episode(clues, official_episodes, reports, now=None, require_official=True,
                        min_matches=MIN_DISTINCTIVE_CLUES):
    """Identify one episode from audio clues plus public archive/report evidence.

    Content identity is derived only from the clue corroboration. A separately
    supplied schedule is never used to invent identity. The official
    broadcaster's numbering wins over differing third-party numbering, while the
    official original air date is retained.
    """
    now = time.time() if now is None else float(now)
    clue_set = normalize_clues(clues)
    if len(clue_set) < min_matches:
        return Verification(False, "insufficient-clues")
    if not isinstance(official_episodes, (list, tuple)):
        official_episodes = []
    if not isinstance(reports, (list, tuple)):
        reports = []
    official = []
    for item in official_episodes[:MAX_OFFICIAL_EPISODES]:
        if isinstance(item, OfficialEpisode) and item.checked():
            official.append(item)
    if require_official and not official:
        return Verification(False, "missing-official-archive")
    rows = []
    rejected = []
    for item in reports[:MAX_REPORTS]:
        if not isinstance(item, IndependentReport):
            continue
        reason = item.rejection()
        if reason:
            rejected.append(reason)
            continue
        matched = clue_matches(clue_set, item.terms)
        if len(matched) < min_matches:
            rejected.append("insufficient-clue-match")
            continue
        rows.append((item, matched))
    if not rows:
        for reason in ("advertisement", "reused-promo", "generic-reference", "missing-date",
                       "unapproved-source", "invalid-numbering", "insufficient-clue-match"):
            if reason in rejected:
                return Verification(False, reason)
        return Verification(False, "no-independent-report")
    groups = OrderedDict()
    for report, matched in rows:
        groups.setdefault(_date(report.original_air_date), []).append((report, matched))
    if require_official and len(groups) > 1:
        # A third-party numbering difference is tolerable; a conflicting
        # original air date is not.
        return Verification(False, "conflicting-air-date")
    candidates = []
    ambiguous_official = False
    for air_date, group in groups.items():
        matching_official = [item for item in official if _date(item.original_air_date) == air_date]
        if require_official:
            if len(matching_official) > 1:
                ambiguous_official = True
                continue
            if len(matching_official) != 1:
                continue
            official_row = matching_official[0]
        else:
            official_row = matching_official[0] if len(matching_official) == 1 else None
        valid_group = []
        for report, matched in group:
            if official_row is not None and report.series_title and report.series_title.casefold() != official_row.series_title.casefold():
                rejected.append("series-mismatch")
                continue
            valid_group.append((report, matched))
        if not valid_group:
            continue
        hosts = {report.host() for report, _matched in valid_group}
        if official_row is None and len(hosts) < 2:
            continue
        score = sum(len(matched) for _report, matched in valid_group)
        conflict = bool(official_row and any(_numbering_differs(report, official_row) for report, _matched in valid_group))
        candidates.append((score, official_row, air_date, valid_group, conflict))
    if not candidates:
        if ambiguous_official:
            return Verification(False, "ambiguous-episode")
        if rejected and all(reason in ("series-mismatch",) for reason in rejected):
            return Verification(False, "series-mismatch")
        return Verification(False, "missing-official-air-date-match" if require_official else "no-corroboration")
    candidates.sort(key=lambda row: row[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return Verification(False, "ambiguous-episode")
    score, official_row, _air_date, group, conflict = candidates[0]
    if official_row is None:
        # Reports-only mode is not enabled for promotion; keep the reason
        # explicit instead of inventing an official code.
        return Verification(False, "missing-official-archive")
    identity = _identity_from_official(official_row)
    return Verification(True, "", identity, matched_clue_count=score,
                        corroborating_sources=len({report.host() for report, _m in group}),
                        numbering_conflict=conflict,
                        verification="official-and-independent-report")


@dataclasses.dataclass(frozen=True)
class PromotedEpisode:
    channel_id: str
    sample_digest: str
    binding: SessionBinding
    identity: ContentIdentity
    confidence: float
    matched_clue_count: int
    corroborating_sources: int
    numbering_conflict: bool
    verification: str
    scheduled_airing: dict | None = None

    def evidence(self):
        payload = {"source": "local-audio-episode-identity", "revision": REVISION,
                   "channelID": self.channel_id, "sampleDigest": self.sample_digest,
                   "sessionDigest": self.binding.digest(),
                   "launchDigest": _digest(self.binding.launch_id),
                   "streamGenerationDigest": _digest(self.binding.stream_generation),
                   "sessionExpiresAt": round(self.binding.expires_at, 3),
                   "confidence": round(self.confidence, 3),
                   "matchedClueCount": self.matched_clue_count,
                   "corroboratingSources": self.corroborating_sources,
                   "numberingConflict": self.numbering_conflict,
                   "verification": self.verification}
        payload.update(self.identity.evidence())
        payload["scheduledAiring"] = self.scheduled_airing
        return payload


@dataclasses.dataclass(frozen=True)
class Promotion:
    accepted: bool
    reason: str
    episode: PromotedEpisode | None = None


def _event_value(event, key):
    if isinstance(event, dict):
        return event.get(key)
    return getattr(event, key, None)


def match_scheduled_airing(identity, events, now=None, station=None):
    """Return a separate scheduled-airing record, never content identity."""
    now = time.time() if now is None else float(now)
    if not isinstance(events, (list, tuple)):
        return None
    active = []
    series = re.sub(r"[^a-z0-9]", "", identity.series_title.casefold())
    for event in events:
        start = _event_value(event, "start")
        end = _event_value(event, "end")
        title = _event_value(event, "title")
        event_station = _event_value(event, "station")
        try:
            start = float(start)
            end = float(end)
        except (TypeError, ValueError):
            continue
        if not (start <= now < end):
            continue
        if station and event_station and event_station != station:
            continue
        normalized = re.sub(r"[^a-z0-9]", "", str(title or "").casefold())
        if normalized and series and normalized not in (series, re.sub(r"[^a-z0-9]", "", identity.episode_title.casefold())):
            continue
        active.append(event)
    if len(active) != 1:
        return None
    event = active[0]
    return {"station": _event_value(event, "station") or station or "",
            "start": float(_event_value(event, "start")),
            "end": float(_event_value(event, "end")),
            "source": _event_value(event, "source") or "public-schedule",
            "observedAt": now}


def promote_episode(request, official_episodes, reports, current, now=None, scheduled_events=None,
                    programme=None):
    now = time.time() if now is None else float(now)
    if not isinstance(request, ClueRequest):
        return Promotion(False, "invalid-request")
    if not request.binding.checked(now):
        return Promotion(False, "session-expired")
    if not request.binding.matches(current, now):
        return Promotion(False, "session-mismatch")
    verification = corroborate_episode(request.clues, official_episodes, reports, now=now)
    if not verification.accepted or verification.identity is None:
        return Promotion(False, verification.reason)
    if not programme_matches(verification.identity, programme):
        return Promotion(False, "programme-mismatch")
    confidence = min(0.99, 0.5 + 0.1 * verification.matched_clue_count + 0.1 * verification.corroborating_sources)
    airing = match_scheduled_airing(verification.identity, scheduled_events, now) if scheduled_events else None
    episode = PromotedEpisode(request.binding.channel_id, request.sample_digest,
                              request.binding, verification.identity, confidence,
                              verification.matched_clue_count, verification.corroborating_sources,
                              verification.numbering_conflict, verification.verification, airing)
    return Promotion(True, "", episode)


def _view_value(view, *names):
    if isinstance(view, dict):
        for name in names:
            if name in view:
                return view[name]
        return None
    for name in names:
        if hasattr(view, name):
            return getattr(view, name)
    return None


def programme_has_episode_identity(row):
    """True only for a verified season/episode identity, never a title alone."""
    if not isinstance(row, dict):
        return False
    season = row.get("seasonNumber", row.get("season_number"))
    number = row.get("episodeNumber", row.get("episode_number"))
    return type(season) is int and 1 <= season <= 99 and type(number) is int and 1 <= number <= 9999


def _bounded_int(value, low, high):
    if type(value) is int and low <= value <= high:
        return value
    return None


def programme_context(row, channel_id=""):
    """Reduce an existing programme metadata row to bounded public context.

    Only public programme metadata is retained: series/episode titles, numbers
    and the episode-scoped original air date. No transcript or audio is read.
    Returns ``None`` when there is no usable series title.
    """
    if not isinstance(row, dict):
        return None
    title = row.get("seriesTitle") or row.get("title") or row.get("name")
    if not isinstance(title, str):
        return None
    title = " ".join(title.split())[:200]
    if not title:
        return None
    cid = row.get("channelID") or channel_id
    if cid and (not isinstance(cid, str) or not CHANNEL_RE.fullmatch(cid)):
        return None
    episode_title = row.get("episodeTitle") or row.get("episode_title") or ""
    episode_title = " ".join(str(episode_title).split())[:300] if episode_title else ""
    air_date = _date(row.get("originalAirDate") or row.get("original_air_date")
                     or row.get("airDate") or row.get("air_date") or "")
    season = _bounded_int(row.get("seasonNumber", row.get("season_number")), 1, 99)
    number = _bounded_int(row.get("episodeNumber", row.get("episode_number")), 1, 9999)
    return {"channelID": cid or "", "seriesTitle": title,
            "seasonNumber": season, "episodeNumber": number,
            "episodeTitle": episode_title, "originalAirDate": air_date,
            "verified": season is not None and number is not None}


def _norm_text(value):
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def source_key(value):
    """Public helper: normalized series key used by the source registry."""
    return _norm_text(value)


def programme_matches(identity, context):
    """Strict channel-agnostic programme/identity agreement.

    An absent context imposes no additional constraint. A present context must
    agree on the series title and, when it carries them, on the official
    numbering or episode-scoped original air date. A mismatch is never promoted.
    """
    if not isinstance(identity, ContentIdentity):
        return False
    if not isinstance(context, dict):
        return True
    series = context.get("seriesTitle")
    if isinstance(series, str) and series and _norm_text(series) != _norm_text(identity.series_title):
        return False
    season = context.get("seasonNumber")
    if type(season) is int and season != identity.season_number:
        return False
    number = context.get("episodeNumber")
    if type(number) is int and number != identity.episode_number:
        return False
    air_date = _date(context.get("originalAirDate"))
    if air_date and air_date != identity.original_air_date:
        return False
    return True



def _session_key(view):
    channel = _view_value(view, "channel_id", "channelID")
    session = _view_value(view, "session_hash", "sessionHash", "session_id", "sessionID", "play_session_id")
    launch = _view_value(view, "launch_id", "launchID")
    generation = _view_value(view, "stream_generation", "streamGeneration")
    if not isinstance(channel, str) or not channel:
        return ""
    values = [channel, str(session or ""), str(launch or ""), str(generation or "")]
    key = "|".join(values)
    return key if any(values[1:]) else ""


class EpisodeBudget:
    """Bounded active-viewing gate with cooldown, dedup and a daily cap."""

    def __init__(self, active_seconds=ACTIVE_VIEWING_SECONDS, max_attempts=MAX_ATTEMPTS,
                 cooldown_seconds=COOLDOWN_SECONDS, dedup_seconds=DEDUP_SECONDS,
                 daily_budget=MAX_DAILY_EPISODE_CALLS, clock=None):
        self.active_seconds = max(1.0, float(active_seconds))
        self.max_attempts = max(1, min(8, int(max_attempts)))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.dedup_seconds = max(0.0, float(dedup_seconds))
        self.daily_budget = max(1, int(daily_budget))
        self.clock = clock or time.monotonic
        self.windows = OrderedDict()
        self.calls = []
        self.seen = OrderedDict()

    def _prune(self, now):
        cutoff = now - 86400.0
        self.calls = [value for value in self.calls if value > cutoff]
        for key, row in list(self.windows.items()):
            if row["last"] < cutoff and len(row["attempts"]) >= self.max_attempts:
                self.windows.pop(key, None)
        while len(self.windows) > 512:
            self.windows.popitem(last=False)
        for key, value in list(self.seen.items()):
            if value < cutoff:
                self.seen.pop(key, None)
        while len(self.seen) > 1024:
            self.seen.popitem(last=False)

    def eligible(self, view, *, has_episode_identity, profile, language, language_outcome,
                 now=None, dedup_key=""):
        now = self.clock() if now is None else float(now)
        if profile not in ALLOWED_PROFILES:
            return False, "profile"
        if language_outcome != "allowed" or language not in ALLOWED_LANGUAGES:
            return False, "language-gated"
        if has_episode_identity:
            return False, "identity-present"
        key = _session_key(view)
        if not key:
            return False, "session-unverified"
        try:
            active = float(_view_value(view, "active_seconds", "activeSeconds") or 0.0)
        except (TypeError, ValueError):
            active = 0.0
        if active < self.active_seconds:
            return False, "inactive"
        self._prune(now)
        if dedup_key and now - self.seen.get(dedup_key, -86400.0) < self.dedup_seconds:
            return False, "duplicate-search"
        row = self.windows.get(key)
        if row:
            if row["attempts"] and now - row["last"] < self.cooldown_seconds:
                return False, "duplicate"
            if len(row["attempts"]) >= self.max_attempts:
                return False, "attempt-limit"
        if len(self.calls) >= self.daily_budget:
            return False, "budget"
        return True, ""

    def begin(self, view, now=None, dedup_key=""):
        now = self.clock() if now is None else float(now)
        key = _session_key(view)
        if not key:
            return False
        self._prune(now)
        row = self.windows.get(key)
        if row is None:
            row = {"attempts": [], "last": now, "outcome": ""}
            self.windows[key] = row
        row["attempts"].append(now)
        row["last"] = now
        self.windows.move_to_end(key)
        self.calls.append(now)
        if dedup_key:
            self.seen[dedup_key] = now
            self.seen.move_to_end(dedup_key)
        return True

    def finish(self, view, outcome, now=None):
        now = self.clock() if now is None else float(now)
        row = self.windows.get(_session_key(view))
        if row is not None:
            row["outcome"] = str(outcome)[:32]
            row["last"] = now
        return row is not None

    def diagnostics(self, now=None):
        now = self.clock() if now is None else float(now)
        self._prune(now)
        rows = []
        for key, row in list(self.windows.items())[-16:]:
            rows.append({"sessionHash": _digest(key)[:12], "attempts": len(row["attempts"]),
                         "lastOutcome": row["outcome"] or None,
                         "nextEligibleSeconds": max(0.0, round(self.cooldown_seconds - (now - row["last"]), 1))})
        return {"revision": REVISION, "activeSeconds": self.active_seconds,
                "maxAttempts": self.max_attempts, "cooldownSeconds": self.cooldown_seconds,
                "dedupSeconds": self.dedup_seconds, "dedupEntries": len(self.seen),
                "dailyBudget": self.daily_budget,
                "callsLast24Hours": len(self.calls), "windows": rows}


MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_JSONLD_OBJECTS = 256
MAX_SOURCE_PAGES = 8
MAX_PAGE_TEXT = 8000


def _json_types(value):
    raw = value.get("@type") if isinstance(value, dict) else None
    if isinstance(raw, str):
        return {raw.casefold()}
    if isinstance(raw, (list, tuple)):
        return {str(item).casefold() for item in raw if isinstance(item, str)}
    return set()


_JSONLD_SCRIPT = re.compile(
    r"<script[^>]*type=['\"]application/ld\+json['\"][^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)


def _jsonld_objects(page):
    """Bounded JSON-LD extraction from a public archive or report page."""
    if isinstance(page, (bytes, bytearray)):
        if len(page) > MAX_PAGE_BYTES:
            return []
        page = bytes(page).decode("utf-8", "replace")
    if not isinstance(page, str) or len(page) > MAX_PAGE_BYTES:
        return []
    rows = []
    for raw in _JSONLD_SCRIPT.findall(page)[:MAX_JSONLD_OBJECTS]:
        try:
            item = json.loads(raw)
        except (ValueError, TypeError):
            continue
        stack = [item]
        while stack and len(rows) < MAX_JSONLD_OBJECTS:
            value = stack.pop()
            if isinstance(value, list):
                stack.extend(value[:MAX_JSONLD_OBJECTS])
                continue
            if not isinstance(value, dict):
                continue
            rows.append(value)
            for key in ("@graph", "itemListElement", "hasPart", "subjectOf"):
                if key in value:
                    stack.append(value[key])
    return rows


def _page_date(value):
    if not isinstance(value, (str, int, float)):
        return ""
    text = str(value).strip()
    return _date(text[:10])


def _episode_numbers(obj):
    season = None
    number = None
    parent = obj.get("partOfSeason") if isinstance(obj, dict) else None
    if isinstance(parent, dict):
        season = _bounded_int(parent.get("seasonNumber"), 1, 99)
        number = _bounded_int(parent.get("episodeNumber"), 1, 9999)
    if season is None and isinstance(obj, dict):
        season = _bounded_int(obj.get("seasonNumber"), 1, 99)
    if number is None and isinstance(obj, dict):
        number = _bounded_int(obj.get("episodeNumber"), 1, 9999)
    return season, number


def _series_title(obj, fallback=""):
    if isinstance(obj, dict):
        parent = obj.get("partOfSeries")
        if isinstance(parent, dict) and isinstance(parent.get("name"), str):
            return " ".join(parent["name"].split())[:200]
        if isinstance(obj.get("seriesName"), str):
            return " ".join(obj["seriesName"].split())[:200]
    return fallback


def parse_official_page(page, source_url, fallback_series=""):
    """Parse bounded official-archive JSON-LD into typed official episodes."""
    host = _host(source_url)
    if host not in OFFICIAL_ARCHIVE_HOSTS:
        return []
    rows = []
    for obj in _jsonld_objects(page):
        if not _json_types(obj).intersection({"tvepisode", "episode", "broadcastevent"}):
            continue
        series = _series_title(obj, fallback_series)
        title = obj.get("name") or obj.get("headline") or obj.get("name")
        title = " ".join(str(title).split())[:300] if title else ""
        season, number = _episode_numbers(obj)
        air = _page_date(obj.get("datePublished") or obj.get("startDate") or obj.get("uploadDate"))
        if not series or season is None or number is None or not air:
            continue
        code = "S%02dE%02d" % (season, number)
        item = OfficialEpisode(series, season, number, title, air, code, None, source_url)
        if item.checked() and item.code() not in {existing.code() for existing in rows}:
            rows.append(item)
        if len(rows) >= MAX_OFFICIAL_EPISODES:
            break
    return rows


def _episode_from_text(text):
    match = CODE_RE.search(text or "")
    if not match:
        return None, None
    return int(match[1]), int(match[2])


def parse_report_page(page, source_url, context=None):
    """Parse bounded independent-report JSON-LD into typed reports."""
    host = _host(source_url)
    if host not in INDEPENDENT_REPORT_HOSTS:
        return []
    context = context if isinstance(context, dict) else {}
    fallback_series = context.get("seriesTitle") if isinstance(context.get("seriesTitle"), str) else ""
    fallback_air = _date(context.get("originalAirDate"))
    rows = []
    for obj in _jsonld_objects(page):
        if not _json_types(obj).intersection({"newsarticle", "article", "reportagenewsarticle"}):
            continue
        headline = " ".join(str(obj.get("headline") or obj.get("name") or "").split())[:300]
        description = " ".join(str(obj.get("description") or obj.get("abstract") or "").split())[:MAX_PAGE_TEXT]
        text = (headline + " " + description).strip()
        if not text:
            continue
        published = _page_date(obj.get("datePublished") or obj.get("dateModified"))
        dates = DATE_RE.findall(text)
        air = dates[0] if dates else fallback_air
        if not published or not air:
            continue
        lowered = headline.casefold()
        season, number = _episode_from_text(text)
        terms = normalize_clues(text)
        report = IndependentReport(
            source_url, published, terms, air,
            fallback_series, season, number, "",
            "independent-report",
            advertisement=("advertisement" in text.casefold() or "sponsored" in text.casefold()),
            reused_promo=any(word in lowered.split() for word in ("promo", "promos", "trailer", "teaser", "clip")),
            generic=not terms,
        )
        if not report.rejection():
            rows.append(report)
        if len(rows) >= MAX_REPORTS:
            break
    return rows


class PublicEvidenceResearch:
    """Actual bounded research adapter for the audio episode handoff.

    It consumes the minimal clue payload plus the gateway's already-observed
    programme context, then reads only explicitly reviewed official broadcaster
    archive and independent-report URLs from a private registry. Network access
    is injected (``fetch_page``); no media stream, microphone or Whisper pass is
    started here. An empty registry or any lookup failure yields no evidence, so
    the handoff stays inconclusive rather than guessing.
    """

    def __init__(self, fetch_page, source_registry=None, max_pages=MAX_SOURCE_PAGES):
        self.fetch_page = fetch_page
        self.source_registry = source_registry if isinstance(source_registry, dict) else {}
        self.max_pages = max(1, min(MAX_SOURCE_PAGES, int(max_pages)))

    def _urls(self, context):
        series = context.get("seriesTitle") if isinstance(context, dict) else ""
        entry = self.source_registry.get(source_key(series)) if series else None
        if not isinstance(entry, dict):
            return [], []
        official = [url for url in entry.get("official", []) if isinstance(url, str)]
        reports = [url for url in entry.get("reports", []) if isinstance(url, str)]
        return official[:self.max_pages], reports[:self.max_pages]

    def _fetch(self, url, allowed_hosts):
        if not callable(self.fetch_page) or _host(url) not in allowed_hosts:
            return None
        try:
            page = self.fetch_page(url)
        except Exception:
            return None
        if isinstance(page, (bytes, bytearray)) and len(page) > MAX_PAGE_BYTES:
            return None
        if isinstance(page, str) and len(page) > MAX_PAGE_BYTES:
            return None
        return page

    def __call__(self, payload):
        context = payload.get("programme") if isinstance(payload, dict) else None
        if not isinstance(context, dict) or not context.get("seriesTitle"):
            return [], []
        official_urls, report_urls = self._urls(context)
        official = []
        for url in official_urls:
            page = self._fetch(url, OFFICIAL_ARCHIVE_HOSTS)
            if page is not None:
                official.extend(parse_official_page(page, url, context.get("seriesTitle", "")))
        reports = []
        for url in report_urls:
            page = self._fetch(url, INDEPENDENT_REPORT_HOSTS)
            if page is not None:
                reports.extend(parse_report_page(page, url, context))
        return official[:MAX_OFFICIAL_EPISODES], reports[:MAX_REPORTS]


class EpisodeObserver:
    """Callable handoff for ``language_audio.Manager(on_speech=...)``.

    Disabled by default. ``research`` is injected and receives only
    ``ClueRequest.research_payload()``; it must return
    ``(official_episodes, independent_reports)``. It is responsible for using
    approved official archives and independent reports only.
    """

    def __init__(self, probe, research, *, enabled=DEFAULT_ENABLED, budget=None,
                 ttl=DEFAULT_SESSION_TTL, log=None, programme=None, activity=None):
        self.probe = probe
        self.research = research
        self.enabled = bool(enabled)
        self.budget = budget
        self.ttl = ttl
        self.log = log or (lambda message: None)
        self.programme = programme
        self.activity = activity
        self._published = OrderedDict()

    def _context(self, channel_id):
        if not callable(self.programme):
            return None
        try:
            row = self.programme(channel_id)
        except Exception:
            return None
        return row if isinstance(row, dict) else None

    def _view(self, binding, job):
        view = {"channel_id": binding.channel_id, "session_hash": binding.digest(),
                "active_seconds": (job.get("activeSeconds") if isinstance(job, dict) else 0) or 0,
                "hasEpisodeIdentity": bool(job.get("hasEpisodeIdentity")) if isinstance(job, dict) else False}
        if callable(self.activity):
            try:
                current = self.activity(binding.channel_id)
            except Exception:
                current = None
            if isinstance(current, dict) and current.get("channel_id") == binding.channel_id:
                view.update(current)
                view["channel_id"] = binding.channel_id
                view["session_hash"] = binding.digest()
        return view

    def _duplicate(self, binding, identity, now):
        key = "|".join((binding.digest(), identity.broadcaster_code,
                        _digest(identity.series_title + "|" + identity.original_air_date)))
        expires = self._published.get(key)
        if expires is not None and now < expires:
            return True
        self._published[key] = now + max(float(self.ttl), DEDUP_SECONDS)
        self._published.move_to_end(key)
        while len(self._published) > 64:
            self._published.popitem(last=False)
        return False

    def __call__(self, job, decision, text):
        if not self.enabled:
            return None
        try:
            now = time.time()
            snapshot = self.probe()
            binding = make_binding(job, snapshot, now, self.ttl)
            if binding is None or not binding.visit_id:
                return None
            context = self._context(binding.channel_id)
            if context is not None:
                if context.get("channelID") and context.get("channelID") != binding.channel_id:
                    return None
                if programme_has_episode_identity(context):
                    return None
            request = build_request(text, job, decision, binding, now)
            if request is None:
                return None
            if self.budget is not None:
                view = self._view(binding, job)
                dedup_key = _digest(binding.channel_id + "|" + _norm_text((context or {}).get("seriesTitle"))
                                    + "|" + "|".join(request.clues))
                allowed, _reason = self.budget.eligible(
                    view, has_episode_identity=bool(view.get("hasEpisodeIdentity")),
                    profile=binding.profile, language=request.language,
                    language_outcome=request.language_outcome, dedup_key=dedup_key)
                if not allowed:
                    return None
                self.budget.begin(view, dedup_key=dedup_key)
            payload = request.research_payload()
            if context is not None:
                payload = dict(payload, channelID=binding.channel_id, programme=context)
            if not callable(self.research):
                official, reports = [], []
            else:
                official, reports = self.research(payload)
            promotion = promote_episode(request, official, reports, snapshot, now, programme=context)
            if promotion.accepted and promotion.episode is not None:
                if self._duplicate(binding, promotion.episode.identity, now):
                    promotion = Promotion(False, "duplicate-publication")
            if self.budget is not None:
                self.budget.finish(view, "promoted" if promotion.accepted else promotion.reason)
            return promotion
        except Exception as error:
            self.log("episode clue observer error " + type(error).__name__)
            return None


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
            "mode": "default-off-wired-handoff", "liveIntegration": False,
            "gatewayWired": True,
            "reason": "Handoff is wired to the single-pass language callback but activation stays default off; no live capture, duplicate Whisper run or unattended research is active",
            "maxCandidates": MAX_CANDIDATES, "maxSampleBytes": MAX_SAMPLE_BYTES,
            "handoff": {"contract": "language_audio.Manager(on_speech=...)",
                        "defaultOff": True,
                        "allowedProfile": "american",
                        "allowedLanguages": sorted(ALLOWED_LANGUAGES),
                        "requires": ["channelID", "launchID", "streamGeneration",
                                     "visitID", "observedAt", "expiresAt", "deviceVerified"],
                        "matches": ["channel", "programme", "visit"],
                        "maxClues": MAX_CLUES,
                        "maxSessionTtlSeconds": MAX_SESSION_TTL,
                        "maxObservationAgeSeconds": MAX_OBSERVATION_AGE,
                        "activeViewingSeconds": ACTIVE_VIEWING_SECONDS,
                        "maxAttempts": MAX_ATTEMPTS,
                        "cooldownSeconds": COOLDOWN_SECONDS,
                        "dedupSeconds": DEDUP_SECONDS,
                        "dailyBudget": MAX_DAILY_EPISODE_CALLS,
                        "officialArchiveHosts": sorted(OFFICIAL_ARCHIVE_HOSTS),
                        "independentReportHosts": sorted(INDEPENDENT_REPORT_HOSTS),
                        "researchAttendance": "injected-reviewed-source-registry",
                        "maxSourcePages": MAX_SOURCE_PAGES,
                        "searchEnabled": False}}
