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
import html as html_module
import ipaddress
import json
import math
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
from collections import OrderedDict

REVISION = "episode-audio-evidence-20261127-v3"
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

# One rejected research attempt (no source, no match or an ad/promo-only page)
# may be retried a small, bounded number of times in the same physical visit
# while real playback continues. The retry reuses the existing audio sampler
# and never records a second language evidence event.
MAX_EPISODE_RETRIES = 2
EPISODE_RETRY_SECONDS = 8.0
COMMERCIAL_REASONS = frozenset({"advertisement", "reused-promo", "commercial"})
NO_MATCH_REASONS = frozenset({
    "missing-official-archive", "no-independent-report", "insufficient-clue-match",
    "missing-official-air-date-match", "no-corroboration", "no-match",
    "series-mismatch", "conflicting-air-date", "ambiguous-episode",
    "ambiguous-content", "no-typed-corroboration",
})
RETRYABLE_REASONS = COMMERCIAL_REASONS | NO_MATCH_REASONS


def reason_category(reason):
    """Stable diagnostics category for a rejection reason."""
    value = str(reason or "")
    if value in COMMERCIAL_REASONS:
        return "commercial"
    if value in NO_MATCH_REASONS:
        return "no-match"
    if value in ("session-expired", "session-changed", "session-mismatch",
                 "session-unprobeable", "programme-changed", "stale"):
        return "stale"
    if value in ("duplicate-publication", "duplicate-search"):
        return "duplicate"
    return value or "unknown"

# The existing language sampler completes a verified sample of at least
# MIN_EVIDENCE_SAMPLE_SECONDS of real progressing playback before it hands over
# a transcript.  A typical 60-second rotation never reaches the 120-second
# active-research priority, so the audio-episode lane may use the verified
# sample window as its own bounded active-viewing floor.  This changes neither
# rotation nor any language confidence/threshold value; it only makes a
# sample-backed, same-visit callback eligible for bounded episode research.
MIN_EVIDENCE_SAMPLE_SECONDS = 40.0
AUDIO_MIN_ACTIVE_SECONDS = 40.0
UNKNOWN_LEAD_PROGRESS_SECONDS = 12.0
UNKNOWN_PUBLISH_PROGRESS_SECONDS = 40.0
UNKNOWN_CONFIRM_WAIT_SECONDS = 12.0

# Bounded publication into the existing programme metadata path.
PUBLICATION_TTL = 1800.0
MAX_PUBLICATIONS = 64
MAX_RESEARCH_SECONDS = 20.0
UNKNOWN_RESEARCH_SECONDS = 30.0
UNKNOWN_PAGE_TIMEOUT_SECONDS = 3.0
UNKNOWN_PROVIDER_SECONDS = 18.0
MAX_PENDING = 8
PENDING_DEDUP_SECONDS = 120.0

# Automatic source discovery is non-date-specific: reviewed official archive
# roots plus an optional bounded search provider. The private registry remains
# an explicit override for a reviewed episode. Candidate and parsed-evidence
# caches are bounded, in-memory and never contain a transcript.
SOURCE_CANDIDATE_TTL = 1800.0
SOURCE_NEGATIVE_TTL = 300.0
MAX_SOURCE_CACHE = 64
MAX_AUTO_CANDIDATES = 8
MAX_INDEX_LINKS = 64
MAX_INDEX_PAGES = 4
MAX_PROVIDER_CANDIDATES = 8

ALLOWED_PROFILES = frozenset({"american"})
# Preserve the generalized-language gate: only an allowed English stream may
# produce programme clues. Excluded/unauthorized languages never reach research.
ALLOWED_LANGUAGES = frozenset({"en"})

# Bounded public source families. Extend only after an explicit source review.
# The lane is not wrestling-specific: general broadcaster archives and general
# television episode reports are accepted when their pages publish an explicit
# episode-scoped original air date.
OFFICIAL_ARCHIVE_HOSTS = frozenset({
    "cwtv.com", "www.cwtv.com",
    "wwe.com", "www.wwe.com",
    "showtime.com", "www.showtime.com", "sho.com", "www.sho.com",
    "nbc.com", "www.nbc.com", "abc.com", "www.abc.com",
    "cbs.com", "www.cbs.com", "fox.com", "www.fox.com",
    "amc.com", "www.amc.com", "hbo.com", "www.hbo.com",
    "max.com", "www.max.com", "usanetwork.com", "www.usanetwork.com",
    "syfy.com", "www.syfy.com", "tntdrama.com", "www.tntdrama.com",
    "tbs.com", "www.tbs.com", "bravotv.com", "www.bravotv.com",
    "paramountnetwork.com", "www.paramountnetwork.com",
    "peacocktv.com", "www.peacocktv.com", "hulu.com", "www.hulu.com",
    "bbc.co.uk", "www.bbc.co.uk", "bbc.com", "www.bbc.com",
    "itv.com", "www.itv.com", "channel4.com", "www.channel4.com",
    "channel5.com", "www.channel5.com", "uktv.co.uk", "www.uktv.co.uk",
    "cbc.ca", "www.cbc.ca", "ctv.ca", "www.ctv.ca",
})
INDEPENDENT_REPORT_HOSTS = frozenset({
    "f4wonline.com", "www.f4wonline.com",
    "pwinsider.com", "www.pwinsider.com",
    "wrestlinginc.com", "www.wrestlinginc.com",
    "sescoops.com", "www.sescoops.com",
    "pwmania.com", "www.pwmania.com",
    "wrestlingobserver.com", "www.wrestlingobserver.com",
    "tvline.com", "www.tvline.com",
    "ew.com", "www.ew.com",
    "variety.com", "www.variety.com",
    "hollywoodreporter.com", "www.hollywoodreporter.com",
    "avclub.com", "www.avclub.com",
    "vulture.com", "www.vulture.com",
    "ign.com", "www.ign.com",
    "decider.com", "www.decider.com",
    "tvguide.com", "www.tvguide.com",
    "theguardian.com", "www.theguardian.com", "radiotimes.com", "www.radiotimes.com",
    "digitalspy.com", "www.digitalspy.com",
})

# Typed content is a distinct evidence lane. Existing approved broadcaster
# and editorial domains may be evaluated; additional domains require reviewed
# source profiles. Never interpret a movie/event as an episode.
TYPED_CONTENT_KINDS = frozenset({"series", "movie", "sports-event", "other-programme"})
TYPED_OFFICIAL_HOSTS = OFFICIAL_ARCHIVE_HOSTS | frozenset({"amblin.com", "www.amblin.com", "indycar.com", "www.indycar.com"})
TYPED_REPORT_HOSTS = INDEPENDENT_REPORT_HOSTS | frozenset({"screamhorrormag.com", "www.screamhorrormag.com", "theapex.racing", "www.theapex.racing", "racingnews.co", "www.racingnews.co"})


# Reviewed, non-date-specific official archive roots. A known series can be
# tried as a direct dated archive URL (``<root>/YYYY-MM-DD``) and as an index
# page whose dated links are bounded. This is deliberately a small reviewed
# map, not an open crawler; the private registry can override it.
DEFAULT_OFFICIAL_HINTS = {
    "wwenxt": ("https://www.wwe.com/shows/wwenxt",),
    "wweraw": ("https://www.wwe.com/shows/raw",),
    "wwesmackdown": ("https://www.wwe.com/shows/smackdown",),
}
DEFAULT_REPORT_HINTS = {}

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


def independent_publisher(url):
    """Canonical publisher identity, distinct from the source URL host."""
    host = _host(url).removeprefix("www.")
    return "f4wonline.com" if host == "wrestlingobserver.com" else host


def _public_host(value):
    """Return a syntactically public DNS host, or an empty string.

    IP literals, credentials, malformed labels and non-443 ports are rejected
    before any source URL can reach a page reader. The caller still applies the
    approved-host allowlist and the gateway still resolves DNS before fetching.
    """
    host = str(value or "").strip().lower()
    if not host or len(host) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
        return ""
    if "." not in host or ".." in host:
        return ""
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    if any(not label or len(label) > 63 for label in host.split(".")):
        return ""
    return host


def safe_source_url(value, allowed_hosts, allow_query=True):
    """Validate one automatic candidate against an explicit host allowlist.

    Only HTTPS, one of the approved DNS hosts, no credentials, no fragment and
    the default port are accepted. Query strings are optional because a reviewed
    independent report may legitimately carry one; they are bounded and never
    used to select a destination host.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 500:
        return ""
    try:
        parts = urllib.parse.urlsplit(value.strip())
    except (TypeError, ValueError):
        return ""
    if parts.scheme.casefold() != "https" or parts.username or parts.password:
        return ""
    try:
        if parts.port not in (None, 443):
            return ""
    except ValueError:
        return ""
    host = _public_host(parts.hostname)
    if not host or host not in allowed_hosts:
        return ""
    if parts.fragment or any(ord(character) < 0x21 for character in value if character not in "\t"):
        return ""
    query = parts.query if allow_query else ""
    if len(query) > 300 or any(character in query for character in "\r\n\x00"):
        return ""
    path = parts.path or "/"
    if len(path) > 400 or any(character in path for character in "\r\n\x00"):
        return ""
    return urllib.parse.urlunsplit(("https", host, path, query, ""))


def _same_site(left, right):
    """Compare hosts ignoring only the conventional ``www.`` alias."""
    a = _public_host(left)
    b = _public_host(right)
    return bool(a and b and a.removeprefix("www.") == b.removeprefix("www."))


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
    # Preserve sentence boundaries and retain capitalized multi-token names
    # before lowercasing. Whisper punctuation is a hint, never evidence.
    kept, names, name_run = [], [], []
    previous_end = 0
    def finish_name():
        if 2 <= len(name_run) <= MAX_CLUE_TOKENS:
            phrase = " ".join(name_run)
            if any(token not in WEAK_SINGLE_TERMS and token not in AD_PROMO_TERMS for token in name_run):
                names.append(phrase)
        name_run.clear()
    for match in _CLUE_TOKEN.finditer(value):
        raw = match.group()
        token = _clue_token(raw)
        gap = value[previous_end:match.start()]
        previous_end = match.end()
        if re.search(r"[.!?;:\n]", gap):
            kept.append(None)
        if not token or len(token) < MIN_TERM_LENGTH or token in CLUE_STOPWORDS:
            kept.append(None)
            finish_name()
            continue
        kept.append(token)
        if gap.strip() or not raw[0].isupper():
            finish_name()
        if raw[0].isupper() and raw[0].isalpha():
            if len(name_run) == MAX_CLUE_TOKENS:
                finish_name()
            name_run.append(token)
        else:
            finish_name()
    finish_name()
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
    # Cover distinct lexical groups before spending the bounded clue budget
    # on overlapping windows or single-token variants of a prior phrase.
    # Keep the original candidate ranking as a secondary ordering.
    ranked = list(dict.fromkeys(names + sorted(set(clues), key=lambda item: (-len(item.split()), -len(item), item))))
    ordered, used = [], set()
    for clue in ranked:
        words = set(clue.split())
        if words and not words.intersection(used):
            ordered.append(clue)
            used.update(words)
        if len(ordered) >= MAX_CLUES:
            return ordered
    for clue in ranked:
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
    # First retain a primary clue from each supplied group. Expanding one
    # phrase into its component terms must not erase later independent names.
    groups = [_clue_candidates(value) for value in list(values)[:MAX_TERMS]]
    out = []
    for candidates in groups:
        if candidates and candidates[0] not in out:
            out.append(candidates[0])
        if len(out) >= MAX_CLUES:
            return tuple(out)
    for candidates in groups:
        for clue in candidates:
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
    return build_request_from_clues(extract_clues(text), job, decision, binding, now)


def build_request_from_clues(clues, job, decision, binding, now=None):
    """Same contract as :func:`build_request` for already-extracted clues."""
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
    clue_values = normalize_clues(clues)
    if len(clue_values) < MIN_DISTINCTIVE_CLUES:
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
    return ClueRequest(clue_values, binding, channel_name, language, "allowed",
                       probability, speech, sample, sample_digest)


@dataclasses.dataclass(frozen=True)
class OfficialEpisode:
    series_title: str
    season_number: int | None
    episode_number: int | None
    episode_title: str
    original_air_date: str
    broadcaster_code: str = ""
    series_premiere_year: int | None = None
    source_url: str = ""
    kind: str = "official-archive"

    def code(self):
        if self.broadcaster_code:
            match = CODE_RE.fullmatch(self.broadcaster_code.strip())
            if match and self.season_number is not None and self.episode_number is not None:
                return "S%02dE%02d" % (int(match[1]), int(match[2])) if len(match[2]) == 2 else "S%02dE%s" % (int(match[1]), match[2])
        if self.season_number is None or self.episode_number is None:
            return ""
        return "S%02dE%02d" % (self.season_number, self.episode_number)

    def episode_year(self):
        date = _date(self.original_air_date)
        return int(date[:4]) if date else None

    def checked(self):
        if not isinstance(self.series_title, str) or not 1 <= len(self.series_title) <= 200:
            return False
        if not isinstance(self.episode_title, str) or len(self.episode_title) > 300:
            return False
        # Official archives that publish an explicit air date without numbering
        # are valid.  Never invent a season/episode number to satisfy the
        # contract; numbering is optional and only trusted when both fields are
        # present and consistent with any broadcaster code.
        if self.season_number is not None and (type(self.season_number) is not int or not 1 <= self.season_number <= 99):
            return False
        if self.episode_number is not None and (type(self.episode_number) is not int or not 1 <= self.episode_number <= 9999):
            return False
        if (self.season_number is None) != (self.episode_number is None):
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
            if self.season_number is None or self.episode_number is None:
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
class TypedProgrammeEvidence:
    """A scoped public work/event identity; no episode numbering or air date."""
    content_kind: str
    title: str
    source_url: str
    terms: tuple
    release_year: int | None = None
    event_date: str = ""
    published_at: str = ""

    def checked(self, official):
        if self.content_kind not in TYPED_CONTENT_KINDS:
            return False
        if not isinstance(self.title, str) or not 1 <= len(self.title.strip()) <= 200:
            return False
        if self.title != " ".join(self.title.split()):
            return False
        if not safe_source_url(self.source_url, TYPED_OFFICIAL_HOSTS if official else TYPED_REPORT_HOSTS, allow_query=True):
            return False
        if not isinstance(self.terms, tuple) or len(self.terms) > MAX_CLUES:
            return False
        if any(not isinstance(t, str) or not t or len(t) > 120 for t in self.terms):
            return False
        if self.content_kind == "sports-event":
            if self.release_year is not None or _date(self.event_date) != self.event_date or not self.event_date:
                return False
        elif self.event_date or type(self.release_year) is not int or _year(self.release_year) is None:
            return False
        return official or bool(_date(self.published_at))

    def key(self):
        return (self.content_kind, _norm_text(self.title), self.release_year, self.event_date)


@dataclasses.dataclass(frozen=True)
class ContentIdentity:
    series_title: str
    season_number: int | None
    episode_number: int | None
    episode_title: str
    original_air_date: str
    broadcaster_code: str
    episode_year: int | None
    series_premiere_year: int | None = None
    content_kind: str = "episode"
    release_year: int | None = None
    event_date: str = ""

    def evidence(self):
        if self.content_kind in TYPED_CONTENT_KINDS:
            value = {"contentKind": self.content_kind, "contentTitle": self.series_title}
            if self.release_year is not None:
                value["contentReleaseYear"] = self.release_year
            if self.event_date:
                value["contentEventDate"] = self.event_date
            return value
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


def _typed_distinct_matches(clues, terms):
    # Typed parsers provide only exact, scoped matches, not article prose.
    wanted = set(normalize_clues(clues))
    exact = wanted.intersection(terms)
    groups, used = [], set()
    for value in sorted(exact, key=lambda t: (-len(t.split()), -len(t), t)):
        words = set(value.split())
        if words and not words.intersection(used):
            groups.append(value)
            used.update(words)
    return tuple(groups)


def corroborate_typed_programme(clues, official, reports, now=None):
    """Require one official identity and two independent matching publishers.

    Film/series/special release year and sporting event date are distinct
    facts. All sources must match the same typed key and share at least two
    exact disjoint content clues from scoped plot/event evidence. For sport,
    also require an event-title clue and a third independent clue group.
    """
    import datetime as dt
    now = time.time() if now is None else float(now)
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
    candidates = {}
    official_rows = official if isinstance(official, (tuple, list)) else ()
    report_rows = reports if isinstance(reports, (tuple, list)) else ()
    for item in official_rows[:MAX_OFFICIAL_EPISODES]:
        if not isinstance(item, TypedProgrammeEvidence) or not item.checked(True):
            continue
        if (item.event_date and item.event_date > today) or (item.release_year is not None and item.release_year > int(today[:4])):
            continue
        official_matches = set(_typed_distinct_matches(clues, item.terms))
        required = 3 if item.content_kind == "sports-event" else MIN_DISTINCTIVE_CLUES
        if len(official_matches) < required:
            continue
        by_publisher = {}
        for row in report_rows[:MAX_REPORTS]:
            if not isinstance(row, TypedProgrammeEvidence) or not row.checked(False) or row.key() != item.key():
                continue
            if _date(row.published_at) > today or (item.event_date and _date(row.published_at) < item.event_date):
                continue
            publisher = independent_publisher(row.source_url)
            if not publisher or publisher == independent_publisher(item.source_url):
                continue
            matches = set(_typed_distinct_matches(clues, row.terms)) & official_matches
            if len(matches) < required:
                continue
            by_publisher.setdefault(publisher, []).append(matches)
        groups = list(by_publisher.items())
        valid = []
        for i, (publisher_a, matches_a) in enumerate(groups):
            for publisher_b, matches_b in groups[i+1:]:
                for aa in matches_a:
                    for bb in matches_b:
                        shared = _typed_distinct_matches(clues, aa & bb)
                        if len(shared) < required:
                            continue
                        if item.content_kind == "sports-event":
                            title_terms = set(_typed_distinct_matches(clues, _clue_candidates(item.title)))
                            if not title_terms.intersection(shared):
                                continue
                        valid.append((len(shared), publisher_a, publisher_b))
        if not valid:
            continue
        count = max(v[0] for v in valid)
        identity = ContentIdentity(item.title, None, None, "", "", "", None,
                                   content_kind=item.content_kind, release_year=item.release_year,
                                   event_date=item.event_date)
        candidates[item.key()] = Verification(True, "", identity, count * 2, 2, False,
                                             "official-and-independent-programme")
    if len(candidates) > 1:
        return Verification(False, "ambiguous-content")
    return next(iter(candidates.values()), Verification(False, "no-typed-corroboration"))


def _numbering_differs(report, official):
    if official.season_number is None or official.episode_number is None:
        # An official air-date-only archive cannot prove numbering.  A
        # third-party number is not a conflict, but it is never adopted as the
        # official identity either.
        if official.broadcaster_code and report.broadcaster_code:
            return report.broadcaster_code.strip().upper() != official.code()
        return False
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
        payload = {"source": "local-audio-programme-identity" if self.identity.content_kind in TYPED_CONTENT_KINDS else "local-audio-episode-identity", "revision": REVISION,
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


def verify_episode_content(request, official_episodes, reports, now=None, programme=None):
    """Verify historical sampled content without making a live UI claim."""
    if not isinstance(request, ClueRequest):
        return Verification(False, "invalid-request")
    now = time.time() if now is None else float(now)
    # Unknown-title discovery needs explicit matching series identities from independent pages.
    unknown_title = isinstance(programme, dict) and not programme.get("seriesTitle")
    typed = None
    if unknown_title:
        if isinstance(official_episodes, (list, tuple)) and any(isinstance(r, TypedProgrammeEvidence) for r in official_episodes[:MAX_OFFICIAL_EPISODES]):
            typed = corroborate_typed_programme(request.clues, official_episodes, reports, now)
        reports = [r for r in (reports or ()) if isinstance(r, IndependentReport) and r.series_title]
    verification = corroborate_episode(request.clues, official_episodes, reports, now=now)
    if unknown_title and verification.accepted:
        identity = verification.identity
        publisher_matches = {}
        for r in reports:
            if (r.rejection() or _norm_text(r.series_title) != _norm_text(identity.series_title)
                    or _date(r.original_air_date) != identity.original_air_date):
                continue
            matched = len(clue_matches(request.clues, r.terms))
            if matched < MIN_DISTINCTIVE_CLUES:
                continue
            publisher = independent_publisher(r.source_url)
            publisher_matches[publisher] = max(matched, publisher_matches.get(publisher, 0))
        if len(publisher_matches) < 2:
            verification = Verification(False, "no-independent-report")
        else:
            # Do not inflate counts/confidence using aliases or several articles
            # from one publisher; retain any numbering conflict from corroboration.
            verification = dataclasses.replace(verification,
                corroborating_sources=len(publisher_matches),
                matched_clue_count=sum(publisher_matches.values()))
    if typed is not None:
        if typed.reason == "ambiguous-content" or (typed.accepted and verification.accepted):
            return Verification(False, "ambiguous-content")
        if typed.accepted:
            verification = typed
        elif not verification.accepted:
            return typed
    if not verification.accepted or verification.identity is None:
        return verification
    if not programme_matches(verification.identity, programme):
        return Verification(False, "programme-mismatch")
    return verification


def promote_episode(request, official_episodes, reports, current, now=None, scheduled_events=None,
                    programme=None):
    now = time.time() if now is None else float(now)
    if not isinstance(request, ClueRequest):
        return Promotion(False, "invalid-request")
    if not request.binding.checked(now):
        return Promotion(False, "session-expired")
    if not request.binding.matches(current, now):
        return Promotion(False, "session-mismatch")
    verification = verify_episode_content(request, official_episodes, reports, now, programme)
    if not verification.accepted or verification.identity is None:
        return Promotion(False, verification.reason)
    confidence = min(0.99, 0.5 + 0.1 * verification.matched_clue_count + 0.1 * verification.corroborating_sources)
    airing = match_scheduled_airing(verification.identity, scheduled_events, now) if scheduled_events and verification.identity.content_kind == "episode" else None
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


def typed_programme_resolved(row, session_hash, now=None):
    """Only complete, fresh typed evidence bound to an independently supplied
    current playback session can suppress further research.
    """
    if not isinstance(row, dict) or not isinstance(session_hash, str) or not session_hash:
        return False
    kind = row.get("contentKind")
    title = row.get("contentTitle")
    if (not isinstance(kind, str) or kind not in TYPED_CONTENT_KINDS or
            not isinstance(title, str) or not 1 <= len(title) <= 200 or
            title != " ".join(title.split()) or
            row.get("contentSource") != "local-audio-programme-identity" or
            row.get("contentVerification") != "official-and-independent-programme" or
            row.get("audioPlaybackSessionHash") != session_hash or
            type(row.get("contentCorroboratingSources")) is not int or
            not 2 <= row["contentCorroboratingSources"] <= MAX_REPORTS or
            type(row.get("contentMatchCount")) is not int or
            row["contentMatchCount"] < (3 if kind == "sports-event" else MIN_DISTINCTIVE_CLUES) or
            not isinstance(row.get("contentVisitDigest"), str) or
            not DIGEST_RE.fullmatch(row["contentVisitDigest"])):
        return False
    for key in ("title", "seriesTitle", "audioSeriesTitle"):
        if key in row and row[key] != title:
            return False
    if any(row.get(key) is not None and row.get(key) != "" for key in
           ("seasonNumber", "episodeNumber", "episodeTitle", "originalAirDate")):
        return False
    now = time.time() if now is None else now
    observed, expires, confidence = (row.get(key) for key in
                                    ("contentObservedAt", "contentExpiresAt", "contentConfidence"))
    try:
        finite = all(type(v) in (int, float) and math.isfinite(v)
                     for v in (now, observed, expires, confidence))
    except OverflowError:
        return False
    if not finite:
        return False
    if not observed <= now + 0.001 or not now < expires or not 0 < expires - observed <= PUBLICATION_TTL + 0.001 or not 0 < confidence <= 1:
        return False
    import datetime as dt
    try:
        today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return False
    year, event = row.get("contentReleaseYear"), row.get("contentEventDate")
    if kind == "sports-event":
        return year is None and isinstance(event, str) and bool(event) and _date(event) == event and event <= today
    return event in (None, "") and type(year) is int and _year(year) is not None and year <= int(today[:4])


def programme_has_resolved_identity(row, session_hash=None, now=None):
    if isinstance(row, dict) and row.get("contentSource") == "local-audio-programme-identity":
        return typed_programme_resolved(row, session_hash, now)
    return programme_has_episode_identity(row)


def _bounded_int(value, low, high):
    if type(value) is int and low <= value <= high:
        return value
    return None


def programme_context(row, channel_id="", session_hash=None, now=None):
    """Reduce an existing programme metadata row to bounded public context.

    Only public programme metadata is retained: series/episode titles, numbers
    and the episode-scoped original air date. No transcript or audio is read.
    Returns ``None`` when there is no usable series title.
    """
    if not isinstance(row, dict):
        return None
    typed = row.get("contentSource") == "local-audio-programme-identity"
    if typed and not typed_programme_resolved(row, session_hash, now):
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
    context = {"channelID": cid or "", "seriesTitle": title,
               "seasonNumber": season, "episodeNumber": number,
               "episodeTitle": episode_title, "originalAirDate": air_date,
               "verified": season is not None and number is not None}
    if typed:
        context.update({k: v for k, v in row.items() if k.startswith("content")})
        context["audioPlaybackSessionHash"] = session_hash
        context["verified"] = True
    return context


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
    if identity.content_kind in TYPED_CONTENT_KINDS:
        # A typed no-EPG identity is never used to overwrite an existing title.
        return isinstance(context, dict) and not context.get("seriesTitle")
    if identity.content_kind != "episode":
        return False
    if not isinstance(context, dict):
        return True
    series = context.get("seriesTitle")
    if isinstance(series, str) and series and _norm_text(series) != _norm_text(identity.series_title):
        return False
    season = context.get("seasonNumber")
    # Existing provider numbering is allowed to remain stronger, but a known
    # identity number must never contradict it.  When the official archive only
    # publishes an air date, no number is invented or compared.
    if type(season) is int and identity.season_number is not None and season != identity.season_number:
        return False
    number = context.get("episodeNumber")
    if type(number) is int and identity.episode_number is not None and number != identity.episode_number:
        return False
    air_date = _date(context.get("originalAirDate"))
    if air_date and air_date != identity.original_air_date:
        return False
    return True


def programme_has_stronger_episode_metadata(row):
    """True when an existing row already carries stronger episode data.

    Audio publication is additive and lower precedence. It never replaces a
    provider/episode-guide title, a verified season/episode number or an
    existing corroborated schedule episode.
    """
    if not isinstance(row, dict):
        return False
    if programme_has_resolved_identity(row):
        return True
    if row.get("episodeTitle"):
        return True
    if row.get("source") == "corroborated-external-schedule" and row.get("guideEpisode"):
        return True
    return False


@dataclasses.dataclass(frozen=True)
class Publication:
    channel_id: str
    expires_at: float
    row: dict

    def checked(self, now=None):
        now = time.time() if now is None else float(now)
        if not CHANNEL_RE.fullmatch(str(self.channel_id or "")):
            return False
        if not isinstance(self.row, dict) or not self.row:
            return False
        return now < self.expires_at


class PublicationStore:
    """Bounded, expiring publication into the existing programme metadata path.

    Only public programme identity is retained. The transient transcript,
    spoken clues, page bodies and raw launch/stream identifiers are never
    stored here.
    """

    def __init__(self, ttl=PUBLICATION_TTL, clock=time.time):
        self.ttl = max(1.0, float(ttl))
        self.clock = clock
        self.entries = OrderedDict()
        self.published = 0
        self.rejected = 0
        self._lock = threading.RLock()

    def _prune(self, now):
        for key, entry in list(self.entries.items()):
            if now >= entry.expires_at:
                self.entries.pop(key, None)
        while len(self.entries) > MAX_PUBLICATIONS:
            self.entries.popitem(last=False)

    def publish(self, promotion, context=None, now=None):
        now = self.clock() if now is None else float(now)
        if not isinstance(promotion, Promotion) or not promotion.accepted or promotion.episode is None:
            return False
        episode = promotion.episode
        identity = episode.identity
        if not isinstance(identity, ContentIdentity):
            return False
        if context is not None and not programme_matches(identity, context):
            return False
        if isinstance(context, dict) and context.get("channelID") and context.get("channelID") != episode.channel_id:
            return False
        if not CHANNEL_RE.fullmatch(str(episode.channel_id or "")):
            return False
        if identity.content_kind in TYPED_CONTENT_KINDS:
            if (not isinstance(context, dict) or context.get("seriesTitle") or
                    episode.corroborating_sources < 2 or episode.verification != "official-and-independent-programme"):
                return False
            session = context.get("publicationSessionHash")
            if not isinstance(session, str) or not session:
                return False
            if (not isinstance(identity.series_title, str) or not identity.series_title.strip() or
                    len(identity.series_title) > 200 or identity.season_number is not None or
                    identity.episode_number is not None or identity.episode_title or identity.original_air_date or
                    identity.broadcaster_code or identity.episode_year is not None or identity.series_premiere_year is not None):
                return False
            if identity.content_kind == "sports-event":
                if identity.release_year is not None or not identity.event_date or _date(identity.event_date) != identity.event_date:
                    return False
            elif identity.event_date or type(identity.release_year) is not int or _year(identity.release_year) is None:
                return False
            row = {"audioSeriesTitle": identity.series_title,
                   "audioPlaybackSessionHash": session,
                   "contentSource": "local-audio-programme-identity",
                   "contentObservedAt": round(now, 3),
                   "contentExpiresAt": round(now + self.ttl, 3),
                   "contentVerification": episode.verification,
                   "contentConfidence": round(float(episode.confidence), 3),
                   "contentMatchCount": int(episode.matched_clue_count),
                   "contentCorroboratingSources": int(episode.corroborating_sources),
                   "contentVisitDigest": episode.binding.digest()}
            row.update(identity.evidence())
            if not typed_programme_resolved(row, session, now):
                return False
            with self._lock:
                self._prune(now)
                self.entries[episode.channel_id] = Publication(episode.channel_id, now + self.ttl, row)
                self.entries.move_to_end(episode.channel_id)
                self._prune(now)
                self.published += 1
            return True
        title = " ".join(str(identity.episode_title or "").split())[:300]
        if not title:
            title = _pretty_date(identity.original_air_date)
        if not title:
            return False
        row = {
            "audioSeriesTitle": identity.series_title,
            "episodeTitle": title,
            "originalAirDate": _date(identity.original_air_date),
            "episodeSource": "local-audio-episode-identity",
            "episodeObservedAt": round(now, 3),
            "episodeExpiresAt": round(now + self.ttl, 3),
            "episodeVerification": str(promotion.episode.verification or "")[:80],
            "episodeConfidence": round(float(episode.confidence), 3),
            "episodeMatchCount": int(episode.matched_clue_count),
            "episodeCorroboratingSources": int(episode.corroborating_sources),
            "episodeNumberingConflict": bool(episode.numbering_conflict),
            "episodeVisitDigest": episode.binding.digest(),
        }
        if isinstance(context, dict):
            session = context.get("publicationSessionHash")
            if not context.get("seriesTitle") and (not isinstance(session, str) or not session):
                return False
            if isinstance(session, str) and session:
                row["audioPlaybackSessionHash"] = session
        if identity.season_number is not None and identity.episode_number is not None:
            row["seasonNumber"] = int(identity.season_number)
            row["episodeNumber"] = int(identity.episode_number)
        if identity.broadcaster_code:
            row["episodeOfficialCode"] = str(identity.broadcaster_code)[:16]
        if identity.episode_year is not None:
            row["episodeYear"] = int(identity.episode_year)
        with self._lock:
            self._prune(now)
            self.entries[episode.channel_id] = Publication(episode.channel_id, now + self.ttl, row)
            self.entries.move_to_end(episode.channel_id)
            self.published += 1
        return True

    def discard_publication(self, promotion, context, now):
        """Remove only this exact failed publication, never a newer visit."""
        episode = promotion.episode
        if episode is None:
            return
        prefix = "content" if episode.identity.content_kind in TYPED_CONTENT_KINDS else "episode"
        with self._lock:
            entry = self.entries.get(episode.channel_id)
            if (entry is not None and
                    entry.row.get(prefix + "VisitDigest") == episode.binding.digest() and
                    entry.row.get(prefix + "ObservedAt") == round(now, 3) and
                    entry.row.get("audioPlaybackSessionHash") == (context or {}).get("publicationSessionHash")):
                self.entries.pop(episode.channel_id, None)

    def snapshot(self, now=None):
        now = self.clock() if now is None else float(now)
        with self._lock:
            self._prune(now)
            return {key: dict(entry.row) for key, entry in self.entries.items() if entry.checked(now)}

    def get(self, channel_id, now=None):
        now = self.clock() if now is None else float(now)
        with self._lock:
            self._prune(now)
            entry = self.entries.get(channel_id)
            return dict(entry.row) if entry is not None and entry.checked(now) else None

    def diagnostics(self, now=None):
        now = self.clock() if now is None else float(now)
        with self._lock:
            self._prune(now)
            return {"revision": REVISION, "ttlSeconds": self.ttl,
                    "entries": len(self.entries), "published": self.published,
                    "channels": [{"channelID": key, "expiresAt": round(entry.expires_at, 3)}
                                 for key, entry in list(self.entries.items())[-16:]]}


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
                 now=None, dedup_key="", sample_seconds=0.0, allow_retry=False, unknown_lead=False):
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
        try:
            sample = float(sample_seconds or 0.0)
        except (TypeError, ValueError):
            sample = 0.0
        required = self.active_seconds
        if sample >= MIN_EVIDENCE_SAMPLE_SECONDS:
            # A completed, verified language sample already demonstrates real
            # progressing playback for its own bounded window.  A 60-second
            # rotation cannot reach 120 seconds, so use the sample window as a
            # deliberate, bounded floor for this lane only.  Rotation and every
            # language confidence threshold remain unchanged.
            required = min(required, max(AUDIO_MIN_ACTIVE_SECONDS, sample))
        if not math.isfinite(active) or not math.isfinite(sample):
            return False, "inactive"
        if unknown_lead and sample < MIN_EVIDENCE_SAMPLE_SECONDS:
            return False, "inactive"
        if unknown_lead:
            # Lead search may start earlier; publishing still requires the
            # independent confirmed-progress gate, full sample and source proof.
            required = UNKNOWN_LEAD_PROGRESS_SECONDS
        if active < required:
            return False, "inactive"
        self._prune(now)
        if not allow_retry:
            if dedup_key and now - self.seen.get(dedup_key, -86400.0) < self.dedup_seconds:
                return False, "duplicate-search"
            row = self.windows.get(key)
            if row:
                if row["attempts"] and now - row["last"] < self.cooldown_seconds:
                    return False, "duplicate"
                if len(row["attempts"]) >= self.max_attempts:
                    return False, "attempt-limit"
        # Even an independent episode retry respects the daily network budget.
        if len(self.calls) >= self.daily_budget:
            return False, "budget"
        return True, ""

    def begin(self, view, now=None, dedup_key="", retry=False):
        now = self.clock() if now is None else float(now)
        key = _session_key(view)
        if not key:
            return False
        self._prune(now)
        row = self.windows.get(key)
        if row is None:
            row = {"attempts": [], "last": now, "outcome": "",
                   "initialResearchAttempts": 0, "episodeRetryAttempts": 0}
            self.windows[key] = row
        row["attempts"].append(now)
        kind = "episodeRetryAttempts" if retry else "initialResearchAttempts"
        row[kind] += 1
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
                         "initialResearchAttempts": row["initialResearchAttempts"],
                         "episodeRetryAttempts": row["episodeRetryAttempts"],
                         "totalResearchAttempts": len(row["attempts"]),
                         "lastOutcome": row["outcome"] or None,
                         "nextEligibleSeconds": max(0.0, round(self.cooldown_seconds - (now - row["last"]), 1))})
        return {"revision": REVISION, "activeSeconds": self.active_seconds,
                "sampleBackedActiveSeconds": AUDIO_MIN_ACTIVE_SECONDS,
                "minEvidenceSampleSeconds": MIN_EVIDENCE_SAMPLE_SECONDS,
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


MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
MONTH_NAMES = ("Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.",
               "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec.")
DATE_TOKEN = (r"(?:[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}"
              r"|\d{1,2}/\d{1,2}/\d{4}"
              r"|(?:19|20)\d{2}-\d{2}-\d{2})")
_ISO_DATE = re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")
_MONTH_DATE = re.compile(
    r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2}),?\s+((?:19|20)\d{2})\b",
    re.IGNORECASE)
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})\b")
_BYLINE_DATE = re.compile(r"\bon\s+((?:19|20)\d{2}-\d{2}-\d{2})(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?",
                          re.IGNORECASE)
_META_TAG = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_META_ATTR = re.compile(r"([A-Za-z:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.DOTALL)
_TITLE_TAG = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_STRIP_BLOCK = re.compile(r"<(?:script|style|noscript|svg)\b.*?</(?:script|style|noscript|svg)>",
                          re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_URL_DATE = re.compile(r"/((?:19|20)\d{2}-\d{2}-\d{2})(?:/|$)")
_LINK_TAG = re.compile(r"<a\b[^>]*href\s*=\s*(?:\"([^\"]*)\"|'([^']*)')[^>]*>(.*?)</a>",
                       re.IGNORECASE | re.DOTALL)


def _valid_iso(year, month, day):
    import datetime as dt
    try:
        value = dt.date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return ""
    return value.isoformat() if 1888 <= value.year <= 2100 else ""


def _page_date(value):
    if not isinstance(value, (str, int, float)):
        return ""
    text = str(value).strip()
    match = _ISO_DATE.search(text)
    if match:
        return _valid_iso(match[1], match[2], match[3])
    return _date(text[:10])


def _date_from_text(value):
    """Extract only an explicitly written date. Never infer a missing date."""
    if not isinstance(value, str) or not value:
        return ""
    candidates = []
    for match in _ISO_DATE.finditer(value):
        air = _valid_iso(match[1], match[2], match[3])
        if air:
            candidates.append((match.start(), air))
    for match in _MONTH_DATE.finditer(value):
        month = MONTHS.get(match[1].casefold().rstrip("."))
        air = _valid_iso(match[3], month, match[2]) if month else ""
        if air:
            candidates.append((match.start(), air))
    for match in _NUMERIC_DATE.finditer(value):
        air = _valid_iso(match[3], match[1], match[2])
        if air:
            candidates.append((match.start(), air))
    if not candidates:
        return ""
    candidates.sort(key=lambda row: row[0])
    return candidates[0][1]


def _pretty_date(value):
    air = _date(value)
    if not air:
        return ""
    return "%s %d, %s" % (MONTH_NAMES[int(air[5:7]) - 1], int(air[8:10]), air[:4])


def _page_text(page):
    if isinstance(page, (bytes, bytearray)):
        if len(page) > MAX_PAGE_BYTES:
            return ""
        page = bytes(page).decode("utf-8", "replace")
    if not isinstance(page, str) or len(page) > MAX_PAGE_BYTES:
        return ""
    return page


def _meta_map(page):
    page = _page_text(page)
    values = {}
    for tag in _META_TAG.findall(page)[:256]:
        attrs = {}
        for name, double, single in _META_ATTR.findall(tag):
            attrs[name.casefold()] = (double or single or "").strip()
        key = attrs.get("property") or attrs.get("name") or attrs.get("http-equiv")
        content = attrs.get("content", "")
        if key and content and len(content) <= MAX_PAGE_TEXT:
            values.setdefault(key.casefold(), content)
    return values


def _title_tag(page):
    page = _page_text(page)
    match = _TITLE_TAG.search(page)
    if not match:
        return ""
    return " ".join(html_module.unescape(_TAG.sub(" ", match[1])).split())[:300]


def _visible_text(page, limit=MAX_PAGE_TEXT):
    page = _page_text(page)
    if not page:
        return ""
    body = _STRIP_BLOCK.sub(" ", page)
    body = _TAG.sub(" ", body)
    body = html_module.unescape(body)
    body = " ".join(body.split())
    return body[:max(0, int(limit))]


def _strip_site_suffix(value):
    text = " ".join(str(value or "").split())
    return re.sub(r"\s+[|]\s+[^|]{1,48}$", "", text).strip()


def _clean_series(value):
    text = " ".join(str(value or "").split())
    text = re.sub(r"\s+[-–—:,]+\s*$", "", text).strip()
    text = re.sub(r"\s+(?:results?|report|recap|highlights?)\s*$", "", text, flags=re.IGNORECASE)
    return text[:200]


def _headline_parts(headline):
    """Split an official headline into (series, episode headline, date text)."""
    text = _strip_site_suffix(headline)
    if not text:
        return "", "", ""
    patterns = (
        (r"^(?P<series>.+?)\s+(?:results?|report|recap)\b\s*[,:]\s*"
         r"(?P<date>" + DATE_TOKEN + r")\s*(?::\s*(?P<rest>.*))?$"),
        (r"^(?P<series>.+?)\s*:\s*(?P<date>" + DATE_TOKEN + r")\s*$"),
        (r"^(?P<series>.+?)\s+(?:results?|report|recap)\b\s*:\s*(?P<rest>.*)$"),
    )
    for pattern in patterns:
        match = re.match(pattern, text, re.IGNORECASE)
        if match:
            return (_clean_series(match.group("series")),
                    " ".join((match.groupdict().get("rest") or "").split())[:300],
                    match.groupdict().get("date") or "")
    return "", "", ""


def _series_from_url(source_url):
    try:
        parts = [part for part in urllib.parse.urlsplit(source_url).path.split("/") if part]
    except ValueError:
        return ""
    if len(parts) >= 2 and parts[0].casefold() == "shows":
        slug = parts[1]
        known = {"wwenxt": "WWE NXT", "raw": "WWE Raw", "smackdown": "WWE SmackDown",
                 "nxt": "WWE NXT", "wweraw": "WWE Raw", "wwesmackdown": "WWE SmackDown"}
        if slug.casefold() in known:
            return known[slug.casefold()]
        words = re.sub(r"([a-z])([A-Z])", r"\1 \2", slug).replace("-", " ").replace("_", " ")
        return " ".join(word.capitalize() for word in words.split())[:200]
    return ""


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
    """Parse bounded official-archive JSON-LD or ordinary HTML.

    Real WWE archive pages publish OpenGraph/title metadata and a URL date but
    no JSON-LD and often no season/episode numbering. The parser therefore
    accepts an explicit official air date without inventing numbers; numbering
    is used only when the archive itself supplies it.
    """
    host = _host(source_url)
    if host not in OFFICIAL_ARCHIVE_HOSTS:
        return []
    rows = []
    seen = set()

    def add(item):
        if not isinstance(item, OfficialEpisode) or not item.checked():
            return
        # One official page contributes at most one row per series/date. A
        # JSON-LD row with numbering is kept and the HTML fallback for the same
        # date is not allowed to create a conflicting date-only duplicate.
        key = (_norm_text(item.series_title), item.original_air_date)
        if key in seen:
            return
        seen.add(key)
        rows.append(item)

    for obj in _jsonld_objects(page):
        if not _json_types(obj).intersection({"tvepisode", "episode", "broadcastevent"}):
            continue
        series = _series_title(obj, fallback_series)
        title = obj.get("name") or obj.get("headline") or obj.get("name")
        title = " ".join(str(title).split())[:300] if title else ""
        season, number = _episode_numbers(obj)
        air = _page_date(obj.get("datePublished") or obj.get("startDate") or obj.get("uploadDate"))
        air = air or _date_from_text(title)
        if not series or not air:
            continue
        code = "S%02dE%02d" % (season, number) if season is not None and number is not None else ""
        add(OfficialEpisode(series, season, number, title or _pretty_date(air), air, code,
                            None, source_url))
        if len(rows) >= MAX_OFFICIAL_EPISODES:
            return rows

    meta = _meta_map(page)
    headline = _strip_site_suffix(meta.get("og:title") or meta.get("twitter:title")
                                  or _title_tag(page))
    description = meta.get("og:description") or meta.get("description") or ""
    if not headline:
        return rows
    series, episode_title, date_text = _headline_parts(headline)
    try:
        path = urllib.parse.urlsplit(source_url).path
    except ValueError:
        path = ""
    match = _URL_DATE.search(path + "/")
    url_date = _valid_iso(match[1][:4], match[1][5:7], match[1][8:10]) if match else ""
    air = url_date or _date_from_text(date_text or headline)
    if not air:
        air = _date_from_text(description)
    if not series:
        series = fallback_series or _series_from_url(source_url)
    if series and air:
        add(OfficialEpisode(series, None, None, episode_title or _pretty_date(air),
                            air, "", None, source_url))
    return rows[:MAX_OFFICIAL_EPISODES]


def _episode_from_text(text):
    match = CODE_RE.search(text or "")
    if not match:
        return None, None
    return int(match[1]), int(match[2])


def _series_from_report_headline(headline):
    match = re.match(r"^(?:\d{1,2}/\d{1,2}\s+)?(?P<series>.+?)\s+"
                     r"(?:report|results|recap|review)\b", headline or "", re.IGNORECASE)
    return _clean_series(match.group("series")) if match else ""


def _single_article(page):
    """Limit body evidence to a unique article, never arbitrary page chrome."""
    text = _page_text(page)
    if len(re.findall(r"<article\b", text, re.IGNORECASE)) != 1:
        return ""
    match = re.search(r"<article\b[^>]*>(.*?)</article\s*>", text, re.IGNORECASE | re.DOTALL)
    return match.group(1) if match else ""


def _report_headline(value, site_name=""):
    title = _strip_site_suffix(html_module.unescape(value or ""))
    site_name = html_module.unescape(site_name or "").strip()
    if site_name:
        title = re.sub(r"\s+[-–—]\s+" + re.escape(site_name) + r"$", "", title, flags=re.IGNORECASE)
    return title.strip()


def _article_matches_headline(article, headline):
    headings = re.findall(r"<h1\b[^>]*>(.*?)</h1\s*>", article, re.IGNORECASE | re.DOTALL)
    if len(headings) != 1:
        return False
    heading = _norm_text(_visible_text(headings[0], 300))
    return bool(heading and heading == _norm_text(headline))


def _article_body(article, headline):
    # Bind the article body to the same page headline. A different article's
    # text cannot supply clues for the metadata identity.
    if not _article_matches_headline(article, headline):
        return ""
    # Exclude common page chrome, related-story blocks and links. A unique
    # article is required; generic page-wide text is not eligible.
    body = re.sub(r"<(nav|aside|header|footer|form|figure|script|style)\b[^>]*>.*?</\1\s*>",
                  " ", article, flags=re.IGNORECASE | re.DOTALL)
    body = re.sub(r"<a\b[^>]*>.*?</a\s*>", " ", body, flags=re.IGNORECASE | re.DOTALL)
    paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p\s*>", body, re.IGNORECASE | re.DOTALL)[:128]
    return " ".join(_visible_text(p, 1000) for p in paragraphs)[:16000]


def _article_publication_dates(article):
    dates = set()
    for tag in re.findall(r"<time\b[^>]{0,512}>", article, re.IGNORECASE)[:16]:
        m = re.search(r"\bdatetime\s*=\s*['\x22]([^'\x22]+)['\x22]", tag, re.IGNORECASE)
        if m:
            date = _page_date(m.group(1))
            if date:
                dates.add(date)
    return dates


def _requested_public_terms(clues, *sections):
    """Find complete requested phrases in independently fetched article text."""
    wanted = normalize_clues(clues)
    matched = []
    for section in sections:
        tokens = [_clue_token(x) for x in _CLUE_TOKEN.findall(html_module.unescape(section or "")[:16000])]
        words = " " + " ".join(x for x in tokens if x) + " "
        for clue in wanted:
            if " " + clue + " " in words and clue not in matched:
                matched.append(clue)
    # Count independent clue groups, not a phrase and its own individual
    # tokens. Longest exact matches take precedence over overlapping terms.
    distinct = []
    used = set()
    for clue in sorted(matched, key=lambda c: (-len(c.split()), -len(c), c)):
        tokens = set(clue.split())
        if not tokens.intersection(used):
            distinct.append(clue)
            used.update(tokens)
    return tuple(distinct[:MAX_CLUES]) if len(distinct) >= MIN_DISTINCTIVE_CLUES else ()


def _report_terms(headline, description, visible=""):
    text = (headline + " " + description).strip()
    terms = normalize_clues(text)
    if len(terms) < MIN_DISTINCTIVE_CLUES and visible:
        terms = normalize_clues(text + " " + visible[:1500])
    return terms


def _report_air_date(headline, description, visible, fallback_air=""):
    air = _date_from_text((headline + " " + description).strip())
    if air:
        return air
    match = _BYLINE_DATE.search(visible or "")
    if match:
        air = _valid_iso(match[1][:4], match[1][5:7], match[1][8:10])
        if air:
            return air
    air = _date_from_text((visible or "")[:1500])
    return air or fallback_air


def _unknown_report_air_date(headline, description):
    """Require an explicitly labelled episode date, not an incidental date."""
    if not isinstance(headline, str) or not isinstance(description, str):
        return ""
    marker = r"(?:report|results|recap|review)"
    patterns = (
        r"\b" + marker + r"\s*(?:[:–—-]\s*)?\(?\s*(" + DATE_TOKEN + r")(?=\s*[:–—),]|\s*$)",
        r"\b(" + DATE_TOKEN + r")\s+" + marker + r"\b",
    )
    matches = []
    for pattern in patterns:
        for match in re.finditer(pattern, headline, re.IGNORECASE):
            air = _date_from_text(match.group(1))
            if air:
                matches.append(air)
    for match in re.finditer(r"\b(?:original air date|episode air date|originally aired)\s*:?\s*(?:on\s+)?(" + DATE_TOKEN + r")", description, re.IGNORECASE):
        air = _date_from_text(match.group(1))
        if air:
            matches.append(air)
    series = _series_from_report_headline(headline)
    if series:
        series_pattern = re.escape(series)
        text = description
        # Publisher text may use typographic quotes around the exact series.
        for match in re.finditer(r"\b(?:results|report|recap|review)\s+(?:for|of)\s+[“\x22‘\x27]?" + series_pattern + r"[”\x22’\x27]?\s+on\s+(" + DATE_TOKEN + r")", text, re.IGNORECASE):
            air = _date_from_text(match.group(1))
            if air:
                matches.append(air)
    dates = set(matches)
    return next(iter(dates)) if len(dates) == 1 else ""


def _report_published(meta, visible):
    published = _page_date(meta.get("article:published_time") or meta.get("article:modified_time"))
    if published:
        return published
    match = _BYLINE_DATE.search(visible or "")
    if match:
        return _valid_iso(match[1][:4], match[1][5:7], match[1][8:10])
    return _date_from_text((visible or "")[:1000])


def _report_row(source_url, headline, description, published, air, series, visible="", matched_terms=None):
    lowered = headline.casefold()
    text = (headline + " " + description).strip()
    season, number = _episode_from_text(text)
    terms = _report_terms(headline, description, visible) if matched_terms is None else matched_terms
    return IndependentReport(
        source_url, published, terms, air,
        series, season, number, "",
        "independent-report",
        advertisement=("advertisement" in text.casefold() or "sponsored" in text.casefold()),
        reused_promo=any(word in lowered.split() for word in ("promo", "promos", "trailer", "teaser", "clip")),
        generic=not terms,
    )


def parse_report_page(page, source_url, context=None, clues=()):
    """Parse bounded independent-report JSON-LD or ordinary HTML."""
    host = _host(source_url)
    if host not in INDEPENDENT_REPORT_HOSTS:
        return []
    context = context if isinstance(context, dict) else {}
    fallback_series = context.get("seriesTitle") if isinstance(context.get("seriesTitle"), str) else ""
    fallback_air = _date(context.get("originalAirDate"))
    requested = normalize_clues(clues) if not fallback_series else ()
    article = _single_article(page) if requested else ""
    report_meta = _meta_map(page) if requested else {}
    site_name = report_meta.get("og:site_name", "")
    if requested:
        # Reject the entire ambiguous report, including structured metadata.
        # An unrelated article cannot supply either a date or clues.
        page_headline = _report_headline(report_meta.get("og:title") or report_meta.get("twitter:title") or _title_tag(page), site_name)
        if not article or not _article_matches_headline(article, page_headline):
            return []
    # Publication dates and episode air dates are separate facts. Explicit
    # dates from different representations of the same article must agree.
    page_dates = _article_publication_dates(article) if requested else set()
    if requested and report_meta.get("article:published_time"):
        meta_date = _page_date(report_meta["article:published_time"])
        if not meta_date:
            return []
        page_dates.add(meta_date)
    if len(page_dates) > 1:
        return []
    page_published = next(iter(page_dates), "")
    rows = []
    seen = set()

    def add(report):
        if not isinstance(report, IndependentReport) or report.rejection():
            return
        key = (report.source_url, report.original_air_date)
        if key in seen:
            if requested:
                for i, old in enumerate(rows):
                    if ((old.source_url, old.original_air_date) == key and
                            _norm_text(old.series_title) == _norm_text(report.series_title) and
                            len(report.terms) > len(old.terms)):
                        rows[i] = report
                        break
            return
        seen.add(key)
        rows.append(report)

    for obj in _jsonld_objects(page):
        if not _json_types(obj).intersection({"newsarticle", "article", "reportagenewsarticle"}):
            continue
        headline = " ".join(str(obj.get("headline") or obj.get("name") or "").split())[:300]
        description = " ".join(str(obj.get("description") or obj.get("abstract") or "").split())[:MAX_PAGE_TEXT]
        if not headline and not description:
            continue
        published = _page_date(obj.get("datePublished") if requested else (obj.get("datePublished") or obj.get("dateModified")))
        if requested and page_published and published and published != page_published:
            return []
        air = (_report_air_date(headline, description, "", fallback_air) if fallback_series
               else _unknown_report_air_date(headline, description))
        if not published or not air:
            continue
        series = fallback_series or _series_from_report_headline(headline)
        if requested:
            if article and not _article_matches_headline(article, _report_headline(headline, site_name)):
                continue
            # Only visible article paragraphs can corroborate audio clues.
            # Metadata and structured articleBody establish no clue matches.
            body = _article_body(article, _report_headline(headline, site_name))
            terms = _requested_public_terms(requested, body)
            add(_report_row(source_url, headline, description, published, air, series, matched_terms=terms))
        else:
            add(_report_row(source_url, headline, description, published, air, series))
        if len(rows) >= MAX_REPORTS:
            return rows

    meta = _meta_map(page)
    headline = _strip_site_suffix(meta.get("og:title") or meta.get("twitter:title")
                                  or _title_tag(page))
    description = meta.get("og:description") or meta.get("description") or ""
    visible = _visible_text(page, 2000)
    if not headline and not description:
        return rows
    if requested:
        headline = _report_headline(headline, site_name)
        description = html_module.unescape(description)
        published = page_published
    else:
        published = _report_published(meta, visible)
    air = (_report_air_date(headline, description, visible, fallback_air) if fallback_series
           else _unknown_report_air_date(headline, description))
    if not published or not air:
        return rows
    series = fallback_series or _series_from_report_headline(headline)
    terms = _requested_public_terms(requested, _article_body(article, headline)) if requested else None
    add(_report_row(source_url, headline, description, published, air, series, visible, matched_terms=terms))
    return rows[:MAX_REPORTS]



# CSS numeric zero may use a sign, fractional notation, exponent, or unit.
_CSS_ZERO = r"[+-]?(?:0+(?:\.0*)?|\.0+)(?:e[+-]?\d+)?"
_CSS_HIDDEN_DECLARATION = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)|"
    r"content-visibility\s*:\s*hidden|opacity\s*:\s*" + _CSS_ZERO + r"%?|"
    r"font-size\s*:\s*" + _CSS_ZERO + r"(?:[a-z]+|%)?)\s*(?:!important\s*)?(?:;|$)", re.I)
_HIDDEN_CLASS = re.compile(r"^(?:hidden|is-hidden|u-hidden|d-none|sr-only|visually-hidden|"
                           r"screen-reader(?:-text|-only)?|hide|hidden-content|"
                           r"hide-on-[\w-]+|hidden-[\w-]+|d-[\w-]+-none)$", re.I)
_TYPED_EXCLUDED = frozenset("script style noscript nav aside footer form figure template svg".split())


def _css_hides(value):
    # CSS comments and escaped identifiers/values are semantically invisible.
    # Normalize them before checking visibility declarations. This also covers
    # comments between a property, colon, value or !important.
    value = re.sub(r"/\*.*?\*/", "", str(value or ""), flags=re.S)
    def decode(match):
        if match[1]:
            code = int(match[1],16)
            return chr(code) if 0 < code <= 0x10ffff else "\ufffd"
        return match[2] or ""
    value = re.sub(r"\\(?:([0-9a-fA-F]{1,6})(?:\r\n|[ \t\r\n\f])?|([^\r\n\f]))", decode, value)
    return bool(_CSS_HIDDEN_DECLARATION.search(value))


def _typed_scope(page, selector):
    """Read a single reviewed semantic scope, never page-wide text.

    Ignore hidden HTML, embedded CSS-hidden elements (including conditional
    rules, conservatively), and conventional externally styled hidden classes.
    No JavaScript execution or secondary stylesheet/network requests. This is
    static source evidence, not a claim about a rendered browser screenshot.
    Missing parser dependencies or unsupported hiding selectors fail closed.
    """
    try:
        from bs4 import BeautifulSoup
        import soupsieve
        soup = BeautifulSoup(_page_text(page), "html.parser")
        hidden = set()
        for node in soup.find_all(True):
            classes = node.get("class", [])
            if (node.name in _TYPED_EXCLUDED or node.has_attr("hidden") or
                    str(node.get("aria-hidden", "")).casefold() == "true" or
                    _css_hides(node.get("style", "")) or
                    any(_HIDDEN_CLASS.fullmatch(str(c)) for c in classes)):
                hidden.add(id(node))
        # Closed disclosures and dialogs are not active semantic evidence.
        # Exclude the entire closed details element, even its summary, rather
        # than interpreting a disclosure label as plot or event evidence.
        for node in soup.find_all(("details", "dialog")):
            if not node.has_attr("open"):
                hidden.add(id(node))
        # Match static stylesheet hiding rules before filtering the source
        # scope, including nested media rules. Ignore keyframe steps and
        # pseudo-elements, which do not hide the source element itself.
        rules = []
        for style in soup.find_all("style"):
            css = re.sub(r"/\*.*?\*/", "", style.get_text(), flags=re.S)
            for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
                if _css_hides(rule[2]):
                    value = rule[1].strip()
                    if re.fullmatch(r"(?:from|to|\d+(?:\.\d+)?%)", value, re.I):
                        continue
                    rules.append(value)
                    if len(rules) > 256:
                        return []
        for value in rules:
            # Pseudo-elements cannot contain the HTML source paragraphs.
            if re.search(r"::[\w-]+|:(?:before|after|first-letter|first-line|-ms-input-placeholder|-moz-placeholder|-webkit-input-placeholder)\b", value, re.I):
                # Remove only individual pseudo-element selectors. A comma
                # inside a selector is uncommon here; fail closed on ambiguous
                # selector lists rather than accidentally dropping a hide rule.
                if "," not in value:
                    continue
                if "(" in value or "[" in value:
                    return []
                value = ",".join(v for v in value.split(",") if not re.search(
                    r"::[\w-]+|:(?:before|after|first-letter|first-line|-ms-input-placeholder|-moz-placeholder|-webkit-input-placeholder)\b", v,re.I))
                if not value.strip():
                    continue
            for node in soupsieve.select(value,soup):
                hidden.add(id(node))
        def is_hidden(node):
            return id(node) in hidden or any(id(parent) in hidden for parent in node.parents)
        scopes = [node for node in soup.find_all(True) if not is_hidden(node) and
                  selector(node.name,{k:" ".join(v) if isinstance(v,list) else v for k,v in node.attrs.items()})]
        if len(scopes) != 1:
            return []
        rows=[]
        size=0
        scope=scopes[0]
        kinds=("p","h1","h2","h3","h4","li","th","td")
        nodes=([scope] if scope.name in kinds else [])+scope.find_all(kinds)
        for node in nodes:
            if is_hidden(node) or any(parent is not scope and parent.name in kinds
                                      for parent in node.parents if parent is not scope and any(ancestor is scope for ancestor in parent.parents)):
                continue
            parts=[str(t) for t in node.find_all(string=True) if not is_hidden(t.parent)
                   and type(t).__name__ == "NavigableString"]
            value=" ".join(" ".join(parts).split())[:2000]
            if value:
                size+=len(value)
                if size>24000:
                    break
                rows.append((node.name,value))
        return rows
    except (ImportError, ValueError, TypeError, RecursionError, RuntimeError):
        return []
    except Exception:
        # Malformed HTML/unsupported CSS must never contribute evidence.
        return []


def _typed_movie_year(title, paragraphs):
    """Only explicit release statements tied to the article's named film."""
    years = set()
    t = re.escape(title)
    for p in paragraphs:
        if not re.search(r"(?<!\w)" + t + r"(?!\w)", p, re.I):
            continue
        patterns = (
            r"\b(\d{4})\s*,?\s+when\s+" + t + r"\s+opened\b",
            r"(?<!\w)" + t + r"\s+(?:was\s+released|opened|premiered)\s+in\s+(?:the\s+summer\s+of\s+)?(\d{4})\b",
            r"\bsince\s+its\s+release,\s+" + t + r"\s+[^.!?]{1,400}[.]\s+Since\s+it['’]s\s+(?:US\s+)?release\s+in\s+(?:the\s+summer\s+of\s+)?(\d{4})\b",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, p, re.I):
                y = _year(match[1])
                if y:
                    years.add(y)
    return next(iter(years)) if len(years) == 1 else None



def _typed_indy500(page, url, clues, official, meta, headline):
    """Reviewed Indy 500 event-result profiles; no generic sports guesses."""
    host = _host(url).removeprefix("www.")
    path = urllib.parse.urlsplit(url).path
    title = ""
    event_date = ""
    sections = []
    published = ""
    if official and host == "indycar.com" and path.startswith("/news/"):
        m = re.fullmatch(r"Fantastic Five of (\d{4}): \d+(?:st|nd|rd|th) Indianapolis 500", headline, re.I)
        if not m:
            return []
        rows = _typed_scope(page, lambda t,a: t == "section" and "section-article" in (a.get("class") or "").split())
        if [_norm_text(v) for k,v in rows if k=="h1"] != [_norm_text(headline)]:
            return []
        race = [v for k,v in rows if k=="li" and re.fullmatch(r"Race: \d+(?:st|nd|rd|th) Indianapolis 500(?: presented by .+)?",v,re.I)]
        dates = [v.split(":",1)[1].strip() for k,v in rows if k=="li" and v.startswith("Date:")]
        winners = [v for k,v in rows if k=="li" and v.startswith("Winner:")]
        if len(race)!=1 or len(dates)!=1 or len(winners)!=1 or not winners[0][7:].strip():
            return []
        event_date = _date_from_text(dates[0]+", "+m[1])
        title = m[1]+" Indianapolis 500"
        sections = [v for k,v in rows if k in ("p","li")]
    elif not official and host == "theapex.racing":
        m = re.fullmatch(r"Race Report: (\d{4}) Indianapolis 500\s*•\s*The Apex",headline,re.I)
        if not m:
            return []
        title = m[1]+" Indianapolis 500"
        rows = _typed_scope(page,lambda t,a:t=="article" and "category-race-report" in (a.get("class") or "").split())
        if [_norm_text(v) for k,v in rows if k=="h1"] != [_norm_text(title)]:
            return []
        dates = [_date_from_text(v) for k,v in rows if k=="h2" and v.startswith("Indianapolis Motor Speedway ")]
        if len(dates)!=1:
            return []
        event_date = dates[0]
        # Only the visible completed-race top-five table, not preview, practice,
        # schedule, historical winners, or arbitrary article paragraphs.
        post = _typed_scope(page,lambda t,a:t=="div" and a.get("id")==m[1]+"-indianapolis-500-tab-0" and "rtbs_content" in (a.get("class") or "").split())
        starts = [i for i,(k,v) in enumerate(post) if k=="h3" and v=="Race Top Five"]
        if len(starts)!=1:
            return []
        rows2=[]
        for k,v in post[starts[0]+1:]:
            if k=="h3":
                break
            if k in ("th","td"):
                rows2.append((k,v))
        if [v for k,v in rows2 if k=="th"] != ["Rank","Start","Car No.","Driver","Team"]:
            return []
        cells=[v for k,v in rows2 if k=="td"]
        if len(cells)!=25 or [cells[i] for i in range(0,25,5)]!=list("12345"):
            return []
        sections=[title]+[cells[i] for i in range(3,25,5)]
        published=_page_date(meta.get("article:published_time"))
    elif not official and host=="racingnews.co":
        m=re.fullmatch(r"Indy 500 Results: (.+) \(Indycar\)",headline,re.I)
        if not m:
            return []
        event_date=_date_from_text(m[1])
        if not event_date:
            return []
        title=event_date[:4]+" Indianapolis 500"
        # The visible page title and the dedicated result section must agree.
        heads=_typed_scope(page,lambda t,a:t=="h1")
        if [_norm_text(v) for k,v in heads if k=="h1"] != [_norm_text(headline)]:
            return []
        rows=_typed_scope(page,lambda t,a:t=="div" and "dynamic-entry-content" in (a.get("class") or "").split())
        starts=[i for i,(k,v) in enumerate(rows) if k=="h4" and re.fullmatch(r"Indy 500 Race Results .+ Indycar Series",v,re.I) and _date_from_text(v)==event_date]
        if len(starts)!=1 or not any(k=="h2" and "Indianapolis 500" in v for k,v in rows):
            return []
        finishers=[]
        for k,v in rows[starts[0]+1:]:
            if k.startswith("h"):
                break
            if k=="p":
                match=re.fullmatch(r"(\d{1,2})[.]\s+(.{1,100})",v)
                if match and int(match[1])==len(finishers)+1:
                    finishers.append(match[2])
                elif finishers:
                    break
        if len(finishers)<5:
            return []
        sections=[title]+finishers
        published=_page_date(meta.get("article:published_time"))
    else:
        return []
    if not event_date or event_date[:4]!=title[:4]:
        return []
    terms=_requested_public_terms(clues,*sections[:128])
    row=TypedProgrammeEvidence("sports-event",title,url,terms,event_date=event_date,published_at=published)
    return [row] if len(terms)>=3 and row.checked(official) else []


def parse_typed_programme_page(page, url, clues, official=False):
    """Reviewed movie profiles; unsupported content never becomes identity."""
    host = _host(url).removeprefix("www.")
    if not safe_source_url(url, TYPED_OFFICIAL_HOSTS if official else TYPED_REPORT_HOSTS):
        return []
    path = urllib.parse.urlsplit(url).path
    meta = _meta_map(page)
    headline = _report_headline(meta.get("og:title", ""), meta.get("og:site_name", ""))
    if host in ("indycar.com", "theapex.racing", "racingnews.co"):
        return _typed_indy500(page, url, clues, official, meta, headline)
    title = ""
    year = None
    published = ""
    paragraphs = []
    if official and host == "amblin.com" and re.fullmatch(r"/movie/[a-z0-9-]+/?", path):
        if meta.get("og:type") != "video.movie" or meta.get("og:url", "").rstrip("/") != url.rstrip("/"):
            return []
        match = re.fullmatch(r"(.{1,200})\s+\((\d{4})\)\s+-\s+About the Movie", headline, re.I)
        if not match:
            return []
        title, year = match[1], _year(match[2])
        rows = _typed_scope(page, lambda tag, a: tag == "div" and "description" in (a.get("class") or "").split() and "data-description" in a)
        # The first plot paragraph is separate from subsequent production history.
        paragraphs = [re.sub(r"\([^()]{1,120}\)", " ", p) for k,p in rows if k == "p"][:1]
    elif not official and host == "theguardian.com" and path.startswith("/film/"):
        match = re.fullmatch(r"(.{1,200}),\s+\d+\s+years on:\s+.+", headline, re.I)
        if not match:
            return []
        title = match[1]
        article = _single_article(page)
        if not article or not _article_matches_headline(article, headline):
            return []
        rows = _typed_scope(page, lambda tag,a: tag == "article")
        paragraphs = [p for k,p in rows if k == "p"]
        year = _typed_movie_year(title, paragraphs)
        published = _page_date(meta.get("article:published_time"))
    elif not official and host == "screamhorrormag.com":
        match = re.fullmatch(r"(.{1,200}):\s*Film Review", headline, re.I)
        if not match:
            return []
        title = match[1]
        rows = _typed_scope(page, lambda tag,a: tag == "div" and "category-film-reviews" in (a.get("class") or "").split())
        if [_norm_text(v) for k,v in rows if k == "h1"] != [_norm_text(headline)]:
            return []
        paragraphs = [p for k,p in rows if k == "p"]
        year = _typed_movie_year(title, paragraphs)
        published = _page_date(meta.get("article:published_time"))
    else:
        return []
    terms = _requested_public_terms(clues, *paragraphs[:128])
    if not year or not terms:
        return []
    row = TypedProgrammeEvidence("movie", " ".join(title.split()), url, terms, release_year=year, published_at=published)
    return [row] if row.checked(official) else []


def _source_context(context):
    if not isinstance(context, dict):
        return {}
    series = context.get("seriesTitle")
    if not isinstance(series, str):
        return {}
    value = {"seriesTitle": " ".join(series.split())[:200],
             "originalAirDate": _date(context.get("originalAirDate"))}
    episode = context.get("episodeTitle")
    if isinstance(episode, str) and episode:
        value["episodeTitle"] = " ".join(episode.split())[:300]
    channel = context.get("channelID")
    if isinstance(channel, str) and CHANNEL_RE.fullmatch(channel):
        value["channelID"] = channel
    name = context.get("channelName")
    if isinstance(name, str) and name:
        value["channelName"] = " ".join(name.split())[:120]
    session = context.get("researchSession")
    if isinstance(session, str) and re.fullmatch(r"[0-9a-f]{64}", session):
        value["researchSession"] = session
    return value


class EpisodeSourceDiscovery:
    """Bounded automatic episode-source discovery with registry override.

    ``resolve`` derives candidate official archive URLs from a small reviewed
    set of non-date-specific roots and asks an optional injected provider for
    additional official/report URLs. A private registry entry for the exact
    series is authoritative and suppresses automatic candidates. Nothing here
    promotes evidence: every candidate still has to be fetched, parsed and
    corroborated by the existing audio episode contract. Candidate results are
    cached in memory, bounded and independent of the transient transcript.
    """

    def __init__(self, fetch_page=None, source_registry=None, registry_loader=None,
                 provider=None, official_hints=None, report_hints=None,
                 max_candidates=MAX_AUTO_CANDIDATES, cache_ttl=SOURCE_CANDIDATE_TTL,
                 clock=time.time):
        if callable(source_registry) and registry_loader is None:
            registry_loader = source_registry
            source_registry = None
        self.fetch_page = fetch_page
        self.source_registry = source_registry if isinstance(source_registry, dict) else {}
        self.registry_loader = registry_loader if callable(registry_loader) else None
        self.provider = provider if callable(provider) else None
        self.official_hints = dict(official_hints if isinstance(official_hints, dict)
                                   else DEFAULT_OFFICIAL_HINTS)
        self.report_hints = dict(report_hints if isinstance(report_hints, dict)
                                 else DEFAULT_REPORT_HINTS)
        self.max_candidates = max(1, min(MAX_SOURCE_PAGES, int(max_candidates)))
        self.cache_ttl = max(1.0, float(cache_ttl))
        self.clock = clock
        self._cache = OrderedDict()
        self._index_cache = OrderedDict()
        self.lookups = 0
        self.provider_calls = 0
        self.rejected = 0

    def _registry(self):
        if self.registry_loader is not None:
            try:
                value = self.registry_loader()
            except Exception:
                return {}
            return value if isinstance(value, dict) else {}
        return self.source_registry

    def _registry_entry(self, series):
        if not series:
            return None
        entry = self._registry().get(source_key(series))
        return entry if isinstance(entry, dict) else None

    @staticmethod
    def _cache_key(context, clues):
        series = _norm_text(context.get("seriesTitle"))
        air = _date(context.get("originalAirDate"))
        if air and series:
            return series + "|" + air
        normalized = normalize_clues(clues)
        episode = _norm_text(context.get("episodeTitle"))
        seed = "|".join((episode, "|".join(normalized)))
        scope = series or ("unknown:" + str(context.get("channelID") or context.get("channelName") or ""))
        return scope + "|" + (context.get("researchSession", "") if not series else "") + "|" + (_digest(seed) if seed.strip("|") else "")

    def _cache_get(self, key, now):
        entry = self._cache.get(key)
        if entry is None:
            return None
        if now >= entry[0]:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return entry[1], entry[2], entry[3]

    def _cache_put(self, key, official, reports, source, now):
        official = tuple(official[:self.max_candidates])
        reports = tuple(reports[:self.max_candidates])
        ttl = self.cache_ttl if (official or reports) else min(self.cache_ttl, SOURCE_NEGATIVE_TTL)
        self._cache[key] = (now + ttl, official, reports, source)
        self._cache.move_to_end(key)
        while len(self._cache) > MAX_SOURCE_CACHE:
            self._cache.popitem(last=False)

    def _fetch(self, url):
        if not callable(self.fetch_page):
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

    @staticmethod
    def _clean_candidates(values, allowed_hosts):
        rows = []
        if not isinstance(values, (list, tuple)):
            return rows
        for value in values[:MAX_PROVIDER_CANDIDATES * 2]:
            url = safe_source_url(value, allowed_hosts, allow_query=True)
            if url and url not in rows:
                rows.append(url)
        return rows

    def _provider_urls(self, context, clues):
        if self.provider is None:
            return [], []
        payload = dict(context, clues=list(normalize_clues(clues)))
        payload.pop("researchSession", None)
        self.provider_calls += 1
        try:
            result = self.provider(payload)
        except Exception:
            return [], []
        if isinstance(result, tuple) and len(result) == 2:
            official_raw, report_raw = result
        elif isinstance(result, dict):
            official_raw, report_raw = result.get("official", []), result.get("reports", [])
        else:
            return [], []
        unknown = not context.get("seriesTitle")
        return (self._clean_candidates(official_raw, TYPED_OFFICIAL_HOSTS if unknown else OFFICIAL_ARCHIVE_HOSTS),
                self._clean_candidates(report_raw, TYPED_REPORT_HOSTS if unknown else INDEPENDENT_REPORT_HOSTS))

    def _index_urls(self, page, root, context, deadline, clues=()):
        if page is None or time.monotonic() >= deadline:
            return []
        clues = normalize_clues(clues)
        root_host = _host(root)
        try:
            root_path = urllib.parse.urlsplit(root).path.rstrip("/")
        except ValueError:
            return []
        text = _page_text(page)
        if not text:
            return []
        wanted = _date(context.get("originalAirDate"))
        rows = []
        for match in _LINK_TAG.finditer(text):
            href = html_module.unescape(match.group(1) or match.group(2) or "")
            try:
                target = urllib.parse.urljoin(root, href)
                target_parts = urllib.parse.urlsplit(target)
            except ValueError:
                continue
            url = safe_source_url(target, OFFICIAL_ARCHIVE_HOSTS, allow_query=False)
            if not url or not _same_site(_host(url), root_host):
                continue
            if root_path and not target_parts.path.startswith(root_path + "/"):
                continue
            date_match = _URL_DATE.search(target_parts.path + "/")
            if not date_match:
                continue
            anchor = " ".join(html_module.unescape(_TAG.sub(" ", match.group(3) or "")).split())
            score = len(clue_matches(clues, anchor)) if clues else 0
            rows.append((date_match[1], url, score))
            if len(rows) >= MAX_INDEX_LINKS:
                break
        rows.sort(key=lambda item: item[0], reverse=True)
        if clues:
            rows.sort(key=lambda item: item[2], reverse=True)
        if wanted:
            rows.sort(key=lambda item: item[0] != wanted)
        seen = []
        for _date_value, url, _score in rows:
            if url not in seen:
                seen.append(url)
            if len(seen) >= MAX_INDEX_PAGES:
                break
        return seen

    def _automatic_urls(self, context, clues, now):
        deadline = time.monotonic() + MAX_RESEARCH_SECONDS
        series = context.get("seriesTitle", "")
        key = source_key(series)
        roots = self.official_hints.get(key, ())
        if isinstance(roots, str):
            roots = (roots,)
        air = _date(context.get("originalAirDate"))
        official = []
        for root in roots if isinstance(roots, (list, tuple)) else ():
            root_url = safe_source_url(root, OFFICIAL_ARCHIVE_HOSTS, allow_query=False)
            if not root_url:
                self.rejected += 1
                continue
            if air:
                for suffix in ("", "/results"):
                    url = safe_source_url(root_url.rstrip("/") + "/" + air + suffix,
                                          OFFICIAL_ARCHIVE_HOSTS, allow_query=False)
                    if url and url not in official:
                        official.append(url)
            else:
                cached = self._index_cache.get(root_url)
                if cached is not None and now < cached[0]:
                    found = list(cached[1])
                else:
                    page = self._fetch(root_url)
                    found = self._index_urls(page, root_url, context, deadline, clues)
                    self._index_cache[root_url] = (now + self.cache_ttl, tuple(found))
                    self._index_cache.move_to_end(root_url)
                    while len(self._index_cache) > MAX_SOURCE_CACHE:
                        self._index_cache.popitem(last=False)
                for url in found:
                    if url not in official:
                        official.append(url)
                if time.monotonic() >= deadline:
                    break
        report_roots = self.report_hints.get(key, ())
        if isinstance(report_roots, str):
            report_roots = (report_roots,)
        reports = []
        for root in report_roots if isinstance(report_roots, (list, tuple)) else ():
            url = safe_source_url(root, INDEPENDENT_REPORT_HOSTS, allow_query=True)
            if url and url not in reports:
                reports.append(url)
        if time.monotonic() < deadline:
            extra_official, extra_reports = self._provider_urls(context, clues)
            for url in extra_official:
                if url not in official:
                    official.append(url)
            for url in extra_reports:
                if url not in reports:
                    reports.append(url)
        return (official[:self.max_candidates], reports[:self.max_candidates],
                "automatic+provider" if self.provider is not None else "automatic")

    def resolve(self, context, clues=(), now=None):
        """Return ``(official_urls, report_urls, source)`` for a programme context.

        A private registry entry always wins. Automatic discovery is otherwise
        bounded by the reviewed roots, the injected provider result limit, the
        per-family candidate limit and the in-memory TTL cache. It never returns
        a URL outside the two existing host allowlists.
        """
        context = _source_context(context)
        clues = normalize_clues(clues)
        if not context or (not context.get("seriesTitle") and (not context.get("channelID") or len(clues) < MIN_DISTINCTIVE_CLUES)):
            return [], [], ""
        now = self.clock() if now is None else float(now)
        key = self._cache_key(context, clues)
        cacheable = bool(context.get("seriesTitle") or context.get("researchSession"))
        cached = self._cache_get(key, now) if cacheable else None
        if cached is not None:
            return list(cached[0]), list(cached[1]), cached[2]
        entry = self._registry_entry(context.get("seriesTitle"))
        if entry is not None:
            official = self._clean_candidates(entry.get("official", []), OFFICIAL_ARCHIVE_HOSTS)
            reports = self._clean_candidates(entry.get("reports", []), INDEPENDENT_REPORT_HOSTS)
            official, reports = official[:self.max_candidates], reports[:self.max_candidates]
            self._cache_put(key, official, reports, "registry", now)
            return official, reports, "registry"
        official, reports, source = self._automatic_urls(context, clues, now)
        self.lookups += 1
        if cacheable:
            self._cache_put(key, official, reports, source, now)
        return official, reports, source

    def prefetch(self, context, clues=()):
        """Warm the bounded candidate cache without any transcript dependency."""
        official, reports, _source = self.resolve(context, ())
        return bool(official or reports)

    def clear(self):
        self._cache.clear()
        self._index_cache.clear()

    def diagnostics(self):
        return {"revision": REVISION, "cacheEntries": len(self._cache),
                "indexCacheEntries": len(self._index_cache),
                "maxCacheEntries": MAX_SOURCE_CACHE, "cacheTtlSeconds": self.cache_ttl,
                "lookups": self.lookups, "providerCalls": self.provider_calls,
                "rejectedCandidates": self.rejected,
                "officialHosts": sorted(OFFICIAL_ARCHIVE_HOSTS),
                "independentReportHosts": sorted(INDEPENDENT_REPORT_HOSTS),
                "maxCandidatesPerFamily": self.max_candidates,
                "registryOverride": True}


class PublicEvidenceResearch:
    """Actual bounded research adapter for the audio episode handoff.

    It consumes the minimal clue payload plus the gateway's already-observed
    programme context, then reads only approved official broadcaster archive
    and independent-report URLs. The optional registry is an override; without
    it, bounded automatic discovery derives candidates from reviewed archive
    roots and an optional injected search provider. Network access is injected
    (``fetch_page``); no media stream, microphone or Whisper pass is started
    here. Missing evidence or any lookup failure stays inconclusive.
    """

    def __init__(self, fetch_page, source_registry=None, max_pages=MAX_SOURCE_PAGES,
                 registry_loader=None, deadline_seconds=MAX_RESEARCH_SECONDS,
                 discovery=None, provider=None, cache_ttl=SOURCE_CANDIDATE_TTL,
                 official_hints=None, report_hints=None, unknown_deadline_seconds=None,
                 fetch_with_timeout=None):
        self.fetch_page = fetch_page
        self.fetch_with_timeout = fetch_with_timeout if callable(fetch_with_timeout) else None
        if callable(source_registry) and registry_loader is None:
            registry_loader = source_registry
            source_registry = None
        self.source_registry = source_registry if isinstance(source_registry, dict) else {}
        self.registry_loader = registry_loader if callable(registry_loader) else None
        self.max_pages = max(1, min(MAX_SOURCE_PAGES, int(max_pages)))
        self.deadline_seconds = max(1.0, float(deadline_seconds))
        self.unknown_deadline_seconds = self.deadline_seconds if unknown_deadline_seconds is None else max(1.0, float(unknown_deadline_seconds))
        self.discovery = discovery if isinstance(discovery, EpisodeSourceDiscovery) else None
        if self.discovery is None:
            self.discovery = EpisodeSourceDiscovery(
                fetch_page, source_registry=self.source_registry,
                registry_loader=self.registry_loader, provider=provider,
                official_hints=official_hints, report_hints=report_hints,
                max_candidates=self.max_pages, cache_ttl=cache_ttl)
        self._evidence = OrderedDict()
        self._evidence_ttl = max(1.0, float(cache_ttl))
        self._read_diag_lock = threading.Lock()
        self._unknown_read_diag = {}

    def diagnostics(self):
        with self._read_diag_lock:
            return dict(self._unknown_read_diag)

    def _record_unknown_read(self, counts):
        with self._read_diag_lock:
            self._unknown_read_diag = {"calls": self._unknown_read_diag.get("calls", 0) + 1,
                                       "last": dict(counts)}


    def _registry(self):
        if self.registry_loader is not None:
            try:
                value = self.registry_loader()
            except Exception:
                return {}
            return value if isinstance(value, dict) else {}
        return self.source_registry

    def _urls(self, context, clues=()):
        return self.discovery.resolve(context, clues)[:2]

    def _evidence_key(self, context, official_urls, report_urls, clues=()):
        identity = "|".join((_norm_text(context.get("seriesTitle")),
                             _date(context.get("originalAirDate")),
                             context.get("researchSession", "") if not context.get("seriesTitle") else "",
                             _digest("|".join(normalize_clues(clues))) if not context.get("seriesTitle") else "",
                              ",".join(official_urls), ",".join(report_urls)))
        return _digest(identity)

    def _cached(self, key, now):
        entry = self._evidence.get(key)
        if entry is None:
            return None
        if now >= entry[0]:
            self._evidence.pop(key, None)
            return None
        self._evidence.move_to_end(key)
        return list(entry[1]), list(entry[2])

    def _store(self, key, official, reports, now):
        ttl = self._evidence_ttl if (official or reports) else min(self._evidence_ttl,
                                                                   SOURCE_NEGATIVE_TTL)
        self._evidence[key] = (now + ttl, tuple(official), tuple(reports))
        self._evidence.move_to_end(key)
        while len(self._evidence) > MAX_SOURCE_CACHE:
            self._evidence.popitem(last=False)

    def _fetch_result(self, url, allowed_hosts, deadline=None, timeout_cap=8.0):
        if not callable(self.fetch_page) or _host(url) not in allowed_hosts:
            return None, False
        try:
            if self.fetch_with_timeout is not None and deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, False
                page = self.fetch_with_timeout(url, min(timeout_cap, remaining))
            else:
                page = self.fetch_page(url)
        except Exception as error:
            blocked = (isinstance(error, urllib.error.HTTPError)
                       and error.code in (401, 403, 451))
            return None, blocked
        if isinstance(page, (bytes, bytearray)) and len(page) > MAX_PAGE_BYTES:
            return None, False
        if isinstance(page, str) and len(page) > MAX_PAGE_BYTES:
            return None, False
        return page, False

    def _fetch(self, url, allowed_hosts):
        return self._fetch_result(url, allowed_hosts)[0]

    def _read_unknown(self, official_urls, report_urls, context, clues, deadline):
        """Use the shared deadline fairly across both required source families.

        Prefer distinct report publishers and interleave archive reads. URL
        dates only affect order: actual fetched pages still establish identity.
        A timeout or rejected source cannot become evidence.
        """
        first, later, publishers = [], [], set()
        for url in report_urls:
            publisher = independent_publisher(url)
            (later if publisher in publishers else first).append(url)
            publishers.add(publisher)
        report_urls = first + later
        official_urls = list(official_urls)
        official, reports, blocked = [], [], set()
        report_turn = bool(report_urls)
        counts = {"officialCandidates": len(official_urls), "reportCandidates": len(report_urls),
                  "officialAttempts": 0, "reportAttempts": 0, "officialFetched": 0,
                  "reportFetched": 0, "denied": 0}
        while (official_urls or report_urls) and time.monotonic() < deadline:
            if report_urls and (report_turn or not official_urls):
                url = report_urls.pop(0)
                report_turn = False
                if independent_publisher(url) in blocked:
                    continue
                counts["reportAttempts"] += 1
                page, denied = self._fetch_result(url, TYPED_REPORT_HOSTS, deadline, UNKNOWN_PAGE_TIMEOUT_SECONDS)
                counts["denied"] += int(denied)
                counts["reportFetched"] += int(page is not None)
                if denied:
                    blocked.add(independent_publisher(url))
                elif page is not None:
                    reports.extend(parse_report_page(page, url, context, clues=clues))
                    reports.extend(parse_typed_programme_page(page, url, clues))
            elif official_urls:
                # A report's explicit air date can prioritize existing archive
                # leads, but cannot generate a new URL or establish identity.
                dates = {r.original_air_date for r in reports if isinstance(r, IndependentReport)}
                def priority(url):
                    match = _URL_DATE.search(urllib.parse.urlsplit(url).path + "/")
                    return bool(match and match[1] in dates)
                index = next((i for i, url in enumerate(official_urls) if priority(url)), 0)
                url = official_urls.pop(index)
                report_turn = True
                if _host(url).removeprefix("www.") in blocked:
                    continue
                counts["officialAttempts"] += 1
                page, denied = self._fetch_result(url, TYPED_OFFICIAL_HOSTS, deadline, UNKNOWN_PAGE_TIMEOUT_SECONDS)
                counts["denied"] += int(denied)
                counts["officialFetched"] += int(page is not None)
                if denied:
                    blocked.add(_host(url).removeprefix("www."))
                elif page is not None:
                    official.extend(parse_official_page(page, url))
                    official.extend(parse_typed_programme_page(page, url, clues, official=True))
            if len(official) >= MAX_OFFICIAL_EPISODES:
                official_urls.clear()
            if len(reports) >= MAX_REPORTS:
                report_urls.clear()
        counts.update(officialParsed=min(len(official), MAX_OFFICIAL_EPISODES),
                      reportParsed=min(len(reports), MAX_REPORTS),
                      remainingSeconds=round(max(0.0, deadline-time.monotonic()), 3))
        self._record_unknown_read(counts)
        return official[:MAX_OFFICIAL_EPISODES], reports[:MAX_REPORTS]

    def _read(self, context, clues=(), now=None):
        context = _source_context(context)
        if not context or (not context.get("seriesTitle") and (not context.get("channelID") or len(normalize_clues(clues)) < MIN_DISTINCTIVE_CLUES)):
            return [], []
        now = time.time() if now is None else float(now)
        # In no-EPG mode, discovery and verification share one research clock.
        # The known-title lane retains its previous fetch budget.
        unknown_title = not context.get("seriesTitle")
        started = time.monotonic()
        official_urls, report_urls, _source = self.discovery.resolve(context, clues, now)
        official_urls = official_urls[:self.max_pages]
        report_urls = report_urls[:self.max_pages]
        key = self._evidence_key(context, official_urls, report_urls, clues)
        cacheable = not unknown_title or bool(context.get("researchSession"))
        cached = self._cached(key, now) if cacheable else None
        if cached is not None:
            return cached
        deadline = (started + self.unknown_deadline_seconds if unknown_title else time.monotonic() + self.deadline_seconds)
        if unknown_title:
            official, reports = self._read_unknown(official_urls, report_urls, context, clues, deadline)
            if cacheable:
                self._store(key, official, reports, now)
            return official, reports
        blocked = set()
        official = []
        for url in official_urls:
            host = _host(url)
            if time.monotonic() >= deadline:
                break
            if host in blocked:
                continue
            page, was_blocked = self._fetch_result(url, OFFICIAL_ARCHIVE_HOSTS, deadline if unknown_title else None)
            if was_blocked:
                blocked.add(host)
                continue
            if page is None:
                continue
            official.extend(parse_official_page(page, url, context.get("seriesTitle", "")))
            if len(official) >= MAX_OFFICIAL_EPISODES:
                official = official[:MAX_OFFICIAL_EPISODES]
                break
        reports = []
        for url in report_urls:
            host = _host(url)
            if time.monotonic() >= deadline:
                break
            if host in blocked:
                continue
            page, was_blocked = self._fetch_result(url, INDEPENDENT_REPORT_HOSTS, deadline if unknown_title else None)
            if was_blocked:
                blocked.add(host)
                continue
            if page is None:
                continue
            reports.extend(parse_report_page(page, url, context, clues=clues if unknown_title else ()))
            if len(reports) >= MAX_REPORTS:
                reports = reports[:MAX_REPORTS]
                break
        if cacheable:
            self._store(key, official, reports, now)
        return official[:MAX_OFFICIAL_EPISODES], reports[:MAX_REPORTS]

    def prefetch(self, context, clues=()):
        """Warm public evidence independently of the transient transcript."""
        official, reports = self._read(context, ())
        return bool(official or reports)

    def __call__(self, payload):
        context = payload.get("programme") if isinstance(payload, dict) else None
        if not isinstance(context, dict):
            return [], []
        clues = payload.get("clues") if isinstance(payload, dict) else ()
        return self._read(context, clues)


@dataclasses.dataclass(frozen=True)
class PendingClue:
    """Bounded in-memory handoff. It never contains the raw transcript."""

    job: dict
    decision: dict
    clues: tuple
    queued_at: float
    key: str


def _sanitized_job(job, now):
    if not isinstance(job, dict):
        return None
    view = {
        "cid": job.get("cid"), "provider": job.get("provider"), "name": job.get("name"),
        "seconds": job.get("seconds"), "deviceVerified": job.get("deviceVerified"),
        "changed": bool(job.get("changed")), "launchID": job.get("launchID", ""),
        "streamGeneration": job.get("streamGeneration", ""), "visitID": job.get("visitID", ""),
        "observedAt": now, "expiresAt": None,
        "sampleDigest": job.get("sampleDigest"), "profile": job.get("profile", "american"),
        "activeSeconds": job.get("activeSeconds"), "hasEpisodeIdentity": bool(job.get("hasEpisodeIdentity")),
        "episodeOnly": bool(job.get("episodeOnly")),
    }
    for key in ("launchID", "streamGeneration", "visitID", "profile"):
        value = view.get(key)
        if not isinstance(value, str) or len(value) > 200:
            view[key] = "" if key != "profile" else "american"
    for key in ("name", "provider"):
        value = view.get(key)
        if not isinstance(value, str) or len(value) > 200:
            view[key] = ""
    return view


def _sanitized_decision(decision):
    if not isinstance(decision, dict):
        return None
    result = {}
    for key in ("outcome", "language", "confidence", "speechSeconds", "sampleSeconds"):
        if key in decision:
            result[key] = decision.get(key)
    return result


class EpisodeObserver:
    """Callable handoff for ``language_audio.Manager(on_speech=...)``.

    Disabled by default. ``research`` is injected and receives only
    ``ClueRequest.research_payload()``; it must return
    ``(official_episodes, independent_reports)``. It is responsible for using
    approved official archives and independent reports only.

    The language worker must never wait for research. ``submit()`` extracts
    bounded clues synchronously and returns; ``probe``, network research, the
    post-research fresh re-probe and publication all run on this observer's own
    bounded daemon worker. ``__call__`` remains the synchronous test surface.
    """

    def __init__(self, probe, research, *, enabled=DEFAULT_ENABLED, budget=None,
                 ttl=DEFAULT_SESSION_TTL, log=None, programme=None, activity=None,
                 publisher=None, publish=None, retry=None,
                 retry_seconds=EPISODE_RETRY_SECONDS, max_retries=MAX_EPISODE_RETRIES):
        self.probe = probe
        self.research = research
        self.enabled = bool(enabled)
        self.budget = budget
        self.ttl = ttl
        self.log = log or (lambda message: None)
        self.programme = programme
        self.activity = activity
        self.publisher = publisher
        self.publish = publish
        self.retry = retry
        self.retry_seconds = max(0.0, float(retry_seconds))
        self.max_retries = max(0, min(6, int(max_retries)))
        self._published = OrderedDict()
        self._retries = OrderedDict()
        self._queue = None
        self._worker = None
        self._queue_lock = threading.Lock()
        self._pending = OrderedDict()
        self.submitted = 0
        self.dropped = 0
        self._diagnostics = {"reasons": OrderedDict(), "lastReason": None,
                             "lastCategory": None, "lastAt": 0.0,
                             "retriesRequested": 0, "retriesAccepted": 0,
                             "retriesSuppressed": 0, "lastRetryReason": None}
        self._diagnostics["contentResearch"] = {"completed": 0, "verified": 0, "verifiedAfterVisit": 0,
                                                "lastOutcome": None, "lastAt": 0.0}
        self._diag_lock = threading.Lock()

    def _diagnose(self, reason, category=None, detail=None):
        reason = str(reason or "unknown")[:80]
        category = str(category or reason_category(reason))[:40]
        with self._diag_lock:
            reasons = self._diagnostics["reasons"]
            reasons[reason] = reasons.get(reason, 0) + 1
            while len(reasons) > 32:
                reasons.popitem(last=False)
            self._diagnostics["lastReason"] = reason
            self._diagnostics["lastCategory"] = category
            self._diagnostics["lastAt"] = round(time.time(), 3)
            if detail:
                self._diagnostics["lastDetail"] = str(detail)[:120]

    def _context(self, channel_id):
        if not callable(self.programme):
            return None
        try:
            row = self.programme(channel_id)
        except Exception:
            return None
        if isinstance(row, dict) and row.get("contentSource") == "local-audio-programme-identity":
            if not self._has_resolved_identity(row, channel_id):
                return {"channelID": channel_id, "seriesTitle": ""}
        return row if isinstance(row, dict) else None

    def _has_resolved_identity(self, row, channel_id):
        if isinstance(row, dict) and row.get("contentSource") == "local-audio-programme-identity":
            view = self._usable_activity(channel_id)
            return typed_programme_resolved(row, (view or {}).get("session_hash"))
        return programme_has_episode_identity(row)

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

    def _duplicate(self, binding, identity, now, record=False, session_hash=None):
        key = "|".join((binding.digest(), str(session_hash or ""), identity.broadcaster_code or "",
                        _digest(identity.series_title + "|" + identity.original_air_date + "|" +
                                identity.content_kind + "|" + str(identity.release_year or "") + "|" + identity.event_date)))
        expires = self._published.get(key)
        if expires is not None and now < expires:
            return True
        if not record:
            return False
        self._published[key] = now + max(float(self.ttl), DEDUP_SECONDS)
        self._published.move_to_end(key)
        while len(self._published) > 64:
            self._published.popitem(last=False)
        return False

    @staticmethod
    def _programme_coherent(before, after):
        if before is None or after is None:
            return before is after
        if not isinstance(before, dict) or not isinstance(after, dict):
            return False
        if before.get("channelID") and after.get("channelID") and before["channelID"] != after["channelID"]:
            return False
        series_before = _norm_text(before.get("seriesTitle"))
        series_after = _norm_text(after.get("seriesTitle"))
        if series_before and series_after and series_before != series_after:
            return False
        for key in ("seasonNumber", "episodeNumber"):
            left, right = before.get(key), after.get(key)
            if type(left) is int and type(right) is int and left != right:
                return False
        date_before = _date(before.get("originalAirDate"))
        date_after = _date(after.get("originalAirDate"))
        if date_before and date_after and date_before != date_after:
            return False
        return True

    def _usable_activity(self, channel_id):
        if not callable(self.activity):
            return None
        try:
            view = self.activity(channel_id)
        except Exception:
            return None
        if not isinstance(view, dict):
            return None
        if view.get("channel_id") not in (None, channel_id):
            return None
        try:
            active = float(view.get("active_seconds", 0) or 0)
        except (TypeError, ValueError):
            active = 0.0
        return view if active > 0 else None

    def _schedule_retry(self, binding, reason, now):
        """Request one bounded, independent episode-only retry if playback runs."""
        category = reason_category(reason)
        if category not in ("commercial", "no-match", "inactive"):
            self._diagnose("retry-not-applicable", category, reason)
            return False
        if not callable(self.retry):
            self._diagnose("retry-unavailable", "retry", reason)
            return False
        if self._usable_activity(binding.channel_id) is None:
            # Actual playback paused/stopped/stale: no new media will be
            # sampled, so the retry is intentionally not requested.
            self._diagnose("retry-inactive", "retry", reason)
            return False
        key = binding.digest()
        row = self._retries.get(key)
        if row is None:
            row = {"attempts": 0, "last": 0.0, "reason": ""}
            self._retries[key] = row
        if row["attempts"] >= self.max_retries:
            self._diagnose("retry-exhausted", "retry", reason)
            with self._diag_lock:
                self._diagnostics["retriesSuppressed"] += 1
            return False
        row["attempts"] += 1
        row["last"] = now
        row["reason"] = category
        self._retries.move_to_end(key)
        while len(self._retries) > 64:
            self._retries.popitem(last=False)
        with self._diag_lock:
            self._diagnostics["retriesRequested"] += 1
            self._diagnostics["lastRetryReason"] = category
        try:
            accepted = bool(self.retry(binding, category))
        except Exception as error:
            self._diagnose("retry-error", "retry", type(error).__name__)
            return False
        self._diagnose("retry-" + ("scheduled" if accepted else "rejected"), "retry", reason)
        if accepted:
            with self._diag_lock:
                self._diagnostics["retriesAccepted"] += 1
        return accepted

    def _publish(self, promotion, context, now):
        if not promotion.accepted or promotion.episode is None:
            return False
        session = (context or {}).get("publicationSessionHash")
        minimum = (context or {}).get("minimumConfirmedSeconds", 0.0)
        def same_session():
            if not session:
                return not minimum
            activity = self._usable_activity(promotion.episode.channel_id) or {}
            return (activity.get("session_hash") == session and
                    (not minimum or self._confirmed_progress(activity) >= minimum))
        def discard():
            if isinstance(self.publisher, PublicationStore):
                self.publisher.discard_publication(promotion, context, now)
        if not same_session():
            return False
        if self.publisher is not None:
            try:
                if not self.publisher.publish(promotion, context, now):
                    return False
            except Exception as error:
                discard()
                self.log("episode publication error " + type(error).__name__)
                return False
        if not same_session():
            discard()
            return False
        if callable(self.publish):
            try:
                delivered = bool(self.publish(promotion, context, now))
            except Exception as error:
                self.log("episode publish callback error " + type(error).__name__)
                delivered = False
            if not delivered:
                discard()
                return False
        if not same_session():
            discard()
            return False
        identity = promotion.episode.identity
        if identity.content_kind in TYPED_CONTENT_KINDS:
            self.log("PROGRAMME_IDENTITY kind=%s title=%s releaseYear=%s eventDate=%s confidence=%.3f sources=%d" % (
                identity.content_kind, identity.series_title[:80], identity.release_year or "-", identity.event_date or "-",
                promotion.episode.confidence, promotion.episode.corroborating_sources))
            return True
        self.log("EPISODE_IDENTITY series=%s date=%s code=%s confidence=%.3f sources=%d" % (
            identity.series_title[:80], identity.original_air_date,
            identity.broadcaster_code or "-", promotion.episode.confidence,
            promotion.episode.corroborating_sources))
        return True

    @staticmethod
    def _confirmed_progress(activity):
        try:
            value = float((activity or {}).get("confirmed_seconds", 0))
            return value if math.isfinite(value) and value > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _evaluate(self, request, snapshot, context, job, now, retry=False):
        binding = request.binding
        unknown_title = isinstance(context, dict) and not context.get("seriesTitle")
        initial_activity = self._usable_activity(binding.channel_id) if unknown_title else None
        session_hash = initial_activity.get("session_hash") if initial_activity else None
        early_lead = bool(unknown_title and self.budget is not None)
        if early_lead and not session_hash:
            self._diagnose("session-unverified", "stale")
            return None
        view = None
        dedup_key = ""
        if self.budget is not None:
            view = self._view(binding, job)
            if early_lead:
                view["active_seconds"] = self._confirmed_progress(initial_activity)
            dedup_key = _digest(binding.channel_id + "|" + _norm_text((context or {}).get("seriesTitle"))
                                + "|" + "|".join(request.clues))
            allowed, budget_reason = self.budget.eligible(
                view, has_episode_identity=bool(view.get("hasEpisodeIdentity")),
                profile=binding.profile, language=request.language,
                language_outcome=request.language_outcome, dedup_key=dedup_key,
                sample_seconds=request.sample_seconds, allow_retry=retry, unknown_lead=early_lead)
            if not allowed:
                self._diagnose("budget-" + str(budget_reason or "rejected"), "budget",
                               ("confirmed=%.1f sample=%.1f" % (view["active_seconds"], request.sample_seconds)) if early_lead else ("episodeOnly" if retry else ""))
                if budget_reason == "inactive":
                    # The clue is not dropped just because independent active
                    # viewing has not reached the unchanged floor yet. Ask the
                    # existing media-triggered episode retry for one bounded,
                    # same-visit reattempt; it only runs while playback is
                    # actually progressing because _usable_activity fails closed.
                    self._schedule_retry(binding, "inactive", now)
                return None
            self.budget.begin(view, dedup_key=dedup_key, retry=retry)
        payload = request.research_payload()
        if context is not None:
            programme = dict(context, channelName=request.channel_name)
            if unknown_title:
                programme["researchSession"] = hashlib.sha256((binding.digest() + "|" + str(session_hash or "")).encode()).hexdigest()
            payload = dict(payload, channelID=binding.channel_id, programme=programme)
        official, reports = [], []
        if callable(self.research):
            try:
                official, reports = self.research(payload)
            except Exception as error:
                self._diagnose("research-error", "research", type(error).__name__)
                official, reports = [], []
        if not official and not reports:
            self._diagnose("no-sources", "no-sources")
        # A fast lead search may finish before the publish floor. Retain only
        # the already bounded clues/evidence, never resample or delay rotation.
        # The worker waits at most 12 seconds and never beyond the binding age.
        if early_lead and official and len(reports) >= 2:
            deadline = time.monotonic() + UNKNOWN_CONFIRM_WAIT_SECONDS
            while binding.checked(time.time()) and time.monotonic() < deadline:
                activity = self._usable_activity(binding.channel_id) or {}
                if activity.get("session_hash") != session_hash or self._confirmed_progress(activity) >= UNKNOWN_PUBLISH_PROGRESS_SECONDS:
                    break
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        # Research is network-bound. Re-probe after it and require the same
        # exact physical visit and a coherent programme before publishing.
        after = time.time()
        fresh = self.probe() if callable(self.probe) else None
        fresh_context = context
        if not isinstance(fresh, dict):
            promotion = Promotion(False, "session-unprobeable")
        elif not binding.checked(after):
            promotion = Promotion(False, "session-expired")
        elif not binding.matches(fresh, after):
            promotion = Promotion(False, "session-changed")
        else:
            fresh_context = self._context(binding.channel_id)
            if context is not None and not self._programme_coherent(context, fresh_context):
                promotion = Promotion(False, "programme-changed")
            elif (fresh_context is not None and fresh_context.get("channelID")
                  and fresh_context.get("channelID") != binding.channel_id):
                promotion = Promotion(False, "programme-changed")
            elif fresh_context is not None and self._has_resolved_identity(fresh_context, binding.channel_id):
                promotion = Promotion(False, "identity-present")
            else:
                try:
                    promotion = promote_episode(request, official, reports, fresh, after,
                                                programme=context if unknown_title else fresh_context)
                    if promotion.accepted and not programme_matches(promotion.episode.identity, fresh_context):
                        promotion = Promotion(False, "programme-mismatch")
                except Exception as error:
                    self._diagnose("promotion-error", "research", type(error).__name__)
                    promotion = Promotion(False, "promotion-error")
        # Source verification is a separate outcome from live delivery.
        # Even after a rotation, evaluate the original sample's content once;
        # never publish or save it as current programme for any other visit.
        if unknown_title:
            try:
                content = verify_episode_content(request, official, reports, after, context)
                with self._diag_lock:
                    result = self._diagnostics["contentResearch"]
                    result["completed"] += 1
                    result["verified"] += int(content.accepted)
                    result["verifiedAfterVisit"] += int(content.accepted and (
                        not isinstance(fresh, dict) or not binding.matches(fresh, after)))
                    result["lastOutcome"] = "verified" if content.accepted else content.reason
                    result["lastAt"] = round(after, 3)
            except Exception as error:
                self._diagnose("content-verification-error", "research", type(error).__name__)
        if promotion.accepted and promotion.episode is not None:
            if self._duplicate(binding, promotion.episode.identity, after, session_hash=session_hash):
                promotion = Promotion(False, "duplicate-publication")
                self._diagnose("duplicate-publication", "duplicate")
            elif unknown_title and (not session_hash or not fresh_context or
                    (self._usable_activity(binding.channel_id) or {}).get("session_hash") != session_hash):
                promotion = Promotion(False, "session-changed")
                self._diagnose("session-changed", "stale")
            elif early_lead and (request.sample_seconds < MIN_EVIDENCE_SAMPLE_SECONDS or
                    self._confirmed_progress(self._usable_activity(binding.channel_id)) < UNKNOWN_PUBLISH_PROGRESS_SECONDS):
                promotion = Promotion(False, "insufficient-confirmed-progress")
                self._diagnose("insufficient-confirmed-progress", "budget")
            elif not self._publish(promotion, dict(fresh_context, publicationSessionHash=session_hash,
                    minimumConfirmedSeconds=UNKNOWN_PUBLISH_PROGRESS_SECONDS if early_lead else 0.0) if unknown_title else fresh_context, after):
                promotion = Promotion(False, "publication-failed")
                self._diagnose("publication-failed", "publication")
            else:
                self._duplicate(binding, promotion.episode.identity, after, record=True, session_hash=session_hash)
                self._diagnose("promoted", "promoted")
        else:
            self._diagnose(promotion.reason, reason_category(promotion.reason))
            # Only a conclusive no-source/no-match/commercial rejection gets a
            # bounded independent retry. Stale sessions and identity-present
            # paths never request another audio sample.
            self._schedule_retry(binding, promotion.reason, after)
        if self.budget is not None and view is not None:
            self.budget.finish(view, "promoted" if promotion.accepted else promotion.reason)
        return promotion

    def __call__(self, job, decision, text):
        if not self.enabled:
            self._diagnose("disabled", "disabled")
            return None
        try:
            now = time.time()
            snapshot = self.probe() if callable(self.probe) else None
            binding = make_binding(job, snapshot, now, self.ttl)
            if binding is None or not binding.visit_id:
                self._diagnose("session-unverified", "stale")
                return None
            context = self._context(binding.channel_id)
            if context is not None:
                if context.get("channelID") and context.get("channelID") != binding.channel_id:
                    self._diagnose("programme-changed", "stale")
                    return None
                if self._has_resolved_identity(context, binding.channel_id):
                    self._diagnose("identity-present", "identity-present")
                    return None
            request = build_request(text, job, decision, binding, now)
            if request is None:
                self._diagnose("request-rejected", "early-return")
                return None
            return self._evaluate(request, snapshot, context, job, now,
                                  retry=bool(job.get("episodeOnly")))
        except Exception as error:
            self._diagnose("observer-error", "error", type(error).__name__)
            self.log("episode clue observer error " + type(error).__name__)
            return None

    def _ensure_worker(self):
        with self._queue_lock:
            if self._queue is not None:
                return True
            self._queue = queue.Queue(maxsize=MAX_PENDING)
            self._worker = threading.Thread(target=self._run, name="episode-audio-research", daemon=True)
            self._worker.start()
        return True

    def submit(self, job, decision, text):
        """Fast, non-blocking handoff from the language worker.

        Only bounded clues survive this call. The transcript is used to extract
        clues and is then dropped; no PCM, transcript or page body is queued.
        """
        if not self.enabled:
            self._diagnose("disabled", "disabled")
            return None
        try:
            clues = extract_clues(text)
            if len(clues) < MIN_DISTINCTIVE_CLUES:
                self._diagnose("insufficient-clues", "early-return")
                return None
            view = _sanitized_job(job, time.time())
            decision_view = _sanitized_decision(decision)
            if view is None or decision_view is None:
                self._diagnose("invalid-handoff", "early-return")
                return None
            if view.get("deviceVerified") is not True or view.get("changed"):
                self._diagnose("device-unverified", "stale")
                return None
            if decision_view.get("outcome") != "allowed" or decision_view.get("language") not in ALLOWED_LANGUAGES:
                self._diagnose("language-gated", "language")
                return None
            key = _digest(str(view.get("cid")) + "|" + str(view.get("visitID")) + "|" + "|".join(clues))
            now = time.time()
            self._prune_pending(now)
            if key in self._pending:
                self._diagnose("pending-duplicate", "duplicate")
                return None
            self._ensure_worker()
            item = PendingClue(view, decision_view, tuple(clues), now, key)
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self.dropped += 1
                self._diagnose("queue-full", "early-return")
                return None
            self._pending[key] = now
            self.submitted += 1
            self._diagnose("queued", "queued")
            return None
        except Exception as error:
            self._diagnose("handoff-error", "error", type(error).__name__)
            self.log("episode handoff error " + type(error).__name__)
            return None

    def _prune_pending(self, now):
        for key, at in list(self._pending.items()):
            if now - at > PENDING_DEDUP_SECONDS:
                self._pending.pop(key, None)
        while len(self._pending) > MAX_PENDING * 4:
            self._pending.popitem(last=False)

    def _run(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._process_pending(item)
            except Exception as error:
                self.log("episode worker error " + type(error).__name__)
            finally:
                if isinstance(item, PendingClue):
                    self._pending.pop(item.key, None)
                self._queue.task_done()

    def _process_pending(self, item):
        if not self.enabled:
            self._diagnose("disabled", "disabled")
            return None
        now = time.time()
        snapshot = self.probe() if callable(self.probe) else None
        binding = make_binding(item.job, snapshot, now, self.ttl)
        if binding is None or not binding.visit_id:
            self._diagnose("session-unverified", "stale")
            return None
        context = self._context(binding.channel_id)
        if context is not None:
            if context.get("channelID") and context.get("channelID") != binding.channel_id:
                self._diagnose("programme-changed", "stale")
                return None
            if self._has_resolved_identity(context, binding.channel_id):
                self._diagnose("identity-present", "identity-present")
                return None
        request = build_request_from_clues(item.clues, item.job, item.decision, binding, now)
        if request is None:
            self._diagnose("request-rejected", "early-return")
            return None
        return self._evaluate(request, snapshot, context, item.job, now,
                              retry=bool(item.job.get("episodeOnly")))

    def prefetch(self, context):
        """Warm public source/evidence caches without a transcript or callback.

        This is intentionally separate from the language worker. It never
        extracts clues and never publishes anything.
        """
        if not self.enabled:
            return False
        prefetch = getattr(self.research, "prefetch", None)
        if not callable(prefetch) or not isinstance(context, dict):
            return False
        try:
            return bool(prefetch(context))
        except Exception as error:
            self.log("episode prefetch error " + type(error).__name__)
            return False

    def flush(self, timeout=5.0):
        """Test/diagnostic barrier for the bounded worker."""
        if self._queue is None:
            return True
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def diagnostics(self):
        with self._diag_lock:
            diagnostics = {key: (dict(value) if isinstance(value, dict) else value)
                           for key, value in self._diagnostics.items()}
        diagnostics.update({
            "submitted": self.submitted, "dropped": self.dropped,
            "pending": len(self._pending) if self._queue is not None else 0,
            "retryWindows": len(self._retries),
            "maxRetriesPerVisit": self.max_retries,
            "retrySeconds": self.retry_seconds,
            "publications": self.publisher.diagnostics() if self.publisher is not None else None})
        return diagnostics


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
            "reason": "Handoff is wired to the single-pass language callback. Module default stays off; the gateway may enable it at runtime. Research runs on a bounded observer worker and never blocks language counting.",
            "maxCandidates": MAX_CANDIDATES, "maxSampleBytes": MAX_SAMPLE_BYTES,
            "handoff": {"contract": "language_audio.Manager(on_speech=...)",
                        "defaultOff": True,
                        "asyncSubmit": True,
                        "postResearchReprobe": True,
                        "publication": True,
                        "publicationTtlSeconds": PUBLICATION_TTL,
                        "allowedProfile": "american",
                        "allowedLanguages": sorted(ALLOWED_LANGUAGES),
                        "requires": ["channelID", "launchID", "streamGeneration",
                                     "visitID", "observedAt", "expiresAt", "deviceVerified"],
                        "matches": ["channel", "programme", "visit"],
                        "maxClues": MAX_CLUES,
                        "maxSessionTtlSeconds": MAX_SESSION_TTL,
                        "maxObservationAgeSeconds": MAX_OBSERVATION_AGE,
                        "activeViewingSeconds": ACTIVE_VIEWING_SECONDS,
                        "sampleBackedActiveSeconds": AUDIO_MIN_ACTIVE_SECONDS,
                        "minEvidenceSampleSeconds": MIN_EVIDENCE_SAMPLE_SECONDS,
                        "maxAttempts": MAX_ATTEMPTS,
                        "cooldownSeconds": COOLDOWN_SECONDS,
                        "dedupSeconds": DEDUP_SECONDS,
                        "dailyBudget": MAX_DAILY_EPISODE_CALLS,
                        "officialArchiveHosts": sorted(OFFICIAL_ARCHIVE_HOSTS),
                        "independentReportHosts": sorted(INDEPENDENT_REPORT_HOSTS),
                        "researchAttendance": "automatic-allowlisted-discovery-with-registry-override",
                        "registryOverride": True,
                        "automaticDiscovery": True,
                        "automaticOfficialHints": sorted(DEFAULT_OFFICIAL_HINTS),
                        "candidateCacheTtlSeconds": SOURCE_CANDIDATE_TTL,
                        "maxCandidateCacheEntries": MAX_SOURCE_CACHE,
                        "maxSourcePages": MAX_SOURCE_PAGES,
                        "maxResearchSeconds": MAX_RESEARCH_SECONDS,
                        "unknownResearchSeconds": UNKNOWN_RESEARCH_SECONDS,
                        "unknownProviderSeconds": UNKNOWN_PROVIDER_SECONDS,
                        "unknownLeadConfirmedSeconds": UNKNOWN_LEAD_PROGRESS_SECONDS,
                        "unknownPublishConfirmedSeconds": UNKNOWN_PUBLISH_PROGRESS_SECONDS,
                        "searchEnabled": False}}
