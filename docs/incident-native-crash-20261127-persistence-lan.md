# Incident report: native crash lane, persistence and LAN control (2026-11-27)

Branch: `engineering/incident-native-crash-20261127`
Baseline: `e105b7a630ee16f6e96c791de5f1c26cebfb0fbe`
Status: diagnostics, retention accounting, launch evidence and LAN connection
lifecycle repaired and tested. The physical crash mechanism is **not**
confirmed. Shared application files (`Sources/AppModel.swift`) were not edited;
the required integration is specified below and is not applied.

## Incident evidence

- iPadOS 15.6.1 crashes and multi-second MainActor stalls after `e105b7a`.
- Supervisor tick gaps of 24-38 seconds.
- Diagnostic retention reduced from 5108 to 1541 event files; 18 second gaps
  remained.
- New launch `076D5E1A-F3F4-454C-89E7-2BDF7866CF80` still timed out.

## Evidence vs hypotheses

Evidence in the repository:

- `Disk` is `@MainActor` and `AppModel.log()` calls the synchronous
  `disk.save()` for every event (`Sources/AppModel.swift:760-764`).
- Startup scans the event directory and reads the last 150 events synchronously
  (`Sources/AppModel.swift:279-281`).
- Telemetry upload reads and decodes up to 200 event files and unlinks them on
  the MainActor (`Sources/AppModel.swift:1801-1820`).
- The LAN listener kept up to 16 sockets for up to 180 seconds with no read
  idle deadline and no timer cancellation after a response
  (`Sources/Runtime.swift` before this change).
- The 18 second gaps remaining after event reduction are consistent with
  fixed-size per-sync work (200-file reads/removals, receipt batching and
  snapshot encoding), not with event count alone.

Hypotheses that remain unproven:

- The observed terminations are watchdog/main-thread starvation rather than
  jetsam, disk exhaustion or a specific AVPlayer defect. No device crash log,
  jetsam reason or launch watchdog report is present in this repository.
- LAN socket pressure is a secondary contributor. LAN parsing runs on a serial
  queue, not the MainActor; only command dispatch and query handling cross back
  to the MainActor. It cannot explain the 18-38 second gaps by itself.
- An unclean launch marker from another subsystem may identify the actual
  termination reason once collected on a device.

The changes below reduce main-thread diagnostic work, bound durable diagnostic
state, provide crash evidence, and remove LAN connection retention. They do not
prove the physical crash mechanism.

## Implemented changes

### `Sources/Persistence.swift`

- Replaced the in-memory integer counters with exact filename indexes
  (`eventIndex`, `batchIndex`). A save increments only a genuinely new name;
  overwrite updates bytes only; removal and pruning subtract the exact removed
  names.
- Removed the per-save `fileExists` stat. An unknown index is left `nil` and
  reconciled by one exact scan, never cached as zero after a listing failure.
- Listings now require `isRegularFile == true`; non-regular entries cannot
  inflate the queued count.
- Diagnostic retention (`pruneEvents`) still prunes only `event-*.json` regular
  files. Receipts, settings, health, rotation, patches, programme/language data
  and queued batches remain excluded.
- Added a durable retention journal (`diagnostic-retention-v1.json`) recording
  last attempt/success/error, cumulative removed events/bytes, last known event
  count/bytes and worker duration. It survives relaunch and is outside the
  event namespace, so retention cannot delete its own evidence.
- Added a launch marker (`diagnostic-launch-v1.json`). Each `Disk` creation
  reads the previous marker; if it was not explicitly marked clean, one bounded
  `UNCLEAN_LAUNCH` event is written with the previous launch ID, start time and
  last known diagnostic size. `markCleanShutdown()` closes the marker.
- Added a hard safety ceiling independent of hot configuration
  (5000 events / 32 MiB): crossing it schedules an off-main emergency prune.
  Hot configuration can request a smaller bound; this ceiling only prevents
  unbounded growth if the scheduled pass never runs.
- Added `appendEventAsync(_:_:)`: event encoding and atomic write run off the
  MainActor; only exact index accounting returns to the actor.
- `load` now refuses files above 16 MiB before `Data(contentsOf:)`.
- Async retention remains serialized by a MainActor flag, so overlapping
  scheduled/emergency passes cannot edit the index concurrently.

### `Sources/Runtime.swift`

- Each connection now owns a `Pending` record with its buffer, response state
  and two timers.
- Added per-connection read/idle deadline (default 10 s), response/query
  deadline (defaults 180 s / 10 s) and one absolute hard deadline.
- Timers are cancelled on completion; late handler completions are ignored.
- Connection slots are removed on response completion, error, cancellation,
  timeout and stop. A slow reader cannot retain a slot after the response
  deadline.
- The receive buffer is appended in place instead of copying the whole
  accumulated buffer on every chunk.
- Once a header is complete, unauthenticated non-`/health` requests are
  rejected before a 4 MiB body is buffered. The final parse still performs the
  authoritative check, and `GET /health` remains unauthenticated.
- The access token uses a lock instead of `queue.sync`, so `configureLAN()` on
  the MainActor cannot block behind a large request parse.
- Connection cap, idle/response/query deadlines are injectable for tests;
  defaults preserve production behavior (`16`, `10 s`, `180 s`, `10 s`).

## Required `AppModel.swift` integration (not applied)

These changes are outside the owned files and must be coordinated by the
integration owner.

1. Event append off the MainActor. Replace the synchronous write at
   `Sources/AppModel.swift:764` with the new disk API and make `log` enqueue
   rather than block:

   ```swift
   // Current
   disk.save(e,"event-\(e.id).json")

   // Proposed
   let name="event-\(e.id).json"
   Task { [disk] in _ = await disk.appendEventAsync(e,name) }
   ```

   ERROR/CRITICAL events should be flushed before a lifecycle boundary or after
   a short bounded delay if crash evidence must be immediate.

2. Startup replay. At `Sources/AppModel.swift:279-281`, replace the
   lexicographic `files(prefix:"event-").suffix(150)` loop with a newest-first
   bounded set derived from `creationDate` (add a `recentEventURLs(limit:)`
   async helper or scan off-main). Run launch retention before replay so a
   launch with thousands of leftover events does not pay O(M) startup cost
   before the first retention pass.

3. Telemetry upload. At `Sources/AppModel.swift:1801-1820`, capture the URLs on
   the MainActor, then perform `Data(contentsOf:)` and JSON decode in a single
   `Task.detached`. Keep the durable ordering: write `batch-*` first, then
   remove source events, then upload the batch. Apply removals through
   `Disk.remove` (index-aware).

4. Other synchronous upload reads. Apply the same off-main read pattern to
   `uploadEngineeringResults` (`~3068-3074`) and `uploadPatchArchives`
   (`~3491-3499`).

5. Receipt ordering. In `execute` (`~1999-2022`), keep the existing durable
   `interrupted` receipt before any side effect. If receipt saves become async,
   insert the ID into `pending` before the first `await` so two LAN/GitHub
   executors cannot both pass the duplicate check.

6. Clean-shutdown evidence. Call `disk.markCleanShutdown()` when the app enters
   background after pending writes are flushed. iOS does not reliably deliver
   `willTerminate`; a marker left unclean is intentionally treated as suspected
   crash/watchdog evidence, not proof.

## Tests

Built and ran the workflow's native runtime test binary (QuickJS-NG vendor
fetched locally, `Sources/PromotedBaseline.generated.swift` generated; both are
git-ignored). Command shape:

```sh
swiftc -parse-as-library -import-objc-header Native/RuntimeBridge.h \
  Sources/Domain.swift Sources/Runtime.swift Sources/RuntimeWorker.swift \
  Sources/DynamicUIDomain.swift Sources/JellyfinAdapterDomain.swift \
  Sources/JellyfinAdapterRuntime.swift Sources/EngineeringDomain.swift \
  Sources/PatchPromotionDomain.swift Sources/PromotedBaseline.generated.swift \
  Sources/Persistence.swift Sources/EngineeringRuntime.swift Sources/Jellyfin.swift \
  Sources/HLSReadiness.swift Sources/BaselineInstaller.swift Sources/PatchVersionStore.swift \
  Tests/HotPatchIntegration.swift Tests/RuntimeSmoke.swift build/quickjs/*.o \
  -o runtime-tests
```

Result: exit code 0. New passes:

- exact diagnostic accounting, byte bound, durable retention, off-main append
  and unclean-launch evidence
- idle and completed LAN control connections release bounded slots
- late command completion cannot retain a LAN control connection
- LAN authentication and `/health` bypass preserved after connection cleanup
- existing receipt idempotency, CAS, LAN round-trip and over-limit tests still
  pass

Diagnostics tests cover:

- new event, overwrite and removal accounting exactly matches the directory
- count and byte trimming, including a byte-only limit
- receipts, settings and other namespaces survive retention and relaunch
- retention totals and last known size survive relaunch
- one bounded crash-evidence event is emitted for an unclean prior launch and
  suppressed after `markCleanShutdown()`
- async event append updates the index and remains prunable

Failures encountered during this lane and fixed before commit:

- Swift rejected `precondition(try await ...)` because the autoclosure is not
  async; values are now bound first.
- The LAN auth fixture initially routed `/health` to a test query handler that
  returned 404; the handler now mirrors the application `/health` route.
- The new receipt-preservation assertion counted only one receipt file; it now
  expects both the dictionary fixture and the typed receipt.

## Preservation and constraints

- Settings, auth-bearing settings, receipt files for mutations, rotation,
  health, patch archives and configuration files are never selected by
  pruning. Tests assert receipts and settings survive retention and relaunch.
- No `AppModel.swift`, server, production, deployment or device command change
  was made.
- No secret values are printed or persisted by the new diagnostics; crash
  evidence contains IDs, timestamps and counts only.
- The physical crash mechanism is not confirmed. Device crash logs, jetsam
  reasons and launch watchdog reports are still required.

## Remaining integration risks

- The largest MainActor cost (per-event synchronous writes and telemetry reads)
  remains until the `AppModel` patch above is applied. This lane improves the
  diagnostics substrate and proves it, but does not complete end-to-end
  off-main persistence.
- iOS provides no reliable clean-termination callback; unclean launch markers
  are deliberately conservative.
- Receipts have no retention policy. They remain bounded only by command
  volume; a future acknowledged-upload retention policy is separate work.
- The absolute hard LAN deadline uses the maximum of the response, query and
  idle deadlines. Changing the injectable defaults changes when slow but
  legitimate commands are cut off.
