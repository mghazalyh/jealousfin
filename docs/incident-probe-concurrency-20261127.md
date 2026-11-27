# Incident: device-probe single-flight repair (2026-11-27)

Worktree: `jealousfin-incident-probe-concurrency-20261127`
Base: `20b78038b1ddcb1a30de9e63103bd5879efc3573`
Scope: `Server/iptv` gateway probe coalescing and launch attribution only.

## Defect repaired

The prior single-flight code used one global version plus a shared result slot.
A waiter that timed out could fall through and start a duplicate owner, and a
waiter could bind to a newer result slot rather than the flight it joined.
Probe failures also used a process-global fault, so an independent fresh probe
could overwrite the fault of an in-flight shared read.

## Changes

- Replaced the version/condition handoff with an explicit `_DeviceProbeFlight`
  (owner, event, result, fault, exception). Waiters bind to that exact flight.
- A waiter timeout now fails closed (`device_probe_timeout`) without starting a
  duplicate device read; later callers start a fresh flight after completion.
- Owner exceptions complete the flight, wake waiters with the same exception,
  and never leave the gate wedged.
- Fault attribution is thread-local. `share=False` observations never mutate
  the shared flight or its diagnostics.
- Launch attribution is enforced before overlay publish: evidence/state jobs
  with a mismatched `launchID` return `stale` (`stale_launch`), and cached
  combined evidence is reused only for the same launch.
- `test_release_integration` fixtures now use the same `fixture-launch` value
  as their probe, so launch gating is exercised rather than bypassed.

## ABI / deployment compatibility

- No native source, Xcode project, release manifest or IPA packaging changed.
- No new device command or presentation field. The installed ABI2 surface is
  unchanged: `get_status`, `get_player_snapshot`, `ui_set_value`, and the
  existing combined overlay fields (`nativeAbi` and layout revision untouched).
- No new IPA is required or produced. No device, service or other worktree was
  touched. No deployment performed.

## Validation

```
cd Server/iptv
python3 -m py_compile gateway.py \
  test_incident_identity_overlay_20261127.py test_release_integration.py
# PYCOMPILE_EXIT=0

python3 -m pytest test_incident_identity_overlay_20261127.py \
  test_release_integration.py -q
# 29 passed, 1 warning, 2 subtests passed in 1.52s
# PYTEST_EXIT=0
```

Focused suite covers the four single-flight defects, stale launch rejection for
evidence and state jobs, and the existing ABI2 presentation check. The separate
deployment harness runs the full regression suite.
