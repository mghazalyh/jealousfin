# Pause protection and physical-visit identity integration (2026-11-27)

This is the isolated pause/visit integration lane on top of release `878c276`.
It preserves the v4 evidence/overlay/programme release and integrates reviewed
deltas from the Spanish hotpatch lane. No production deployment, service
restart, iPad command, queue change, IPA install or live UI mutation was
performed by this session. The deployment harness owns rollout.

## Integrated deltas

- Pause protection from `a90f3b6` (`Preserve user pause and recheck language on
  every new visit`), adapted to the v4 evidence store:
  - new `Server/iptv/language_pending.py`;
  - new `Server/iptv/surf_maintenance.py`;
  - read-only `device_rotation_state()` and pause-aware `language_visible()`;
  - exclusive-language evidence is deferred to a bounded pending store while
    the device is paused, stopped or its state is unreadable, and applied only
    after a fresh read-only status shows active rotation without a user pause;
  - allowed/unauthorized evidence is recorded normally because it never changes
    membership, rotation, playback or catalogue visibility;
  - no code path calls `pause_surf`, `resume_surf`, `start_surf`, `stop_surf`,
    `next_channel`, `previous_channel` or `reload_current_stream`.
- Model path preference from `fc05474`: prefer the official whisper.cpp
  `~/.local/share/whisper.cpp/models/ggml-base.bin`, then the verified small
  model, then the private config/root fallbacks. The existing production
  gateway already carries this delta; it is preserved here.

The v3 one-detection exclusion logic was **not** cherry-picked over v4. The
store remains `language-exclusions-v4`, observation and enforcement remain
separate and default off, and the default threshold remains 25 per language.

## Main blocker: physical visit identity

The release `878c276` sampler built `visitID` from the sample start time and
then added a 600-second same-launch dedup window. That produced two opposite
failures:

- a continuous view longer than 600 seconds produced a second count;
- a genuine departure and return inside 600 seconds was suppressed;
- stream generation alone could not distinguish a reload from a real revisit.

### Durable ledger

`Server/iptv/language_visits.py` maintains a bounded, atomic
`language-visits.json` (mode 0600) keyed by app `launchID`. It consumes only
the read-only values the sampler already probes:

- `launchID` from `get_status`;
- `channelID` and `streamGeneration` from `get_player_snapshot`.

A physical visit is:

- the first observation of a channel in a launch;
- the same channel while the device stays on it, including reloads, retries,
  re-seeks and stream-generation changes;
- a **new** visit only when the device was observed on a different channel and
  then observed back on the original channel (a verified departure and
  return).

A stream-generation change by itself is never a new visit. Time is never used
as identity. If the launch or channel cannot be verified, the ledger returns
`verified:false`; the evidence store then fails closed and suppresses
additional same-launch automatic events instead of inventing a new visit.
Legacy migration events are the only historical evidence allowed a bounded
time-window replay guard.

The actual native sampler/telemetry/rotation ledger was inspected. The native
`AppModel.stateSnapshot()` exposes `launchID`, `currentChannelID`, `surfing`,
`surfPaused`, `rotationCycle` and `streamGeneration`; the player snapshot
exposes `streamGeneration`. The native `surf-rotation.json` ledger and uploaded
telemetry events are not exposed through the authorized gateway transport, so
they are not used as a hidden side channel. The visit ledger is derived only
from the two read-only commands the gateway already uses, and it never writes
native state.

### Exactly-once automatic evidence

`language_exclusions.record_evidence` now treats the physical visit as the
dedup key:

- one physical visit contributes at most one conclusive automatic event, even
  if the detected language changes during that visit;
- two distinct verified visit identities both count, even inside the old
  600-second window;
- reloads, retries and repeated samples in the same visit are duplicates;
- manual audit events (including the Mistaken English correction) never
  suppress a later automatic visit;
- unverified identities do not create new counts from elapsed time.

The sampler also refuses to start a new sample for a visit that already has a
conclusive automatic result. If the first sample was inconclusive, a bounded
retry is allowed after the existing finalize cooldown. Stale inference jobs are
rejected by re-probing the device and comparing the durable visit identity;
a reload inside the same visit is accepted, but a job from a visit that ended
after a departure is discarded.

## Pause and maintenance safety

- `language_auto_commit(..., respect_pause=True)` defers an excluded-language
  result while the device reports `surfPaused:true`, `surfing:false` or an
  unreadable/uncertain state. The pending record keeps the exact evidence,
  including the physical `visitID` when available.
- `process_language_pending_once()` applies pending evidence only after a fresh
  read-only status shows active rotation and no pause. It uses the v4
  threshold/enforcement flags and the same visit identity, so a pending sample
  cannot double count its visit.
- `language_visible()` keeps an automatic exclusion visible while the user is
  paused on that exact channel; manual exclusions are still hidden. Unknown
  device state keeps the automatic entry visible (fail safe for the user's
  current view).
- `surf_maintenance.reconcile()` reports the state to preserve and explicitly
  forbids every state-changing command. It never resumes a pre-existing pause
  or a pause made during maintenance.
- Manual Spanish, manual "not Spanish", Favorites, Malay, health/hiatus/ban and
  rotation records are untouched.

## Staged layout asset

`Examples/three-panel-night-fit-language-evidence-20261127.json` is the
reconciled latest compact night bridge layout from the release run directory:

- night presentation preserved (`fullscreenControlTheme:"night"`, 0.35 panel
  opacity, 4pt padding, 3pt spacing, 44pt buttons, 24pt icons, fit-content);
- English/Malay source switcher preserved;
- manual Spanish add/remove actions preserved (including their existing
  user-initiated advance behavior);
- Mistaken English correction (`schemaVersion:2`, `mistaken_english`) verified
  present and free of any playback command;
- hidden editable `form.languageOverlay` input and native text label preserved;
- no `fetch(` or `XMLHttpRequest`.

The earlier `Examples/language-evidence-overlay-20261127.json` remains for
rollback and existing bridge tests. The final layout is a separate staged UI
revision; no IPA/native rebuild is involved.

## Tests run locally

From `Server/iptv/`, 182 tests passed:

- existing release regressions: `test_language_auto`, `test_overlay_bridge`,
  `test_release_integration`, `test_spanish_auto`, `test_gateway_policy`,
  `test_programme_research`, `test_schedule_evidence`,
  `test_opencode_discovery`, `test_episode_audio`, `test_policy_snapshot`,
  `test_endurance_acceptance`;
- new `test_language_visits`: first observation, reload, verified departure and
  return inside cooldown, new launch, unverified fail-closed identity, restart
  durability, bounded pruning, exactly one count per visit across language
  changes, distinct verified visits inside the old window, manual correction
  not suppressing a later visit, stale-job rejection, long continuous visit,
  inconclusive retry cooldown, and v3 migration idempotence on an isolated
  fixture;
- new `test_language_pause`: paused deferral then active apply, pending visit
  identity not double counted, observation-disabled pending, uncertain state,
  manual allow clears pending, visibility while paused, unknown state, and a
  proof that only `get_status`/`get_player_snapshot` are used;
- new `test_surf_maintenance`: pre-existing pause, user pause/resume during
  maintenance, duplicate user events, stale launch/stream and uncertain state;
- new staged-layout test: correction action, source switcher, hidden form key,
  night presentation and no browser fetch.

Native Python release/promotion/filter tests were run separately. Native Swift
source was not modified.

## Remaining risks

- A very fast channel switch that is never observed by a probe (shorter than the
  sampler's probe cadence) cannot be proven as a revisited visit and will be
  treated as the same visit. This is intentional fail-closed behavior.
- If the gateway loses `language-visits.json` while the event store survives,
  a same-launch continuous visit is treated as unverified and suppressed; it
  will not double count, but it may undercount until the next launch.
- Transitional upgrade case: pre-integration events carry sample-start
  identities that cannot be proven against the new ledger. If a channel
  already has an automatic event in the current app launch, new verified
  visits on that channel may be suppressed until the app starts a new launch.
  This is intentional fail-closed behavior. For clean post-rollout counting,
  have the user relaunch the app during the rollout window; do not clear the
  evidence audit to force a recount.
- Pending excluded evidence for a channel that is later manually corrected is
  cleared without a mark; the manual decision wins.
- The final layout is validated structurally, not rendered on a live iPad in
  this session. The harness must still verify the deployed ABI rendering,
  button placement and the absence of clipping.
- The installed native `stateSnapshot` still has no language evidence object;
  the overlay continues to depend on the single hidden form field and the
  authorized `ui_set_value` transport.
- Audio-assisted episode evidence remains deferred and is not part of this
  release gate.

## Rollout (harness only)

1. Keep observation and enforcement off. Back up the private state directory.
2. Deploy the gateway package. Do **not** restart or issue device commands from
   this session.
3. Stage-copy `language-exclusions.json`, `language-auto-control.json`,
   `language-exclusions-pending.json` (if present) and `language-visits.json`
   (if present). Confirm v3 -> v4 migration there: automatic entries become one
   event each, manual rows/decisions remain, policy rows are unchanged, and a
   second migration is byte-stable.
4. Install `Examples/three-panel-night-fit-language-evidence-20261127.json`
   through the existing `install_ui_layout` path and confirm `get_ui_layout`
   reports `three-panel-night-fit-language-evidence-20261127`.
5. On the live iPad (authorized LAN only), verify the English/Malay switcher,
   Spanish add/remove and Mistaken English correction still render; the hidden
   `languageOverlay` field is not visible; a channel change shows the new
   channel with "waiting for language evidence"; a manual pause stays paused
   with rotation/countdown unchanged; no play/pause/stop/next/previous/resume/
   reload command is emitted by the bridge.
6. Enable observation only (`enabled:true`, `enforcementEnabled:false`) through
   `/language-auto`; inspect counts and the bounded evidence window. Leave
   enforcement off until counts, deduplication and corrections are reviewed.
7. If approved, enable enforcement separately with `threshold:25`. Turning it
   off is immediate; evidence and manual decisions are retained.

## Rollback

- Restore the prior UI revision with `rollback_ui_layout` or
  `restore_patch_version`; no native change is required.
- Set `enabled:false` and `enforcementEnabled:false` through `/language-auto`
  or atomic control-file replacement. This stops new sampling and makes
  automatic membership dormant; manual entries remain.
- Revert the integration commits if gateway code must be withdrawn. Do not
  resume a user pause, restart rotation, delete manual exclusions or rewrite
  the evidence audit to roll back.
- `language-visits.json` and `language-exclusions-pending.json` are private,
  bounded state; leaving them in place is safe, and deleting them fails closed
  rather than opening new counts.
