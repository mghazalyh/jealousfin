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

Run python3 test_policy_observer.py for isolated policy checks. The first successful startup does not prove sustained playback. The observer can miss old events that have already rotated out of the device log window. It uses observed terminal failures, not individual retry errors, to avoid penalizing an otherwise successful session. A policy database outage leaves the last loaded catalogue policy active in the gateway.

## Future iOS deployment

Native defaults: strict hiatus, 10-second stall timeout, 5-second failure retry delay, advance on stall, user-configurable rotation interval. Rotation state is persisted by server, account and category and seeded once from available native ledger history. Existing settings, credentials, and active hot revisions remain intact. A hot configuration or runtime override can still control policy. Device-owned Surf tracks actual channels; shared server-owned Surf does not pretend to identify underlying channels. Keep the existing active adapter revision until the gateway and migration are verified; do not embed fixed channel IDs or expiry dates in an IPA.

The PS3 profile is a separate JSON fixture for future development. Do not advertise a compatible PS3 endpoint until it has passed physical console testing.

## Offline compatibility snapshot

Use `python3 make_policy_snapshot.py /private/policy.sqlite3 /private/new-adapter.js` to generate an ABI-1 catalogue filter for older clients. This read-only generator is not an installer or continuous synchronization service. Keep the output private. Each activation can reconnect an older client. The gateway marker delegates ordering and exclusions to the SQLite-aware gateway. Run `python3 test_policy_snapshot.py` for regression checks.
