# Language evidence v4 and the hotpatch overlay

This change is an isolated server/gateway and hotpatch-asset change. It does
not deploy, restart, mutate the Fast Consumer queue, modify another worktree,
or alter native Swift source. The live detector remains off until an explicit
control is written and the evidence gate is reviewed on the host.

## Why v3 was disabled

The v3 store excluded a provider on the first confident detection. That made a
single sample irreversible and produced three legacy automatic entries. v4 is
evidence-first:

- every conclusive visit is an evidence event;
- previous v3 `source:"auto"` entries become exactly one evidence event each;
- those automatic membership entries are removed, so the channels become
  eligible again subject to ordinary playback/health policy;
- manual Spanish entries and manual "not Spanish" allow decisions survive;
- health policy, hiatus, bans, favorites, rotation and playback are untouched.

Migration is one-way and idempotent: a v4 file is validated and returned as-is.
A v3 file is rewritten as v4 by `load_or_migrate`. The legacy
`spanish-classification.json` audit file is not deleted.

## Data model

`Server/iptv/language_exclusions.py` (schema `language-exclusions-v4`) stores:

- `entries`: manual and (only after threshold enforcement) automatic membership
  rows. Automatic rows never carry a permanent allow decision.
- `decisions`: manual `spanish` and `not_spanish` overrides.
- `evidence.events`: bounded audit history (5000 events) with exact provider,
  channel, language, confidence, reason, outcome, `visitID`, `launchID`,
  `streamGeneration`, `retractedAt` and `retractedBy`.
- `evidence.counts`: persistent per-channel, per-language visit counts. Counts
  survive evidence-window pruning.

Only conclusive outcomes enter evidence: `excluded`, `allowed` and
`unauthorized`. Weak, mixed, music, silent, insufficient-speech and stale
samples stay in telemetry as inconclusive and never increment evidence counts.

Allowed English visits are counted and audited but never whitelisted. A later
Spanish visit is tested normally.

## Threshold and controls

Defaults:

- observation: off when no control file exists;
- enforcement: off;
- threshold: 25 confirmed visits of the same unwanted language.

The local control file is `language-auto-control.json` (schema 2):

```json
{"schemaVersion":2,"enabled":true,"enforcementEnabled":false,"threshold":25,"updatedAt":0}
```

LAN endpoints (loopback only, existing path):

- `GET /language-auto` returns observation, enforcement, threshold, status and
  language evidence summary.
- `POST /language-auto` accepts any subset of `enabled`, `enforcementEnabled`
  and `threshold`. Threshold is an integer from 1 to 1000.

Observation and enforcement are independent. Observation records evidence;
enforcement creates an automatic membership entry only when the same unwanted
language reaches the threshold. `enabled:false` leaves evidence and manual
decisions intact but stops new sampling and hides dormant automatic entries.

## Deduplication and identities

- The sampler already deduplicates segment resources and has a finalize
  cooldown. v4 adds an evidence-level duplicate-visit guard.
- Exact identities are `visitID`, `launchID` and `streamGeneration` from the
  device probe. The manager re-probes before committing an inference result.
  A changed channel, launch, stream generation or unverified device discards
  the sample as stale.
- Declared maintenance windows suppress language evidence for the listed
  channel/window.
- Reloads inside the 600-second per-language dedup window with the same launch
  do not add a second count. Distinct launch identities or later visits do.

## Manual correction

The overlay's "Mistaken — English" control writes marker schema 2 action
`mistaken_english` through the existing `spanishRequest` form field and then
requests a catalogue refresh. The gateway:

1. finds the exact latest non-retracted automatic unwanted-language event for
   that provider/channel;
2. marks it retracted (audit retained), decrements its language count;
3. records one manual English allowed evidence event;
4. removes an automatic membership entry whose count is now below threshold;
5. never creates a permanent allow decision and never issues play/pause/stop/
   next/previous/resume/reload/rotation commands.

A repeated nonce is idempotent. A correction always targets the latest excluded
event only: if that event was already retracted, a fresh nonce returns
`already_corrected` and never walks backwards to older evidence. A manual
Spanish entry is never retracted.

## Hotpatch overlay

`Examples/language-evidence-overlay-20261127.json` is a normal dynamic UI
layout revision. The release integration uses the bridge revision
`language-evidence-overlay-bridge-20261127`, which preserves the deployed
English/Malay switcher, the Spanish mark/unmark actions and the Mistaken English
correction. A hidden editable `input` and a native `text` node share
`form.languageOverlay`; the gateway bridge writes that one field through the
existing authorized `ui_set_value` command. It never resumes a manually paused
stream, advances, reloads or resets rotation.

### Exact overlay capability blocker

The current hotpatch UI ABI still cannot let the overlay read the gateway
detector directly:

- `AppModel.stateSnapshot()` has no language/evidence field. It exposes only
  `gatewayPolicyRevision`, `gatewayPolicyChannels` and poll time.
- `GatewayHealthSnapshot` decodes only the health schema; unknown response
  fields are ignored.
- The dynamic web component runs with Content-Security-Policy
  `connect-src 'none'`, so it cannot call the LAN `/language-auto` endpoint.
- Dynamic UI commands are the compiled native command set; there is no
  network-fetch or gateway-language command.
- Adding a snapshot field, a native command or relaxing the CSP would require
  native Swift/release work, which this lane is explicitly not allowed to do.

The release integration avoids native changes by pushing one bounded label into
the supported hidden form field. The label always includes the observation
timestamp and channel name, plus language, count/threshold, phase and
confidence when available. Because the binding cannot compare channel IDs, the
gateway suppresses evidence for another channel or stream generation before
each push. If a future host build exposes a language snapshot object, the same
overlay can render it without another layout change. Full language/evidence
readback is available now at the loopback endpoints: `GET /language-auto` and
`GET /trial-status` (`languageAuto.classification`, `overlay`).

## Staged rollout

1. Keep observation and enforcement off. Deploy only the store/gateway code and
   the overlay layout asset locally.
2. Confirm v3 migration on a copied state directory: three automatic entries
   become three events; manual entries/decisions remain; health rows are
   unchanged.
3. Install the overlay through the existing dynamic UI command
   (`install_ui_layout` with this JSON). No IPA/native rebuild/reinstall.
4. Enable observation only (`enabled:true`, `enforcementEnabled:false`) and
   inspect `/language-auto` counts and the audit event window.
5. Enable enforcement only after reviewing counts, threshold and stale/dedup
   behavior. Use a higher threshold first if desired; runtime adjustment is
   immediate.
6. Rollback: set `enabled:false` and `enforcementEnabled:false`, then restore
   the prior UI layout revision through `rollback_ui_layout` or
   `restore_patch_version`. Automatic entries are dormant while enforcement is
   off; manual entries remain. The v4 store is backward-compatible with the
   overlay because the overlay is a separate UI revision.

Do not change queued tasks or deployment artifacts; the harness reconciles
those revisions separately.

## Local resource measurement

Isolated real-time fixture run
(`LanguageRealtimeFixtureTests`, existing `ggml-small.bin`, local TTS and
ffmpeg, temporary audio deleted):

- model: 487,601,967 bytes; SHA-256
  `1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b`;
- wall time 86.23 s, user CPU 38.69 s, system CPU 13.29 s;
- maximum resident set size 959,905,792 bytes (includes whisper small model and
  ffmpeg fixtures).

Inference remains bounded by the existing sampler: one decode thread, one
inference queue of 8 jobs, at most 4 whisper threads, 55 s target audio,
`MIN_SAMPLE_SECONDS`/`MIN_SPEECH_SECONDS` gates and no model download.
