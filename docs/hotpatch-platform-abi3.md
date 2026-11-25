# Jealousfin hot patch platform ABI 3

This extension is native source only until an IPA containing it is installed. It retains ABI 2's existing configure, playbackAttempt, select and tick interfaces and the existing adapter/UI/configuration stores. It adds separately managed event modules; it does not replace or weaken the native recovery shell, eligibility checks, or protected credentials.

## Discover and query

LAN and authenticated GitHub commands use the existing command envelope. Discover commands with get_capability_registry, get_command_schema and get_native_capabilities. The registry lists availability by LAN, legacy runtime, event module and dynamic UI; schemaComplete indicates whether an explicit command argument schema is present. Existing operation-specific validation remains authoritative. get_effective_configuration reports current values, section owners, native runtime defaults, active attempt and gateway penalty. Optional unset values continue to use native defaults.

query_native_data takes a JSON string argument named query. Example:
`{"provider":"catalogue","offset":0,"limit":100,"search":"sports","sort":"name","eligible":true}`
Providers: catalogue, health, rotation, playback, telemetry (retained in-memory events), gateway, preferences, patches, capabilities. Page limit 200. Response contains total, offset, limit, nextOffset and rows. Eligibility applies to catalogue, health and gateway. No arbitrary filesystem/network/SQL access or credential export is provided.

## Event modules

install_event_module arguments: module (JSON string), expectedRevision ("none" for new module, otherwise active revision), preserveState ("true" to retain compatible key-value state). validate_event_module preflights all subscribed hooks, including the declarative rules, without executing proposed actions. list_event_modules shows per-module evaluations, failures, latency, pending actions and last result. rollback_event_module and remove_event_module require id and expectedRevision. Rollback retains the previous definition, not its old execution checkpoint.

Example module JSON:
```json
{"id":"diagnostics","revision":"v1","hooks":["onPlaybackFailure"],"shadow":true,"cooldownSeconds":5,"dataProviders":["playback","health"],"rules":[{"id":"observe","hook":"onPlaybackFailure","equals":{"state.surfing":"true"},"actions":[{"action":"get_player_snapshot"}]}]}
```

Modules may instead or additionally return a ModuleDecision from JavaScript hooks, for example:
```js
var hooks = {
  onPlaybackFailure: function (p) {
    return {
      state: { count: String(Number(p.state.count || 0) + 1) },
      actions: [{ action: "get_player_snapshot" }],
      nextSeconds: 5
    };
  }
};
```

Hooks receive hook, event, snapshot, state, data and preflight. Subscribe to specific hooks; onEvent is the fallback handler. Data providers supply bounded first pages, not unrestricted live network calls. Event execution is drained by the native tick, with a bounded retained event window, cooldowns, one active module task and a maximum of eight modules/eight actions per decision. Actions are validated, journaled before dispatch, have expiry, and use native receipts. The host does not replay expired actions. Three consecutive failures suspend a module. Shadow mode records proposed actions and state but never dispatches them. It does not run a second media stream or automatically compare two policy outcomes.

Supported events: onLaunch, onForeground, onBackground, beforePlayback, afterPlaybackStart, onPlaybackFailure, onPlaybackStall, onChannelChanged, onRotationComplete, onCatalogueRefresh, onHealthChanged, onNetworkChanged, onMemoryPressure, onConfigurationChanged, onFullscreenChanged, onTelemetrySample, onGatewayPolicyChanged, onCommandCompleted. Event hooks are asynchronous observations, not synchronous veto hooks. The separate ABI 2 selectDecision hook supports {outcome:"select",channelID:"eligible-id"}, {outcome:"defer",retrySeconds:5}, and {outcome:"fallback"}. Native hiatus eligibility always wins.

## Activation modes

- Existing install_jellyfin_api retains the reconnect behavior by default. A new revision with scope:"catalogue" and activation:"immediate" may replace native or an existing catalogue-only adapter. It performs read-only authenticated catalogue probing using the current connection. The adapter can transform channels/programs responses, not authentication, request plans, media URLs, or authoritative gateway policy metadata. No active player item is deliberately replaced.
- stage_api_activation takes id, source, optional scope, expectedRevision and mode (when-idle, next-channel, next-cycle). The latter two are natural device-owned Surf boundaries, not server-owned rotation. The schedule is durable, CAS-checked, and consumed before attempting activation. get_staged_api_activation and cancel_staged_api_activation inspect/cancel the schedule. A general adapter still needs the existing controlled reconnection at the selected boundary.
- UI/config/runtime changes retain their existing activation and recovery semantics. This is not a universal transactional scheduler across all four source types.

## UI

Nine additional native primitives: segmented, menu, disclosure, scroll, searchList, table, barChart, banner and liveBrightness. Native tab descriptors may reorder, rename, change icons and hide optional tabs, but More/recovery must remain visible. liveBrightness is restricted to the fullscreen Surf replacement region, fixed to -10...10, and uses the native video-plane control. No contrast, saturation or gamma controls were added. Native defaults still show only one brightness control. Table and chart primitives are bounded child-node compositions; they are not general SQL or streaming chart engines.

## Adaptive startup and operational state

Per-channel profiles persist a bounded EWMA and deviation of successful item startup durations, after three observations, scoped by hashed server/account/channel identity. The learned deadline is bounded relative to the native baseline; explicit per-attempt startupSeconds wins. The runtime adaptiveChannelStartup setting can disable learning-based deadline selection. Profiles survive IPA upgrades; event modules, module checkpoints and deferred API schedules do not. Native safe reset clears the new patch-owned stores. The release audit blocks upgrades if active event modules remain or an adapter activation is staged. Promote behavior to source or explicitly retire/remove those revisions first.

## Gateway

The reviewed gateway reads gateway-runtime.json from its existing state directory at startup and at most once every two seconds while serving requests. Atomically replace the file with a new revision. No restart is required after this gateway implementation has been deployed. An invalid configuration retains the prior accepted runtime values. Example:
```json
{"schemaVersion":1,"revision":"runtime-2","upstreamTimeoutSeconds":10,"providerTimeoutSeconds":8,"upstreamRetries":1,"resourceTTLSeconds":900,"resourceLimit":10000,"activeProtectionSeconds":120,"sourceSelection":"round-robin","preferredHosts":{}}
```
Bounds: upstream/provider timeout 2...30s, retries 0...2, TTL 120...3600s, resource capacity 500...50000, active protection 30...300s (not above TTL). Selection is round-robin, preferred-first or first. preferredHosts maps known catalogue IDs to candidate hostnames, not arbitrary URLs. Existing upstream URL validation and strict SQLite policy remain in force. Lower capacity protects active resources even if temporarily above cap; new registration may fail rather than evict protected resources. trial-status reports configurationRevision, configurationFault, policy readiness and resourceLimit. Existing request IDs are propagated through registered HLS resources into source-error records; reused shared resources may be attributed to the latest registration, so this is not a complete per-client distributed trace.

## Remaining validation and scope

The isolated Swift/domain, native runtime, gateway HTTP and Python release tests and unsigned iOS build verify source contracts. Device-side scheduling, touch behavior, continuous playback through immediate activation, real provider behavior and long-duration acceptance require physical deployment. The live gateway still must migrate to policy v2 before the next IPA. This version does not implement full multi-module synchronous policy composition, comprehensive per-command argument schemas, per-hook memory attribution, a universal transaction engine, arbitrary UI replacement of the recovery shell, general reactive data subscriptions, or online provider catalogue mutation. Event module archives and rollback are separate from the legacy four-component application bundle. Further native work can extend these without exposing arbitrary OS execution. No release or live service update is performed by these tests.
