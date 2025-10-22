from __future__ import annotations
import json
import logging

from transitions.core import MachineError, EventData
from user_context import UserContext

# Module-level logger for FSM operations
fsm_logger = logging.getLogger(__name__)


def tool_get_user_context(user: UserContext):
    snap = user.get_fsm_snapshot()
    return json.dumps({
        "current_state": user.get_current_state(),
        "allowed_triggers": sorted(user.fsm.machine.get_triggers(user.get_current_state())),
        "phone_number": user.phone_number,
        "user_data": user.user_data,
        "twilio_data": user.twilio_data,
        "fsm": snap,
        "nlu_hint": "If current_state is 'follow_up', map acknowledgements like 'ok/thanks/got it' to 'polite_ack' or 'complete_flow', not 'retry_confused'."
    })


# Helper: decide coercions based on current state + incoming event
def _coerce_event(state: str, event_name: str) -> tuple[str, str | None]:
    """
    Returns (final_event_name, reason)
    reason is None if no coercion.
    """
    # When we are awaiting closure, treat short acks as completion.
    if state == "follow_up":
        if event_name in {"retry_confused", "receive_positive_response", "resume_flow"}:
            return "polite_ack", "coerced_from_follow_up_ack"
        # Allow explicit finishes to pass through, e.g. 'complete_flow'
        # Let 'receive_negative_response' fall through if you want to support back-outs.

    # When we are awaiting closure, treat short acks as completion.
    if state == "action_sqft":
        if event_name in {"go_to_sqft"}:
            return "receive_followup", "coerced_from_gotosqft"
        # Allow explicit finishes to pass through, e.g. 'complete_flow'
        # Let 'receive_negative_response' fall through if you want to support back-outs.

    if state == "interested":
        if event_name in {"go_to_sqft"}:
            return "receive_followup", "coerced_from_gotosqft_and_interested"

    # In pause, only allow explicit stop; everything else should no-op or self-loop
    if state == "pause":
        if event_name not in {"user_stopped"}:
            return event_name, "noop_if_invalid_in_pause"  # we'll no-op below if invalid

    return event_name, None


def tool_update_fsm(user, event_name: str, kwargs=None, verbose: bool = True):
    kwargs = kwargs or {}

    # Initial state capture
    state_before = user.get_current_state()
    fsm_snapshot_before = user.get_fsm_snapshot()

    if verbose:
        fsm_logger.info("=== FSM UPDATE ATTEMPT ===")
        fsm_logger.info(f"User ID: {getattr(user, 'phone_number', 'unknown')}")
        fsm_logger.info(f"Current state: {state_before}")
        fsm_logger.info(f"Requested event: {event_name}")
        fsm_logger.info(f"Event kwargs: {kwargs}")

    # Event coercion
    event_to_fire, coercion_reason = _coerce_event(state_before, event_name)
    if verbose:
        if coercion_reason:
            fsm_logger.info(f"Event coerced: {event_name} -> {event_to_fire} (reason: {coercion_reason})")
        else:
            fsm_logger.info(f"No coercion needed: {event_name}")

    # Check allowed triggers
    allowed = _get_allowed_triggers(state_before, user, verbose=verbose)
    if verbose:
        fsm_logger.info(f"Allowed triggers from {state_before}: {sorted(allowed) if allowed else 'None'}")

    # Validate trigger
    if allowed and event_to_fire not in allowed:
        if verbose:
            fsm_logger.warning(
                "REJECTED: %s not in allowed triggers %s",
                event_to_fire,
                sorted(allowed),
            )
        result = {
            "applied": False,
            "reason": "invalid_trigger_for_state",
            "coercion": coercion_reason,
            "event_requested": event_name,
            "event_fired": None,
            "state_before": state_before,
            "state_after": state_before,  # stays the same
            "allowed_triggers": sorted(allowed),
            "fsm": fsm_snapshot_before,
        }
        if verbose:
            fsm_logger.info(f"Returning rejection result: {result}")
        return json.dumps(result)

    # Attempt the transition
    try:
        if verbose:
            fsm_logger.info("Attempting to fire event: %s with kwargs: %s", event_to_fire, kwargs)

        pre_transition_snapshot = user.get_fsm_snapshot()

        # Fire the event
        user.trigger_event(event_to_fire, verbose=verbose, **kwargs)

        state_after = user.get_current_state()
        post_transition_snapshot = user.get_fsm_snapshot()
        changed = (state_after != state_before)

        if verbose:
            fsm_logger.info(f"Event fired successfully: {event_to_fire}")
            fsm_logger.info(f"State transition: {state_before} -> {state_after}")
            fsm_logger.info(f"State actually changed: {changed}")
            if changed:
                fsm_logger.info("SUCCESSFUL TRANSITION")
            else:
                fsm_logger.warning("NO STATE CHANGE (possible self-transition or condition failure)")

        new_allowed = _get_allowed_triggers(state_after, user, verbose=verbose)
        if verbose:
            fsm_logger.info(f"New allowed triggers: {sorted(new_allowed) if new_allowed else 'None'}")
            if pre_transition_snapshot != post_transition_snapshot:
                fsm_logger.info("FSM snapshot changed during transition")
            else:
                fsm_logger.info("FSM snapshot unchanged")

        result = {
            "applied": changed,
            "reason": None if changed else "no_state_change",
            "event": event_to_fire,
            "from_state": state_before,
            "to_state": state_after,
            "allowed_triggers": sorted(new_allowed),
            "fsm": post_transition_snapshot,
        }

        if verbose:
            fsm_logger.info("Returning success result: %s", result)
        return json.dumps(result)

    except MachineError as e:
        if verbose:
            fsm_logger.error("MACHINE ERROR during transition: %s", e)
            fsm_logger.error("Event: %s, State: %s, Kwargs: %s", event_to_fire, state_before, kwargs)

        result = {
            "applied": False,
            "reason": "machine_error",
            "error": str(e),
            "event": event_to_fire,
            "from_state": state_before,
            "to_state": state_before,
            "allowed_triggers": sorted(allowed),
            "fsm": fsm_snapshot_before,
        }
        if verbose:
            fsm_logger.info("Returning error result: %s", result)
        return json.dumps(result)

    except Exception as e:
        if verbose:
            fsm_logger.error("UNEXPECTED ERROR during transition: %s", e)
            fsm_logger.error("Exception type: %s", type(e).__name__)
            fsm_logger.error("Event: %s, State: %s, Kwargs: %s", event_to_fire, state_before, kwargs)

        result = {
            "applied": False,
            "reason": "unexpected_error",
            "error": f"{type(e).__name__}: {str(e)}",
            "event": event_to_fire,
            "from_state": state_before,
            "to_state": state_before,
            "allowed_triggers": sorted(allowed),
            "fsm": fsm_snapshot_before,
        }
        if verbose:
            fsm_logger.info("Returning unexpected error result: %s", result)
        return json.dumps(result)

    finally:
        if verbose:
            fsm_logger.info("=== FSM UPDATE COMPLETE ===\n")


def _get_allowed_triggers_prod(state: str, user: UserContext, verbose: bool = False) -> set[str]:
    try:
        # Machine exposed by `transitions`
        a = set(sorted(user.fsm.machine.get_triggers(user.get_current_state())))
        b = set(user.fsm.machine.get_triggers(state))
        if verbose:
            fsm_logger.debug("Allowed triggers for state %s: %s", state, b)
            fsm_logger.debug("Allowed triggers after coercion: %s", a)
        return set(a)
        # return set(user.fsm.get_triggers(state))
    except Exception as e:
        if verbose:
            fsm_logger.exception("Failed to determine allowed triggers for state %s", state)
        return set()


def _get_allowed_triggers(state: str, user: UserContext, verbose: bool = False) -> set[str]:
    """
    Get allowed triggers for a specific state using only explicitly defined triggers.
    """
    def log(message: str, *args) -> None:
        if verbose:
            fsm_logger.debug(message, *args)

    separator = "=" * 60
    log("")
    log(separator)
    log("DEBUG: _get_allowed_triggers called (EXPLICIT TRIGGERS ONLY)")
    log(separator)

    # Define your explicit triggers from IntentionFlow class
    EXPLICIT_TRIGGERS = {
        'receive_positive_response',
        'go_to_sqft',
        'receive_followup',
        'complete_flow',
        'receive_negative_response',
        'user_stopped',
        'retry_confused',
        'pause_flow',
        'resume_flow',
        'polite_ack'
    }

    try:
        # Input validation
        log("INPUT PARAMETERS:")
        log("   - Requested state: '%s' (type: %s)", state, type(state))
        log("   - User object: %s (type: %s)", user, type(user))
        log("   - Explicit triggers to check: %s", sorted(EXPLICIT_TRIGGERS))

        # Check if user and fsm exist
        if not hasattr(user, 'fsm'):
            log("ERROR: User object has no 'fsm' attribute")
            return set()

        if not hasattr(user.fsm, 'machine'):
            log("ERROR: FSM object has no 'machine' attribute")
            return set()

        log("User FSM structure looks valid")

        # FSM introspection
        log("")
        log("FSM INTROSPECTION:")
        fsm_machine = user.fsm.machine

        # Get current state
        try:
            current_state = user.get_current_state()
            log("   - Current state: '%s' (type: %s)", current_state, type(current_state))
        except Exception as e:
            log("   - ERROR getting current state: %s", e)
            current_state = None

        # Get all available states
        try:
            all_states = list(fsm_machine.states)
            log("   - All available states (%s): %s", len(all_states), all_states)
        except Exception as e:
            log("   - ERROR getting all states: %s", e)
            all_states = []

        # Check if requested state exists
        state_exists = state in [str(s) for s in all_states]
        if state_exists:
            log("   - Requested state '%s' found in available states", state)
        else:
            log("   - Requested state '%s' NOT found in available states", state)
            # Check for similar states (case sensitivity, whitespace)
            similar_states = [s for s in all_states if str(s).lower() == state.lower()]
            if similar_states:
                log("   - Similar states (case insensitive): %s", similar_states)

        # Check which explicit triggers are valid from the requested state
        log("")
        log("EXPLICIT TRIGGER VALIDATION:")
        valid_triggers = set()

        for trigger in EXPLICIT_TRIGGERS:
            try:
                try:
                    triggers_from_state = set(fsm_machine.get_triggers(state))
                    is_valid = trigger in triggers_from_state
                except Exception:
                    # Fallback: check transitions manually
                    is_valid = False
                    for transition in fsm_machine.transitions:
                        if (
                            transition.trigger == trigger and
                            (transition.source == state or
                             transition.source == '*' or
                             state in transition.source)
                        ):
                            is_valid = True
                            break

                if is_valid:
                    valid_triggers.add(trigger)
                    log("   - '%s' is valid from state '%s'", trigger, state)
                else:
                    log("   - '%s' is NOT valid from state '%s'", trigger, state)

            except Exception as e:
                log("   - Error checking trigger '%s': %s", trigger, e)

        result = valid_triggers

        # Final result
        log("")
        log("FINAL DECISION:")
        log("   - Returning explicit triggers for state '%s': %s", state, sorted(result))
        log("   - Filtered out auto-generated triggers like 'to_*'")
        log("%s\n", separator)
        return result

    except Exception as e:
        if verbose:
            fsm_logger.exception("UNEXPECTED ERROR in _get_allowed_triggers for state '%s'", state)
        return set()



def tool_get_fsm_reply(user: UserContext):
    snap = user.get_fsm_snapshot()
    reply = user.reply_for_state(snap)
    return json.dumps({"reply": reply, "fsm": snap})


# Additional helper function for debugging
def debug_fsm_state(user, context=""):
    """Call this anywhere to get detailed FSM state info"""
    label = f" {context}" if context else ""
    fsm_logger.info("=== FSM STATE DEBUG%s ===", label)
    fsm_logger.info("Current state: %s", user.get_current_state())
    fsm_logger.info(
        "Allowed triggers: %s",
        sorted(_get_allowed_triggers(user.get_current_state(), user)),
    )
    fsm_logger.info("FSM snapshot: %s", user.get_fsm_snapshot())
    fsm_logger.info("=== END DEBUG ===")
