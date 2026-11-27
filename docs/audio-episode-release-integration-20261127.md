# Audio episode identity — corrected core release integration (2026-11-27)

> Follow-up: real public-source acceptance, optional date-only official
> identities, post-research re-probe, bounded programme-metadata publication,
> runtime opt-in and asynchronous research are documented in
> [`audio-episode-real-source-20261127.md`](audio-episode-real-source-20261127.md).
> Bounded automatic source discovery, optional registry override and
> transcript-independent prefetch/caching are documented in
> [`episode-autodiscovery-20261127.md`](episode-autodiscovery-20261127.md).

Isolated engineering lane that reconciles the completed audio episode handoff
(`f3ef187`/`943a937`) with the corrected core release `404859d`
(`Fail closed on visit ledger loss and stale probes`). This is parallel to the
core release acceptance and does not block it. No deployment, service restart,
live device command, scheduled job, queue edit, credential access, lock bypass
or IPA installation was performed. All tests use local fixtures and synthetic
public metadata; no audio or transcript fixture is stored.

## Worktree and base

- Worktree: `jealousfin-audio-episode-core-release-20261127`
- Branch: `engineering/audio-episode-core-release-20261127`
- Base: `404859d` (corrected combined release with fresh coherent device probe,
  durable physical-visit ledger and pause protection)
- Audio source: `f3ef187` plus verification commit `943a937` on
  `engineering/audio-episode-release-integration-20261127` (read-only)
- The existing audio worktree, the pause/visit worktree, `main`, production,
  the iPad, the Malay profile, global infrastructure, queues and native code were
  not edited. No device or network was used.

## What was integrated (audio only)

1. **Single-pass callback.** `language_audio.Manager` gains only `on_speech=None`
   and `_notify_speech()`. It reuses the transcript already produced by the one
   existing decode/transcribe pass. The hook fires only for an `allowed`
   language decision, a device-verified sample whose channel did not change, and
   a present transcript. It passes a sanitized job view with no PCM bytes and no
   stored transcript. The core visit/pause/`_record_result` path is untouched.
2. **Gateway wiring.** `gateway.main()` passes
   `on_speech=episode_observer_instance()` together with the existing
   `visits=language_visit_identity()`. The observer is constructed default off
   and reuses the existing `device_language_probe()`; it adds no media request,
   tap, Whisper model, worker or schedule.
3. **Programme metadata and matching.** `current_programme_context()` reduces the
   already-observed programme row to bounded public metadata. Promotion requires
   the official identity to match the current programme title and, when present,
   its numbering and episode-scoped air date. A channel with a verified episode
   identity is skipped.
4. **Bounded public-evidence adapter.** `episode_audio.PublicEvidenceResearch`
   reads only explicitly reviewed URLs from the private `episode-sources.json`
   registry (schema 1, bounded lists, `https` only, official/report host
   allowlists). It parses bounded JSON-LD and returns typed official/report
   evidence. The gateway injects a bounded page reader through the existing
   validated opener. An empty registry, unapproved host or any failure stays
   inconclusive. No registry is committed; the default is empty.
5. **Default-off status.** `episodeAudio` in `/trial-status` comes from
   `episode_audio_status()`; it still reports `enabled=false`, `active=false`,
   `searchEnabled=false`, and adds `gatewayWired=true` plus bounded budget
   diagnostics. No live research is triggered.

## Corrected-core reconciliation

The audio source was written against the earlier base `878c276`. The corrected
core release changed the device/visit contract, so the integration adapts only
the audio boundary:

- **Fresh coherent probe preserved.** The observer uses a new `episode_probe()`
  that calls the core's fresh, retried, fail-closed `device_language_probe()`
  immediately before promotion. There is no cached launch ID.
- **Durable physical-visit recovery preserved.** `episode_probe()` observes the
  existing `language_visits` ledger read-only and passes its exact `visitID`
  through. It does not create a second identity system and does not issue a
  device command. A recovery (`recover-`) identity is accepted only when it is
  the exact currently observed visit; once a departure/return issues a new
  verified visit, the old recovery job is stale and is rejected.
- **Exact visit ID.** `SessionBinding`/`make_binding` now treat the durable
  visit as authoritative. The probe must carry the same non-empty visit ID as
  the job.
- **Same-launch reload.** A stream-generation change inside the same visit is a
  reload, not a new session. The binding and its digest are visit-scoped, so a
  reload cannot republish the same episode.
- **Stale visit rejected.** A job whose visit ended (departure/return) or whose
  probe has no matching visit fails closed before research.
- **Pause/rotation safety.** `episode_probe()` refuses to publish while the
  device reports `surfPaused:true`, `surfing:false` or unknown pause/surf state.
  The core pause/pending/maintenance, health, rotation and manual exclusion
  paths are not modified.
- **No stale publication or transcript persistence.** Only bounded clues leave
  the audio callback; the transcript is dropped and never logged or written.
  Promotion outcome is returned in memory and is not persisted by this module.
- **Bounded allowlists and default off.** Clues, candidates, sources, pages,
  reports, attempts, cooldowns, TTLs, observation age and daily calls stay
  bounded. `DEFAULT_ENABLED=False`; the source registry is empty by default.

## Preserved guarantees

- Channel/programme/visit matching: exact channel ID, app launch, durable
  visit ID and binding expiry; programme title/number/air-date agreement when
  available; visit-scoped budget/session key and publication digest.
- Official corroboration: one official broadcaster archive plus at least one
  independent report with matching original air date; ads, reused promos,
  generic references, wrong series, conflicting dates and ambiguity are
  rejected. Official numbering wins over conflicting third-party numbering; a
  series premiere year is never substituted for an episode year.
- Inconclusive is allowed and is the default when evidence is absent.
- No change to the sampler, the single decode/transcribe pass, language
  decision, evidence thresholds, policy, favourites, Malay separation, overlay,
  native code, queues or global infrastructure.

## Tests and exact commands

Run from `Server/iptv/`:

```
python3 -m py_compile episode_audio.py language_audio.py gateway.py test_episode_audio.py
python3 -m unittest test_episode_audio -q
python3 -m unittest test_episode_audio test_language_visits test_language_pause \
  test_surf_maintenance test_language_auto test_spanish_auto -q
python3 -m unittest test_overlay_bridge test_release_integration test_gateway_policy \
  test_programme_research test_schedule_evidence test_opencode_discovery \
  test_policy_snapshot test_endurance_acceptance -q
python3 test_policy_observer.py
```

Results on this worktree:

- `py_compile` — OK
- `test_episode_audio` — 47 tests, OK. New coverage: audio callback freshness
  and exact visit pass-through; visit-scoped digest across a reload; same-launch
  reload promotes once and a stale visit is rejected; recovery visit promotes
  with its exact `recover-` ID and is rejected once a trusted departure/return
  supersedes it; pause/stop and unknown device state fail closed with no
  research; programme mismatch and default-off behavior.
- focused integration set — 144 tests, OK (1 skipped: Node.js unavailable)
- remaining release regressions — 98 tests, OK (1 skipped)
- `test_policy_observer.py` — all assertion groups PASS

The visit and pause suites (`test_language_visits`, `test_language_pause`,
`test_surf_maintenance`) pass unchanged, confirming the core recovery, stale
probe, pause deferral, visibility and maintenance behavior was preserved.

## Limitations

- No live device, live research or real network was exercised. The JSON-LD
  parser and reviewed registry are validated only against local fixtures.
- The host allowlist is intentionally small (CW/WWE official archives and five
  independent report families) and requires an explicit source review before
  any live trial.
- Programme titles must agree with the official series title (normalized exact
  match), so provider suffixes can cause inconclusive results instead of false
  positives.
- Recovery visits are allowed to publish only with the exact currently observed
  recovery ID. This is fail-closed for stale jobs but does not prove the missing
  pre-loss history; the language lane retains its own fail-closed dedup.
- NXT rendition identity is not proven; the retained `S20E24` / 2026-06-16
  fixture is public reference metadata for the contract only.
- The final behavior is validated structurally and by unit/integration tests,
  not rendered or run on the iPad. This lane does not deploy.

## Rollout (harness, after review)

1. Keep the feature off. Do not deploy or restart from this lane.
2. Deploy `episode_audio.py`, `language_audio.py`, `gateway.py`, the test and the
   README only after review; no native/IPA change is required.
3. Leave `episode-sources.json` absent (empty registry). To trial, add a reviewed
   private schema-1 registry with only allowlisted official/report URLs. Do not
   add live-search or model calls.
4. Confirm `/trial-status` shows `episodeAudio.enabled=false`,
   `gatewayWired=true`, empty budget and no research activity. Then, if approved,
   enable the observer in a bounded reviewed build only.
5. Verify promotion needs a live channel/launch/visit/programme match, that a
   same-visit reload does not republish, and that a stale visit is rejected.

## Rollback

- Stop passing `on_speech` to `language_audio.Manager`; the adapter then stays
  inert and no research happens. Remove `episode-sources.json` to remove all
  source candidates.
- Revert the additive commit. No state database, queue, native asset, policy,
  rotation, favourites or pause migration is involved. Language evidence, visit
  recovery, pause protection and overlay behavior are unchanged by removal.
