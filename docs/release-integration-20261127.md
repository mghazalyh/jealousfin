# Combined release integration (2026-11-27)

This is the isolated release-integration lane. It combines the evidence-first
language store, the authorized overlay bridge and programme research on one
local branch. No production deployment, service restart, live device command,
queue mutation, push or native/IPA change was performed by this session.

## Branch and commits

- Base: `8abac98` (`engineering/language-evidence-parallel-20261127`, 90 tests)
- Overlay bridge: `5e43411` -> release commit `7c66f95`
- Structured schedule evidence / verified episode identity: `b91ac19` ->
  release commit `55501f1`
- Active-viewing research priority / deferred audio evidence: `9838d32` ->
  release commit `aabee4d`
- Research integration documentation: `3997d2a` -> release commit `7f3c655`
- Gateway/overlay wiring and release fixtures: this documentation commit

`b91ac19`, `9838d32` and `3997d2a` replayed cleanly on the integrated tree. The
only content conflict was the overlay asset added by both evidence and bridge
lanes. It was resolved in favour of the bridge revision
(`language-evidence-overlay-bridge-20261127`), which contains the deployed
English/Malay switcher, the Spanish mark/unmark actions and the Mistaken
English correction.

## Preserved behaviour

The integrated gateway still preserves:

- the existing English/Malay source switcher web component and manual Spanish
  add/remove marker path (`schemaVersion:1`, `spanishRequest`);
- the Mistaken English correction (`schemaVersion:2`, `mistaken_english`) with
  backend nonce replay protection and at-most-one retraction per stored event;
- Favorites, pause protection, health/hiatus/ban/rotation and all existing
  policy routes;
- the authorized device command transport (`/command`, Command
  `schemaVersion:1`, unique `id`, `action`, `arguments`) and its exact
  `status:"succeeded"` receipt;
- authentication. The overlay transport never disables auth, reads a new
  credential source, logs a token or selects a host from HTTP input.

## Overlay bridge wiring

`Server/iptv/overlay_bridge.py` is unchanged in its own contract. The gateway
adds a bounded publisher around it:

- one daemon worker, started with the existing American worker set and stopped
  with the gateway; there is no periodic timer, new scheduler, playback or
  rotation command;
- lifecycle jobs only: `/LiveTv/Channels` and authenticated `PlaybackInfo`
  enqueue a channel refresh; a recorded evidence commit enqueues one evidence
  update; an accepted correction enqueues one corrected label; `/trial-status`
  may trigger a pending retry pump;
- before writing, the worker calls the existing `device_language_probe()`
  (`get_player_snapshot` + `get_status` launch identity). Evidence for another
  channel or another stream generation is suppressed. If the probe is
  unavailable, nothing is guessed;
- transport attempts are bounded and fail safe. A missing selector config, an
  auth-required device without a configured token, or a transport error leaves
  `overlay.fault` populated and a `staleLabel` flag where applicable; a later
  lifecycle event retries;
- the only device action is `ui_set_value` with
  `arguments:{key:"form.languageOverlay",value:"..."}`. The rendered value
  always includes the observation timestamp, the channel name, language,
  count/threshold, phase and confidence when available.

The installed ABI supports the layout: `DynamicUIDomain.formNode` finds the
hidden editable `input` for `form.languageOverlay`; `DynamicUIViews` renders the
`text` node from `model.dynamicValue("form.languageOverlay")`; a non-editable
node may share a `form.` key with the single editable node. No new IPA is
required.

## Evidence, threshold and migration

The store remains `language-exclusions-v4`:

- observation defaults off until `language-auto-control.json` schema 2 exists;
- enforcement is a separate flag and defaults off; the default threshold is 25
  confirmed visits of the same unwanted language;
- conclusive visits are counted once per independent physical visit. The
  existing sampler dedup plus the 600-second same-launch evidence guard prevent
  reloads from adding another count; a new launch or later visit does count;
- manual Spanish and manual "not Spanish" decisions always win and are never
  retracted by automatic logic;
- enforcement never creates a policy failure, ban or hiatus and never touches
  rotation or playback history;
- a v3 file is migrated once and idempotently: each of the three previous
  automatic entries becomes exactly one evidence event, the automatic
  membership rows are removed, and the channels become eligible again subject
  to ordinary health/rotation policy. Manual entries/decisions remain. A v4
  file is returned unchanged. The legacy `spanish-classification.json` audit
  file is not deleted.

The optional audio-assisted episode work (`episode_audio.py`) remains default
off and is explicitly outside this release gate.

## Tests run locally

From `Server/iptv/` (all passed):

- `test_language_auto.py` (29), `test_overlay_bridge.py` (15),
  `test_release_integration.py` (7), `test_spanish_auto.py` (15),
  `test_gateway_policy.py` (33), `test_programme_research.py` (11),
  `test_schedule_evidence.py` (14), `test_opencode_discovery.py` (9),
  `test_episode_audio.py` (4), `test_policy_snapshot.py` (3),
  `test_endurance_acceptance.py` (5);
- `test_policy_observer.py` assertion groups all PASS.

The new release fixtures cover: the exact `/command` payload
(`schemaVersion:1`, generated `id`, `ui_set_value`, `arguments:{key,value}`) and
succeeded receipt, lifecycle evidence publishing and form key,
channel-name/timestamp rendering, stale
evidence on channel change, stream-generation mismatch, bounded auth/transport
failure without token exposure, missing authorized transport, manual correction
idempotence with English count publication, and three-entry v3 -> v4 migration
with restored eligibility and unchanged policy rows.

Native Python release/promotion/filter tests also pass. Native Swift source was
not modified.

## Rollout instructions (harness, after review)

1. Keep observation and enforcement off. Deploy the gateway package and the
   dynamic UI asset; do not restart or send device commands from this session.
2. Back up the private state directory. Copy `language-exclusions.json` and
   `language-auto-control.json` to a staging copy and confirm migration there:
   three previous automatic entries -> three events, automatic membership rows
   removed, manual rows/decisions unchanged, policy rows byte-identical.
3. Install `Examples/language-evidence-overlay-20261127.json` through the
   existing `install_ui_layout` command. Confirm `get_ui_layout` reports
   `language-evidence-overlay-bridge-20261127`. No IPA/native rebuild.
4. On the live iPad (authorized LAN only), verify:
   - the English/Malay switcher and both Spanish actions are still present and
     unchanged in layout;
   - the hidden `languageOverlay` field is not visible and the native label
     updates only after a successful `ui_set_value` receipt;
   - a channel change first shows the new channel and timestamp with
     "waiting for language evidence", never old-channel evidence;
   - a manual pause stays paused and rotation/countdown is unchanged; no
     play/pause/stop/next/previous/resume/reload/rotation command is emitted by
     the bridge;
   - `GET /trial-status` shows `overlay.configured=true`, an empty fault, a
     current channel and no pending queue buildup.
5. Enable observation only (`enabled:true`, `enforcementEnabled:false`) through
   the existing loopback `/language-auto` control. Inspect per-channel counts
   and the bounded evidence window. Leave enforcement off until counts,
   deduplication and manual corrections have been reviewed.
6. If approved, enable enforcement separately with `threshold:25` (runtime
   adjustable 1-1000). Turning observation or enforcement off is immediate;
   evidence and manual decisions are retained, and automatic membership rows
   are dormant while enforcement is off.

## Rollback instructions

- Restore the prior dynamic UI revision with `rollback_ui_layout` or
  `restore_patch_version`; the overlay asset is a separate UI revision and does
  not require changing native code.
- Set `enabled:false` and `enforcementEnabled:false` (or remove the control
  file) through `/language-auto` or by atomic control-file replacement. This
  stops sampling and makes automatic membership dormant; manual entries remain.
- Revert the release commits if the gateway code must be withdrawn. The v4
  store is backward-compatible with the prior overlay because the overlay is a
  separate revision. No policy database change, queue change or media handle
  migration is involved.
- Do not resume a user pause, restart rotation, delete manual exclusions or
  rewrite the evidence audit to roll back.

## Dependencies

- Python 3 standard library only for the gateway, bridge, language store and
  research modules; no new third-party package.
- Existing gateway environment and private state directory.
- Existing `prototype-source-selector.json` (private LAN device IP + fixed
  backend port) and `JEALOUSFIN_DEVICE_TOKEN` when the device requires auth.
- Existing optional local Whisper/ffmpeg fixtures are unchanged; the bridge
  itself does no audio work.

## Blockers and limits

- The installed native `stateSnapshot` still has no language/evidence object and
  the dynamic web component cannot fetch the LAN endpoint. The bridge works
  around this only by writing the one supported hidden form field; it is not a
  per-frame proof that the displayed stream is still the same channel. Strict
  matching happens immediately before each push.
- If the selector config is absent or the device requires auth while no
  authorized token is configured, the label is not updated and
  `overlay.fault` reports the failure. This is intentional fail-safe behaviour.
- During a transport outage the previous label cannot be cleared without a
  successful command; `overlay.staleLabel` reports that condition until the next
  lifecycle-driven publish succeeds.
- The overlay correction button can be pressed again. A replayed nonce is
  idempotent, and even a fresh nonce cannot retract a second event: the store
  only corrects the latest excluded event and returns `already_corrected` when
  that event is already retracted.
- Audio-assisted episode evidence is deferred and must not delay this release.
- This session did not send any live command, deploy, restart, push or mutate a
  queue. The harness owns rollout and live verification.
