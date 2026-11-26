"""Bounded, read-only schedule evidence extraction.

The model is only a lead generator. This module turns public schedule pages,
official structured EPG data, XMLTV and explicit schedule tables into a small
set of interval events with an exact station identity, a region and
timezone-aware boundaries. Anything ambiguous is rejected instead of guessed.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

REVISION = "schedule-evidence-20261127"
MAX_EVENTS = 500
MAX_TEXT = 300
MAX_JSON_SCRIPTS = 100
MAX_JSON_BYTES = 1024 * 1024
MAX_TABLE_ROWS = 500

# Approved public schedule hosts. Region is the feed region, not the owner's
# corporate domicile. Keep this list explicit; never accept a model-provided
# host merely because it looks like a guide.
SOURCE_REGIONS = {
    "tvpassport.com": "us", "www.tvpassport.com": "us",
    "tvguide.com": "us", "www.tvguide.com": "us",
    "ontvtonight.com": "us", "www.ontvtonight.com": "us",
    "titantv.com": "us", "www.titantv.com": "us",
    "zap2it.com": "us", "www.zap2it.com": "us",
    "tvguide.co.uk": "uk", "www.tvguide.co.uk": "uk",
    "tv24.co.uk": "uk", "www.tv24.co.uk": "uk",
    "radiotimes.com": "uk", "www.radiotimes.com": "uk",
    "tvmustra.hu": "hu", "www.tvmustra.hu": "hu",
    "filmkatalogus.hu": "hu", "www.filmkatalogus.hu": "hu",
    "musor.tv": "hu", "www.musor.tv": "hu",
    "pluto.tv": "us", "www.pluto.tv": "us",
}

# A first-party schedule may stand alone only when it is structured
# (JSON-LD/embedded JSON/XMLTV), names the exact station and carries explicit
# timezone-aware boundaries. HTML tables still require independent agreement.
FIRST_PARTY_REGIONS = {
    "pluto.tv": {"us"}, "www.pluto.tv": {"us"},
}

REGION_ALIASES = {
    "us": {"us", "usa", "unitedstates", "unitedstatesofamerica", "america"},
    "uk": {"uk", "gb", "unitedkingdom", "greatbritain", "england", "scotland", "wales"},
    "hu": {"hu", "hungary", "magyarorszag"},
    "sg": {"sg", "singapore"},
    "ca": {"ca", "canada"},
}

ZONE_ALIASES = {
    "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York",
    "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago",
    "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver",
    "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "at": "America/Halifax", "ast": "America/Halifax", "adt": "America/Halifax",
    "gmt": "Europe/London", "utc": "UTC", "bst": "Europe/London",
    "cet": "Europe/Budapest", "cest": "Europe/Budapest",
    "sgt": "Asia/Singapore",
}

EPISODE_TEXT = re.compile(r"(?i)(?:^|\s)(?:S(?:eason)?\s*(\d{1,2})\s*[.:,/-]?\s*E(?:p(?:isode)?)?\s*(\d{1,4})|(?:season)\s*(\d{1,2})\s*(?:episode|ep)\s*(\d{1,4})|(?:episode|ep)\s*(\d{1,4})\s*(?:of|/)\s*(?:season)\s*(\d{1,2}))")
ONSREEN = re.compile(r"(?i)^S(\d{1,2})E(\d{1,4})$")


class EvidenceError(ValueError):
    """Rejected schedule evidence with a stable, non-sensitive reason."""


@dataclass(frozen=True)
class Event:
    title: str
    start: float
    end: float
    station: str
    region: str
    zone: str
    source: str
    format: str
    episode_title: str = ""
    season_number: int | None = None
    episode_number: int | None = None
    season_year: int | None = None
    release_year: int | None = None
    description: str = ""
    first_party: bool = False
    structured: bool = False


@dataclass
class ParseResult:
    events: list[Event] = field(default_factory=list)
    rejections: list[dict] = field(default_factory=list)
    identity: str = ""
    formats: list[str] = field(default_factory=list)

    def reject(self, reason: str, **details):
        row = {"reason": reason}
        row.update({k: v for k, v in details.items() if v not in (None, "")})
        if row not in self.rejections and len(self.rejections) < 64:
            self.rejections.append(row)


def norm(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum()) if isinstance(value, str) else ""


def clean(value, limit=MAX_TEXT):
    value = " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value)).split()) if isinstance(value, str) else ""
    return value if 0 < len(value) <= limit and not re.search(r"(?i)https?://|api_key=|token=|[<>]", value) else ""


def region_ok(value, region):
    if isinstance(value, dict):
        value = value.get("name") or value.get("address") or value.get("@id")
    if isinstance(value, list):
        return any(region_ok(v, region) for v in value)
    return norm(value) in REGION_ALIASES.get(region, set())


def station_ok(value, channel, region=None):
    if isinstance(value, dict):
        value = value.get("name") or value.get("legalName")
    if isinstance(value, list):
        return any(station_ok(v, channel, region) for v in value)
    if not isinstance(value, str) or not norm(value):
        return False
    return norm(value) == norm(channel)


def zone(name):
    if not isinstance(name, str) or not name:
        raise EvidenceError("missing-timezone")
    candidate = name.strip()
    lowered = candidate.casefold()
    if lowered in ZONE_ALIASES:
        candidate = ZONE_ALIASES[lowered]
    if not re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z0-9_+.-]+)+", candidate):
        raise EvidenceError("invalid-timezone")
    try:
        return ZoneInfo(candidate)
    except Exception as exc:  # zoneinfo raises several platform errors
        raise EvidenceError("unknown-timezone") from exc


def local_epoch(day, clock, tz_name):
    try:
        hour, minute = (int(part) for part in clock.split(":", 1))
        base = dt.datetime.combine(day, dt.time(hour, minute))
        z = zone(tz_name)
    except (ValueError, TypeError) as exc:
        raise EvidenceError("invalid-local-time") from exc
    # Refuse nonexistent and ambiguous DST wall times. The feed must be
    # explicit rather than have us choose a fold.
    first = base.replace(tzinfo=z, fold=0)
    second = base.replace(tzinfo=z, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise EvidenceError("ambiguous-dst-time")
    if dt.datetime.fromtimestamp(first.timestamp(), z).replace(tzinfo=None) != base:
        raise EvidenceError("nonexistent-dst-time")
    return first.timestamp(), z.key


def iso_epoch(value, default_zone=None):
    if not isinstance(value, str) or len(value) > 80:
        raise EvidenceError("invalid-time")
    text = value.strip().replace("Z", "+00:00")
    try:
        stamp = dt.datetime.fromisoformat(text)
    except (ValueError, TypeError) as exc:
        raise EvidenceError("invalid-time") from exc
    if stamp.tzinfo is None:
        if not default_zone:
            raise EvidenceError("missing-timezone")
        stamp = stamp.replace(tzinfo=zone(default_zone))
    return stamp.timestamp(), getattr(stamp.tzinfo, "key", None) or str(stamp.tzinfo)


def valid_year(value):
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1888 <= year <= dt.datetime.now(dt.timezone.utc).year + 1 else None


def year_from_date(value):
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"\d{8}", value.strip()):
        return valid_year(value.strip()[:4])
    match = re.search(r"(?<!\d)(\d{4})(?!\d)", value)
    return valid_year(match[1]) if match else None


def episode_numbers(text):
    if not isinstance(text, str):
        return None
    match = EPISODE_TEXT.search(text)
    if not match:
        match = ONSREEN.search(text.strip())
        if match:
            return int(match[1]), int(match[2])
        return None
    groups = match.groups()
    if groups[0] is not None:
        season, number = groups[0], groups[1]
    elif groups[2] is not None:
        season, number = groups[2], groups[3]
    else:
        number, season = groups[4], groups[5]
    try:
        s, e = int(season), int(number)
    except (TypeError, ValueError):
        return None
    return (s, e) if 0 < s <= 99 and 0 < e <= 9999 else None


def _active(start, end, now):
    return 0 < end - start <= 8 * 3600 and start <= now < end


def _dedupe(events):
    out = []
    seen = set()
    for event in events:
        key = (norm(event.title), norm(event.episode_title), event.start, event.end, event.station, event.region)
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
        if len(out) >= MAX_EVENTS:
            break
    return out


def _description(node):
    if not isinstance(node, dict):
        return ""
    for key in ("description", "abstract", "text"):
        value = clean(node.get(key))
        if value:
            return value
    return ""


def _episode_from_json(obj):
    """Extract episode identity without using a series-level year as episode year."""
    if not isinstance(obj, dict):
        return {}, None, None, None, None
    types=obj.get("@type");types=types if isinstance(types,list) else [types]
    episode=obj.get("partOfEpisode") if isinstance(obj.get("partOfEpisode"),dict) else obj
    season_obj=obj.get("partOfSeason") if isinstance(obj.get("partOfSeason"),dict) else (episode.get("partOfSeason") if isinstance(episode.get("partOfSeason"),dict) else {})
    title = clean(episode.get("name") or episode.get("headline")) if (episode is not obj or "TVEpisode" in types) else ""
    season = season_obj.get("seasonNumber") if isinstance(season_obj,dict) else None
    if season is None:
        season = obj.get("seasonNumber")
    number = obj.get("episodeNumber") if obj.get("episodeNumber") is not None else episode.get("episodeNumber")
    if isinstance(number, str):
        parsed = episode_numbers(number) or ONSREEN.match(number.strip())
        if parsed:
            if hasattr(parsed, "groups"):
                season, number = int(parsed[1]), int(parsed[2])
            else:
                season, number = parsed
    try:
        season = int(season) if season is not None and 0 < int(season) <= 99 else None
        number = int(number) if number is not None and 0 < int(number) <= 9999 else None
    except (TypeError, ValueError):
        season = number = None
    release = season_year = None
    for source in (episode, obj):
        for key in ("datePublished", "releaseDate", "releasedEvent"):
            value = source.get(key)
            if isinstance(value, dict):
                value = value.get("startDate") or value.get("datePublished")
            if isinstance(value, str):
                year = year_from_date(value)
                if year:
                    season_year = year
                    break
        if season_year is not None:
            break
    if season_year is None and season is not None:
        # Only an episode-scoped date is acceptable here. Never copy a series
        # premiere/copyright year into seasonYear.
        season_year = year_from_date(episode.get("dateCreated"))
    if season is None and number is None:
        release = valid_year(obj.get("copyrightYear"))
    return ({"episode_title": title} if title else {}), season, number, season_year, release


def _event_from_json(obj, station, channel, region, default_zone, source, fmt, first_party, now):
    if not isinstance(obj, dict):
        return None
    start_raw = obj.get("startDate") or obj.get("startTime")
    end_raw = obj.get("endDate") or obj.get("endTime")
    try:
        start, start_zone = iso_epoch(start_raw, default_zone)
        end, end_zone = iso_epoch(end_raw, start_zone or default_zone)
    except EvidenceError:
        return None
    title = clean(obj.get("name") or obj.get("headline"))
    series = obj.get("partOfSeries")
    if not isinstance(series, dict) and isinstance(obj.get("partOfEpisode"),dict):
        series = obj["partOfEpisode"].get("partOfSeries")
    if isinstance(series, dict) and clean(series.get("name")):
        title = clean(series.get("name"))
    if not title:
        return None
    extra, season, number, season_year, release = _episode_from_json(obj)
    if not _active(start, end, now):
        return None
    return Event(title=title, start=start, end=end, station=station, region=region,
                 zone=start_zone or default_zone or "", source=source, format=fmt,
                 episode_title=extra.get("episode_title", ""), season_number=season,
                 episode_number=number, season_year=season_year, release_year=release,
                 description=_description(obj), first_party=first_party, structured=True)


def _walk_json(obj, out, depth=0):
    if depth > 24:
        return
    if isinstance(obj, dict):
        out.append(obj)
        for value in obj.values():
            if isinstance(value, (dict, list)):
                _walk_json(value, out, depth + 1)
    elif isinstance(obj, list):
        for value in obj[:1000]:
            if isinstance(value, (dict, list)):
                _walk_json(value, out, depth + 1)


def _json_scripts(page):
    scripts = []
    for match in re.finditer(r"<script\b([^>]*)>(.*?)</script>", page, re.I | re.S):
        attrs = match[1]
        body = match[2]
        if len(body) > MAX_JSON_BYTES:
            continue
        if not re.search(r"(?i)type\s*=\s*['\"](?:application/ld\+json|application/json)['\"]", attrs) and "id=\"__NEXT_DATA__\"" not in attrs and "id='__NEXT_DATA__'" not in attrs:
            continue
        scripts.append((attrs, body))
        if len(scripts) >= MAX_JSON_SCRIPTS:
            break
    return scripts


def _parse_jsonld(page, channel, region, now, source, first_party, result):
    objects = []
    for attrs, body in _json_scripts(page):
        if "ld+json" not in attrs.casefold():
            continue
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            result.reject("invalid-json-ld")
            continue
        _walk_json(data, objects)
    if not objects:
        return []
    by_id = {}
    for obj in objects:
        identifier = obj.get("@id")
        if isinstance(identifier, str):
            by_id[identifier] = obj
    stations = []
    for obj in objects:
        types = obj.get("@type")
        types = types if isinstance(types, list) else [types]
        if not any(t in ("BroadcastService", "TelevisionChannel", "RadioChannel") for t in types):
            continue
        if not station_ok(obj.get("name") or obj.get("broadcastDisplayName") or obj.get("legalName"), channel):
            continue
        if not region_ok(obj.get("areaServed") or obj.get("broadcastArea"), region):
            continue
        stations.append(obj)
    if not stations:
        if any(station_ok(obj.get("name"), channel) for obj in objects):
            result.reject("region-or-station-identity-missing")
        return []
    if len({id(obj) for obj in stations}) > 8:
        result.reject("ambiguous-station")
        return []
    events = []
    for station in stations:
        values = station.get("hasBroadcastEvent") or station.get("eventSchedule") or []
        if isinstance(values, dict):
            values = [values]
        if not isinstance(values, list):
            continue
        for value in values[:MAX_EVENTS]:
            if isinstance(value, str):
                value = by_id.get(value)
            if not isinstance(value, dict):
                continue
            types = value.get("@type")
            types = types if isinstance(types, list) else [types]
            if types and not any(t in ("BroadcastEvent", "ScreeningEvent", "TVEpisode", "CreativeWork") for t in types):
                continue
            event = _event_from_json(value, clean(station.get("name")), channel, region, None, source, "json-ld", first_party, now)
            if event:
                events.append(event)
    if events:
        result.formats.append("json-ld")
    return events


def _looks_like_schedule(obj):
    return isinstance(obj, dict) and any(isinstance(obj.get(k), list) for k in ("programs", "schedule", "timelines", "events", "programmes", "airings"))


def _station_from_embedded(obj, channel):
    for key in ("channelName", "stationName", "serviceName", "displayName"):
        if station_ok(obj.get(key), channel):
            return clean(obj.get(key))
    channel_obj = obj.get("channel") or obj.get("station")
    if isinstance(channel_obj, dict):
        for key in ("name", "displayName", "title"):
            if station_ok(channel_obj.get(key), channel):
                return clean(channel_obj.get(key))
    if station_ok(obj.get("name"), channel) and any(k in obj for k in ("programs", "schedule", "timelines", "events", "programmes")):
        return clean(obj.get("name"))
    return ""


def _parse_embedded(page, channel, region, now, source, first_party, result):
    objects = []
    for attrs, body in _json_scripts(page):
        if "ld+json" in attrs.casefold():
            continue
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            result.reject("invalid-embedded-json")
            continue
        _walk_json(data, objects)
    events = []
    for obj in objects:
        if not _looks_like_schedule(obj):
            continue
        station = _station_from_embedded(obj, channel)
        if not station:
            continue
        if not region_ok(obj.get("region") or obj.get("areaServed") or obj.get("country"), region):
            # A station without an explicit region is not usable as a
            # standalone structured source.
            result.reject("region-identity-missing")
            continue
        tz_name = obj.get("timeZone") or obj.get("timezone") or obj.get("tz")
        rows = next((obj.get(k) for k in ("programs", "schedule", "timelines", "events", "programmes", "airings") if isinstance(obj.get(k), list)), [])
        for row in rows[:MAX_EVENTS]:
            if not isinstance(row, dict):
                continue
            event = _event_from_json(row, station, channel, region, tz_name, source, "embedded-json", first_party, now)
            if event:
                events.append(event)
    if events:
        result.formats.append("embedded-json")
    return events


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.headings = []
        self.titles = []
        self.rows = []
        self.row = None
        self.cell = None
        self._heading_depth = None
        self._title_depth = None
        self._script_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tag = tag.casefold()
        if tag in ("script", "style"):
            self._script_depth += 1
            return
        if tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").casefold()
            value = attrs.get("content") or ""
            if name in ("timezone", "channel", "station", "date", "og:title", "region", "country", "data-region"):
                self.meta.setdefault(name, value)
        if tag in ("h1", "h2", "h3"):
            self.headings.append([tag, attrs, []])
            self._heading_depth = len(self.headings) - 1
        if tag == "title":
            self.titles.append([])
            self._title_depth = len(self.titles) - 1
        if tag == "tr":
            self.row = {"attrs": attrs, "cells": []}
        if tag in ("td", "th") and self.row is not None:
            self.cell = {"attrs": attrs, "text": [], "times": [], "links": []}
        if tag == "time" and self.cell is not None:
            self.cell["times"].append({"attrs": attrs, "text": []})
        if tag == "a" and self.cell is not None:
            self.cell["links"].append([])

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in ("script", "style"):
            self._script_depth = max(0, self._script_depth - 1)
            return
        if tag in ("h1", "h2", "h3"):
            self._heading_depth = None
        if tag == "title":
            self._title_depth = None
        if tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row["cells"].append(self.cell)
            self.cell = None
        if tag == "tr" and self.row is not None:
            if self.row["cells"]:
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self._script_depth:
            return
        if self._heading_depth is not None and self.headings and self._heading_depth < len(self.headings):
            self.headings[self._heading_depth][2].append(data)
        if self._title_depth is not None and self.titles and self._title_depth < len(self.titles):
            self.titles[self._title_depth].append(data)
        if self.cell is not None:
            self.cell["text"].append(data)
            if self.cell["times"]:
                self.cell["times"][-1]["text"].append(data)
            if self.cell["links"]:
                self.cell["links"][-1].append(data)


MONTHS = {name.casefold(): i for i, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}


def _page_date(text):
    match = re.search(r"(?i)\b(20\d{2})-(\d{2})-(\d{2})\b", text)
    if match:
        try:
            return dt.date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            return None
    match = re.search(r"(?i)\b(" + "|".join(MONTHS) + r")\s+(\d{1,2}),?\s+(20\d{2})\b", text)
    if match:
        try:
            return dt.date(int(match[3]), MONTHS[match[1].casefold()], int(match[2]))
        except (ValueError, KeyError):
            return None
    match = re.search(r"(?i)\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(20\d{2})\b", text)
    if match:
        try:
            return dt.date(int(match[3]), MONTHS[match[2].casefold()], int(match[1]))
        except (ValueError, KeyError):
            return None
    return None


def _cell_time(cell, day, tz_name):
    for item in cell.get("times", []):
        raw = (item["attrs"].get("datetime") or item["attrs"].get("data-start") or
               item["attrs"].get("data-time") or item["attrs"].get("content") or item["attrs"].get("title"))
        if raw:
            try:
                stamp, zone_name = iso_epoch(raw, tz_name)
                return stamp, zone_name
            except EvidenceError as error:
                clock_match = re.fullmatch(r"((?:[01]\d|2[0-3]):[0-5]\d)(?::[0-5]\d)?", raw.strip())
                if clock_match and day and tz_name:
                    return local_epoch(day, clock_match[1], tz_name)
                if str(error) in ("ambiguous-dst-time", "nonexistent-dst-time"):
                    raise
        clock_text = " ".join(item["text"]).strip()
        match = re.fullmatch(r"((?:[01]\d|2[0-3]):[0-5]\d)(?::[0-5]\d)?", clock_text)
        if match and day and tz_name:
            return local_epoch(day, match[1], tz_name)
    for key in ("data-start", "data-time", "start"):
        raw = cell["attrs"].get(key) or ""
        if raw:
            return iso_epoch(raw, tz_name)
    text = " ".join(cell["text"])
    match = re.search(r"\b((?:[01]\d|2[0-3]):[0-5]\d)(?::[0-5]\d)?\b", text)
    if match and day and tz_name:
        return local_epoch(day, match[1], tz_name)
    return None


def _parse_table(page, channel, region, now, source, first_party, result):
    parser = _TableParser()
    try:
        parser.feed(page)
        parser.close()
    except Exception:
        result.reject("invalid-html")
        return []
    identify = ""
    for key in ("channel", "station"):
        if station_ok(parser.meta.get(key), channel):
            identify = clean(parser.meta[key])
    if not identify:
        for _tag, attrs, text in parser.headings:
            candidate = clean(" ".join(text))
            if station_ok(candidate, channel):
                identify = candidate
    if not identify:
        if parser.titles:
            heading = clean(" ".join(parser.titles[0]))
            if station_ok(heading, channel):
                identify = heading
    if not identify:
        result.reject("station-identity-missing")
        return []
    declared_region = parser.meta.get("region") or parser.meta.get("country") or parser.meta.get("data-region")
    if declared_region and not region_ok(declared_region, region):
        result.reject("region-mismatch")
        return []
    tz_name = parser.meta.get("timezone") or parser.meta.get("data-timezone")
    heading_text = " ".join(" ".join(text) for _tag, _attrs, text in parser.headings) + " " + " ".join(" ".join(text) for text in parser.titles)
    day = _page_date(parser.meta.get("date") or "") or _page_date(heading_text)
    events = []
    rows = parser.rows[:MAX_TABLE_ROWS]
    prepared = []
    for row in rows:
        cells = row["cells"]
        start = end = zone_name = None
        for cell in cells:
            try:
                parsed = _cell_time(cell, day, tz_name)
            except EvidenceError as error:
                result.reject(str(error))
                parsed = None
            if start is None:
                if parsed:
                    start, zone_name = parsed
            elif end is None:
                if parsed and parsed[0] > start:
                    end = parsed[0]
        text = " ".join(" ".join(cell["text"]) for cell in cells)
        title = ""
        for cell in cells:
            classes = (cell["attrs"].get("class") or "").casefold()
            if any(word in classes for word in ("title", "program", "name", "show")):
                candidate = clean(" ".join(cell["text"]))
                candidate = re.sub(r"(?i)\bS\d{1,2}E\d{1,4}\b", "", candidate).strip(" -–—|")
                if candidate:
                    title = candidate
                    break
        if not title:
            for cell in cells:
                for link in cell["links"]:
                    candidate = clean(" ".join(link))
                    if candidate and not re.fullmatch(r"(?i)\d{1,2}:\d{2}\s*(?:am|pm)?", candidate):
                        title = candidate
                        break
                if title:
                    break
        if not title:
            for cell in cells:
                candidate = clean(" ".join(cell["text"]))
                candidate = re.sub(r"(?i)^\s*\d{1,2}:\d{2}(?:\s*(?:am|pm))?\s*", "", candidate)
                candidate = re.sub(r"(?i)\bS\d{1,2}E\d{1,4}\b", "", candidate).strip(" -–—|")
                if candidate and not re.fullmatch(r"(?i)\d{1,2}:\d{2}\s*(?:am|pm)?", candidate):
                    title = candidate
                    break
        if start is None or not title:
            continue
        prepared.append((start, end, zone_name, title, text))
    if not prepared:
        result.reject("no-timed-rows")
        return []
    prepared.sort(key=lambda item: item[0])
    if any(b[0] <= a[0] for a, b in zip(prepared, prepared[1:])):
        result.reject("ambiguous-table-order")
        return []
    for index, (start, end, zone_name, title, text) in enumerate(prepared):
        if end is None and index + 1 < len(prepared):
            end = prepared[index + 1][0]
        if end is None:
            continue
        if not _active(start, end, now):
            continue
        if not zone_name:
            continue
        season = number = None
        parsed = episode_numbers(text)
        if parsed:
            season, number = parsed
        event = Event(title=title, start=start, end=end, station=identify, region=region,
                      zone=zone_name, source=source, format="html-table",
                      season_number=season, episode_number=number,
                      description=clean(text), first_party=first_party, structured=False)
        events.append(event)
    if events:
        result.formats.append("html-table")
    return events


def _parse_xmltv(page, channel, region, now, source, first_party, result):
    if not page.lstrip().startswith("<"):
        return []
    upper = page.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper or "<!ELEMENT" in upper:
        result.reject("unsupported-xml-doctype")
        return []
    try:
        root = ET.fromstring(page)
    except (ET.ParseError, ValueError):
        return []
    if root.tag != "tv":
        return []
    matched = set()
    for node in root.findall("channel"):
        names = [clean(item.text) for item in node.findall("display-name")]
        if any(station_ok(name, channel) for name in names):
            matched.add(node.get("id"))
    if not matched:
        result.reject("station-identity-missing")
        return []
    events = []
    for programme in root.findall("programme")[:MAX_EVENTS]:
        if programme.get("channel") not in matched:
            continue
        try:
            start, zone_name = iso_epoch_xmltv(programme.get("start"))
            end, _ = iso_epoch_xmltv(programme.get("stop"))
        except EvidenceError:
            continue
        title = clean(programme.findtext("title"))
        if not title or not _active(start, end, now):
            continue
        sub = clean(programme.findtext("sub-title"))
        season = number = None
        for item in programme.findall("episode-num"):
            system = (item.get("system") or "").casefold()
            value = clean(item.text)
            if system == "onscreen":
                parsed = episode_numbers(value)
            elif system == "xmltv_ns":
                parsed = xmltv_ns_numbers(value)
            else:
                continue
            if parsed:
                season, number = parsed
                break
        season_year = None
        date_value = programme.findtext("date")
        if season is not None and date_value:
            season_year = year_from_date(date_value)
        event = Event(title=title, start=start, end=end, station=channel, region=region,
                      zone=zone_name, source=source, format="xmltv",
                      episode_title=sub, season_number=season, episode_number=number,
                      season_year=season_year, description=clean(programme.findtext("desc")),
                      first_party=first_party, structured=True)
        events.append(event)
    if events:
        result.formats.append("xmltv")
    return events


def iso_epoch_xmltv(value):
    if not isinstance(value, str):
        raise EvidenceError("invalid-time")
    try:
        stamp = dt.datetime.strptime(value.strip(), "%Y%m%d%H%M%S %z")
    except (ValueError, TypeError) as exc:
        raise EvidenceError("timezone-required") from exc
    return stamp.timestamp(), getattr(stamp.tzinfo, "key", None) or stamp.strftime("%z")


def xmltv_ns_numbers(value):
    match = re.fullmatch(r"\s*(\d+)\s*\.\s*(\d+)\s*(?:\.\s*(\d+))?\s*", value or "")
    if not match:
        return None
    try:
        season, number = int(match[1]) + 1, int(match[2]) + 1
    except (TypeError, ValueError):
        return None
    return (season, number) if 0 < season <= 99 and 0 < number <= 9999 else None


def parse_schedule(page, channel, region, now, source="", first_party=False):
    """Return bounded events plus explicit rejection reasons. Never guesses."""
    result = ParseResult()
    if not isinstance(page, str) or not page or len(page) > 2 * 1024 * 1024:
        result.reject("page-size")
        return result
    if region not in REGION_ALIASES:
        result.reject("unsupported-region")
        return result
    events = []
    events.extend(_parse_jsonld(page, channel, region, now, source, first_party, result))
    events.extend(_parse_xmltv(page, channel, region, now, source, first_party, result))
    if not events:
        events.extend(_parse_embedded(page, channel, region, now, source, first_party, result))
    if not events:
        events.extend(_parse_table(page, channel, region, now, source, first_party, result))
    if events:
        result.identity = events[0].station
    result.events = _dedupe(events)
    return result
