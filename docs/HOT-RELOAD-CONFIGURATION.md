# Native foundation: hot reload configuration ABI 1

The goal is to reduce future IPA installs, not to replace the iOS operating
system or inject native code. Existing runtime JavaScript, API adapters,
declarative UI, HTML components, configuration and device campaigns are
separate extensibility layers. Start by querying get_native_capabilities.

## Configuration lifecycle

- get_hot_configuration returns active and previous configurations.
- install_hot_configuration accepts configuration (JSON text) and expectedRevision.
- install_hot_configuration_from_db accepts id, sha (exact blob SHA),
  expectedRevision. Reads engineering/configurations/<id>.json in the
  configured private JealousDB when GitHub control is selected. LAN mode
  can send the same JSON directly; no token is required.
- reload_hot_configuration accepts expectedRevision, reapplies the already
  validated persisted active configuration, and rejects an unexpected file.
- rollback_hot_configuration, reset_hot_configuration and
  restore_hot_configuration (id) require expectedRevision.
- list_hot_configurations supports limit and offset.
- Each nonbaseline revision has an immutable local record; the active/previous
  pointer is written atomically. Invalid or stale candidates are rejected.
- Configuration revisions have their own history, separate from the runtime,
  API and UI patch archives. Neither history is a substitute for physical QA.

## Sections and precedence

See Examples/hot-configuration.json. The example is a test profile, not an
activated or promoted playback default. Unknown configuration keys and
out-of-range values fail closed. Rules match an ordinary channel ID and/or
retry number, in order, with the last matching full policy winning. Missing
fields in a playback policy use native defaults. Partial runtime objects
are composed against the factory Settings defaults, not arbitrary device state.
A JavaScript configure hook takes precedence over configuration runtime policy;
per-attempt hooks receive configuration rules as input. UI revision presentation
takes precedence over the configuration presentation object. Existing per-attempt
runtime overrides keep ownership of the active item; reload the current stream
to apply new transport or per-attempt selection. Shared gateway playback retains
its native ticket path and is not affected by ordinary stream transport rules.

Live configuration can change sampling/sync/reconnect intervals, future
runtime policy, video gravity, overlay geometry and interpreter budgets.
AVPlayer item buffer/bitrate/resolution are native preferences, not guarantees
of the server's encoder output. Transport format, encoding, and session changes
take effect on next stream open. reload_current_stream requests that transition
without relaunching the app. Its receipt is not playback acceptance.
get_media_tracks, select_media_track (kind=audio|subtitle, index=number|auto|off),
seek_live (offsetSeconds), seek_relative (seconds) are bounded by actual
available tracks and seekable time ranges. Some live feeds are not seekable.

## Resource and reliability controls

Process physical footprint and accumulated process CPU time are sampled through
native Mach/getrusage counters. CPU percent is measured in units of one logical
core; it can exceed 100% on multicore hardware. The host does not report other
processes such as WebKit subprocesses as app process CPU. Telemetry includes
interpreter evaluations, failures and elapsed wall time. A configurable memory
threshold or an iOS memory warning suspends scheduled module automation; the
native player, LAN recovery and explicit engineering commands remain available.
resume_automation explicitly re-enables it. Resource thresholds are not iOS
jetsam limits. Interpreter deadlines, memory and command counts have validated
native ceilings; no unbounded scripts, OS modules or arbitrary native calls.

Old diagnostic event files are trimmed by age when count/byte limits are
exceeded. Pruning counts and bytes are exposed. Receipt files, health ledger,
patch versions and pending upload batches are not deleted by that routine.
Unuploaded diagnostic events beyond quota may be lost; export before lowering
quotas if historical logs matter.

## Session learnings applied

- Working build 1015 remains the packaging baseline: Intel macOS 15 runner,
  Xcode 16.4, SDK 18.5, ARM64, iOS 15 deployment, existing ad hoc signing and
  both signed/unsigned IPA assets. Never substitute the local compiler build
  for a release candidate.
- Ordinary TS attempts had zero player bytes/clock despite server segments;
  fMP4 decoded once but failed continuity. Retain default transport until
  a scoped policy passes physical endurance. Format/copy choices now apply at
  negotiation and the actual stream URL. Do not increase timeout as a cure.
- Shared Surf is server-owned. No fake next/previous underlying channel
  assertion. Separate shared continuity from real rotation.
- Existing UI and runtime revisions remain backward compatible. Use the
  fullscreen replacement example and configuration example as independent tests.
- Live telemetry has a direct snapshot command, not just the periodic metrics.
- Config failures, activation conflicts and immutable revision collisions must
  preserve the current source. Existing patch rollback remains independent.
- Private credential import is opt-in and only fills missing compatible fields;
  secrets never belong in a public IPA or configuration revision. The actual
  private defaults file must be provisioned separately.

## Acceptance matrix

CI: policy fixtures, legacy decoding, per-retry matching, stale CAS rejection,
configuration persistence/collision, invalid field/range rejection, interpreter
timeout/command cap, process counters, bounded log retention, HLS fixture,
runtime/API/UI rollback, TCP malformed and oversized requests, unsigned
native compile, accepted packaging and signature checks.

Physical candidate: install through TrollStore; query version/capability;
measure idle and active process CPU/memory; start shared All/Sports/Movies;
install a configuration while playing; check unchanged launch ID and media
generation unless explicitly reopening; verify buffer/bitrate/resolution request;
check fullscreen transition and replacement; seek and tracks when available;
verify rejection, reload, rollback, restart persistence, sustained frame and
clock progress; check ordinary transport experiments individually; preserve
baseline if any fail.

## Remaining native boundaries

Native decoding/OS frameworks, entitlements, signing, permissions, application
installation, recovery shell, new native primitives, and server internals
remain compiled/externally deployed. Picture in Picture restoration needs
separate native integration and physical verification. Resource suspension
currently pauses scheduled module automation, not all interpreter calls or
WebKit processes. No guarantee that a future OS/server change will never
require another IPA.

References:
- https://developer.apple.com/documentation/avfoundation/avplayeritem
- https://developer.apple.com/documentation/avfoundation/avplayeritem/preferredmaximumresolution
- https://developer.apple.com/documentation/avfoundation/avplayeritem/seekabletimeranges
- https://developer.apple.com/documentation/xcode/reducing-your-app-s-memory-use
- https://developer.apple.com/documentation/avkit/adopting-picture-in-picture-in-a-custom-player
- https://bellard.org/quickjs/quickjs.html

## Staged files and timed experiments
stage_hot_configuration accepts configuration JSON and expectedRevision.
stage_hot_configuration_from_db accepts an exact id/blob SHA and expectedRevision.
These write a staged file without touching the active player. To apply, call
reload_hot_configuration with expectedRevision and stagedRevision. Alternatively
begin_configuration_trial accepts the same staged/base revisions and seconds
(30–3600). The prior configuration is durably journaled before activation.
finish_configuration_trial accepts trial id, expectedRevision and outcome
accept/rollback; accept also requires approval=explicit. It is explicit operator
confirmation, not an automatic hardware certification. A pending trial rolls
back on expiry or on the next app launch, including an interrupted activation.
An active campaign is paused on rollback; test results record all four source
revisions, and revision changes during a step block that result. A transport
change still requires opening/reloading the stream. Rollback does not prove
video has resumed until a new playback test confirms it.

The presentation options nativePlaybackControls, pictureInPictureAllowed and
automaticPictureInPicture can restore the AVKit system control path without
another build. The custom fullscreen controls remain the default. Native PiP
availability and system fullscreen behavior must be verified on target iOS;
the configuration does not promise unsupported PiP behavior.
Read-only telemetry queries no longer create persistent telemetry events.

Native item events (stall, error log, end, access log) are recorded with
numeric metrics, revision and stream generation, without URLs. Configure
playerEventDiagnostics and playerEventSampleSeconds in operations to control
access-log sampling; direct player snapshots expose the generation for
continuity assertions. WebKit component termination has one retry and then a
native-visible suspended component message; no repeated automatic recovery
loop. An explicit configuration reset quarantines an unreadable trial journal.

operations.surfTransport defaults to auto (working shared gateway when
available). Set device explicitly to test real device-owned rotation, then
start Surf again; this is experimental on channels whose ordinary playback
has not yet passed. Explicit server Surf remains available. Direct shared
playback reports server ownership even when local Surf mode is stopped;
Next reconnects the current shared category rather than pretending to
advance its underlying source. Ordinary Next does not implicitly start Surf.
