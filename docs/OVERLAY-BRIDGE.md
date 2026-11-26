# Language evidence overlay bridge

This lane adds one supported, server-side publisher for the installed app. It
does not modify Swift, install an IPA, restart a service, issue a live command
or touch the gateway source.

## Release integration

The release-integration branch wires this adapter into the existing gateway
lifecycle in `Server/iptv/gateway.py`:

- one bounded daemon worker is started with the existing American workers and
  stopped with the gateway; there is no periodic timer or new schedule;
- catalogue and authenticated playback boundaries enqueue a channel refresh;
  an evidence commit enqueues one update; an accepted Mistaken English marker
  enqueues one corrected label; `/trial-status` may pump a pending retry;
- before every write the gateway re-probes the verified current device channel
  and stream generation. Evidence for a different channel or stream generation
  is suppressed, and an unavailable probe publishes nothing;
- the only command is the existing authorized `ui_set_value` with
  `arguments:{key:"form.languageOverlay",value:"..."}`. Authentication is never
  disabled and the token is never logged or exposed;
- failures are bounded and fail safe; `GET /trial-status` exposes
  `overlay.configured`, `overlay.fault`, `overlay.staleLabel`, the current
  channel and queue depth. See `docs/release-integration-20261127.md`.

## Native contract used

The installed ABI-1 UI accepts a remote value only through an editable node:

- `Sources/AppModel.swift` `ui_set_value` validates the value with the active
  layout's `formNode`; `formNode` only finds `input`, `toggle`, `picker`,
  `slider`, `segmented` and `menu` nodes.
- `DynamicUIDomain.swift` allows a non-editable node to use the same
  `form.<key>` binding as an editable node. Only duplicate *editable* form
  fields are rejected.
- `DynamicUIViews.swift` renders `text`, `metric` and `banner` from
  `model.dynamicValue(node.valueKey)`; `AppModel.dynamicValue` resolves a
  `form.` key from the persisted form values.

Therefore `Examples/language-evidence-overlay-20261127.json` (revision
`language-evidence-overlay-bridge-20261127`, deliberately distinct from Lane
A's candidate revision so installing this merge is never rejected as a
duplicate) declares:

- a hidden editable input `language-overlay-value` with
  `valueKey: "form.languageOverlay"` and
  `visibleWhen: "form.languageOverlayVisible"` (that condition key has no
  editable node, so it is absent and false);
- a native text label `language-evidence` with the same
  `valueKey: "form.languageOverlay"`.

One `ui_set_value` command atomically replaces the label. `languageOverlay` is
the only field the bridge writes. The layout also preserves the deployed
English/Malay source switcher and the existing Spanish mark/unmark actions.
The `language-mistaken` button retains the manual correction action. It emits
the Lane A v4 `schemaVersion: 2` / `mistaken_english` marker; on the older v3
backend it is rejected, while the existing `schemaVersion: 1` Spanish add/remove
actions remain unchanged.

## Adapter

`Server/iptv/overlay_bridge.py` contains:

- `AuthorizedDeviceTransport`: wraps the existing
  `prototype_selector.device_command(config, token, "ui_set_value", ...)` path.
  It never reads the environment, logs the token, or chooses a host.
- `OverlayBridge`: formats one string, deduplicates by event ID and text,
  rejects evidence for another channel or an older event, rate-limits transport
  attempts, retries a bounded number of times, expires stale pending values and
  fails safe with a diagnostic fault.
- `manual_correction` / `confirm_correction`: a gate for the Lane A v4
  `mistaken_english` marker. It requires the adapter's latest published event
  and the current channel to match, rejects a second correction of the same
  event, and never issues playback or rotation commands.
- `validate_layout`: a deployment preflight for the JSON asset. It mirrors the
  bounded native checks, not the native validator itself.

No locks are held during transport, and there is no timer, scheduler, worker or
periodic external job. If the transport is unavailable when a channel changes,
the old label cannot be cleared without a successful command; the bridge stops
after the bounded retries, exposes `status()["staleLabel"]` and the fault, and
publishes a fresh label on the next lifecycle-driven evidence event. It never
pretends the stale text was replaced.

## Exact gateway integration points (not edited by this lane)

Lane A owns the v4 evidence backend and `gateway.py`. Construct the adapter
where the gateway already owns the selector configuration and device token:

```python
import overlay_bridge

transport = overlay_bridge.AuthorizedDeviceTransport(config, token)
bridge = overlay_bridge.OverlayBridge(transport)
```

Call it only from existing boundaries:

1. In `GatewayHandler.route_prototype`, after the existing
   `apply_device_spanish_request(config, token)` call on
   `/LiveTv/Channels` or `/Items/<id>/PlaybackInfo`, call
   `bridge.observe_channel(channel_id, channel_name)` when the current
   channel changes.
2. When Lane A records or reloads evidence, call
   `bridge.publish_evidence(channel_id, channel_name, evidence)`. The accepted
   adapter row keys are `eventID`, `at`, `language`, `languageName`, `count`,
   `threshold`, `phase`/`outcome` and `confidence`.
3. On an existing poll/trial-status boundary, call `bridge.pump()` to deliver a
   deferred or retried value. Do not add a new timer.
4. Before applying a manual `mistaken_english` marker, use
   `bridge.manual_correction(...)`; after Lane A accepts the retraction, call
   `bridge.confirm_correction()` and publish the cleared state. The existing
   `spanish-read` node and Spanish add/remove marker path stay unchanged.

The gateway remains responsible for resolving the device's current channel. If
that identity is unknown, do not publish a language label; publish the waiting
label or leave the field alone rather than guessing.

## Staleness and matching limits

The native binding language can compare only for truthiness
(`visibleWhen`/`enabledWhen`); it cannot compare an event's channel ID with the
current channel ID. An event module could compare `currentChannelID` in a JS
hook, but it would add a second execution surface and still depends on the same
gateway channel identity, so this lane keeps the comparison in the adapter's
pre-publish gate instead of adding a module. Strict matching is therefore
enforced before the text is pushed, using the gateway's current-channel
identity and the event's own `channelID`/`at`/`eventID`. The label is not
per-frame proof that the shown stream is still the same channel. On a
transition the bridge immediately replaces the prior label with a waiting
label for the new channel, and evidence for the old channel is suppressed. A
manual correction is checked against the adapter's latest published event and
current channel; Lane A's `retract_evidence` remains the authority that chooses
the exact latest stored event and rejects replayed nonces.

## Verification

Run from `Server/iptv`:

```sh
python3 test_overlay_bridge.py
```

The tests use only the JSON layout asset and
`Server/iptv/fixtures/language-overlay-evidence.json`. They cover layout
validity, the hidden editable field, text-value replacement, the authorized
transport command shape, channel transitions, stale/out-of-order evidence,
deduplication, rate limiting, bounded retries, transport failure, pending
expiry and the manual-correction gate.
