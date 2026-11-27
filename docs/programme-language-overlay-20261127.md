# Durable combined programme + language overlay (2026-11-27)

This is the presentation half of the schedule/episode lane. It replaces the
temporary `programme-language-combined-20261127` web title with a native 22 px
text node in the existing `player.fullscreen.programmeTitle` replacement slot.

The permanent bottom-left target is:

```
The Mentalist — S06E18: Forest Green (2014) English — 1/25 — 99.1%
```

Nothing in the formatter or state machine hard-codes a series, episode,
language, count, threshold or confidence.

## Separation of metadata and presentation

- The provider/Jellyfin `Name`, `ProductionYear` and
  `JealousfinProgramme` metadata stay the actual programme and episode
  identity. Language text is **never** written into them.
- The combined label is generated at the gateway from the current programme
  row plus current verified language evidence and written only to the
  gateway-managed `form.programmeOverlay` field.
- The existing `form.languageOverlay` detail and the existing Spanish
  mark/unmark and Mistaken English actions remain in place. They are not
  removed or repurposed.

## Exact formatter

`Server/iptv/programme_overlay.py` renders:

```
<series> — SxxExx: <episode> (<original episode year>) <language> — <count>/<threshold> — <confidence one decimal>%
```

- Episode year comes from the canonical/episode-scoped original air date; it is
  not the season premiere, series premiere or rerun date.
- Confidence is formatted from the verified 0..1 evidence with one decimal
  (`0.9907 -> 99.1%`).
- With no language evidence the label is the programme identity only.
- With stale/inconclusive evidence it appends `— language evidence pending`;
  it never invents a count or confidence.
- With no verified programme it shows the waiting text, never a static mapped
  episode.

## Scope and stale protection

`OverlayScope` binds:

```
channelID, providerID, channelName, launchID, visitID,
streamGeneration, rotationAttempt
```

- A channel/provider/launch/visit change increments the generation and replaces
  the label with the waiting text.
- A same-name channel on another provider or channel ID is rejected.
- A programme change clears the previous language evidence.
- A stream-generation change (reload) keeps the same programme but clears
  language until fresh evidence arrives for the new stream.
- A pause is not a channel change; the current label may remain until expiry,
  but no new evidence is promoted by the overlay.
- Programme and language evidence expire independently (default 1800 s).
- Out-of-order, future, stale, wrong-channel and post-correction conflicting
  evidence is rejected with a stable reason.
- Pending sampling shows the programme identity only.

`ProgrammeOverlayBridge` adds deduplication by scope/event/text, bounded
retries, a pending TTL and fail-safe diagnostics. It uses the existing
authorized `ui_set_value` transport and writes only
`form.programmeOverlay`.

## Ready-to-install layout

`Examples/programme-language-combined-20261127.json`
(`revision: programme-language-combined-20261127`):

- `player.fullscreen.programmeTitle` → native text node
  `programme-language-title`, `style: "programme"` (22 px), bound to
  `form.programmeOverlay`;
- hidden editable `programme-overlay-value` with
  `form.programmeOverlay` and the absent `form.programmeOverlayVisible` gate;
- the existing English/Malay source switcher, language evidence disclosure,
  Spanish actions and Mistaken English correction are preserved;
- no web title and no 80 px minimum-height component is used for the title.

Native support: `Sources/DynamicUIViews.swift` maps `style: "programme"` to
`.system(size: 22, weight: .semibold)`; `DynamicUIDomain.swift` and
`Server/iptv/overlay_bridge.py` accept the style.

## Gateway lifecycle

The existing overlay worker remains the only lifecycle worker:

- `/LiveTv/Channels` and authenticated `PlaybackInfo` enqueue a refresh;
- accepted language evidence enqueues the existing language label and the
  combined label from the same scoped evidence;
- before writing, the worker re-probes the device channel and stream;
- a changed channel, changed stream, unavailable probe or old evidence is
  suppressed;
- `GET /trial-status` exposes `programmeOverlay` (configured, fault, state,
  label, generation, expiry) alongside the existing `overlay` status.

No new timer, scheduler, playback command or rotation command is added.

## Tests

`Server/iptv/test_programme_overlay.py` covers:

- the exact target string and a second non-hardcoded series/language;
- missing/inconclusive language and missing episode identity fallbacks;
- channel change, same-name channel, programme change, stream-generation
  reload, same-visit reload, pending sampling, correction, expiry,
  out-of-order/future evidence;
- transport field/value, duplicate suppression, scope transition, bounded
  retry and pending expiry;
- the ready-to-install layout keeps the native title slot, hidden field and
  existing language disclosure/actions.

## Deployment plan (harness, not this worker)

1. Review and deploy the additive Python modules and the two gateway wiring
   points.
2. Install `Examples/programme-language-combined-20261127.json` through the
   existing `install_ui_layout` command; confirm `get_ui_layout` reports
   `programme-language-combined-20261127`.
3. Keep the existing layout asset for rollback.
4. On the authorized LAN device, verify the 22 px bottom-left title, the exact
   combined format only after verified evidence, the waiting/fallback states on
   channel change/reload/expiry, and the unchanged language disclosure/actions.
5. Roll back with `rollback_ui_layout`/`restore_patch_version`; no restart,
   reset, IPA change or rotation change is required.

## Limits

- The native binding cannot compare channel IDs itself; matching is enforced in
  the gateway immediately before each write, as with the existing language
  bridge.
- A transport outage cannot clear a previously written label without a
  successful command; the status exposes the fault and a later lifecycle event
  retries.
- No live device command, deployment, restart or screenshot was performed by
  this lane.
