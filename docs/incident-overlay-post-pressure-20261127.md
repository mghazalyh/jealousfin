# Incident: combined overlay starvation after the pressure patch (2026-11-27)

Base: `7178d3d` (`Scope device probe backoff by failure lane`)
Follow-up fix: `eb4db60` (`Track overlay pump ownership across eviction and completion`)
Lane: gateway overlay / device-probe admission (`Server/iptv` only)

No live call, native change, IPA build, deployment or metadata write was made
while producing this audit.

## Observed shape

After 20 minutes on the deployed candidate:

- programme count 285, episode verified 16, year 108;
- language decisions 899, combined published 0, combined fallback 4;
- device identity probes 92, failures 57, all `device_status_unavailable`;
- backoff rejects 120.

The split backoff tables explain why identity/snapshot work can be healthy
while status reads are not: status stays in `DEVICE_PROBE_BACKOFF`, identity in
`DEVICE_PROBE_IDENTITY_BACKOFF` (`gateway.py:2043-2047`). The overlay path
still needs the status lane.

## Exact overlay schedule

There is no timer. Jobs are admitted only at existing boundaries:

- catalogue `/LiveTv/Channels` and playback `PlaybackInfo` enqueue one
  `refresh` (`gateway.py:3288-3292`);
- `/trial-status` calls `overlay_request_pump()` on every poll
  (`gateway.py:3341`);
- language evidence/state/correction callbacks enqueue `evidence`, `state` or
  `correction` (`gateway.py:2840-2868`).

`overlay_jobs` is a bounded queue of 8. Pump admission is gated by
`overlay_bridge_state.status()["pending"]`, then by a 10 s queue TTL, a 1 s
minimum spacing, and (after this fix) per-admission token ownership
`gateway.py:3004-3040`. The worker blocks on the queue and, for a pump, reads
the current device identity, pumps the language bridge and settles retries for
at most 2 s (`gateway.py:2902-3002`).

Freshness gates are unchanged:

- every non-pump job re-reads device identity before channel/launch/stream/
  verified-visit attribution (`overlay_current_channel`, `gateway.py:2879`);
- `ProgrammeOverlayPublisher.publish` writes the value and only enables the
  native node after a **non-shared** fresh recheck (`_overlay_current_scope`,
  `gateway.py:2719`, `fresh=True`), otherwise it returns to the safe fallback;
- language evidence is still rejected when its own TTL is expired, and the
  combined node still requires a programme identity plus either an episode or
  fresh accepted language evidence (`gateway.py:2792-2803`).

Language bridge gates remain: pending TTL 30 s, 0.5 s minimum interval, two
bounded retries, generation/channel suppression and event ordering
(`overlay_bridge.py:323-580`).

## Pump bookkeeping residual (fixed)

`overlay_enqueue` evicts the oldest job when the queue is full. Before
`eb4db60`, an evicted pump unconditionally cleared `overlay_pump_pending`.
When the caller was `overlay_request_pump` enqueueing a replacement pump, the
old pump was evicted and the new pump stayed queued while the flag was false.
The worker's completion path had the same issue: an older in-flight pump
cleared the flag of a newer queued replacement.

The fix makes each admitted pump own a monotonically increasing token. Only
the token being evicted or completed is retired; `overlay_pump_pending` is the
truth value of the outstanding token set (`gateway.py:105-118`). Untracked
direct-enqueue pumps remain compatible. This cannot add device reads: it only
keeps coalescing accurate, so it can only reduce duplicate pump jobs.

Targeted fixtures in `test_incident_identity_overlay_20261127.py`:

- `test_queue_full_pump_replacement_retains_pending_ownership`
- `test_inflight_pump_completion_does_not_clear_newer_pending`

## Does the pipeline depend on a fresh status read? Yes

- A pump job calls `overlay_current_channel()` before `bridge.pump()`. That
  read is shareable only when another probe is already in flight; with no
  flight it starts a real `get_status` (and, on the legacy IPA, a
  `get_player_snapshot`). If the status lane is in backoff, the pump returns
  `device_unverified` and never pumps.
- `refresh`, `evidence`, `state` and `correction` use the same identity read
  for channel/stream/launch/visit attribution, so they fail closed as well.
- The combined publisher's mandatory recheck is deliberately `fresh=True` and
  cannot join the single-flight read. Even if a label is ready, a contended
  status/snapshot read makes the recheck fail and keeps the safe fallback.

Therefore the 57 status-only failures and 120 backoff rejects can keep
`combined published` at 0 while the separate identity lane is healthy. The
pipeline does depend on the status lane; the lane split only prevents a
snapshot outage from causing status-only HTTP 503s.

## Actual blockers

1. `get_status` is timing out or being rejected by status backoff. No
   bookkeeping change can make a device read succeed.
2. The combined node needs a second, intentionally non-shared fresh recheck.
   This is a freshness guarantee, not a bug.
3. A legacy installed IPA that omits `streamGeneration` from `get_status`
   forces the extra `get_player_snapshot` command, increasing contention.
4. With status reads failing, no pump or evidence job can reach the publisher,
   so the combined and episode counters cannot move regardless of stored
   language decisions.

## Safe mitigation with the existing IPA

- Keep the safe 22 px web fallback visible: it already shows live programme
  metadata and is the intended degraded state, so the user is not blank.
- Keep the pump-ownership fix: it bounds pump jobs and duplicate device probes
  without relaxing any attribution gate.
- Keep single-flight and fallback dedup; do not add a completed-status cache,
  bypass the fresh recheck, or reuse a stale snapshot to force publication —
  each would weaken the identity/stream/visit guarantee.
- No existing-IPA-only code change can eliminate the dependency on a
  successful status read. A durable reduction needs a native/IPA improvement
  that exposes atomic channel plus stream generation in `get_status`, or
  lower control contention; that is outside this lane and was not attempted.
