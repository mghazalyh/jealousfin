# Programme research integration (2026-11-27)

This note records the isolated integration of the two local research commits onto
the deployed generalized-language gateway. It contains no deployment action and
no live research calls. Tests use local fixtures only.

## Base and integrated commits

- Base (deployed generalized language): `882ca56590da4b591dcdc90a61059f82232ff083`
- Structured schedule evidence / verified episode identity: `e89a3bd`
  -> integrated as `b91ac19` on `engineering/research-integration-parallel-20261127`
- Active-viewing research priority / deferred audio evidence: `1768d40`
  -> integrated as `9838d32`
- Integration worktree: `jealousfin-research-integration-parallel-20261127`
- Branch: `engineering/research-integration-parallel-20261127`

The two research commits form a chain (`e89a3bd` is the parent of `1768d40`);
both share merge base `072d6e5` with the base. They were replayed in order on top
of `882ca56`.

## Conflict resolution

`git cherry-pick e89a3bd` applied cleanly. `git cherry-pick 1768d40` produced one
content conflict, in `Server/iptv/gateway.py`, inside the `/trial-status` payload
of `handle_request`. All other files auto-merged.

Resolution:

- Retained the generalized-language fields from the base:
  `languageExclusions`, `languageAuto`, and the `spanishClassification` /
  `spanishAuto` aliases, all derived from `language_exclusions` /
  `language_audio` through the existing `spanish_classification` /
  `spanish_audio` aliases.
- Added the research commit's `episodeAudio` status field.
- Kept the base's dual control route
  `('/spanish-auto','/language-auto')` rather than reverting to
  `/spanish-auto` only.
- No language-policy behaviour was changed in this lane.

## Retained behaviour

- Generalized language exclusions and the `language-exclusions.json` /
  `language-auto-control.json` state, including legacy-file migration, are
  unchanged from `882ca56`.
- English/Malay separation is unchanged: the Malay selector/proxy path in
  `route_prototype` still precedes research and playback handling, and
  language-exclusion state remains American-profile only.
- Active viewing is measured from authenticated playback sessions with real
  progress, separate from the short resource `activeProtectionSeconds` guard.
- Research retry escalates LOW on the first active attempt to HIGH on bounded,
  jittered retries; configured engineering HIGH/MAX is preserved; background
  channels keep the six-hour cooldown.
- Verification remains strict: exact station identity, approved source region,
  timezone-aware start/end, no invented titles or years, and research answers
  stay untrusted leads until independently verified. Series premiere years are
  never copied into episode years.
- Calls remain bounded (one research subprocess, per-window attempt cap,
  progress/cooldown gating) with no live external calls in this lane.

## Tests (local fixtures)

Run from `Server/iptv/`; all passed.

- `test_schedule_evidence.py` - 14 tests, OK
- `test_opencode_discovery.py` - 9 tests, OK
- `test_programme_research.py` - 11 tests, OK
- `test_episode_audio.py` - 4 tests, OK
- `test_gateway_policy.py` - 33 tests, OK
- `test_language_auto.py` - 17 tests, OK
- `test_spanish_auto.py` - 15 tests, OK
- `test_policy_snapshot.py` - 3 tests, OK
- `test_endurance_acceptance.py` - 5 tests, OK
- `test_policy_observer.py` - all assertion groups PASS

`python3 -m py_compile Server/iptv/gateway.py` also passes.

## Integration boundaries

- Independent evidence/overlay lane
  (`engineering/language-evidence-parallel-20261127`) is currently at the same
  base `882ca56`. No overlay commits are merged by this lane. If that lane
  advances with further `gateway.py` edits, its status/route changes must be
  reconciled against the resolution above before combining.
- This lane does not modify `prototype_selector.py`, `source_profiles.py`, or the
  Malay/overlay profiles.
- `episode_audio.py` is default OFF and not wired to playback. It performs no
  audio capture, microphone access, transcript retention, external calls, or
  additional Whisper model loading. The pure ranking adapter is the only active
  surface; live integration raises an explicit deferred error.
- No production/device access, deployment, restarts, live research calls,
  external audio, push, queue changes, or edits to other worktrees occurred.

## Rollout proposal

1. Land `b91ac19` and `9838d32` on the integration branch only after review.
2. Deploy the gateway package with the existing policy-readiness checks; do not
   change language state files or `.json` research state.
3. Keep `episode_audio` disabled (default). Verify `/trial-status` exposes
   `metadataResearch.activeViewing`, `activeResearch`, and `episodeAudio`
   with `enabled=false`, `active=false`.
4. Observe bounded research diagnostics (`lastVerification`,
   `lastToolDiagnostics`, attempt/variant/retry windows) during controlled,
   authorized viewing. Do not trigger live research from this lane.

## Rollback proposal

- Revert the integration commits (`9838d32`, then `b91ac19`) to return to base
  `882ca56`; language behaviour is untouched by either commit.
- Research state is the existing `opencode-discovery.json`; new fields are
  additive and ignored by older code. No database or queue migration is needed.
- Removing `episode_audio.py`/`programme_research.py`/`schedule_evidence.py`
  together with their imports fully removes the feature.

## Blockers

- No code or test blocker: all combined regressions pass.
- Deferred (not a defect): a safe exact-session local audio handoff is not
  established, so the pure audio ranking module stays default OFF.
- Conditional: if the independent evidence/overlay lane advances past
  `882ca56`, a follow-up gateway reconciliation is required.
