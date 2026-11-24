# Jealousfin

Independent native SwiftUI Live TV client targeting iOS 15.0 and newer. This initial development build uses AVPlayer, real Jellyfin catalogue and EPG, server-negotiated playback, Favorites, session reporting, local Surf rotation, progressive health exclusions, Insights, persistent structured events, LAN commands, JavaScriptCore scripting, and GitHub-backed synchronization.

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
A five-second script deadline returns an error; a nonterminating interpreter remains isolated and unavailable until it exits or the app restarts. This is not unrestricted native hot patching. Native defects still need an IPA update.

## Server-wide enforcement
The client filters its own catalogue. `Server/filter_m3u.py` is an offline reference adapter for a filtered tuner catalogue using a verified mapping between Jellyfin item IDs and stable M3U tvg-ids. It never edits the production source. **Global exclusion in other Jellyfin clients is not active** until an independently verified server integration also filters the Surf companion source selection and handles existing sessions/catalogue refresh. No server changes are performed by this release.

## Build
GitHub-hosted macOS Actions only. Push to main triggers policy regressions, XcodeGen generation, iOS arm64 build, IPA packaging, checksum and prerelease publication.

## Initial release limitations
Physical iOS 15/16 playback and TrollStore installation, later iOS, long backgrounding, recovery after process termination, global Jellyfin exclusion, exact server Surf category membership, complete decoder/renderer instrumentation, advanced script hooks and full DVR lifecycle remain unverified or incomplete. AVPlayer codec limits may require server transcoding. No existing PlayTorrio or PS3 components are used or modified.
