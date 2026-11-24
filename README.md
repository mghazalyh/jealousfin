# Jealousfin

Independent native SwiftUI Live TV client targeting iOS 15.0 and newer. This initial development build uses AVPlayer, real Jellyfin catalogue and EPG, server-negotiated playback, Favorites, session reporting, local Surf rotation, progressive health exclusions, Insights, persistent structured events, LAN commands, QuickJS-NG scripting, and GitHub-backed synchronization.

## Install and configure
Use the IPA from GitHub Releases with TrollStore on a compatible device. No signing certificates are used by CI; installation and actual iOS 15/16 acceptance have not yet been tested. In Settings supply the Jellyfin origin, username and password and a GitHub token with access to the private JealousDB repository. Credentials remain in local application configuration; no credentials are embedded in the public source or release. The UI uses real server metadata and does not embed simulated channel health data.

## Operational contract
The client stores health observations as a deduplicated ledger. A confirmed failure produces a 3, 6, 9, 12, 15, 18 or 21 day hiatus. A failed eighth attempt after expiry permanently excludes a channel; explicit approval reinstates it. A successful video/progress validation resets the consecutive streak but preserves lifetime counts. The initial classifier avoids imposing widespread exclusions during an apparent infrastructure failure; unconfirmed failures are logged. Playback evidence is limited to AVPlayer-reported progress and video readiness/decoded samples; this is not a physical display or lip-sync measurement.

JealousDB namespace: `jealousfin/`. Configure token in the app, not in GitHub Actions or the public source. Local events survive upload outages, and are sent in immutable batches. Remote command and receipt files are under `control/devices/<device-id>/commands/<id>.json` and `receipts/<id>.json`. Device heartbeat identifies the target and capabilities.

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

System telemetry begins immediately on launch, before Jellyfin login, and repeats every 15 seconds while executing. Startup, network changes, foreground/background lifecycle transitions and heartbeats produce durable structured events. System samples include launch ID, build, uptime, device/OS, thermal and battery state, physical memory capacity, available storage, network path, connection and queue status. Playback samples remain separate and are cleared when changing or stopping streams. JealousDB upload is attempted immediately when configured, independent of successful health merging or remote command execution; pending records remain local on failure. The Telemetry page and get_telemetry expose system and playback measurements separately. No telemetry enable switch or manual start is required.


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

Repository-hosted patches: commit `jealousfin/engineering/modules/<revision>.js` into private JealousDB, then submit `install_runtime_module_from_db` with the `id` and exact Git blob `sha`. Modules can be up to 192 KiB. The module state includes a durable command journal so interrupted actions reuse the existing command receipt rather than being silently executed twice. The engineering screen displays active revision and campaign evidence and provides pause, resume and stop controls.


## Runtime-driven UI (ABI 1)

Jealousfin supports durable UI layout revisions independently of runtime policy modules. A layout can add native SwiftUI controls to extension slots on every built-in screen and create custom pages reachable through More or a runtime link. Existing core navigation, guide, playback and settings remain available. Use Examples/dynamic-ui.json as a working example.

Native primitives: text, metric, button, input, toggle, picker, slider, progress, card, vstack, hstack, divider, link, spacer. The web primitive accepts a local HTML/CSS/JS/SVG fragment and a fixed height in points for bespoke visualizations. Its WebKit view receives a sanitized application state snapshot via the jealousfin-state event and can invoke registered application commands through window.jealousfin.command(name, stringArguments, requestID), receiving jealousfin-receipt events. It does not load remote navigation or expose native symbols. Use native forms for persistent input fields; the HTML component maintains its own ephemeral DOM state.

Remote operations through the normal command mailbox:
- ui_get_capabilities: enumerate slots, components and supported commands.
- install_ui_layout: layout = JSON manifest string, for smaller changes.
- install_ui_layout_from_db: id + sha of jealousfin/engineering/ui/<id>.json in JealousDB. Maximum manifest size 192 KiB.
- get_ui_layout, rollback_ui_layout, reset_ui_layout.
- ui_set_value: key and value, for an existing form field.
- ui_open_page: id; ui_close_page.
- ui_trigger: id of a native command-backed button. This invokes its action, not a simulated touch; genuine touches have origin=touch in logs.

Bindings: valueKey, visibleWhen and enabledWhen reference state.<path> or form.<field>. Arguments beginning with $state. or $form. are resolved when invoked. A script action may use api.state.form for current native UI inputs. Native inputs persist across launches and compatible revisions. Layouts are validated before activation; a rejected revision does not replace the previous one. More > Restore native interface removes all runtime extensions. Runtime UI fields are for ordinary non-secret application data, not credentials.

UI revision and page IDs appear in the device heartbeat; rendered slots, command origin, activation and action results are logged. No new IPA is required for changes supported by this UI ABI. New native controls or changes to the compiled base screens still require a host build.


## Interactive engineering decisions (ABI 1)

Use the normal device command mailbox with action `present_decision` and argument `prompt` containing a JSON manifest. Required fields: schemaVersion=1, id, title, message, priority (Emergency, High, Attention, Informational), expiresAt (ISO 8601, within seven days), choices (2–4 with at least one no-action choice). Optional fields: evidence, expectedChannelID, expectedPatchID. Choice actions contain command and fixed string arguments. The runtime can propose decisions but cannot choose them. Local native overlay taps are the approval mechanism; remote `cancel_decision` cancels an unanswered prompt without applying a repair. `get_decisions` lists recent decision state or returns an exact record by id. Duplicate IDs cannot replace prior decisions. Context and expiry are rechecked before execution, and the original command receipt is recovered after relaunch. Decision status is shown in Insights; immutable uploaded snapshots live under `engineering/<deviceID>/decisions/<id>/revision-N.json`. The normal telemetry journal records presentation, response and outcome. Prompts display over the app's SwiftUI screens and dynamic pages while active; they do not attempt to display above system-owned fullscreen playback or while suspended. JealousDB delivery follows the existing sync cadence, not an instant push channel. See Examples/decision-prompt.json for the schema; supply a fresh expiry before use.


## Reloadable Jellyfin API adapter (ABI 1)

Independent of UI and business logic modules. Install with install_jellyfin_api (id, source) or install_jellyfin_api_from_db (id, sha of jealousfin/engineering/jellyfin-api/<id>.js in JealousDB). get_jellyfin_api reports active revision and fault. rollback_jellyfin_api and reset_jellyfin_api can restore a locally validated adapter during a server outage; offline restoration is not a successful live probe.

Hooks: configure(spec) for authentication field names/response paths, auth header mode, image path template and timeout; request(input) for endpoint, method, query and JSON payload; response(input) for response normalization; stream(input) for selected playback path and method. Return null to use native defaults. Authentication credentials and token extraction remain native. Read-only operations retain GET. The final playback origin must match the configured Jellyfin server. Hooks are synchronous and must not dispatch commands or call console.log.

Activation stages authentication, health and catalogue probes, attempts EPG retrieval, drains existing operations, persists the new adapter and resumes viewing or campaigns where possible. Request telemetry includes operation, revision, HTTP status, latency and X-Jealousfin-Request-ID for server-side correlation if that header is captured. Probe commands support health, channels, programs and recordings. Decision prompts can propose an API module revision and bind expectedJellyfinAPIRevision. Settings retains a native recovery control.

Server-side changes remain separate: inspect Windows Jellyfin and companion logs, diagnose a real defect, prepare a scoped reversible change and verify both sides. This update changes no Windows services. The adapter does not expose OS shell execution or replace native transport.


## Live patch archival and release promotion

Runtime, dynamic UI and Jellyfin API activation, reset and rollback automatically capture the **complete active source set** in a durable local queue. Launch and heartbeat reconciliation recover a missed archive. No settings, form values, credentials or operational telemetry belong in the source set. The private archive lives at jealousfin/engineering/<deviceID>/patches/<archiveID>.json in JealousDB. Each archive is immutable. Heartbeats expose the archive ID and bundled release revision/digest.

Commands:
- get_patch_archive: current source set metadata.
- archive_active_patches: reconcile and return the current durable archive ID.
- promote_patch_archive: id (current archive), expectedRevision (current canonical release revision), evidence (reviewed test/result references). Promotion is explicit so failed or experimental fixes do not become a release default. It publishes immutable release history and updates jealousfin/engineering/releases/baseline.json using its prior Git blob SHA. A changed active source set or competing baseline is rejected.
- get_release_baseline: read the canonical reviewed baseline.

GitHub Actions imports the reviewed baseline using the repository secret JEALOUSDB_RELEASE_TOKEN (a token scoped to private jealousdb, Contents read-only), validates the manifest, generates actual Swift source containing the approved JavaScript and declarative UI source, runs regression and interpreter fixture tests, then packages the source into the IPA. The release notes identify the embedded revision. The secret is mandatory for release builds. The workflow imports the canonical private baseline before installing build dependencies, checks its Git blob integrity, and fails rather than falling back to a potentially stale source-controlled copy. The token is scoped to the import step only; it is never passed to compilation or packaging. The build does not execute unreviewed device archives or automatically synthesize native Swift equivalents from JavaScript. Approved modules are shipped verbatim as application code interpreted by the native host.

Fresh installs seed the packaged modules without downloading them. Upgrades replace components that still follow the previous packaged baseline, preserving independent local overrides and compatible UI form values. A source revision rejected during startup still uses the ordinary module recovery path. Device identity and review notes are excluded from the embedded manifest. Promotion does not remotely install an IPA: the next normal IPA installation carries the accepted sources.


## Internal hot patch version control

Every source-set change retains an immutable local version as well as the existing private JealousDB archive. The archive includes a parent version ID, optional named checkpoint and known-good marker, and the original target ID when created by restoration. The upload queue is removed after successful upload but the local version is retained. Earlier uploaded versions of this device can also be retrieved by their archive ID if not available locally.

More > Hot patch version history displays source revisions, timestamps, checkpoints and the current head. Use the native restore confirmation to restore the runtime, UI, Jellyfin adapter, or the complete source set. Remote commands: list_patch_versions (offset, limit), compare_patch_versions (from, to; component-level comparisons), checkpoint_patch_version (label, knownGood=true only after review), restore_patch_version (id, scope=all/runtime/jellyfinAPI/ui, expectedHead), restore_known_good (scope, expectedHead). A restore creates a new version rather than deleting history. The expected head protects against stale concurrent edits. Existing commands for one-step rollback remain available.

Restoration prevalidates interpreter code and UI, saves the current versions, and journals all three local stores before replacement. An interrupted transaction restores the previous stores on startup. API changes drain sessions and reconnect when possible; restoration does not require the server to be reachable and is not proof of playback health. Compatible UI form values are retained. Device settings, server state, health ledger, and release promotion are separate from this version history. No automatic decision that a behavioral change is good is made; mark a known-good checkpoint after evidence, then request a restore when needed. Existing archives predating this feature are not automatically enumerated; retrieve a specific archived ID from JealousDB to import it. No automatic history deletion is performed.

## Dedicated database migration

This branch targets `mghazalyh/jealousdb` on `main` with the existing `jealousfin/` directory layout. A token restricted to the new repository requires a client built from this branch; the old installed application still targets TorrioDB. The initial snapshot is not continuous replication. Do not switch tokens or issue new remote commands against the new repository until the client has been switched and verified. Preserve existing local device IDs and refresh the snapshot before actual cutover if the old client continues to write.

### Editable database address

The native Settings screen prefills `https://github.com/mghazalyh/jealousdb` for fresh or older installations. The URL is editable and persisted alongside the existing token. Only HTTPS github.com owner/repository addresses are accepted; the client derives api.github.com and refuses redirects. The active repository is pinned until app relaunch to avoid changing destinations during multi-request writes or remote commands. Save settings, then fully close and reopen the app to apply a new repository address. Pending local records and device identity are retained. The CI baseline importer is separately configured for the reviewed release database, not controlled by device settings.

### LAN primary remote control

LAN machine mode is the default, including older settings without a stored mode. Settings offers LAN machine or GitHub database. LAN mode cancels cloud synchronization and blocks all database requests while preserving local telemetry and receipts. GitHub mode permits a LAN machine to take over at any time with POST /command, action set_control_mode and arguments {"mode":"lan"}. LAN control does not require a token. Direct control uses TCP 8765, with Bonjour service `_jealousfin._tcp`; HTTP command body limit is 4 MiB, including JSON overhead. Read-only GET endpoints: /health, /status, /telemetry, /logs, /capabilities, /results, /receipt/<id>. Commands use the existing durable receipt journal and full application dispatcher. Supply patch source inline for GitHub-independent editing. iOS suspension can interrupt reachability; keep the app running. No OS shell endpoint is added. In-flight commands may finish when mode changes; new GitHub operations are blocked in LAN mode.

### Private deployment defaults

A private, user-specific build can include `JealousfinDefaults.plist` inside its application bundle, containing string keys `server`, `username`, and `password`. On first launch or when connection fields are empty, these values prefill Settings. Existing server and account values take precedence. Do not commit the private plist to this public repository or publish an IPA containing private credentials to public releases. The default public build contains no credentials. The GitHub token remains a separately persisted setting.
