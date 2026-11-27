# Paused-rotation episode research and existing-IPA combined overlay

Revision: `episode-paused-retry-20261127`

## Problem

`episode_probe` rejected any status with `surfPaused: true`, even when the
player was still progressing the current stream. A valid English sample on
channel `577f658204732598e0aded0401c994cb` (97.69%, 59.26 s, provider series
title only) therefore discarded the queued audio clue job.

The language lane also stops sampling after a conclusive language result for a
physical visit. A rejected episode lookup (no source, no match, ad/promo) had
no way to obtain another transcript in that visit.

The installed hotpatch already contains the combined presentation surface
(`form.programmeOverlay`, `form.programmeOverlayReady`,
`form.programmeOverlayFallback`, `programme-combined-native-v1` with the
existing `headline` style). No native rebuild or layout install is needed.

## Changes

### Paused rotation is not a scope change

`episode_probe` now allows `surfPaused: true` only when:

- rotation is still active (`surfing: true`), and
- the player is not explicitly paused (`playing` is not `false`), and
- an existing `ActiveViewing` view for the exact channel proves fresh,
  non-paused, non-stopped, progressing playback.

Stopped rotation, unknown pause state, actual player pause, stale playback,
unverified visit, missing visit, channel/launch/stream incoherence, and any
post-research reload/departure still fail closed. `episode_audio_status()`
exposes `probe` diagnostics for every early return.

### Bounded episode-only retry

After a commercial/no-match rejection the observer may request at most two
episode-only samples in the same physical visit (`retry` callback). The
language sampler reuses already-fetched media bytes for that visit and marks
the sample `episodeOnly`. Completion:

- still calls the existing `on_speech` handoff,
- never calls `on_exclusion`,
- never increments language counters, languages, evidence or visit counts,
- keeps the per-visit marker conclusive so normal sampling cannot count a
  second language result.

Actual playback must still be progressing; if the player is paused or stale no
new media is decoded and no retry is requested.

### Diagnostics

Observer diagnostics include explicit reasons/categories for disabled,
insufficient clues, device unverified, language gated, queue full, session
unverified, programme changed, identity present, budget rejection (with the
budget reason), no sources, research error, commercial, no match, stale,
duplicate, publication failure, retry scheduled/rejected/exhausted and
retry inactive.

General television sources (not only wrestling) were added to the reviewed
official archive and independent report allowlists: Showtime/SHO, NBC, ABC,
CBS, Fox, AMC, HBO/Max, USA, Syfy, TNT, TBS, Bravo, Paramount, Peacock, Hulu;
TVLine, EW, Variety, THR, AV Club, Vulture, IGN, Decider and TV Guide.

### Existing-IPA combined overlay

`programme_overlay.py` formats:

```
<series> — SxxExx: <episode> (<episode original-air year>) <language> — <count>/<threshold> — <confidence>%
```

The year preference is `episodeYear`, then `seasonYear`; series premiere year
is never substituted. With no current language evidence the label says
`language evidence pending` rather than inventing a count.

Publication order to the exact installed fields:

1. scope transition/clear: `programmeOverlayReady=false`,
   `programmeOverlayFallback=true`, `programmeOverlay=""`
2. valid current-scope value: write `form.programmeOverlay`
3. re-probe channel/launch/visit/stream
4. `programmeOverlayReady=true`
5. `programmeOverlayFallback=false`

A scope change during the re-probe, a field transport error, or a Ready
failure reverts to the safe fallback (Ready off, Fallback on). The native
binding is the installed existing `headline` style; this work does not claim
the draft 22 px `programme` style or install any new provenance.

Combined publication is allowed while surf rotation is paused because pause is
not part of the scope identity; it still requires the same channel, provider,
launch, physical visit and stream. The existing language label settles before
the combined transport is attempted, so a slow combined write cannot turn a
valid language label into `stale_pending`.

### Terminal transient-state ordering

The overlay bridge now remembers an exhausted/terminal state per
channel/visit/stream. A late in-flight `collecting` event for the same or older
attempt within the grace window is reported as `superseded` instead of hiding
the terminal `inconclusive: sample_too_short … retry limit reached` result. A
new visit or a genuinely newer attempt is still allowed.

## Tests

- `test_episode_audio` (70): paused rotation accepted only with progressing
  playback; actual pause/stale/stop rejected; no-source, commercial, no-match,
  stale and retry-exhausted diagnostics; bounded retry; episode-only completion
  leaves language counters/evidence unchanged; general source allowlists.
- `test_programme_overlay` (11): exact label and episode-vs-series year,
  pending/stale language, transition order, value→re-probe→visibility,
  reprobe scope change, transport/Ready failure fallback, dedup, installed
  layout ABI validation.
- `test_release_integration` (13): language overlay unaffected; combined label
  publishes with `surfPaused: true`; scope guards; transition clears old label;
  transport failure never enables Ready; status reports the installed ABI.
- `test_overlay_bridge` (20): exhausted retry is not overwritten by late
  collecting; retryable inconclusive still allows the next collecting state.

## Coordination boundary

The separate `engineering/schedule-episode-discovery-20261127` worktree keeps
its own draft `programme_overlay.py` and `style: programme` layout. This lane
does not modify or overwrite that worktree and does not install its draft
layout. A later merge review must reconcile the two `programme_overlay.py`
modules and adopt the installed `headline`/Ready/Fallback ABI described here.

Deployment still requires explicit user approval. No production gateway was
restarted, no UI layout was installed and no IPA was rebuilt or installed.
