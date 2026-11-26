"""Read-only live validation for the audio episode source parsers.

This is an engineering tool, not a gateway worker. It fetches only the URLs
given on the command line (or the reviewed 2025-11-04 NXT defaults), applies the
same bounded parsers the gateway uses, and prints the typed rows. It never
stores a page, never follows a blocked source, and never writes state.

Examples:
    python3 validate_episode_sources.py
    python3 validate_episode_sources.py --series "WWE NXT" URL [URL ...]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import episode_audio

DEFAULT_SERIES = "WWE NXT"
DEFAULT_URLS = (
    "https://www.wwe.com/shows/wwenxt/2025-11-04",
    "https://www.wwe.com/shows/wwenxt/2025-11-04/results",
    ("https://www.wrestlinginc.com/2016429/wwe-nxt-november-4-six-woman-"
     "tag-team-match-jevon-evans-saquon-shugars-more/"),
    ("https://pwinsider.com/article/201974/114-wwe-nxt-report-ricky-and-trick-"
     "talk-ethan-and-chelsea-celebrate-grande-americano-in-action-you-dont-"
     "want-to-be-crossing-jordan-and-more.html?p=1"),
)
MAX_BYTES = episode_audio.MAX_PAGE_BYTES


def fetch(url, timeout=20):
    request = urllib.request.Request(
        url, headers={"User-Agent": "Jealousfin-Episode-Evidence/1.0",
                      "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
        status = getattr(response, "status", 200)
    if len(data) > MAX_BYTES:
        raise ValueError("page too large")
    return status, data


def classify(url):
    host = episode_audio._host(url)
    if host in episode_audio.OFFICIAL_ARCHIVE_HOSTS:
        return "official"
    if host in episode_audio.INDEPENDENT_REPORT_HOSTS:
        return "report"
    return "unsupported"


def official_row(row):
    return {"series": row.series_title, "airDate": row.original_air_date,
            "season": row.season_number, "episode": row.episode_number,
            "code": row.code(), "title": row.episode_title}


def report_row(row):
    return {"series": row.series_title, "airDate": row.original_air_date,
            "published": row.published_at, "season": row.season_number,
            "episode": row.episode_number, "terms": list(row.terms)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*", help="public official/report URLs")
    parser.add_argument("--series", default=DEFAULT_SERIES)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    urls = args.urls or list(DEFAULT_URLS)
    context = {"seriesTitle": args.series, "channelID": "",
               "originalAirDate": "", "seasonNumber": None, "episodeNumber": None}
    result = {"revision": episode_audio.REVISION, "series": args.series,
              "sources": [], "officialRows": 0, "reportRows": 0, "errors": 0}
    for url in urls:
        kind = classify(url)
        entry = {"url": url, "kind": kind, "status": None, "rows": [], "error": None}
        if kind == "unsupported":
            entry["error"] = "host not on the reviewed allowlist"
            result["errors"] += 1
            result["sources"].append(entry)
            continue
        try:
            status, page = fetch(url, args.timeout)
            entry["status"] = status
            if kind == "official":
                rows = episode_audio.parse_official_page(page, url, args.series)
                entry["rows"] = [official_row(row) for row in rows]
                result["officialRows"] += len(rows)
            else:
                rows = episode_audio.parse_report_page(page, url, context)
                entry["rows"] = [report_row(row) for row in rows]
                result["reportRows"] += len(rows)
        except urllib.error.HTTPError as error:
            # A blocked or denied source stays blocked. Never retry around it.
            entry["status"] = error.code
            entry["error"] = "HTTP %d" % error.code
            result["errors"] += 1
        except Exception as error:
            entry["error"] = type(error).__name__ + ": " + str(error)[:160]
            result["errors"] += 1
        result["sources"].append(entry)
    if args.as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("revision", result["revision"], "series", result["series"])
        for entry in result["sources"]:
            print("%-9s %-4s %s" % (entry["kind"], entry["status"] or "-", entry["url"]))
            if entry["error"]:
                print("          error:", entry["error"])
            for row in entry["rows"]:
                print("          ", json.dumps(row, ensure_ascii=False))
        print("officialRows=%d reportRows=%d errors=%d" %
              (result["officialRows"], result["reportRows"], result["errors"]))
    return 0 if result["officialRows"] and result["reportRows"] else 1


if __name__ == "__main__":
    sys.exit(main())
