# Hot patch ABI 2 candidate

Goal: keep the native application stable while moving future playback decisions
into validated, reversible QuickJS policy. The previous working IPA is preserved.

- Runtime configure and playbackAttempt hooks are versioned together; the latter
  receives channel identity, attempt number, previous sanitized failure and a
  bounded policy object. Fixture preflight precedes activation. Channel-specific
  hook errors fall back to native policy. Existing v1 modules remain supported.
- Ordinary HLS policy can adjust segment format, segment length/count, encoding,
  resolution/bitrate, direct stream negotiation, peak rate, buffering and
  startup deadlines without changing the binary. Shared gateway tickets and
  transport remain native and unchanged.
- get_native_capabilities exposes the host contract; get_player_snapshot returns
  fresh player state without waiting for the 15-second periodic sample.
  probe_active_hls checks the active same-origin ordinary playlist, variant and
  first media bytes. No redirects, no arbitrary URLs, no credential values.
  These diagnostics are not substitutes for decoded-frame acceptance.
- Malformed LAN envelopes return 400; decoded commands retain durable receipts.
  Rotation ownership is explicit. Shared continuity has its own campaign kind;
  distinct underlying rotation is not inferred from a synthetic stream ID.
- Private defaults import is explicitly opt-in, checks the canonical private
  repository, fills only missing compatible fields and leaves LAN control
  selected. Expected private path: jealousfin/database/connection-defaults-v1.json
  (schemaVersion, server, username, password). Never include that file or tokens
  in public source or release assets. No private defaults file is created here.
- Physical evidence: shared modes decoded successfully; TS ordinary attempts
  repeatedly had zero media bytes despite generated server segments. One fMP4
  attempt decoded but stalled. None of the experimental formats passed sustained
  acceptance, so no experimental format is promoted by default.
- Acceptance: test hooks, rejection and rollback; then actual device startup,
  pause/resume, endurance, distinct rotation, restart persistence and shared
  regression. Keep the accepted build 1015 latest until physical approval.
- Native limitations: arbitrary Swift/OS changes, entitlements, native UI
  primitives and installation still require a new IPA.

Fullscreen uses the existing AVPlayer with an interactive application-owned
overlay. Category, transport controls, rotation state and start/stop are
available in fullscreen. New dynamic slots: player.fullscreen.top,
player.fullscreen.controls and player.fullscreen.bottom. Commands
set_fullscreen and set_fullscreen_controls accept visible=true/false.
The app's own fullscreen entry replaces the inline system transport controls.
Physical transition and accessibility acceptance remains required.
