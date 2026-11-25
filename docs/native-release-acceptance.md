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
