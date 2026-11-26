"""Read-only reconciliation for surf pause state around maintenance.

This module never calls the iPad and never returns a state-changing command.
It exists so a maintenance procedure cannot repeat the unsafe pattern of
`if surfPaused: resume_surf()`. Given a pre-maintenance snapshot, observed
user actions and the post-maintenance state, it reports the state that should
be preserved. If the launch/stream identity is stale or the state is unknown,
it reports uncertainty and preserves the current state unchanged.

Actions such as resume_surf, start_surf, next_channel and
reload_current_stream are explicitly forbidden here; a human can decide to
issue them separately with fresh evidence.
"""

STATE_CHANGING = (
    "pause_surf", "resume_surf", "start_surf", "stop_surf",
    "next_channel", "previous_channel", "reload_current_stream",
)
USER_ACTIONS = {"pause": "paused", "stop": "paused", "resume": "running", "start": "running"}


def _result(preserve, reason, final=None, stale=False):
    return {
        "action": "leave",
        "preserve": preserve,
        "reason": reason,
        "stale": bool(stale),
        "finalObservedPaused": final.get("surfPaused") if isinstance(final, dict) else None,
        "forbiddenStateChanges": list(STATE_CHANGING),
        "note": "No device command is issued; preserve this state unless the user explicitly requests a change.",
    }


def reconcile(initial, events=None, final=None, current_launch=None, current_stream=None):
    """Return the state to preserve without issuing any device command.

    initial/final: {surfPaused, surfing, currentChannelID, launchID,
                    streamGeneration}
    events: user/maintenance observations with at, source, action, id and
            optionally launchID/streamGeneration. Only source == "user"
            changes the preserved intent.
    """
    if not isinstance(initial, dict) or not isinstance(initial.get("surfPaused"), bool):
        return _result("unknown", "initial_state_unknown", final, True)
    initial_launch = initial.get("launchID") or ""
    initial_stream = initial.get("streamGeneration") or ""
    if current_launch and initial_launch and current_launch != initial_launch:
        return _result("unknown", "stale_launch", final, True)
    if current_stream and initial_stream and current_stream != initial_stream:
        return _result("unknown", "stale_stream", final, True)
    if final is not None and not isinstance(final, dict):
        return _result("unknown", "final_state_unknown", final, True)
    events = events if isinstance(events, list) else []
    user_events = []
    seen = set()
    for event in events:
        if not isinstance(event, dict) or event.get("source") != "user":
            continue
        identifier = event.get("id")
        if identifier and identifier in seen:
            continue
        if identifier:
            seen.add(identifier)
        action = event.get("action")
        if action not in USER_ACTIONS:
            continue
        user_events.append(event)
    user_events.sort(key=lambda event: event.get("at", 0))
    intent = "paused" if initial["surfPaused"] else "running"
    for event in user_events:
        intent = USER_ACTIONS[event["action"]]
    if not user_events:
        if initial["surfPaused"]:
            return _result("paused", "initial_paused_no_user_resume", final)
        return _result("running", "initial_running_no_user_pause", final)
    if intent == "paused":
        return _result("paused", "user_paused_during_maintenance", final)
    return _result("running", "user_resumed_during_maintenance", final)


def validate_snapshot(document):
    if not isinstance(document, dict) or set(document) - {"initial", "events", "final", "current_launch", "current_stream"}:
        raise ValueError("Invalid surf maintenance snapshot")
    return document


def report(document):
    document = validate_snapshot(document)
    return reconcile(document.get("initial", {}), document.get("events", []),
                    document.get("final"), document.get("current_launch"),
                    document.get("current_stream"))


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(report(json.loads(sys.stdin.read())), separators=(",", ":")))
