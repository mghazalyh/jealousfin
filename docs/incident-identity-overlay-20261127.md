# Incident: confirmed identity evidence not reaching the display (2026-11-27)

Base: `e105b7a630ee16f6e96c791de5f1c26cebfb0fbe`
Lane: gateway identity / overlay / language / episode (Server/iptv only)

This report records a bounded, read-only investigation plus targeted gateway
changes. No production deploy, service restart, device command, credential
change, release-manifest edit or live UI mutation was performed.

## Observed live shape

- 576 channels; `programmeCount` 231; `episodeVerifiedCount` ~24.
- 801 language decisions and 155 language evidence events already stored.
- `combinedPublished` 0 and `episodeAccepted` 0.
- Device control repeatedly times out after a new app launch.
- Episode rejection reason is the opaque `device_unavailable`.
- Previous safe-fallback rewrites are already deduplicated.

## What the code does

A confirmed language event enqueues one bounded overlay job
(`overlay_publish_evidence` -> `overlay_enqueue`). `_overlay_process` then:

1. reads a coherent device identity (`overlay_current_channel` ->
   `device_language_probe` -> `device_identity_pair`);
2. gates stream/visit/launch/channel attribution;
3. publishes the language node, then the combined programme+language node;
4. `ProgrammeOverlayPublisher.publish` writes the value, performs a fresh
   scope recheck, and only then enables the native label.

`episode_probe` uses the same identity read before any episode research. A
single episode attempt therefore performed: sampler start/end probes, episode
start probe, post-research probe, overlay job-start probe and overlay recheck.
On the installed legacy IPA a probe can require `get_status` (3 s) plus
`get_player_snapshot` (8 s), so contention after a launch turns into serial
timeouts. When every probe fails, `episode_probe` returns `device_unavailable`
and both combined/episode counters stay at zero, even though the evidence
store is full.

The second contributor is device write amplification: a new stream generation
inside one physical visit changed the combined publisher fingerprint, causing
three fallback writes (`Ready=false`, `Fallback=true`, value cleared) before
the same safe fallback was already known to be visible. Those repeats were
"already deduplicated" only after the first scope, not across reloads.

## Changes (all in `Server/iptv`)

`gateway.py`

- Split the probe into `_device_language_probe_once` (unchanged coherence and
  fail-closed semantics) and `device_language_probe(share=True)`.
- Added **single-flight** coalescing: only callers that arrive while a probe is
  genuinely in flight share it. There is no completed-probe/launch cache, so
  sequential probes still read a fresh launch (locked by a regression test).
  This removes duplicate 3 s/8 s device calls from concurrent language,
  episode and overlay workers after a new launch.
- Presentation rechecks (`_overlay_current_scope`) use `fresh=True`, i.e. they
  never share the coalesced read. The node cannot be switched on from a reused
  observation.
- Record stable failure reasons (`device_status_unavailable`,
  `device_snapshot_unavailable`, `device_status_invalid`,
  `device_snapshot_invalid`, `device_incoherent`, `device_not_configured`).
  `episode_probe` keeps the legacy `device_unavailable` bucket but exposes the
  exact reason in its detail and in `deviceProbe.byReason`.
- Carry `launchID`/`visitID` from the verified language payload into evidence
  and state jobs. This makes the existing `_overlay_visit_matches` gate stricter
  (it no longer runs with an empty visit for these jobs).
- Add bounded `overlay_combined_stats` and expose it via
  `overlay_combined_status()["stats"]`, plus `episodeAudio.status.deviceProbe`.

`programme_overlay.py`

- Added `OverlayScope.visit_fingerprint()` (channel/provider/name/launch/visit,
  excluding stream generation).
- The publisher now remembers the confirmed fallback scope. `transition`,
  `clear` and `publish` skip the three fallback writes when the same physical
  visit is already in a confirmed safe fallback (reloads included), while still
  writing the value and enabling `Ready` only after the fresh recheck.
- A new launch, new visit, channel change, failed write or non-fallback state
  still reconciles normally. `Ready` is never enabled on a failed recheck, so
  no stale combined label is shown.

## Regression fixtures

`Server/iptv/test_incident_identity_overlay_20261127.py` (9 tests):

- concurrent probes collapse to one coherent device read (`shared == 1`);
- sequential probes still observe a changed launch (no launch caching);
- snapshot timeout is reported as `device_snapshot_unavailable`, not a
  generic failure;
- incoherent status/snapshot pairs report `device_incoherent`;
- `episodeAudio.status` exposes the probe breakdown;
- a reload inside one visit does not rewrite the safe fallback;
- a new launch still rewrites the safe fallback;
- confirmed evidence reaches the combined display through a real queued
  `evidence` job;
- a failed fresh recheck keeps the combined node off and prefers the fallback.

## Validation run in this lane

```
cd Server/iptv
python3 -m pytest test_incident_identity_overlay_20261127.py \
  test_programme_overlay.py test_release_integration.py \
  test_language_pause.py test_episode_audio.py \
  test_release_readiness.py test_language_auto.py -q
# 176 passed, 8 subtests passed
```

The full gateway regression suite is intentionally left to integration.

## Policy and attribution safety

- No language threshold, exclusion, enforcement, health, hiatus, ban, rotation,
  catalogue or provider/profile logic was relaxed or edited.
- No new device command type was introduced; only `get_status`,
  `get_player_snapshot` (legacy) and the existing `ui_set_value` are used.
- Channel + launch + stream generation + durable visit attribution is
  preserved. The evidence job now carries the verified launch/visit, which
  makes the existing gate stricter, not looser.
- No stale combined label: `Ready` is enabled only after a fresh, coherent
  recheck; every failure path writes the safe fallback first.

## Remaining uncertainties

- The live device may be genuinely slow enough that even one 3 s status read
  times out. Single-flight and write dedup reduce contention but cannot make a
  single read succeed. The new `deviceProbe` breakdown will distinguish this.
- If the installed IPA never exposes `streamGeneration` in `get_status`, the
  legacy `get_player_snapshot` command remains on the probe path. A bounded
  status-only fallback was intentionally **not** invented because it would
  weaken strict stream-generation attribution.
- The exact live counts should be re-read after integration via
  `/trial-status` (`overlayCombined.stats`, `episodeAudio.deviceProbe`,
  `episodeAudio.probe`). This lane could not inspect the live device or state
  directory.
