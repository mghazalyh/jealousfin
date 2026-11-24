# Jealousfin

Independent native SwiftUI Live TV client targeting iOS 15.0 and newer. This initial development build uses AVPlayer, real Jellyfin catalogue and EPG, server-negotiated playback, Favorites, session reporting, local Surf rotation, progressive health exclusions, Insights, persistent structured events, LAN commands, QuickJS-NG scripting, and GitHub-backed synchronization.

## Install and configure
Use the IPA from GitHub Releases with TrollStore on a compatible device. No signing certificates are used by CI; installation and actual iOS 15/16 acceptance have not yet been tested. In Settings supply the Jellyfin origin, username and password and a GitHub token with access to the private TorrioDB repository. Credentials remain in local application configuration; no credentials are embedded in the public source or release. The UI uses real server metadata and does not embed simulated channel health data.

## Operational contract
The client stores health observations as a deduplicated ledger. A confirmed failure produces a 3, 6, 9, 12, 15, 18 or 21 day hiatus. A failed eighth attempt after expiry permanently excludes a channel; explicit approval reinstates it. A successful video/progress validation resets the consecutive streak but preserves lifetime counts. The initial classifier avoids imposing widespread exclusions during an apparent infrastructure failure; unconfirmed failures are logged. Playback evidence is limited to AVPlayer-reported progress and video readiness/decoded samples; this is not a physical display or lip-sync measurement.

TorrioDB namespace: `jealousfin/`. Configure token in the app, not in GitHub Actions or the public source. Local events survive upload outages, and are sent in immutable batches. Remote command and receipt files are under `control/devices/<device-id>/commands/<id>.json` and `receipts/<id>.json`. Device heartbeat identifies the target and capabilities.

LAN HTTP: `GET /health`, `POST /command` on port 8765; request JSON includes schemaVersion=1, id, action, arguments (string-valued object), optional expiresAt ISO timestamp. Example:
```json
{"schemaVersion":1,"id":"status-001","action":"get_status","arguments":{}}
```
The LAN endpoint has no app-level authentication; deployment relies on the external network boundary. The command dispatcher provides application capabilities, not OS shell access. JS scripts execute through the same dispatcher:
```js
console.log(api.state.status);
api.command("set_rotation_minutes", {minutes:"5"});
api.command("start_surf", {category:"All Channels"});
```
A five-second interpreter deadline interrupts runaway JavaScript and the next invocation starts a fresh runtime. This is not unrestricted native hot patching. Native defects still need an IPA update.

## Server-wide enforcement
The client filters its own catalogue. `Server/filter_m3u.py` is an offline reference adapter for a filtered tuner catalogue using a verified mapping between Jellyfin item IDs and stable M3U tvg-ids. It never edits the production source. **Global exclusion in other Jellyfin clients is not active** until an independently verified server integration also filters the Surf companion source selection and handles existing sessions/catalogue refresh. No server changes are performed by this release.

## Build
GitHub-hosted macOS Actions only. Push to main triggers policy regressions, XcodeGen generation, iOS arm64 build, IPA packaging, checksum and prerelease publication.

## Initial release limitations
Physical iOS 15/16 playback and TrollStore installation, later iOS, long backgrounding, recovery after process termination, global Jellyfin exclusion, exact server Surf category membership, complete decoder/renderer instrumentation, advanced script hooks and full DVR lifecycle remain unverified or incomplete. AVPlayer codec limits may require server transcoding. No existing PlayTorrio or PS3 components are used or modified.

## Review corrections

The post-initial-release audit adds serialized playback session teardown, pause-aware rotation clocks, foreground failure classification, multiple distinct decoded-frame samples with playback progression, bounded retries and recovery, separation of client-managed versus server-managed Surf, serialized Favorites requests, full-size paginated channel query responses, malformed-command isolation, command queue fairness beyond 1000 GitHub entries, and canonical health-ledger ordering with fractional timestamps. The test suite covers these policy and queue invariants and the offline server filter's progressive ban and reinstatement behavior.

Selecting a Jellyfin virtual Surf row normally starts client-managed rotation; the antenna button requests the original opaque server Surf stream. Category membership is derived from server-provided genres/tags, not guessed. An empty category is surfaced in Insights and retried. The command protocol distinguishes a request to open a channel from confirmed playback.

The server filtering adapter remains an offline integration component, not an installed or active server-wide enforcement service.

## Automatic telemetry

System telemetry begins immediately on launch, before Jellyfin login, and repeats every 15 seconds while executing. Startup, network changes, foreground/background lifecycle transitions and heartbeats produce durable structured events. System samples include launch ID, build, uptime, device/OS, thermal and battery state, physical memory capacity, available storage, network path, connection and queue status. Playback samples remain separate and are cleared when changing or stopping streams. TorrioDB upload is attempted immediately when configured, independent of successful health merging or remote command execution; pending records remain local on failure. The Telemetry page and get_telemetry expose system and playback measurements separately. No telemetry enable switch or manual start is required.


## On-device engineering and reloadable modules (ABI 1)

After launch, use the existing device command mailbox. Command arguments are string values.
- start_device_tests: plan = JSON of {id, steps:[{kind, channelID?, seconds?, source?}], repeatCount, repeatForever}. Steps: system, catalogue, network, audio_route, reconnect, playback, pause_resume, endurance, surf_rotation, background, stop, script_probe.
- pause_device_tests / resume_device_tests / stop_device_tests / get_device_tests.
- install_runtime_module: id and source (JavaScript). reload_runtime_module, rollback_runtime_module, reset_runtime_module, get_runtime_module.
- Results are durable under engineering/<deviceID>/results and checkpoint.json. Modules persist across launch; interrupted activation rolls back. Tests never alter channel health by their observations.

Module example:
```javascript
var hooks = {
  configure: function(p) {
    p.startupTimeout = 45;
    p.forwardBufferSeconds = 8;
    p.recoveryMode = "advance_surf";
    return p;
  },
  select: function(input) {
    return input.candidates.length ? input.candidates[0].id : null;
  },
  tick: function(input) {
    var state = input.state || {};
    state.runs = String(Number(state.runs || 0) + 1);
    return {state: state, actions: [], nextSeconds: 30, insights: []};
  }
};
```
The tick hook receives current application state, recent events, previous action receipts and persistent string state. It can emit application commands, custom insights and schedule its next evaluation. It has a bounded interface and cannot modify compiled iOS/native frameworks, the OS, or another application. It cannot approve bans or self-replace modules. Three consecutive tick errors disable the hook until reload or replacement. Native host updates still require a new IPA. Actual background transitions are measured, not manufactured. No indefinite execution while iOS suspends the process is promised.

### Interpreter dependency

The GitHub workflow fetches QuickJS-NG v0.10.1 at commit `3c9afc9943323ee9c7dbd123c0cd991448f4b6c2`, compiles it into the native host, and runs an interrupt-and-recovery smoke test before the IPA build. Each evaluation uses a fresh interpreter with a 32 MiB allocation limit, bounded stack, and a five-second interrupt deadline. No JIT, standard-library OS modules or arbitrary native symbol invocation is required. Module and command interfaces are unchanged. See https://github.com/quickjs-ng/quickjs and the included upstream license.

Repository-hosted patches: commit `jealousfin/engineering/modules/<revision>.js` into private TorrioDB, then submit `install_runtime_module_from_db` with the `id` and exact Git blob `sha`. Modules can be up to 192 KiB. The module state includes a durable command journal so interrupted actions reuse the existing command receipt rather than being silently executed twice. The engineering screen displays active revision and campaign evidence and provides pause, resume and stop controls.


## Runtime-driven UI (ABI 1)

Jealousfin supports durable UI layout revisions independently of runtime policy modules. A layout can add native SwiftUI controls to extension slots on every built-in screen and create custom pages reachable through More or a runtime link. Existing core navigation, guide, playback and settings remain available. Use Examples/dynamic-ui.json as a working example.

Native primitives: text, metric, button, input, toggle, picker, slider, progress, card, vstack, hstack, divider, link, spacer. The web primitive accepts a local HTML/CSS/JS/SVG fragment and a fixed height in points for bespoke visualizations. Its WebKit view receives a sanitized application state snapshot via the jealousfin-state event and can invoke registered application commands through window.jealousfin.command(name, stringArguments, requestID), receiving jealousfin-receipt events. It does not load remote navigation or expose native symbols. Use native forms for persistent input fields; the HTML component maintains its own ephemeral DOM state.

Remote operations through the normal command mailbox:
- ui_get_capabilities: enumerate slots, components and supported commands.
- install_ui_layout: layout = JSON manifest string, for smaller changes.
- install_ui_layout_from_db: id + sha of jealousfin/engineering/ui/<id>.json in TorrioDB. Maximum manifest size 192 KiB.
- get_ui_layout, rollback_ui_layout, reset_ui_layout.
- ui_set_value: key and value, for an existing form field.
- ui_open_page: id; ui_close_page.
- ui_trigger: id of a native command-backed button. This invokes its action, not a simulated touch; genuine touches have origin=touch in logs.

Bindings: valueKey, visibleWhen and enabledWhen reference state.<path> or form.<field>. Arguments beginning with $state. or $form. are resolved when invoked. A script action may use api.state.form for current native UI inputs. Native inputs persist across launches and compatible revisions. Layouts are validated before activation; a rejected revision does not replace the previous one. More > Restore native interface removes all runtime extensions. Runtime UI fields are for ordinary non-secret application data, not credentials.

UI revision and page IDs appear in the device heartbeat; rendered slots, command origin, activation and action results are logged. No new IPA is required for changes supported by this UI ABI. New native controls or changes to the compiled base screens still require a host build.
