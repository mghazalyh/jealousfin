# Episode release integration, backup/rollback and startup health (2026-11-27)

Non-production release engineering for the isolated worktree
`jealousfin-episode-release-20261127`. Nothing here deploys, restarts a
service, issues a device command, installs a layout, or builds/installs an IPA.

## Base and integrated lanes

- Verified production base: `8a6921d` (`engineering/episode-release-20261127`).
- Paused-rotation retry + existing-IPA overlay: `b81f741`
  (`engineering/episode-paused-retry-20261127`).
- FAST schedule/canonical episode discovery: `c0f5e28`
  (`engineering/schedule-episode-discovery-20261127`).
- Already contained in the base: `42a1a5c` (audio episode autodiscovery),
  `ee9e6dd` (metadata candidate isolation), `e403798` (restart-correlated
  media continuity).
- Explicitly **not** part of this release: `engineering/server-frame-freeze-*`
  and the client-side `engineering/frame-freeze-detection-*` lane. They remain
  separate work.

## Reconciliation decisions

1. **One overlay publisher.** The paused-retry `programme_overlay.py` is kept:
   it targets the installed hotpatch fields `form.programmeOverlay`,
   `form.programmeOverlayReady`, `form.programmeOverlayFallback` and the native
   `headline` binding `programme-combined-native-v1`. The schedule lane's
   draft `ProgrammeOverlayBridge`, `form.programmeOverlayVisible` gate and
   `style: "programme"` layout were rejected because the installed IPA does
   not support that style. `overlay_bridge.STYLES` is unchanged.
2. **Schedule discovery is kept.** `schedule_episode.py`, the bounded catalogue
   worker, `guide_snapshot` enrichment and `scheduleEpisode` status are
   preserved. The combined label reads them through `programme_snapshot()`.
3. **Language retry and policy are preserved.** Episode-only retry still reuses
   the same physical visit, never counts language evidence twice, never calls
   exclusion, and never changes policy, rotation, pause or maintenance paths.
4. **Episode provenance.** Server-emitted values are `pluto-xmltv`,
   `catalog-schedule` and `local-audio-episode-identity`. The native parser
   allowlist now covers all three. The currently installed IPA predates the
   last two and will ignore them until a reviewed native build; the server-side
   combined overlay is unaffected.

## File manifest

Review every path before packaging. This list is the exact delta from `8a6921d`
and is asserted by `test_release_readiness.ReleaseManifestTests`.

```
Server/iptv/episode_audio.py
Server/iptv/fixtures/programme-language-existing-ipa-20261127.json
Server/iptv/gateway.py
Server/iptv/language_audio.py
Server/iptv/overlay_bridge.py
Server/iptv/programme_overlay.py
Server/iptv/schedule_episode.py
Server/iptv/spanish_audio.py
Server/iptv/test_deployment_continuity.py
Server/iptv/test_episode_audio.py
Server/iptv/test_gateway_policy.py
Server/iptv/test_language_pause.py
Server/iptv/test_overlay_bridge.py
Server/iptv/test_programme_overlay.py
Server/iptv/test_release_integration.py
Server/iptv/test_release_readiness.py
Server/iptv/test_schedule_episode.py
Sources/AppModel.swift
Sources/Domain.swift
Sources/FullscreenSurfPlayer.swift
Sources/JealousfinApp.swift
Sources/Persistence.swift
Sources/PlayerSurface.swift
Sources/Runtime.swift
Tests/RuntimeSmoke.swift
Tests/main.swift
docs/episode-paused-retry-20261127.md
docs/episode-release-20261127.md
docs/schedule-episode-discovery-20261127.md
```

No generated artifact, credential, registry, transcript, page body, audio
sample, device token, provider URL or policy database is part of the manifest.
`episode-sources.json` remains operator-supplied and empty by default.

## Backup procedure (harness, not executed here)

1. Record the deployed revision and the state root
   (`JEALOUSFIN_STATE_DIR`), and take a dated copy of the whole gateway
   directory and state root.
2. Copy `policy.sqlite3`, `deployment-generation.json`,
   `media-handles.jsonl`, `media-stale.jsonl`, `deployment-ignored.jsonl`,
   `language-*` state, `overlay` state, `maintenance-window.json` and the
   currently installed UI layout asset.
3. Preserve the previous `Server/iptv/*.py` files and the previous native
   `Sources/Domain.swift` / `Tests/main.swift` even though this release does
   not build them.
4. Verify the backup is readable and that `policy.sqlite3` passes an integrity
   check before touching the live copy.
5. Keep the backup outside the worktree and outside the gateway state root.

## Rollback procedure (harness, not executed here)

1. Stop only the gateway process under the normal supervised procedure; never
   stop, reset or reconfigure the device.
2. Restore the backed-up `Server/iptv` tree (or revert the release commit) and
   the backed-up state files, preserving the pre-release policy database.
3. Restore the previously installed UI layout asset only if this release had
   installed one; this release installs none.
4. Start the gateway under the same supervisor and re-run the startup health
   criteria below.
5. If the combined overlay was written, the installed publisher's safe state is
   Ready=false, Fallback=true; confirm both fields before declaring rollback
   complete.

## Startup health criteria

Declare the gateway healthy only when **all** of these hold:

- `python3 -m py_compile Server/iptv/*.py` succeeds.
- Policy storage is seeded and `/trial-status` reports `policyReady=true` with
  `policyChannels` equal to the visible channel count.
- `configurationFault` is empty; the expected configuration revision is
  active.
- `deployment-generation.json` is schema 1, has a 32-hex current generation and
  a finite `startedAt`; `media-handles.jsonl` is readable.
- `episodeAudio.enabled` is false unless explicitly opted in; `probe` counters
  exist; `publication`/`sourceDiscovery` report no unbounded growth.
- `programmeOverlay.layoutRevision` is
  `programme-language-existing-ipa-20261127` and `nativeAbi` names the existing
  headline binding, or `configured=false` because no device selector exists.
- `scheduleEpisode.revision` is `schedule-episode-20261127`; catalogue fault is
  empty or bounded, and no mapping is treated as a static current episode.
- Within the post-restart window, only
  `NATIVE_POLICY_DEPLOYMENT_IGNORED` entries are accepted for an after-startup
  stall, and only for the same channel with at least two stale-handle 404s.
  `Playback stalled`, HTTP/network errors and every other real failure must
  still be recorded as `NATIVE_POLICY_failure`.
- No new channel penalty, ban, hiatus or rotation command was produced by
  the release itself. Deployment issues no device command; at runtime the
  combined overlay uses only the existing authorized `ui_set_value` transport.

## Test commands and results

From `Server/iptv/`:

```
python3 -m py_compile *.py
python3 -m unittest test_episode_audio test_schedule_episode test_programme_overlay \
  test_schedule_evidence test_gateway_policy test_overlay_bridge \
  test_release_integration test_release_readiness test_deployment_continuity -q
python3 -m unittest discover -s . -p 'test_*.py' -q
```

Verified on the integrated tree (2026-11-27):

- `py_compile` of every `Server/iptv/*.py` — OK.
- Focused integrated set (`test_episode_audio`, `test_schedule_episode`,
  `test_programme_overlay`, `test_schedule_evidence`, `test_gateway_policy`,
  `test_overlay_bridge`, `test_release_integration`, `test_release_readiness`,
  `test_deployment_continuity`) — 241 tests, OK.
- `test_release_readiness` alone — 19 tests, OK (manifest, installed ABI,
  provenance, startup health, backup/restore, restart-vs-real-failure matrix).
- Full `unittest discover` — 395 tests, OK (2 skipped). Baseline `8a6921d`
  was 312 tests, OK (2 skipped).
- The native `Tests/main.swift` assertions are updated for provenance but are
  not compiled here (that would be a native build, which this task excludes).

## Blockers and limits

- The installed IPA predates `catalog-schedule` and
  `local-audio-episode-identity`; those episode rows are dropped by the
  installed client until a reviewed rebuild. The combined server-side overlay
  and `pluto-xmltv` behaviour are unaffected.
- The schedule catalogue has one reviewed mapping
  (`nltv:us:TinyHouseNation.us`); everything else fails closed.
- No live device, live catalogue, network or production database was used.
- The repository fixture documents the reviewed installed bindings, but
  this worktree cannot prove the device's current layout. Before enabling
  the combined overlay, the operator must read `get_ui_layout` and run
  `programme_overlay.validate_existing_ipa_layout`. If the fields are
  absent every combined write fails closed (Ready stays false, Fallback
  true); `programmeOverlay.configured` alone is not layout proof.
- The fixture's `headline` style decorates the node title; the dynamic
  value itself renders with the app's subheadline font until a reviewed
  native rebuild. This is cosmetic and does not affect the form fields.
- The audio lane publishes `episodeYear`; the installed client and
  `programme_item()` consume `seasonYear`, so an audio-derived episode
  year is visible in the combined overlay but not in the client episode
  title until the native/source reconciliation is rebuilt.
- Server frame-freeze and client frame-freeze work are out of scope and must
  stay on their own branches.
