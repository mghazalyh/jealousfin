# Bounded automatic episode-source discovery (2026-11-27)

Isolated follow-up lane on `4201e1e`. It removes the operational requirement
to add a date-specific URL to the private `episode-sources.json` registry before
every trial. Runtime remains default OFF; no deployment, service restart, live
device command, audio capture or production/iPad/Malay change was performed.

## Behaviour

- **Registry is an override, not a requirement.** If the reviewed schema-1
  registry has an entry for the series, only that entry is used. Without an
  entry, discovery is automatic. An explicit empty entry intentionally
  disables automatic discovery for that series.
- **Reviewed official roots.** A small non-date-specific map
  (`DEFAULT_OFFICIAL_HINTS`) covers the reviewed WWE NXT, Raw and SmackDown
  archive roots. A known original air date builds the dated archive and
  `/results` URLs. An unknown date scans the root page for bounded dated links
  (at most 64 links, 4 episode pages) on the same approved site and under the
  same archive path. When clue phrases match an index link's visible text,
  those bounded pages are preferred, which helps an older rerun without turning
  discovery into an open crawler.
- **Optional OpenCode lead provider.** When available and runtime-enabled, the
  existing bounded OpenCode runner may search the public web for official and
  independent-report URLs. The prompt contains the series, optional date and
  bounded clue phrases, not a transcript. The answer is normalized to HTTPS
  URLs on the existing allowlists only. It is a lead list, never evidence:
  the gateway must fetch, parse and corroborate the actual page.
- **Existing verification is unchanged.** Promotion still requires one official
  broadcaster archive plus an independent report on the same original air date,
  at least two actual distinctive clue matches, a coherent post-research
  physical visit and programme context, and the unexpired exact visit binding.
  Date-only official identities remain valid and never invent season/episode
  numbers. Official numbering still wins over conflicting third-party numbers.
- **Historical reruns / unknown dates.** A historical date uses the direct
  dated official URL. With no date, only bounded recent official index links are
  scanned; a rerun of an older episode relies on the provider's clue-assisted
  lead, and fails closed when it is absent. No date is inferred from a publish
  time or from the current clock.
- **Failures stay inconclusive.** Missing registry, unavailable provider,
  timeout, oversized/invalid page, blocked source, unapproved redirect or absent
  corroboration yields no promotion. A `401`/`403`/`451` response marks that host
  blocked for the bounded read; remaining URLs on that host are skipped and no
  proxy, archive or alternate route is used.

## Caching and the existing workers

- Candidate cache: in-memory, 1800 s TTL, at most 64 keys, at most 8 candidates
  per family. Negative results use a 300 s TTL.
- Official-index cache: in-memory, same TTL/key bound, so clue-specific lookups
  do not repeatedly fetch the same archive root.
- Parsed-evidence cache: in-memory, keyed by a digest of series/date/candidate
  URLs, 1800 s positive TTL and 300 s negative TTL, at most 64 entries. Raw
  pages and transcripts are never stored.
- A separate daemon prefetch worker runs every 15 s only while
  `episodeAudioEnabled` is true and the observer is enabled. It warms public
  official/report candidates and evidence for at most four active channels. It
  performs no audio work, language decision, rotation change or playback
  command, and is independent of any transcript.
- Language counting still never waits for research. The existing bounded
  observer worker (20 s research deadline) is unchanged, as is the 60 s surf
  rotation and every language/confidence/pause/overlay policy.

## URL, SSRF and redirect policy

- Only `https`, the default port, no credentials, no fragment, an approved DNS
  host and the existing official/report allowlists are accepted. IP literals
  and malformed labels are rejected before fetch; the gateway's existing
  `validate()` still resolves DNS and rejects non-global addresses.
- Automatic discovery never widens the host allowlists.
- Redirects must remain HTTPS and on the same approved site (ignoring only the
  `www.` alias). A redirect to another host fails closed. A blocked source stays
  blocked; there is no proxy or alternate-route fallback.
- Total research remains bounded at 20 s, each page read at 8 s / 2 MiB, and
  the optional provider timeout is `min(configured research timeout, 20 s)`.

## Live validation (2026-11-27)

The existing bounded parsers were run against real reviewed pages:

| Source | HTTP | Result |
| --- | --- | --- |
| `https://www.wwe.com/shows/wwenxt/2025-11-04` | 200 | 1 official row, 2025-11-04, no invented numbering |
| `https://www.wwe.com/shows/wwenxt/2025-11-04/results` | 200 | 1 official row, 2025-11-04, no invented numbering |
| WrestlingInc 11/4 results article | 200 | 1 report, show date 2025-11-04, published 2025-11-05 |
| PWInsider 11/4 NXT report | 200 | 1 report, show date 2025-11-04, published 2025-11-04 |

The automatic official index path was also exercised live without a registry:
`https://www.wwe.com/shows/wwenxt` yielded four bounded dated candidates
(`2026-11-24` archive/results, `2026-11-17`, `2026-11-10`) and no report URLs
without a provider. F4W `403` remains blocked and is not bypassed.

## Tests

From `Server/iptv/`:

```
python3 -m py_compile episode_audio.py gateway.py opencode_discovery.py \
  test_episode_autodiscovery.py
python3 -m unittest test_episode_audio test_episode_autodiscovery -q
python3 -m unittest test_episode_audio test_episode_autodiscovery test_language_visits \
  test_language_pause test_surf_maintenance test_language_auto test_spanish_auto \
  test_overlay_bridge test_release_integration test_gateway_policy \
  test_programme_research test_schedule_evidence test_opencode_discovery \
  test_policy_snapshot test_endurance_acceptance -q
python3 test_policy_observer.py
```

Coverage added in `test_episode_autodiscovery.py`:

- registry override suppresses automatic discovery and provider calls;
- known-date direct official URLs plus validated provider leads;
- unknown-date bounded official index scanning;
- bounded candidate/cache expiry;
- transcript-independent prefetch and cache reuse;
- blocked-host no-retry/no-bypass while an independent host is still usable;
- date-only identity with no invented numbering;
- historical date and unknown-date fail-closed behavior;
- OpenCode answer normalization to the existing allowlists and lead-only use.

## Limitations

- No live device, transcript or audio sample was exercised. Publication is
  proved with fixtures and the existing probe contract only.
- Automatic independent-report discovery normally depends on the optional
  OpenCode provider. If it is unavailable or times out, official archive pages
  may still be found but a second independent report is not created, so the
  result is inconclusive and nothing is published.
- The reviewed official-root map is intentionally small. Unknown series without
  a registry entry or provider lead are not crawled.
- Without a known original air date only a few recent official index links are
  scanned. An older rerun needs a provider lead or a registry override.
- Date-only identity does not prove episode numbering. It is published only
  after official plus independent same-date corroboration and matching clues.
- The candidate/evidence caches are process-memory only. A restart drops them;
  no page, transcript or clue text is persisted.
- A provider answer can be slow; it is bounded and runs off the language worker,
  but it may still consume the observer's 20 s research budget and then fail
  closed.
- Actual promotion, title rendering and blocked-source behavior on the iPad were
  not run in this lane.

## Rollout / rollback

- Keep `episodeAudioEnabled` false. No deployment or restart is required from
  this lane.
- On review, deploy the additive Python files only. No IPA/native change is
  needed; the existing programme `Name` path carries a publication.
- To override a series, add its schema-1 registry entry. To disable automatic
  discovery for a series, add an explicit empty entry.
- Roll back by reverting this additive commit, by disabling the lane, or by
  removing the provider injection. Removing all cache entries requires only a
  process restart; none are persisted.
