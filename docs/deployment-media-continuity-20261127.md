# Deployment media continuity (2026-11-27)

This lane is based on `4201e1e` in the isolated
`engineering/deployment-media-continuity-20261127` worktree. It contains no
production deployment, service restart, device command, IPA installation,
Malay change, shared-infrastructure change or database mutation. Rollout is
owned by the authorized deployment workflow.

## Incident reconstruction from read-only copies

The live gateway log and deployment state were read, not modified. The
authorized deployment restarted the gateway at 08:38:06 SGT. The process logged
`READY channels=556` at 08:38:08. From 08:38:10 the iPad repeatedly requested
old `/media/...` handles and received local HTTP 404 responses until 08:38:30.
The device obtained a fresh `PlaybackInfo` and resumed media after the failure.
At 08:38:32 it reported a terminal failure with reason
`Video frames stalled after startup`; that was a gateway-restart artefact, not
a channel fault. The orchestrator later corrected that one penalty from the
pre-deployment backup and retained the original observed event plus a
correction audit. The device still caches its hiatus until the normal policy
refresh; no device command was issued by this lane.

The old failure was possible because media handles are intentionally
process-local and memory-only. Their upstream URL can contain provider
credentials or signed query data, so this lane does **not** try to make the
handle itself portable by writing that URL or token to disk.

## Implemented contract

`Server/iptv/deployment_continuity.py` adds a private, bounded handle ledger
and a narrow failure correlation.

### Persisted data

Only these files are written under the existing profile state root, mode 0600:

- `deployment-generation.json`: schema version, random gateway generation,
  previous generation, start time and PID.
- `media-handles.jsonl`: SHA-256 hash of each opaque handle, channel ID, kind
  (`playlist`, `segment` or `media`) and timestamp. The handle, upstream URL,
  query, token and credentials are never stored.
- `media-stale.jsonl`: hashed old handle, channel ID, kind, current/old
  generation and observation time when the device hits a pre-restart handle.
- `deployment-ignored.jsonl`: correction audit preserving the original event
  code, trace, reason and the bounded correlation evidence.

The handle ledger is pruned to seven days and at most 50,000 records. The stale
journal is compacted above 2 MiB. Corrupt or missing continuity files fail
closed to the normal strict health policy.

### Suppression eligibility

A terminal failure is ignored **only** when every condition holds:

1. The reason is one of the native after-startup reasons
   `Video frames stalled after startup`, `Playback stalled after startup` or
   `Player failed after startup`.
2. The failure is no later than 180 seconds after the current gateway
   generation start (with a 5-second clock-skew allowance).
3. The same channel has at least two stale-handle observations after restart.
4. The last stale-handle observation is within 90 seconds before the failure.

A generic `Playback stalled`, `PLAYBACK_STALLED`, stream HTTP error, player
error, another channel's stale handles, a single stale request, an expired
window or a corrupt ledger all remain real failures. One channel cannot borrow
another channel's stale-handle evidence.

### Event preservation

The original event identity (the same `observed.id`) is retained. The stored
code becomes `NATIVE_POLICY_DEPLOYMENT_IGNORED` through the gateway policy
endpoint, or `DEPLOYMENT_IGNORED_<CODE>` through the observer. The audit file
keeps the original code, trace and reason. No channel counter, hiatus, ban,
approval, sustained count, rotation record, favorite, language decision,
Surf state or media cache is changed by suppression. No pause, resume, start,
stop, reload or channel-switch command is issued.

## Rollout

1. Keep the current service configuration, credentials and SQLite database.
   Take a consistent pre-deployment backup through the existing workflow.
2. Deploy `Server/iptv/deployment_continuity.py`, the updated
   `Server/iptv/gateway.py` and the updated `Server/iptv/policy_observer.py`
   together. Both processes must share the same profile state root.
3. Restart through the authorized service manager only. Do not start a second
   instance on the live port and do not issue device commands from the release
   session.
4. Verify the gateway reports `READY`, policy readiness is true and
   `/trial-status` still reports `sqlite-policy-v2`. Confirm the private state
   root contains `deployment-generation.json`, `media-handles.jsonl` and, after
   a restart under a live visit, `media-stale.jsonl`.
5. If a correlated failure is suppressed, verify the expected
   `*_DEPLOYMENT_IGNORED` observed row and the `deployment-ignored.jsonl` audit
   while confirming the channel's health row is unchanged. A real stall must
   still increment the normal failure/hiatus fields.
6. Keep the normal physical acceptance: a live channel, a controlled gateway
   restart, a stale-handle 404 burst and an after-startup stall should not create
   a new gateway penalty. A deliberately induced ordinary stall must still
   enter hiatus.

## Rollback

1. Restore the previous `gateway.py` and `policy_observer.py` through the same
   authorized workflow and restart the managed services.
2. Leave `deployment-generation.json`, `media-handles.jsonl`,
   `media-stale.jsonl` and `deployment-ignored.jsonl` in place. The previous
   code ignores them; deleting the audit is not part of rollback.
3. No SQLite migration or channel correction is required. This feature never
   changes an existing penalty. If an operator manually corrects a historical
   event, use the existing approved backup/correction process.
4. Delete `deployment_continuity.py` only after both processes no longer import
   it.

## Limitations

- This is failure correlation, not transparent handle portability. Old handles
  still return 404 after restart; the device can still have a brief playback
  interruption until it obtains a fresh `PlaybackInfo`. The change prevents a
  *penalty* for that restart artefact.
- A genuine stall that happens to use one of the fixed after-startup reasons,
  on the same channel, within 180 seconds of restart and alongside repeated
  stale-handle 404s remains indistinguishable from the deployment artefact.
  The three-minute window and repeated same-channel evidence keep this narrow.
- The ledger covers handles issued by this gateway for seven days. Handles
  older than that, or handles from another gateway/profile/deployment, do not
  correlate.
- The gateway and observer compare the observer's event time with the gateway
  generation start time. Clock skew beyond five seconds is not suppressed.
- The device's already-cached hiatus is not cleared by the gateway. It expires
  at the normal policy refresh. This lane issues no device commands.
- Failures reported outside the gateway policy endpoint and observer log
  ingestion are not covered. Existing manual approval and backup/correction
  procedures remain necessary for historical corrections.
- The ledger and stale journal are local private state, not a distributed
  trace. They identify a channel and handle generation, not a full user session
  or a guarantee that every frame was displayed.

## Tests

Run from `Server/iptv/`:

```sh
python3 -m unittest test_deployment_continuity -v
python3 -m unittest test_gateway_policy -v
python3 test_policy_observer.py
```

`test_deployment_continuity.py` covers generation rollover, the happy-path
correlation, unrelated-channel isolation, too few hits, expired windows,
generic real stalls, corrupt state/ledger, secret-free file contents and
concurrent registration. `test_gateway_policy` includes the policy-endpoint
path with a fresh unrelated channel and a real stall. `test_policy_observer.py`
includes observer-path suppression and fail-closed cases. Existing gateway,
observer, language, overlay, snapshot, research and endurance tests must remain
unchanged and passing.
