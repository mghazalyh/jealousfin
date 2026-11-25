"""Reviewed regional schedule adapter for the exact AXN Crime Hungary feed.
No media inspection, credentials, or channel-name-only matching.
TVMustra's broadcast day rolls past midnight; Filmkatalogus uses calendar days.
"""
import datetime as dt
import html
import json
import re
import unicodedata
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Budapest")
PROVIDER_ID = "nltv:us:AXNCrime.us"
CHANNEL = "AXN Crime"
MAX_AGE = 1800
REFRESH = 900
MONTHS = ("január","február","március","április","május","június","július","augusztus","szeptember","október","november","december")

def text(value):
    value = " ".join(html.unescape(re.sub(r"<[^>]*>", "", value)).split()) if isinstance(value, str) else ""
    return value if 0 < len(value) <= 300 and not re.search(r"(?i)https?://|api_key=|token=", value) else ""

def norm(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())

def epoch(day, clock):
    value = dt.datetime.combine(day, dt.time.fromisoformat(clock))
    local = value.replace(tzinfo=ZONE)
    # Never invent a time in a DST gap or ambiguous fold.
    if local.utcoffset() != value.replace(tzinfo=ZONE, fold=1).utcoffset():
        raise ValueError("Ambiguous regional schedule time")
    if dt.datetime.fromtimestamp(local.timestamp(), ZONE).replace(tzinfo=None) != value:
        raise ValueError("Invalid regional schedule time")
    return local.timestamp()

def year(value):
    if isinstance(value, str) and re.fullmatch(r"\d{4}", value):
        n = int(value)
        if 1888 <= n <= dt.datetime.now(dt.timezone.utc).year+1:
            return n
    return None

def mustra(page, day):
    if not re.search(r"currentDateStr\s*:\s*['\"]"+re.escape(day.isoformat())+r"['\"]", page):
        raise ValueError("Wrong TVMustra date")
    events = None
    for script in re.findall(r"<script[^>]*type=['\"]application/ld\+json['\"][^>]*>(.*?)</script>", page, re.I|re.S):
        try:
            item = json.loads(script)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("name")==CHANNEL and item.get("broadcastDisplayName")==CHANNEL and isinstance(item.get("hasBroadcastEvent"),list):
            if events is not None: raise ValueError("Ambiguous TVMustra channel")
            events=item["hasBroadcastEvent"]
    if not events: raise ValueError("Missing TVMustra channel identity")
    details={}
    for row in events:
        if not isinstance(row,dict) or row.get("@type")!="BroadcastEvent": continue
        try:
            stamp=dt.datetime.fromisoformat(row["startDate"])
            if stamp.tzinfo is not None or stamp.date()!=day: continue
            key=(epoch(day,stamp.strftime("%H:%M")),norm(text(row.get("name"))))
            match=re.match(r"^\((?:[^)]*,\s*)?(\d{4})\.\)",row.get("description",""))
            details[key]=year(match[1]) if match else None
        except (ValueError,TypeError,KeyError,AttributeError):
            continue
    pattern=r"""loadDetailsVertical\(this,\s*'viasat_film',\s*\d+,\s*'(\d{2}:\d{2})'\)"[^>]*>\s*<div class="v-time">\s*(\d{2}:\d{2})\s*</div>\s*<div class="v-title">(.*?)</div>"""
    found=[]; date=day; previous=None
    for clock, displayed, raw in re.findall(pattern,page,re.S):
        if clock!=displayed: raise ValueError("Inconsistent TVMustra clock")
        minute=int(clock[:2])*60+int(clock[3:])
        if previous is not None and minute<previous: date+=dt.timedelta(days=1)
        if date>day+dt.timedelta(days=1):raise ValueError("Invalid broadcast day")
        previous=minute
        title=text(raw); at=epoch(date,clock)
        if not title:raise ValueError("Missing programme title")
        found.append((at,title,title,details.get((at,norm(title)))))
    if not 2<=len(found)<=200 or any(b[0]<=a[0] for a,b in zip(found,found[1:])):raise ValueError("Invalid TVMustra schedule")
    return found

def filmkatalogus(page, day):
    expected=f"TV műsor ({CHANNEL} - {day.year}. {MONTHS[day.month-1]} {day.day}."
    header=re.search(r"<title>(.*?)</title>",page,re.I|re.S)
    if not header or not text(header[1]).startswith(expected):raise ValueError("Wrong Filmkatalogus channel/date")
    found=[]
    for clock, body in re.findall(r"""<tr>\s*<td\s+valign=['"]?top['"]?>\s*(\d{2}:\d{2})(.*?)</tr>""",page,re.I|re.S):
        # The second cell starts with the localized title in bold.
        cell=re.search(r"</td>\s*<td\b[^>]*>\s*<b>(.*?)</b>(.*)",body,re.I|re.S)
        if not cell:continue
        link=re.search(r"<a\b[^>]*>(.*?)</a>",cell[1],re.I|re.S)
        localized=text(link[1] if link else cell[1])
        if not localized:continue
        original=localized; release=None
        match=re.match(r"\s*<br\s*/?>\s*\(([^<>]{1,300})\)",cell[2],re.I)
        if match:
            fields=text(match[1])
            y=re.fullmatch(r"(?:(.+),\s*)?(\d{4})",fields)
            if y:
                release=year(y[2]);original=text(y[1]) or localized
        found.append((epoch(day,clock),localized,original,release))
    if not 2<=len(found)<=200 or any(b[0]<=a[0] for a,b in zip(found,found[1:])):raise ValueError("Invalid Filmkatalogus schedule")
    return found

def intervals(rows):
    unique={}
    for row in rows:
        if row[0] in unique and unique[row[0]]!=row:raise ValueError("Conflicting regional listings")
        unique[row[0]]=row
    ordered=sorted(unique.values())
    return [(a[0],b[0],*a[1:]) for a,b in zip(ordered,ordered[1:]) if 0<b[0]-a[0]<=6*3600]

def corroborate(first,second,now):
    a=[r for r in intervals(first) if r[0]<=now<r[1]]
    b=[r for r in intervals(second) if r[0]<=now<r[1]]
    if len(a)!=1 or len(b)!=1:return None
    a,b=a[0],b[0]
    if norm(a[2])!=norm(b[2]) or abs(a[0]-b[0])>60:return None
    # One calendar guide may omit an overnight entry. Do not extend past
    # the earliest reported next event even when the other listing is longer.
    title=b[3]
    if a[4] and a[4]==b[4]:title+=f" ({a[4]})"
    return {"title":title,"source":"corroborated-external-schedule","observedAt":now,"start":max(a[0],b[0]),"end":min(a[1],b[1])}

def fetch_current(fetch, now):
    local=dt.datetime.fromtimestamp(now,ZONE)
    day=local.date()-dt.timedelta(days=1) if local.hour<6 else local.date()
    tomorrow=day+dt.timedelta(days=1)
    # Explicit channel paths, no user input or arbitrary network targets.
    a=[];b=[]
    for date in (day,tomorrow):
        a+=mustra(fetch("https://www.tvmustra.hu/tvmusor/AXN_CRIME/"+date.isoformat()),date)
        b+=filmkatalogus(fetch("https://www.filmkatalogus.hu/tvmusor-axncrime-"+date.isoformat()),date)
    return corroborate(a,b,now)
