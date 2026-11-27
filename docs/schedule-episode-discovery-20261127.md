# FAST schedule and canonical-episode identity (2026-11-27)

This is the isolated `engineering/schedule-episode-discovery-20261127` lane.
It is additive to the existing `schedule_evidence`, `programme_research`,
`episode_audio`, gateway and overlay paths. No production gateway, service,
IPA, device, playback policy, rotation policy or language policy was changed by
this session.

## Problem

A show-name-only FAST channel can be playing an episode while the provider
row has no current programme, title, season or year (for example
`nltv:us:TinyHouseNation.us`). The inverse is also unsafe: a reviewed
channel-to-series map, or a schedule copied from a similarly named provider,
must not be treated as a current airing.

## Precedence

`Server/iptv/schedule_episode.py` resolves exactly one current episode with this
fixed precedence:

1. **Source-owned structured schedule** for the exact station/feed, with an
   explicit timezone and active interval.
2. **Two independent public schedules** that agree on the series title and the
   active interval (UTC start/end within 60 seconds).
3. **Unique canonical catalog episode** matched by at least two distinctive
   audio clues when the trusted schedule identifies the series but not the
   episode.

A single ordinary HTML schedule is a research lead, not corroboration. A
mapping or catalog row alone is never a current schedule.

## Feed identity and time

- The provider ID is the identity. `nltv:us:TinyHouseNation.us` is accepted;
  another distributor with the same channel name is rejected as
  `unreviewed-provider` or `channel-name-mismatch`.
- The channel name, region and reviewed map must all agree.
- Every usable event must have an explicit timezone (`zone`/`timezone`). A
  missing zone, DST ambiguity, interval disagreement, region conflict or
  timezone conflict between the feed and its evidence fails closed.
- The schedule observation must be current (120 s bound). Stale guide data is
  rejected rather than promoted.
- A future/next programme is not promoted; only the active interval is used.

## Canonical catalogs and years

Reviewed map (lookup only, not a schedule):

```
nltv:us:TinyHouseNation.us
  channel  Tiny House Nation
  region   us
  series   Tiny House Nation
  catalog  tvmaze:1418, tvdb:tiny-house-nation,
           aetv:tiny-house-nation, apple:tiny-house-nation
```

Bounded adapters in the same module:

| Adapter | Host | Shape |
| --- | --- | --- |
| `tvmaze-api` | `api.tvmaze.com` | `/shows/<id>/episodes` JSON |
| `tvmaze-html` | `www.tvmaze.com` | season episode-guide HTML |
| `tvdb-html` | `thetvdb.com` | aired-order all-seasons HTML |
| `aetv-html` | `www.aetv.com` | official season page / JSON-LD |
| `apple-episode` | `tv.apple.com` | official episode page |

Only HTTPS, default port, no credentials and no fragment/query on a catalog URL
is accepted. Each adapter has an explicit host and parser; there is no generic
crawler and no model-provided catalog URL is trusted.

A catalog episode is accepted only when:

- two independent adapters agree on the episode title and original air date; or
- one broadcaster-owned adapter (`aetv`/`apple`) supplies the identity.

Different database numbering is tolerated only when the official adapter wins,
or when all database adapters agree. A database-only numbering conflict is
rejected. Advertisements are never episodes. A special is kept as a special and
is never silently merged into a regular-season episode.

Year fields are deliberately separate:

- `seasonYear` / `episodeYear` = year of the canonical episode's **original air
  date**;
- `seasonPremiereYear` = earliest episode date in that season;
- `seriesPremiereYear` = earliest episode date in the catalog;
- the schedule `start`/`end` is the **rerun/current airing** and is never used
  as the episode's original year.

## Live public validation (2026-11-27)

The reviewed example was checked against real public pages, without persisting
page bodies:

| Source | Result |
| --- | --- |
| `https://api.tvmaze.com/seasons/5604/episodes` | S1E5 **500 Sq. Ft. Rocky Mountain Mansion**, original `2014-08-13`, summary includes Colorado, wildfire, 500 sq. ft., two offices |
| `https://www.tvmaze.com/seasons/5604/tiny-house-nation-season-1/episodeguide` | same S1E5 / `Aug 13, 2014` |
| `https://thetvdb.com/series/tiny-house-nation/allseasons/official` | S01E05 / `August 13, 2014`; official aired order |
| `https://tv.apple.com/us/episode/500-sq-ft-rocky-mountain-mansion/umc.cmc.3y50upj71pnyr52a5nt14kwzw` | official `S1, E5`, `Aug 13, 2014`, released `2014` |
| `https://www.aetv.com/shows/tiny-house-nation/season-5` | official catalog shell; episode identity is corroborated by the catalog adapters above |

The two research leads (`caigi.app` and `watch.sling.com`) are **not** accepted
as evidence: they are not source-owned and their displayed dates/times may be a
different feed, timezone or a stale snippet. They remain leads only.

## Gateway integration

`gateway.py` now:

- fetches the reviewed catalog (`api.tvmaze.com` + `thetvdb.com`) on a bounded
  background worker (6 h refresh, 8 h maximum age, 2 MiB per page);
- passes the existing corroborated guide rows and the catalog rows through
  `schedule_episode.resolve_current`;
- only enriches a guide row when the mapping, station, region, timezone,
  interval and catalog all agree;
- exposes `scheduleEpisode` in `/trial-status` with catalog counts, last
  observation, fault and refresh bounds;
- leaves the provider `Name`, language evidence, playback, rotation, Favorites,
  health and overlay policies unchanged.

Existing `pluto-xmltv` episode metadata remains valid. The canonical schedule
row uses the additive `catalog-schedule` provenance value; the
`audio-clue-catalog` value is a *verification* label carried in
`scheduleVerification`, not an `episodeSource`. Unknown provenance still
cannot masquerade as an episode identity in the native
`ProgrammeEvidence.checked()` path. The native allowlist in this release also
accepts `local-audio-episode-identity` so the existing audio lane is not
silently dropped; the currently installed IPA predates these additions and
will ignore them until the next reviewed native build (this release does not
build or install an IPA).

## Release integration note

This lane originally proposed its own `programme_overlay.py`,
`ProgrammeOverlayBridge`, a hidden `form.programmeOverlayVisible` gate and a
`style: "programme"` layout. Integration rejected that draft: the installed
IPA only supports `normal|headline|caption|prominent|destructive|muted` and the
existing hotpatch form bindings `form.programmeOverlay`,
`form.programmeOverlayReady` and `form.programmeOverlayFallback`. The combined
label therefore uses the existing-IPA publisher from
`docs/episode-paused-retry-20261127.md` (native `headline` binding
`programme-combined-native-v1`). The schedule discovery module, catalogue
worker and guide enrichment are unchanged by that reconciliation.

## Tests

From `Server/iptv/`:

```
python3 -m py_compile schedule_episode.py gateway.py programme_overlay.py
python3 -m unittest test_schedule_episode test_programme_overlay \
  test_schedule_evidence test_gateway_policy test_overlay_bridge -q
```

`test_programme_overlay.py` here is the integrated existing-IPA suite owned by
the paused-retry lane, not the rejected draft-ABI suite from this lane.

From the repository root:

```
python3 Scripts/embed-promoted.py Promoted/baseline.json Sources/PromotedBaseline.generated.swift
swiftc Sources/Domain.swift Sources/EngineeringDomain.swift Sources/DynamicUIDomain.swift \
  Sources/DecisionDomain.swift Sources/JellyfinAdapterDomain.swift \
  Sources/PatchPromotionDomain.swift Sources/PromotedBaseline.generated.swift \
  Tests/main.swift -o build/policy-tests
build/policy-tests
```

`test_schedule_episode.py` covers:

- real TVMaze JSON and episode-guide HTML, TVDB aired-order HTML, Apple
  episode HTML and A&E JSON-LD shapes;
- canonical agreement, official numbering winning a database difference,
  database-only numbering conflict, single-database non-corroboration;
- source-owned first-party schedule, two-schedule corroboration, one-schedule
  lead rejection;
- show-name-only channel without a current guide failing closed;
- ambiguous active feeds, missing timezone, feed/timezone conflict, stale
  observation, different provider with the same channel name;
- rerun date vs original episode year vs season/series premiere years;
- specials, advertisements, conflicting schedule/catalog numbering and
  ambiguous audio clues.

## Rollout and rollback

- Deploy only the additive Python modules and gateway wiring. The native
  change is the small allowance for existing episode provenance values; the
  rejected 22 px `programme` style and draft layout are not part of this
  release.
- No provider credential, private URL, raw transcript, page body or audio
  sample is persisted.
- No restart or device command is required from this lane.
- Roll back by reverting this lane. Existing provider guide, `pluto-xmltv`
  episode data and audio publication remain compatible.
