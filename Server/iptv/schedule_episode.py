"""Bounded FAST schedule and canonical-episode identity.

This module is deliberately separate from audio language detection and from
playback/rotation policy.  It answers one question only: for an exact provider
feed, what episode (if any) is *currently* scheduled, and what is that
episode's canonical original-air identity?

Evidence precedence is fixed:

1. a source-owned, structured schedule for the exact station/feed;
2. two independent public schedules that agree on the active interval;
3. a unique canonical catalog episode matched by at least two distinctive
   audio clues when the schedule publishes the series but not the episode.

A reviewed channel-to-series map is only a lookup.  It is never a static
current-episode schedule.  A show-name-only channel with no guide and no unique
audio clue therefore fails closed instead of inventing a programme.

The adapters never persist page bodies, transcripts, credentials or private
URLs.  Callers own fetching and must use the existing bounded source policy.
"""
from __future__ import annotations

import datetime as dt
import dataclasses
import html
import json
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import episode_audio

REVISION = "schedule-episode-20261127"
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_EPISODES = 5000
MAX_EVENTS = 64
MAX_TEXT = 600
MAX_CLUES = episode_audio.MAX_CLUES
MIN_CLUE_MATCHES = 2
MAX_SCHEDULE_AGE = 120.0
MAX_CATALOG_AGE = 1800.0
INTERVAL_TOLERANCE = 60.0

# Canonical/public catalog adapters.  Each host is explicit; a model-provided
# URL is never treated as a catalog merely because it looks plausible.
CATALOG_ADAPTERS = {
    "api.tvmaze.com": "tvmaze-api",
    "www.tvmaze.com": "tvmaze-html",
    "thetvdb.com": "tvdb-html",
    "www.thetvdb.com": "tvdb-html",
    "www.aetv.com": "aetv-html",
    "tv.apple.com": "apple-episode",
}
# Broadcaster-owned catalogs can stand alone; independent databases need one
# agreeing peer.  This is catalog provenance, not current-schedule evidence.
OFFICIAL_CATALOG_ADAPTERS = frozenset({"aetv-html", "apple-episode"})
CATALOG_PRIORITY = {"apple-episode": 5, "aetv-html": 4, "tvmaze-api": 3,
                    "tvmaze-html": 2, "tvdb-html": 1}

DATE_PATTERNS = (
    "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y",
    "%m/%d/%Y", "%m/%d/%y",
)


class EpisodeEvidenceError(ValueError):
    """Stable, non-sensitive rejection reason."""


@dataclass(frozen=True)
class ChannelSeriesMapping:
    """A reviewed lookup, never a current programme schedule."""

    provider_id: str
    channel_name: str
    region: str
    series_title: str
    catalog_ids: Mapping[str, str] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()

    def checked(self) -> bool:
        return (isinstance(self.provider_id, str)
                and re.fullmatch(r"nltv:[A-Za-z0-9_-]+:[A-Za-z0-9_.-]{1,160}", self.provider_id) is not None
                and isinstance(self.channel_name, str) and 0 < len(self.channel_name) <= 160
                and isinstance(self.region, str) and re.fullmatch(r"[a-z]{2}", self.region) is not None
                and isinstance(self.series_title, str) and 0 < len(self.series_title) <= 200
                and isinstance(self.catalog_ids, Mapping)
                and all(isinstance(k, str) and isinstance(v, str) and 0 < len(v) <= 160
                        for k, v in self.catalog_ids.items()))

    def channel_matches(self, channel_name: str) -> bool:
        wanted = {_norm(self.channel_name), *(_norm(v) for v in self.aliases)}
        return _norm(channel_name) in wanted


# Reviewed on 2026-11-27.  The provider ID is the identity.  The same channel
# name on another distributor is intentionally not accepted.
REVIEWED_MAPPINGS = {
    "nltv:us:TinyHouseNation.us": ChannelSeriesMapping(
        provider_id="nltv:us:TinyHouseNation.us",
        channel_name="Tiny House Nation",
        region="us",
        series_title="Tiny House Nation",
        catalog_ids={"tvmaze": "1418", "tvdb": "tiny-house-nation",
                     "aetv": "tiny-house-nation", "apple": "tiny-house-nation"},
        aliases=("TinyHouseNation",),
    ),
}


@dataclass(frozen=True)
class FeedIdentity:
    provider_id: str
    channel_name: str
    region: str
    station: str = ""
    guide_source: str = ""
    zone: str = ""
    first_party: bool = False

    def checked(self) -> bool:
        return (isinstance(self.provider_id, str)
                and re.fullmatch(r"nltv:[A-Za-z0-9_-]+:[A-Za-z0-9_.-]{1,160}", self.provider_id) is not None
                and isinstance(self.channel_name, str) and 0 < len(self.channel_name) <= 160
                and isinstance(self.region, str) and re.fullmatch(r"[a-z]{2}", self.region) is not None)


@dataclass(frozen=True)
class CatalogEpisode:
    series_title: str
    season_number: int | None
    episode_number: int | None
    episode_title: str
    original_air_date: str
    source_url: str
    adapter: str
    kind: str = "regular"
    summary: str = ""
    season_premiere_year: int | None = None
    series_premiere_year: int | None = None

    def checked(self) -> bool:
        if not isinstance(self.series_title, str) or not 0 < len(self.series_title) <= 200:
            return False
        if not isinstance(self.episode_title, str) or not 0 < len(self.episode_title) <= 300:
            return False
        if not isinstance(self.original_air_date, str) or not _date(self.original_air_date):
            return False
        if self.season_number is not None and (type(self.season_number) is not int or not 0 <= self.season_number <= 99):
            return False
        if self.episode_number is not None and (type(self.episode_number) is not int or not 0 < self.episode_number <= 9999):
            return False
        if (self.season_number is None) != (self.episode_number is None):
            return False
        if self.adapter not in CATALOG_ADAPTERS.values():
            return False
        if self.kind not in ("regular", "special", "advertisement"):
            return False
        host = _host(self.source_url)
        if host not in CATALOG_ADAPTERS or CATALOG_ADAPTERS[host] != self.adapter:
            return False
        return True

    def episode_year(self) -> int | None:
        value = _date(self.original_air_date)
        return int(value[:4]) if value else None

    def code(self) -> str:
        if self.season_number is None or self.episode_number is None:
            return ""
        return "S%02dE%02d" % (self.season_number, self.episode_number)

    def text(self) -> str:
        return " ".join((self.series_title, self.episode_title, self.summary))


@dataclass(frozen=True)
class CanonicalIdentity:
    series_title: str
    season_number: int | None
    episode_number: int | None
    episode_title: str
    original_air_date: str
    episode_year: int | None
    season_premiere_year: int | None
    series_premiere_year: int | None
    verification: str
    sources: tuple[str, ...]
    kind: str = "regular"

    def evidence(self) -> dict:
        return {"seriesTitle": self.series_title,
                "seasonNumber": self.season_number,
                "episodeNumber": self.episode_number,
                "episodeTitle": self.episode_title,
                "originalAirDate": self.original_air_date,
                "episodeYear": self.episode_year,
                "seasonPremiereYear": self.season_premiere_year,
                "seriesPremiereYear": self.series_premiere_year,
                "episodeKind": self.kind,
                "verification": self.verification,
                "catalogSources": list(self.sources)}


@dataclass(frozen=True)
class Resolution:
    accepted: bool
    reason: str
    identity: CanonicalIdentity | None = None
    row: dict | None = None
    verification: str = ""
    sources: tuple[str, ...] = ()
    diagnostics: dict = field(default_factory=dict)


def _norm(value: Any) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", str(value or "")).casefold()
                   if c.isalnum())


def _clean(value: Any, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    value = " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value)).split())
    if not value or len(value) > limit or re.search(r"(?i)https?://|api_key=|token=|[<>]", value):
        return ""
    return value


def _host(value: str) -> str:
    try:
        return (urllib.parse.urlsplit(value).hostname or "").casefold()
    except (TypeError, ValueError):
        return ""


def _date(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.replace(",", ", ").split())
    for pattern in DATE_PATTERNS:
        try:
            return dt.datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
    if match:
        try:
            return dt.date(int(match[1]), int(match[2]), int(match[3])).isoformat()
        except ValueError:
            return ""
    return ""


def _year(value: Any) -> int | None:
    stamp = _date(value)
    return int(stamp[:4]) if stamp else None


def _event_value(event: Any, *names: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        for name in names:
            if name in event:
                return event[name]
        return default
    for name in names:
        if hasattr(event, name):
            return getattr(event, name)
    return default


def _catalog_adapter(url: str) -> str:
    host = _host(url)
    return CATALOG_ADAPTERS.get(host, "")


def catalog_url(url: str) -> bool:
    """Validate an HTTPS catalog URL on the explicit adapter allowlist."""
    if not isinstance(url, str) or len(url) > 500:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
    except (TypeError, ValueError):
        return False
    return bool(parsed.scheme == "https" and parsed.hostname in CATALOG_ADAPTERS
                and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and not parsed.fragment)


def _catalog_episode(series_title, season, number, episode_title, air_date,
                     source_url, adapter, kind="regular", summary="",
                     season_premiere_year=None, series_premiere_year=None):
    episode = CatalogEpisode(
        series_title=_clean(series_title, 200), season_number=season,
        episode_number=number, episode_title=_clean(episode_title, 300),
        original_air_date=_date(air_date) or (air_date if isinstance(air_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", air_date) else ""),
        source_url=source_url, adapter=adapter, kind=kind,
        summary=_clean(summary, MAX_TEXT), season_premiere_year=season_premiere_year,
        series_premiere_year=series_premiere_year)
    return episode if episode.checked() else None


def _decoded_page(page: Any) -> str:
    if isinstance(page, (bytes, bytearray)):
        if len(page) > MAX_PAGE_BYTES:
            raise EpisodeEvidenceError("page-size")
        return bytes(page).decode("utf-8", "replace")
    if not isinstance(page, str) or len(page.encode("utf-8")) > MAX_PAGE_BYTES:
        raise EpisodeEvidenceError("page-size")
    return page


def parse_tvmaze_episodes(payload: Any, series_title: str = "", source_url: str = "https://api.tvmaze.com/shows/1418/episodes") -> list[CatalogEpisode]:
    page = _decoded_page(payload)
    try:
        rows = json.loads(page)
    except (ValueError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows[:MAX_EPISODES]:
        if not isinstance(row, Mapping):
            continue
        season = row.get("season")
        number = row.get("number")
        if type(season) is not int or type(number) is not int:
            continue
        kind = "special" if season == 0 else "regular"
        episode = _catalog_episode(
            series_title or row.get("_links", {}).get("show", {}).get("name", ""),
            season, number, row.get("name") or "", row.get("airdate") or "",
            source_url, "tvmaze-api", kind=kind, summary=row.get("summary") or "")
        if episode:
            out.append(episode)
    return out


def parse_tvmaze_episodeguide_html(page: Any, series_title: str = "", source_url: str = "https://www.tvmaze.com/seasons/5604/tiny-house-nation-season-1/episodeguide") -> list[CatalogEpisode]:
    text = _decoded_page(page)
    adapter = "tvmaze-html"
    out = []
    pattern = re.compile(r"Episode:\s*(\d{1,2})x(\d{1,4})\s*\|\s*Airdate:\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})", re.I)
    for match in pattern.finditer(text):
        before = text[max(0, match.start() - 600):match.start()]
        candidates = re.findall(r"<a\b[^>]*>(.*?)</a>", before, re.I | re.S)
        title = _clean(candidates[-1] if candidates else "", 300)
        if not title:
            lines = [_clean(line, 300) for line in re.sub(r"<[^>]*>", "\n", before).splitlines()]
            lines = [line for line in lines if line]
            title = lines[-1] if lines else ""
        if not title:
            continue
        season, number = int(match[1]), int(match[2])
        episode = _catalog_episode(series_title, season, number, title, match[3],
                                   source_url, adapter)
        if episode:
            out.append(episode)
    return out


def parse_tvdb_allseasons_html(page: Any, series_title: str = "", source_url: str = "https://thetvdb.com/series/tiny-house-nation/allseasons/official") -> list[CatalogEpisode]:
    text = _decoded_page(page)
    visible = _clean_lines(text)
    joined = "\n".join(visible)
    out = []
    pattern = re.compile(
        r"(?:S(\d{1,2})E(\d{1,4})|SPECIAL\s+(\d{1,2})x(\d{1,4}))\s*\n"
        r"([^\n]{1,300})\n"
        r"((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4})", re.I)
    for match in pattern.finditer(joined):
        if match.group(1) is not None:
            season, number = int(match[1]), int(match[2])
            kind = "special" if season == 0 else "regular"
        else:
            season, number = int(match[3]), int(match[4])
            kind = "special"
        episode = _catalog_episode(series_title, season, number, match[5], match[6],
                                   source_url, "tvdb-html", kind=kind)
        if episode:
            out.append(episode)
    return out


def parse_apple_episode_html(page: Any, series_title: str = "", source_url: str = "") -> list[CatalogEpisode]:
    text = _decoded_page(page)
    adapter = "apple-episode"
    title_match = re.search(r"<title>\s*(.*?)\s*-\s*(.*?)\s*\(Season\s+(\d{1,2}),\s*Episode\s+(\d{1,4})\)", text, re.I | re.S)
    if not title_match:
        return []
    episode_title = _clean(title_match.group(1), 300)
    series = _clean(title_match.group(2), 200) or series_title
    season, number = int(title_match[3]), int(title_match[4])
    visible = " ".join(_clean_lines(text))
    months = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
    date_match = re.search(r"\b(" + months + r"\s+\d{1,2},\s+\d{4})\b", visible, re.I)
    if not date_match:
        return []
    episode = _catalog_episode(series, season, number, episode_title, date_match[1],
                               source_url, adapter)
    return [episode] if episode else []


def parse_aetv_season_html(page: Any, series_title: str = "", source_url: str = "") -> list[CatalogEpisode]:
    text = _decoded_page(page)
    adapter = "aetv-html"
    out = []
    for obj in _walk_json_objects(text):
        types = obj.get("@type")
        types = types if isinstance(types, list) else [types]
        if "TVEpisode" not in types:
            continue
        season_obj = obj.get("partOfSeason") if isinstance(obj.get("partOfSeason"), Mapping) else {}
        season = season_obj.get("seasonNumber", obj.get("seasonNumber"))
        number = obj.get("episodeNumber")
        try:
            season = int(season) if season is not None else None
            number = int(number) if number is not None else None
        except (TypeError, ValueError):
            continue
        if season is None or number is None:
            continue
        air = obj.get("datePublished") or obj.get("releaseDate") or ""
        episode = _catalog_episode(series_title, season, number,
                                   obj.get("name") or "", air, source_url, adapter,
                                   summary=obj.get("description") or "")
        if episode:
            out.append(episode)
    return out


def parse_catalog_page(page: Any, source_url: str, series_title: str = "") -> list[CatalogEpisode]:
    adapter = _catalog_adapter(source_url)
    if not adapter or not catalog_url(source_url):
        return []
    if adapter == "tvmaze-api":
        return parse_tvmaze_episodes(page, series_title, source_url)
    if adapter == "tvmaze-html":
        return parse_tvmaze_episodeguide_html(page, series_title, source_url)
    if adapter == "tvdb-html":
        return parse_tvdb_allseasons_html(page, series_title, source_url)
    if adapter == "apple-episode":
        return parse_apple_episode_html(page, series_title, source_url)
    if adapter == "aetv-html":
        return parse_aetv_season_html(page, series_title, source_url)
    return []


def _clean_lines(page: str) -> list[str]:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "\n", page)
    text = re.sub(r"<[^>]*>", "\n", text)
    return [line for line in (html.unescape(re.sub(r"\s+", " ", chunk)).strip() for chunk in text.splitlines())
            if line]


def _walk_json_objects(page: str) -> list[dict]:
    out = []
    for match in re.finditer(r"(?is)<script\b[^>]*>(.*?)</script>", page):
        body = match[1]
        if len(body) > MAX_PAGE_BYTES:
            continue
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack and len(out) < 512:
            item = stack.pop()
            if isinstance(item, Mapping):
                out.append(dict(item))
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return out


def _season_and_series_premiere_years(episodes: Sequence[CatalogEpisode], season: int | None) -> tuple[int | None, int | None]:
    dates = [_date(e.original_air_date) for e in episodes if _date(e.original_air_date)]
    season_dates = [_date(e.original_air_date) for e in episodes
                    if season is not None and e.season_number == season and _date(e.original_air_date)]
    series_year = int(min(dates)[:4]) if dates else None
    season_year = int(min(season_dates)[:4]) if season_dates else None
    return season_year, series_year


def _matching_episodes(episodes: Sequence[CatalogEpisode], *, air_date: str = "",
                       episode_title: str = "", season: int | None = None,
                       number: int | None = None, clues: Sequence[str] = ()) -> list[CatalogEpisode]:
    rows = []
    wanted_date = _date(air_date)
    wanted_title = _norm(episode_title)
    clue_set = episode_audio.normalize_clues(clues) if clues else []
    for episode in episodes:
        if not episode.checked() or episode.kind == "advertisement":
            continue
        if wanted_date and _date(episode.original_air_date) != wanted_date:
            continue
        if wanted_title and _norm(episode.episode_title) != wanted_title:
            continue
        if season is not None and episode.season_number is not None and episode.season_number != season:
            continue
        if number is not None and episode.episode_number is not None and episode.episode_number != number:
            continue
        if clue_set:
            matched = episode_audio.clue_matches(clue_set, episode.text())
            if len(matched) < MIN_CLUE_MATCHES:
                continue
        rows.append(episode)
    return rows


def resolve_catalog(catalogs: Sequence[CatalogEpisode], *, series_title: str = "",
                    air_date: str = "", episode_title: str = "",
                    season: int | None = None, number: int | None = None,
                    clues: Sequence[str] = ()) -> tuple[CanonicalIdentity | None, str]:
    """Return one canonical episode only when independent catalogs agree."""
    rows = [episode for episode in catalogs if isinstance(episode, CatalogEpisode) and episode.checked()]
    if not rows:
        return None, "catalog-unavailable"
    if series_title:
        wanted = _norm(series_title)
        rows = [episode for episode in rows if _norm(episode.series_title) == wanted]
    matches = _matching_episodes(rows, air_date=air_date, episode_title=episode_title,
                                 season=season, number=number, clues=clues)
    if not matches:
        return None, "catalog-no-match"
    # Group by content (title + original date), never by numbering.  A catalog
    # that renumbers a special must still agree on what the episode actually is.
    # Once a clue/date match identifies the content, include the same content
    # from every catalog even when that catalog has no synopsis text (for
    # example a terse episode guide).  Missing prose is not disagreement.
    keys = {(_norm(episode.episode_title), _date(episode.original_air_date)) for episode in matches}
    groups: dict[tuple[str, str], list[CatalogEpisode]] = {}
    for episode in rows:
        key = (_norm(episode.episode_title), _date(episode.original_air_date))
        if key in keys and key[0] and key[1]:
            groups.setdefault(key, []).append(episode)
    if not groups:
        return None, "catalog-incomplete"
    ranked = sorted(groups.items(), key=lambda item: (-len({e.adapter for e in item[1]}),
                                                      min(-CATALOG_PRIORITY.get(e.adapter, 0) for e in item[1])))
    key, group = ranked[0]
    if len(ranked) > 1 and len({e.adapter for e in ranked[1][1]}) == len({e.adapter for e in group}):
        return None, "ambiguous-catalog-episode"
    adapters = sorted({e.adapter for e in group}, key=lambda a: CATALOG_PRIORITY.get(a, 0), reverse=True)
    if len(adapters) < 2 and not (adapters and adapters[0] in OFFICIAL_CATALOG_ADAPTERS):
        return None, "catalog-not-corroborated"
    # Broadcaster-owned numbering wins over database numbering.  If only
    # databases agree, they must agree with each other.
    numbered = [e for e in group if e.season_number is not None and e.episode_number is not None]
    official = [e for e in numbered if e.adapter in OFFICIAL_CATALOG_ADAPTERS]
    chosen = None
    if official:
        chosen = sorted(official, key=lambda e: CATALOG_PRIORITY.get(e.adapter, 0), reverse=True)[0]
    elif numbered and len({(e.season_number, e.episode_number) for e in numbered}) == 1:
        chosen = sorted(numbered, key=lambda e: CATALOG_PRIORITY.get(e.adapter, 0), reverse=True)[0]
    elif numbered:
        return None, "numbering-conflict"
    else:
        chosen = group[0]
    season_year, series_year = _season_and_series_premiere_years(rows, chosen.season_number)
    display_title = chosen.episode_title
    if episode_title and _norm(episode_title) == _norm(chosen.episode_title):
        display_title = _clean(episode_title, 300) or display_title
    else:
        # Keep the canonical database punctuation when the official page uses
        # a shortened form (for example "500 Sq Ft" vs "500 Sq. Ft.").
        for adapter in ("tvmaze-api", "tvmaze-html", "tvdb-html"):
            match = next((e for e in group if e.adapter == adapter
                          and _norm(e.episode_title) == _norm(chosen.episode_title)), None)
            if match:
                display_title = match.episode_title
                break
    identity = CanonicalIdentity(
        series_title=chosen.series_title,
        season_number=chosen.season_number,
        episode_number=chosen.episode_number,
        episode_title=display_title,
        original_air_date=_date(chosen.original_air_date),
        episode_year=chosen.episode_year(),
        season_premiere_year=season_year,
        series_premiere_year=series_year,
        verification="canonical-catalog",
        sources=tuple(f"{e.adapter}:{_host(e.source_url)}" for e in group),
        kind=chosen.kind)
    return identity, ""


def reviewed_mapping(provider_id: str, channel_name: str, region: str) -> ChannelSeriesMapping | None:
    mapping = REVIEWED_MAPPINGS.get(provider_id)
    if mapping is None or not mapping.checked():
        return None
    if mapping.region != region or not mapping.channel_matches(channel_name):
        return None
    return mapping


def feed_identity(provider_id: str, channel_name: str, mapping: ChannelSeriesMapping, *,
                  station: str = "", guide_source: str = "", zone: str = "",
                  first_party: bool = False) -> FeedIdentity | None:
    if mapping is None or not mapping.checked():
        return None
    if provider_id != mapping.provider_id or not mapping.channel_matches(channel_name):
        return None
    region = provider_id.split(":")[1] if provider_id.count(":") == 2 else ""
    if region != mapping.region:
        return None
    candidate = FeedIdentity(provider_id, channel_name, mapping.region,
                             station=station or channel_name, guide_source=guide_source,
                             zone=zone, first_party=first_party)
    return candidate if candidate.checked() else None


def _event_candidate(event: Any) -> dict | None:
    title = _clean(_event_value(event, "title", "name"), 300)
    start = _event_value(event, "start")
    end = _event_value(event, "end")
    try:
        start = float(start)
        end = float(end)
    except (TypeError, ValueError):
        return None
    if not (0 < end - start <= 8 * 3600):
        return None
    return {
        "title": title,
        "start": start, "end": end,
        "station": _clean(_event_value(event, "station", "channel", "name"), 160),
        "region": str(_event_value(event, "region", default="") or ""),
        "zone": str(_event_value(event, "zone", "timezone", default="") or ""),
        "source": str(_event_value(event, "source", default="") or ""),
        "first_party": bool(_event_value(event, "first_party", default=False)),
        "structured": bool(_event_value(event, "structured", default=False)),
        "episode_title": _clean(_event_value(event, "episode_title", "episodeTitle"), 300),
        "season_number": _event_value(event, "season_number", "seasonNumber"),
        "episode_number": _event_value(event, "episode_number", "episodeNumber"),
        "air_date": _date(_event_value(event, "original_air_date", "originalAirDate", "air_date", "season_year")),
        "description": _clean(_event_value(event, "description", "summary"), MAX_TEXT),
    }


def _valid_number(value: Any, limit: int) -> int | None:
    return value if type(value) is int and 0 < value <= limit else None


def _active_unique(events: Sequence[Any], now: float, mapping: ChannelSeriesMapping) -> tuple[dict | None, str, list[dict]]:
    candidates = []
    diagnostics = []
    series_norm = _norm(mapping.series_title)
    for event in events[:MAX_EVENTS]:
        candidate = _event_candidate(event)
        if not candidate:
            diagnostics.append({"reason": "invalid-event"})
            continue
        if not candidate["zone"]:
            diagnostics.append({"reason": "missing-timezone", "source": candidate["source"]})
            continue
        if candidate["region"] and candidate["region"].casefold() != mapping.region:
            diagnostics.append({"reason": "region-mismatch", "source": candidate["source"]})
            continue
        if candidate["station"] and _norm(candidate["station"]) not in {_norm(mapping.channel_name), _norm(mapping.series_title)}:
            diagnostics.append({"reason": "station-mismatch", "source": candidate["source"]})
            continue
        active = candidate["start"] <= now < candidate["end"]
        identical = _norm(candidate["title"]) == series_norm or not candidate["title"]
        if not active:
            continue
        if not identical:
            diagnostics.append({"reason": "series-mismatch", "source": candidate["source"]})
            continue
        candidates.append(candidate)
    if not candidates:
        return None, "no-current-schedule", diagnostics
    if len(candidates) > 1:
        # Multiple independent guides are expected; they corroborate only when
        # every active row names the same series and the same interval.
        first = candidates[0]
        for other in candidates[1:]:
            if (_norm(other["title"]) != _norm(first["title"])
                    or abs(other["start"] - first["start"]) > INTERVAL_TOLERANCE
                    or abs(other["end"] - first["end"]) > INTERVAL_TOLERANCE):
                return None, "ambiguous-current-schedule", diagnostics
    return candidates[0], "", diagnostics


def _corroborated(events: Sequence[dict]) -> bool:
    by_site: dict[str, dict] = {}
    for event in events:
        if not event.get("zone") or event.get("first_party"):
            continue
        by_site.setdefault(event.get("source") or "", event)
    if len(by_site) < 2:
        return False
    rows = list(by_site.values())
    first = rows[0]
    for other in rows[1:]:
        if _norm(other["title"]) != _norm(first["title"]):
            return False
        if abs(other["start"] - first["start"]) > INTERVAL_TOLERANCE:
            return False
        if abs(other["end"] - first["end"]) > INTERVAL_TOLERANCE:
            return False
    return True


def _row_from_identity(identity: CanonicalIdentity, event: dict, observed_at: float) -> dict:
    row = {
        "title": event["title"] or identity.series_title,
        "source": "corroborated-external-schedule",
        "observedAt": float(observed_at),
        "start": float(event["start"]),
        "end": float(event["end"]),
        "station": event.get("station") or "",
        "region": event.get("region") or "",
        "episodeTitle": identity.episode_title,
        "episodeSource": "catalog-schedule",
        "episodeObservedAt": float(observed_at),
        "episodeOriginalAirDate": identity.original_air_date,
        "seasonYear": identity.episode_year,
        "scheduleVerification": identity.verification,
        "scheduleEpisodeSources": list(identity.sources),
    }
    if identity.season_number is not None and identity.episode_number is not None:
        row["seasonNumber"] = identity.season_number
        row["episodeNumber"] = identity.episode_number
    if identity.kind != "regular":
        row["episodeKind"] = identity.kind
    if identity.season_premiere_year is not None:
        row["seasonPremiereYear"] = identity.season_premiere_year
    if identity.series_premiere_year is not None:
        row["seriesPremiereYear"] = identity.series_premiere_year
    return row


def resolve_current(now: float, provider_id: str, channel_name: str, *,
                   schedule_events: Sequence[Any] = (), catalogs: Sequence[CatalogEpisode] = (),
                   audio_clues: Sequence[str] = (), observed_at: float | None = None,
                   feed: FeedIdentity | None = None) -> Resolution:
    """Resolve the current episode for one exact FAST provider feed.

    ``schedule_events`` are already parsed, bounded schedule events (for example
    ``schedule_evidence.Event``).  ``catalogs`` are already fetched canonical
    catalog rows.  This function performs no network or audio work.
    """
    now = float(now)
    observed = now if observed_at is None else float(observed_at)
    mapping = REVIEWED_MAPPINGS.get(provider_id)
    if mapping is None or not mapping.checked():
        return Resolution(False, "unreviewed-provider")
    if not mapping.channel_matches(channel_name):
        return Resolution(False, "channel-name-mismatch")
    provider_region = provider_id.split(":")[1] if provider_id.count(":") == 2 else ""
    if provider_region != mapping.region:
        return Resolution(False, "region-mismatch")
    if not 0 <= observed - now <= MAX_SCHEDULE_AGE and not 0 <= now - observed <= MAX_SCHEDULE_AGE:
        return Resolution(False, "stale-schedule")
    if feed is not None and (not feed.checked() or feed.provider_id != provider_id
                             or feed.region != mapping.region
                             or not mapping.channel_matches(feed.channel_name)
                             or not feed.zone):
        return Resolution(False, "feed-identity-mismatch")
    event, reason, diagnostics = _active_unique(schedule_events, now, mapping)
    if event is None:
        return Resolution(False, reason, diagnostics={"rejected": diagnostics})
    if event["first_party"] and not event["structured"]:
        return Resolution(False, "first-party-unstructured", diagnostics={"rejected": diagnostics})
    active_rows = []
    for item in schedule_events[:MAX_EVENTS]:
        candidate = _event_candidate(item)
        if candidate and candidate["start"] <= now < candidate["end"]:
            active_rows.append(candidate)
    if not (event["first_party"] or _corroborated(active_rows)):
        # A single ordinary HTML schedule is a lead, not corroboration.
        return Resolution(False, "schedule-not-corroborated", diagnostics={"rejected": diagnostics})
    if feed is not None and feed.zone and event["zone"] and feed.zone != event["zone"]:
        return Resolution(False, "timezone-conflict", diagnostics={"rejected": diagnostics})
    # One guide may publish the episode subtitle while its corroborating peer
    # publishes only the series. Merge non-empty clues, but never choose
    # between conflicting episode titles/dates/numbers.
    for row in active_rows:
        for key in ("episode_title", "air_date"):
            current_value = event.get(key) or ""
            other = row.get(key) or ""
            if other and current_value and _norm(other) != _norm(current_value):
                return Resolution(False, "episode-clue-conflict", diagnostics={"rejected": diagnostics})
            if other and not current_value:
                event[key] = other
        for key, limit in (("season_number", 99), ("episode_number", 9999)):
            current_value = _valid_number(event.get(key), limit)
            other = _valid_number(row.get(key), limit)
            if other is not None and current_value is not None and other != current_value:
                return Resolution(False, "episode-clue-conflict", diagnostics={"rejected": diagnostics})
            if other is not None and current_value is None:
                event[key] = other
    season = _valid_number(event.get("season_number"), 99)
    number = _valid_number(event.get("episode_number"), 9999)
    if (season is None) != (number is None):
        season = number = None
    identity = None
    catalog_reason = "episode-unidentified"
    has_episode_clue = bool(event.get("air_date") or event.get("episode_title")
                            or season is not None or number is not None)
    if has_episode_clue:
        identity, catalog_reason = resolve_catalog(
            catalogs, series_title=mapping.series_title,
            air_date=event.get("air_date") or "",
            episode_title=event.get("episode_title") or "",
            season=season, number=number)
    verification = ""
    if identity is not None:
        verification = ("source-owned-schedule" if event["first_party"] and event["structured"]
                        else "corroborated-schedule")
    elif audio_clues:
        identity, catalog_reason = resolve_catalog(
            catalogs, series_title=mapping.series_title, clues=audio_clues)
        if identity is not None:
            verification = "audio-clue-catalog"
    if identity is None:
        # The schedule is current and trustworthy but it does not identify an
        # episode.  Never turn a catalog/mapping entry into a static airing.
        return Resolution(False, catalog_reason or "episode-unidentified",
                          diagnostics={"rejected": diagnostics})
    identity = dataclasses.replace(identity, verification=verification)
    row = _row_from_identity(identity, event, observed)
    return Resolution(True, "", identity=identity, row=row,
                      verification=verification, sources=identity.sources,
                      diagnostics={"rejected": diagnostics})


def reviewed_mapping_status() -> list[dict]:
    out = []
    for provider_id, mapping in sorted(REVIEWED_MAPPINGS.items()):
        out.append({"providerID": provider_id, "channelName": mapping.channel_name,
                    "region": mapping.region, "seriesTitle": mapping.series_title,
                    "catalogIDs": dict(mapping.catalog_ids),
                    "mappingOnly": True, "staticCurrentEpisode": False})
    return out


def status() -> dict:
    return {"revision": REVISION, "mappings": reviewed_mapping_status(),
            "catalogAdapters": sorted(set(CATALOG_ADAPTERS.values())),
            "catalogHosts": sorted(CATALOG_ADAPTERS),
            "maxPageBytes": MAX_PAGE_BYTES,
            "maxEpisodes": MAX_EPISODES, "maxScheduleAgeSeconds": MAX_SCHEDULE_AGE,
            "maxCatalogAgeSeconds": MAX_CATALOG_AGE,
            "minClueMatches": MIN_CLUE_MATCHES,
            "precedence": ["source-owned-schedule", "corroborated-schedule",
                           "audio-clue-catalog"]}
