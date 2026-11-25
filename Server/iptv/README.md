# IPTV gateway and strict health observer

This is the portable source for the Mac Mini IPTV experiment, not part of the iOS binary. The existing Jellyfin-compatible HLS path remains the default. The PS3 profile is an encoder fixture only; a PS3 streaming route and physical playback are not implemented.

## Deployment

Set these environment variables in a protected local service configuration:
- JEALOUSFIN_BIND, JEALOUSFIN_PORT (defaults: loopback and 18796)
- JEALOUSFIN_STATE_DIR (private directory outside the repository)
- JEALOUSFIN_PROVIDER_URL (authorized Stremio-compatible provider base)
- JEALOUSFIN_USERNAME, JEALOUSFIN_PASSWORD, JEALOUSFIN_TOKEN, JEALOUSFIN_SERVER_ID (stable values supplied locally)
- JEALOUSFIN_DEVICE_URL (iOS LAN control URL), JEALOUSFIN_DEVICE_TOKEN (optional existing LAN authorization)

Run gateway.py and policy_observer.py as separate managed processes. Do not expose the plain HTTP endpoints to the public Internet. Keep the original gateway and client credentials until a controlled migration has been verified. Use a service manager with restart-on-failure and protected local logs. The observer polls every five seconds and never issues playback mutations. Its SQLite file stores attempt order, terminal failure events, hiatus, sustained session evidence, and resource samples; it is not a substitute for the native health ledger.

An optional local hiatus-seed.json in the state directory may contain an array of objects with id, at (ISO 8601), reason. Never commit deployment-specific data or secrets. The native app's record_channel_failure command requires explicit approval. Gateway filtering is independent of native health accounting; the client's hiatus screen only shows native ledger entries. Native and gateway exclusions may overlap.

Run python3 test_policy_observer.py for isolated policy checks. The first successful startup does not prove sustained playback. The observer can miss old events that have already rotated out of the device log window. It uses observed terminal failures, not individual retry errors, to avoid penalizing an otherwise successful session. A policy database outage fails closed; do not install a new IPA until policy readiness is verified.

## Future iOS deployment

Native defaults: strict hiatus, 10-second stall timeout, 5-second failure retry delay, advance on stall, user-configurable rotation interval. Rotation state is persisted by server, account and category and seeded once from available native ledger history. Existing settings and credentials remain intact. Active hot revisions and local rollback history reset on a different IPA version/build. A hot configuration or runtime override can still control policy. Device-owned Surf tracks actual channels; shared server-owned Surf does not pretend to identify underlying channels. Before installing a new IPA, activate and verify the SQLite-aware gateway; the new release discards the previous static adapter override and its exclusion snapshot. Do not embed fixed channel IDs or expiry dates in an IPA.

The PS3 profile is a separate JSON fixture for future development. Do not advertise a compatible PS3 endpoint until it has passed physical console testing.

## Offline compatibility snapshot

Use `python3 make_policy_snapshot.py /private/policy.sqlite3 /private/new-adapter.js` to generate an ABI-1 catalogue filter for older clients. This read-only generator is not an installer or continuous synchronization service. Keep the output private. Each activation can reconnect an older client. The gateway marker delegates ordering and exclusions to the SQLite-aware gateway. Run `python3 test_policy_snapshot.py` for regression checks.

## Policy v2 and acceptance

The new gateway requires an initialized complete SQLite catalogue, fails closed when policy is unavailable, returns the complete catalogue plus health only to opt-in clients, and rejects excluded PlaybackInfo before source resolution. Older clients still receive a filtered catalogue. The authenticated policy event endpoint deduplicates native failure and sustained-success reports; explicit approval is required for reinstatement. The default remains 3 days per failure, ban at 8. Gateway policy takes precedence over native settings. Idle resource entries expire; recently accessed active-channel entries are protected under pressure. See docs/native-release-acceptance.md for the mandatory live migration and physical/endurance checks.

## Runtime configuration reload

After deploying the reviewed gateway, place a validated `gateway-runtime.json` in its existing state directory using atomic replacement and a new revision. The gateway retains the last accepted configuration on invalid input and reports the active revision and fault via /trial-status. See [the ABI 3 contract](../../docs/hotpatch-platform-abi3.md#gateway) for allowed fields, bounds, and deployment limitations.

## Restart-safe quality and rotation

Gateway favorites are persisted atomically in private `favorites.json`; migration from a legacy gateway must export the full authenticated favorites catalogue before stopping it, and retain the original token, account/server ID and login credentials. Keep a consistent SQLite backup and checkpoint the current channel/category/pause/countdown. Existing attempt history is preserved. A legacy client requires a same-channel reload after a controlled gateway restart; opaque media handles are not portable across processes.

Explicitly sequenced complete HLS segments can be coalesced and cached for 45 seconds, with a 64 MiB global cap and 8 MiB item cap. Manifests, keys, byte ranges and unsequenced resources bypass caching. Private status exposes bounded per-channel delivery latency samples, errors and cache hits. This is delivery telemetry, not decoded frame cadence measurement, and does not transcode the stream.

The native app saves its scoped current channel, rotation pause and countdown independently of hot patch state. On startup or reconnect it chooses the newest eligible local or gateway checkpoint. If excluded, it uses the normal durable cycle instead. Live HLS resumes at a new playable position, not necessarily the exact prior frame.

On a successful catalogue fetch the gateway saves only channel names, source IDs and genres. Subsequent transient provider catalogue failures may use this known metadata snapshot, with explicit degraded status; playback resolution still uses the live provider. Segment delivery percentiles exclude cache hits. The latest media playlist duration and discontinuity count are separately exposed. Native telemetry reports contiguous buffer ahead, live-edge distance, item-level access-log totals, and decoded-frame sample freshness. Adaptive startup learning can extend, but never shorten, the configured timeout. These diagnostics do not establish displayed frame cadence or prove that judder is resolved.

## Current programme metadata

The provider catalogue's `description` (`Now: ... (until HH:MM)` and `Next: ... at HH:MM`) and `releaseInfo` (`Now · ...`) can identify a current programme. A background worker refreshes metadata every 60 seconds, independently of playback attempts, channel policy and catalogue identity. Records expire after 150 seconds without a successful refresh. Inconsistent/missing titles are omitted. CurrentProgram and LiveTv/Programs expose the current title; JealousfinProgramme retains the provider's next title and raw clock strings, observation timestamp and provenance. Those strings are not converted into UTC dates until the provider timezone is established. No fabricated programme durations, stream probes or media downloads are needed. The guide is current-only, not a complete EPG.

### Corroborated external schedule fallback

For an explicit mapping of 15 audited US FAST channel names, the Plex US and Samsung US XMLTV listings (public i.mjh.nz mirrors) are refreshed every 15 minutes on a separate daemon worker. The gateway only uses an external title if both mapped distributor IDs report a unique overlapping current programme with matching normalized title and start/end within 60 seconds. Primary provider metadata always wins. External programme labels include `Scheduled:` because distributor schedule agreement does not establish the identity of the actual provider stream. UTC timestamps come from XMLTV offsets; records are hidden at schedule end or after 30 minutes without a successful refresh. No additional upstream media requests or channel attempts occur. No fuzzy automatic channel mapping. Bounded gzip/XML parsing and error isolation protect playback; conflicting, absent, ambiguous or expired schedules are omitted. The source mirror is shared, so agreement is corroboration across distributor listings, not independent verification of actual decoded video.

### Programme release years

A separate worker looks up exact programme titles in Cinemeta movie and series catalogues, prioritizing recently active channels. Accept a supplied parenthesized release year or a unique exact title across both result sets; ambiguous remakes require matching synopsis phrases or two cast names from corroborated guide evidence. Never choose by popularity or programme broadcast date. The catalogue release year is used (festival and theatrical release years may differ). Cache positive results for seven days and unresolved results for six hours, capped at 2048 hashed context keys. Bounded responses, request timeouts, a three-second worker interval and error backoff keep metadata off the playback path. Return ProductionYear and append (YYYY) to the programme display name only when matched; preserve provenance and avoid duplicate years. No AI, API key, or media decoding is involved.
