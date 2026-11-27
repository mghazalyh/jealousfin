# Native release acceptance

The next IPA contains native brightness (single fullscreen control), content-sized fullscreen overlay, expanded validated presentation/recovery configuration, persistent rotation counters, frame-freeze diagnostics, gateway policy v2 and a release-scoped patch reset. Brightness is a video-rectangle black/white luminance overlay, not decoded-pixel color correction. It does not change system brightness and is not guaranteed in external playback/PiP. No contrast or saturation controls were added.

## Required pre-install checks

1. Deploy the reviewed gateway and policy observer on the existing state directory through the authorized service management workflow. Preserve the SQLite database and credentials. Do not run a second instance on the live port.
2. Verify authenticated policy and catalogue endpoints, excluded playback returning 409, and local /trial-status showing policyRevision=sqlite-policy-v2, policyReady=true, and all catalogue IDs represented.
3. Export fresh device /status and gateway /trial-status into protected local files. Run Scripts/audit-native-release.py with --status and --gateway-status. Any unknown active patch revision blocks the audit until reviewed. The release workflow checks the committed native-only baseline but cannot see live LAN status on its own.
4. Build/sign the accepted IPA using the normal release process; install over the existing bundle/data container. Verify the new build reports patchReleaseResetApplied and native baseline revisions, with old history removed. Existing credentials, user brightness, rotation history and channel health must survive.
5. Physically verify video brightness -10, 0, +10; reset; fullscreen and inline transitions; rotation pause independent of video pause; auto-hide/reveal; both orientations; no video reopen while adjusting brightness. Check real live HLS and PiP separately; the overlay is intentionally not a PiP filter.
6. Verify failed channels enter hiatus immediately, gateway blocks stale selection, recovery advances, and intentional pause/stop does not add penalties. Verify the full Health tab including gateway bans and explicit reinstatement. Confirm the configured default 3-day progressive policy and permanent ban on eighth failure.
7. Run at least six hours of unattended playback. The observer records build and launch IDs on minute samples; use Server/iptv/endurance_acceptance.py DATABASE --build BUILD --launch LAUNCH to assess duration, gaps, memory growth and the advertised gateway contract. This is telemetry acceptance, not proof that every frame was visible; use the physical player and native acceptance campaign as well.

No physical-device or six-hour acceptance is implied by successful compilation or fixture tests. The current older live gateway must not be replaced by an unreviewed process change.

## Hot patch controls

Discover the bounds through get_configuration_schema and get_native_capabilities. Use set_video_brightness with level -10...10 for a persistent user adjustment; runtime tick and dynamic UI can dispatch it. Fullscreen width, padding, spacing, radius, button height, text scale, control order, auto-hide, tap-to-reveal, content sizing and portrait height are configuration fields. Recovery inter-attempt delay, terminal advance, adaptive extension, gateway policy poll interval, and memory growth warning are configurable. The fixed default progressive hiatus remains 3 days per consecutive failure, permanent ban on failure 8; a gateway-supplied penalty is authoritative for gateway channels. Policy changes apply prospectively to native events, not retroactively.

Gateway configuration metadata is stored under hiatus_base_days and hiatus_ban_after in the SQLite meta table. Do not edit these in production without an explicit policy change and verified migration. Ordinary users cannot change the gateway penalty by changing native runtime settings.

Old cloud patch bundles are no longer imported automatically into native-only release builds; source fixtures and review evidence remain in Git. The source-level release audit must be run with fresh live status before installation.

## Planned maintenance and connection recovery

Before a gateway restart, pause rotation **and** playback through the supported client controls, record the current channel and countdown, and confirm both pause receipts. Back up SQLite and verify the persisted gateway identity before restarting. Restore the same eligible channel after policy readiness is confirmed; resume the original pause/play state. Avoid classifying planned maintenance as a channel failure. The native client now holds its rotation on typed server API authentication/connection failures or unavailable gateway policy, and retries authentication under the existing unattended reconnect policy. Upstream stream errors and genuine terminal playback failures still receive strict hiatus.

The gateway also records a private hash-only handle ledger. If a restart invalidates a live visit's handles, repeated same-channel stale 404s plus one fixed native after-startup stall reason inside a short window suppress only that deployment artefact, preserving the original observed event and an audit. Generic stalls, other channels, expired windows and corrupt state remain strict failures. See [deployment media continuity](deployment-media-continuity-20261127.md). The observer retains numeric playback-quality samples (including contiguous buffer and frame freshness on new clients) for 30 days; older clients leave unsupported metrics null. Physical frame cadence is not measured by this database alone.

The native fullscreen heading replaces the redundant Surf active/stopped label and shuffle icon with the current programme title, or channel name if no fresh programme is known. The rotation countdown remains separate; the rotation button indicates pause/resume. The entire heading is replaceable via `player.fullscreen.heading` and `state.currentProgrammeTitle` is available for bindings. On installed build 1021 the old heading is outside every patchable region, so this removal requires a native upgrade. Verify that current titles change without stream reopen and stale/missing programme data falls back to the channel name.

### Previous and icon-only promotion

The native six-button transport row is rotation, Previous, Play/Pause, Next/Reconnect, Stop and Favorite. Large 29pt icons, 48pt targets, semantic accessibility labels, muted night theme and confirmed favorite fill replace the WebView toolbar. Legacy five-control order remains accepted and gains Previous before playback; new controls are configurable with fullscreenIconOnly/fullscreenIconSize and the existing replacement slots. Channel-only header, current programme title with optional (YYYY), no Scheduled prefix, current catalogue preference, 60-second default gateway catalogue refresh, real brightness and compact footer are native. Previous selects the most recent eligible different channel in the current category, cancels stale selection tasks, records an explicit revisit without clearing visited channels, retains Surf and rotation-pause state, and starts a full dwell on successful validation. Press Previous while running and while paused, check ineligible history is skipped and no automatic duplicate is introduced, and verify countdown expiration advances normally. Pause/resume playback independently of rotation, then reload while paused; remaining time including zero survives. Shared-server transport cannot select a previous underlying channel. No gateway mutation is needed. Physical acceptance remains required.

The standalone HLS readiness test must not depend on DynamicUILayout to validate adapter revision IDs. Deferred adapter IDs use the identical bounded ASCII identifier grammar in JellyfinAdapterRules, which restores the release workflow's isolated test compilation. Manual switches seed a full dwell checkpoint before validation; reload and restore explicitly supply their saved dwell instead.


### Independent fullscreen component replacement

The native layout advertises individual replacement locations under
`player.fullscreen.`: showControls, hideControls, exit, programmeTitle,
countdown, rotationToggle, previous, playPause, next, stop, favorite,
category, surfToggle, brightness, favoriteError and sharedHint. Existing
whole-region replacements remain supported and take precedence over their
native descendants. An absent or empty replacement retains the native
fallback. A nonempty spacer can intentionally suppress a component.
The legacy Surf replacement can still host the fixed native brightness
primitive; the new brightness location can also host it. Duplicate
brightness primitives and brightness in unrelated locations are rejected.
These regions do not recreate the AVPlayer or reopen the stream. Device
acceptance must confirm that recovery, exit and reveal remain accessible
under the chosen configuration. Engineering CI also executes the exact
standalone HLS compilation and test command from the IPA workflow, without
producing an IPA.


### Durable history, programme evidence and countdown acceptance

Surf retains a bounded, scope-specific recency history independently of the
unordered cycle ledger. Previous deliberately behaves as a television
last-channel toggle, not a browser history cursor; the target is resolved
from the refreshed eligible pool. A legacy unordered visited set is preserved
but cannot reconstruct a historical ordering. A healthy validated stream that
is the sole eligible channel remains connected at automatic dwell expiry.
Manual Next and actual failures still take their normal paths.

Repeated playback resume must not reset the active deadline or stall clock.
Changing rotation preferences while playback or rotation is paused preserves
the saved dwell; a deliberate duration change while running updates the
deadline using effective runtime policy and saves a checkpoint.

Typed gateway programme evidence retains source, observed timestamp, release
year, optional upcoming title and unzoned provider clock text. Primary
evidence expires at 150 seconds, corroborated external evidence at 1800 seconds;
standard programme start/end boundaries still apply. The server owns the
display title and year; no new title is guessed. Next-programme information
is exposed in diagnostics without adding a default fullscreen row. Unsorted
telemetry queries serialize only the requested page. Sorted queries retain
their existing behavior pending further profiling.

Validate on the physical iPad: reload while paused, repeated resume, duration
change while paused, restart across a previous-channel revisit, single-channel
automatic dwell expiry, genuine single-channel playback failure, and preserved
rotation scope and history across release installation.

### Episode metadata promotion

The temporary Beverly Hills catalogue adapter is superseded by permanent reviewed Pluto schedule matching and typed episode evidence. Native displayTitle(channelName:at:) uses season premiere year, canonical episode number and title on one line; omit duplicate series title only on normalized exact channel match. Verify current episode, next boundary, mixed channel prefix, stale/conflicting guide fallback and unchanged playback, rotation and hiatus. Unknown years are omitted. No new IPA required for the gateway's standard Name field; typed native fields take effect in a future build.

### Native American/Malay source selector

The fullscreen `player.fullscreen.category` region now renders a native
American/Malay source selector instead of the reserved category menu. Both
buttons are accessible, show the selected source, the current catalogue count,
and a busy or error state. The native adapter is typed as
`SurfSourceIdentity`: American stays on `All Channels` (and the existing
Favorites/Sports paths), while Malay uses the temporary `Movies & Series`
alias already implemented by the opt-in same-origin gateway selector. It does
not add a host, port or gateway protocol.

Selection is single-flight and follows the installed hot-switch order:
pause Surf, stop playback, persist the target preference, refresh the
catalogue, then start Surf on the alias. Success is reported only after the
refreshed catalogue is ready and Surf is active. The outgoing source,
category, Surf mode, paused state, current channel and dwell are checkpointed;
any failure restores the previous category and Surf state and attempts the
previous catalogue. Per-source category memory preserves an American
Favorites/Sports path across a Malay switch, and the existing category-scoped
rotation ledger, favorites and gateway policy are not rewritten. The selector
is still replaceable through `player.fullscreen.category` (and its parent
`player.fullscreen.surf`) without double rendering.

Acceptance still required on the physical iPad: select Malay and American while
playing, while paused, while stopped, during an unavailable Malay backend, and
during a slow catalogue refresh; verify rollback returns the previous category
and Surf state, that Favorites/Sports survive a round trip, that no duplicate
selector appears with a replacement layout, and that accessibility labels and
selected values are announced. This change performs no gateway deployment,
device mutation or source switch by itself.

### Outstanding evidence limitation

`episode-gateway-passthrough-20261126` remains unverified as an adapter. The
live Jellyfin API adapter source is not present in this repository or its
historical patch archives; `get_jellyfin_api` exposes only active-revision
metadata. Repository evidence covers the gateway schedule worker and native
typed `ProgrammeEvidence` parsing. Native tests now pin exact gateway
programme Name passthrough, season-year preservation, absence-of-evidence
fallback and policy negotiation, but those tests do not certify the live
adapter. Do not mark the adapter as promoted until its exact source and
read-only behavior are available.
