# Audio episode identity — real public-source acceptance and bounded publication (2026-11-27)

Isolated follow-up lane on top of `3f94486` (production release, language
observation enabled with enforcement OFF). It fixes the observed end-to-end
failure: the strict JSON-LD parser found zero official rows on the real WWE
archive/results pages and zero rows on WrestlingInc/PWInsider, and the gateway
observer stayed hard-coded off.

This lane does **not** modify the deployed files or the iPad, does not restart a
service, and does not use a real device, microphone or media capture. All
fixtures are small public-source shapes; no transcript or audio is stored.

## Worktree and base

- Worktree: `jealousfin-audio-episode-real-source-20261127`
- Branch: `engineering/audio-episode-real-source-20261127`
- Base: `3f94486`
- Related, unchanged: `main`, production, the iPad, the Malay profile, overlay,
  language V4 thresholds, pause protection, visit recovery and rotation.

## Live acceptance (2026-11-27)

The parser was run live through the same bounded functions the gateway uses
(`python3 validate_episode_sources.py --json`):

| Source | HTTP | Parsed result |
| --- | --- | --- |
| `https://www.wwe.com/shows/wwenxt/2025-11-04` | 200 | 1 official row, series `WWE NXT`, air date `2025-11-04`, no numbering |
| `https://www.wwe.com/shows/wwenxt/2025-11-04/results` | 200 | 1 official row, series `WWE NXT`, air date `2025-11-04`, no numbering |
| WrestlingInc NXT 11/4 results article | 200 | 1 report, show date `2025-11-04`, published `2025-11-05` |
| PWInsider 11/4 NXT report | 200 | 1 report, show date `2025-11-04`, published `2025-11-04` |
| F4W NXT results page | 403 | intentionally not bypassed; no rows |

The two official rows are the same episode. With an independent report on the
same date and matching transcript clues, corroboration accepts a **date-only**
identity. No season or episode number is invented. When the official archive
does publish numbering, the official number still wins over differing
third-party numbering.

## What changed

1. **Real-source parsing.** `parse_official_page` and `parse_report_page` keep
   the bounded JSON-LD path and add ordinary HTML parsing:
   - OpenGraph/`<title>`/meta description and a bounded visible-text snippet;
   - explicit ISO, month-name and `M/D/YYYY` dates only;
   - an official URL date (`/shows/<series>/<YYYY-MM-DD>`) when present;
   - show date is kept distinct from publication date;
   - no date inference from a publish time, "last Tuesday" or similar.
2. **Optional official numbering.** `OfficialEpisode`/`ContentIdentity` allow
   `season_number`/`episode_number` to be `None`. `corroborate_episode` still
   requires the official archive plus an independent report on the same air
   date with matching clues. Existing provider numbering is not contradicted by
   an archive that omits it, and third-party numbers are never promoted to
   official numbers.
3. **Post-research re-probe.** `EpisodeObserver` used the pre-research probe to
   promote. It now re-probes after the network research and requires:
   - the same binding (unexpired, same channel/launch and exact physical visit);
   - a coherent programme context (same series and, when present, the same
     numbering/air date);
   - no newly verified episode identity in the meantime.
4. **Actual bounded publication.** `PublicationStore` publishes only public
   identity (series/date, optional official numbers/title, confidence,
   verification and a visit digest) into the existing programme metadata path
   (`programme_snapshot` → Jellyfin programme item → client `Name`). It:
   - expires after 1800 seconds and is bounded to 64 channels;
   - never overwrites a provider/episode-guide title, verified numbering or a
     corroborated schedule episode;
   - never stores a transcript, spoken clues, page bodies or raw launch/stream
     identifiers.
5. **Runtime opt-in, no restart.** The gateway observer is constructed off and
   `episodeAudioEnabled` was added to the reloadable gateway runtime config.
   `episode_speech_callback` re-reads the configuration and toggles the existing
   observer instance per handoff. A reviewed configuration revision with
   `episodeAudioEnabled: true` enables the lane without restarting. The module
   default remains `DEFAULT_ENABLED = False`.
6. **Research is off the language worker.** `_notify_speech` now calls
   `EpisodeObserver.submit`, which extracts at most eight bounded clues and
   returns immediately. Probe, network fetch, post-research re-probe and
   publication run on a separate daemon worker with a bounded queue (`8`) and a
   `20 s` research deadline (inside the 30 s observation-age bound). Language
   counting and `_record_result` never wait
   for research. The transcript is dropped after clue extraction and is never
   queued, logged or persisted.
7. **Promotion return is used.** `_notify_speech` inspects a synchronous
   promotion return and logs a bounded outcome (`EPISODE_IDENTITY_PROMOTED` /
   `..._SKIPPED reason=...`), never the transcript or clues. The asynchronous
   gateway path logs `EPISODE_IDENTITY` when publication succeeds.

## Active viewing reconciliation (120 s vs 60 s rotation)

The metadata-research lane keeps its deliberate `120 s` active-viewing
threshold. A typical 60-second surf rotation can never satisfy it, so the
episode lane previously never ran.

The audio episode lane now uses a separate, explicit sample-backed floor:
a completed language sample of at least `40 s` of verified progressing playback
lowers only this lane's requirement to `max(40 s, sampleSeconds)` (capped at the
120 s default). A 55-second verified sample in a 60-second visit is eligible.
No rotation value, language confidence, window count, probability, pause,
overlay or exclusion threshold was changed. A sample shorter than `40 s`, a
non-progressing session, a paused/stopped visit, a changed channel or a changed
programme still fails closed.

## Supported sources

- Official archives: `wwe.com` / `www.wwe.com`, `cwtv.com` / `www.cwtv.com`.
- Independent reports: `f4wonline.com`, `pwinsider.com`, `wrestlinginc.com`,
  `sescoops.com`, `wrestlingobserver.com` (and `www.` forms).
- Only URLs explicitly listed in the private, schema-1 `episode-sources.json`
  registry are fetched. The registry is absent by default (empty). See
  `Server/iptv/episode-sources.example.json`.
- Because reviewed registry URLs are date-specific, an operator must add the
  current episode's official archive/results URL and corroborating report URLs
  before a live trial. No live search or model call is performed.
- A blocked source (for example F4W `403`) stays blocked; it is reported as an
  error and never retried around, proxied or scraped through another route.

## Tests and results

Run from `Server/iptv/`:

```
python3 -m py_compile episode_audio.py language_audio.py gateway.py test_episode_audio.py validate_episode_sources.py
python3 -m unittest test_episode_audio -q
python3 -m unittest test_episode_audio test_language_visits test_language_pause \
  test_surf_maintenance test_language_auto test_spanish_auto -q
python3 -m unittest test_overlay_bridge test_release_integration test_gateway_policy \
  test_programme_research test_schedule_evidence test_opencode_discovery \
  test_policy_snapshot test_endurance_acceptance -q
python3 test_policy_observer.py
python3 validate_episode_sources.py --json
```

Results on this worktree:

- `test_episode_audio`: 64 tests, OK. New coverage includes the real WWE
  archive/results shapes, WrestlingInc JSON-LD (show date vs publish date),
  PWInsider HTML without JSON-LD, date-only corroboration, no invented numbers,
  publication expiry/no-overwrite/no-transcript, post-research visit/programme
  re-probe, asynchronous submit/publication and sample-backed active viewing.
- Focused language/visit/pause set: 161 tests, OK (1 skipped).
- Remaining release regressions: 98 tests, OK (1 skipped).
- `test_policy_observer.py`: all assertion groups PASS.
- Live validation: 2 official rows + 2 report rows from the URLs above; F4W
  reports HTTP 403 without bypass.

## Limits

- No live channel/launch/visit or real transcript was exercised. Publication is
  proven with local fixtures and the gateway's existing read-only probe paths.
- A date-only identity does not prove episode numbering. It is published only
  when an official archive and an independent same-date report both corroborate
  the transcript clues.
- The registry is a reviewed, static, date-specific allowlist. There is no live
  search, crawler or model call.
- Native title rendering is unchanged. The gateway's programme `Name` carries
  the published episode text; no IPA/native change is required or included.
- Research is bounded but still depends on the reviewed pages being reachable.
  A blocked or slow source yields inconclusive evidence, never a guess.

## Rollback

- Set `episodeAudioEnabled: false` in the gateway runtime configuration (new
  revision) or remove the key. The observer disables on the next handoff without
  a restart.
- Remove `episode-sources.json` to remove all source candidates.
- Delete the audio publication from the running process by restarting the
  gateway; publications are in memory and expire on their own.
- Revert this additive branch. No policy, rotation, overlay, language V4,
  pause/visit, health, favourites, queue, native asset or device state migration
  is involved.
