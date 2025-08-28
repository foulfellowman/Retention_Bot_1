from __future__ import annotations
import json
import logging

from transitions.core import MachineError, EventData
from user_context import UserContext

# Set up dedicated logger for FSM operations
fsm_logger = logging.getLogger('fsm_debug')
fsm_logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - FSM - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
fsm_logger.addHandler(handler)


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
    allowed = _get_allowed_triggers(state_before, user)
    if verbose:
        fsm_logger.info(f"Allowed triggers from {state_before}: {sorted(allowed) if allowed else 'None'}")

    # Validate trigger
    if allowed and event_to_fire not in allowed:
        fsm_logger.warning(f"REJECTED: {event_to_fire} not in allowed triggers {sorted(allowed)}")
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
            fsm_logger.info(f"Attempting to fire event: {event_to_fire} with kwargs: {kwargs}")

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

        new_allowed = _get_allowed_triggers(state_after, user)
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
            fsm_logger.info(f"Returning success result: {result}")
        return json.dumps(result)

    except MachineError as e:
        fsm_logger.error(f"MACHINE ERROR during transition: {str(e)}")
        fsm_logger.error(f"Event: {event_to_fire}, State: {state_before}, Kwargs: {kwargs}")

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
            fsm_logger.info(f"Returning error result: {result}")
        return json.dumps(result)

    except Exception as e:
        fsm_logger.error(f"UNEXPECTED ERROR during transition: {str(e)}")
        fsm_logger.error(f"Exception type: {type(e).__name__}")
        fsm_logger.error(f"Event: {event_to_fire}, State: {state_before}, Kwargs: {kwargs}")

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
            fsm_logger.info(f"Returning unexpected error result: {result}")
        return json.dumps(result)

    finally:
        if verbose:
            fsm_logger.info("=== FSM UPDATE COMPLETE ===\n")


def _get_allowed_triggers_prod(state: str, user: UserContext) -> set[str]:
    try:
        # Machine exposed by `transitions`
        a = set(sorted(user.fsm.machine.get_triggers(user.get_current_state())))
        b = set(user.fsm.machine.get_triggers(state))
        print("allowed: ", b)
        print("allowed with change: ", a)
        return set(a)
        # return set(user.fsm.get_triggers(state))
    except Exception as e:
        print(e)
        return set()


def _get_allowed_triggers(state: str, user: UserContext, verbose: bool = False) -> set[str]:
    """
    Get allowed triggers for a specific state using only explicitly defined triggers.
    """
    if verbose:
        print(f"\n{'=' * 60}")
        print("DEBUG: _get_allowed_triggers called (EXPLICIT TRIGGERS ONLY)")
        print(f"{'=' * 60}")

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
        if verbose:
            print("INPUT PARAMETERS:")
            print(f"   - Requested state: '{state}' (type: {type(state)})")
            print(f"   - User object: {user} (type: {type(user)})")
            print(f"   - Explicit triggers to check: {sorted(EXPLICIT_TRIGGERS)}")

        # Check if user and fsm exist
        if not hasattr(user, 'fsm'):
            if verbose:
                print("ERROR: User object has no 'fsm' attribute")
            return set()

        if not hasattr(user.fsm, 'machine'):
            if verbose:
                print("ERROR: FSM object has no 'machine' attribute")
            return set()

        if verbose:
            print("User FSM structure looks valid")

        # FSM introspection
        if verbose:
            print("\nFSM INTROSPECTION:")
        fsm_machine = user.fsm.machine

        # Get current state
        try:
            current_state = user.get_current_state()
            if verbose:
                print(f"   - Current state: '{current_state}' (type: {type(current_state)})")
        except Exception as e:
            if verbose:
                print(f"   - ERROR getting current state: {e}")
            current_state = None

        # Get all available states
        try:
            all_states = list(fsm_machine.states)
            if verbose:
                print(f"   - All available states ({len(all_states)}): {all_states}")
        except Exception as e:
            if verbose:
                print(f"   - ERROR getting all states: {e}")
            all_states = []

        # Check if requested state exists
        state_exists = state in [str(s) for s in all_states]
        if state_exists:
            if verbose:
                print(f"   - Requested state '{state}' found in available states")
        else:
            if verbose:
                print(f"   - Requested state '{state}' NOT found in available states")
            # Check for similar states (case sensitivity, whitespace)
            similar_states = [s for s in all_states if str(s).lower() == state.lower()]
            if similar_states and verbose:
                print(f"   - Similar states (case insensitive): {similar_states}")

        # Check which explicit triggers are valid from the requested state
        if verbose:
            print("\nEXPLICIT TRIGGER VALIDATION:")
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
                    if verbose:
                        print(f"   - '{trigger}' is valid from state '{state}'")
                else:
                    if verbose:
                        print(f"   - '{trigger}' is NOT valid from state '{state}'")

            except Exception as e:
                if verbose:
                    print(f"   - Error checking trigger '{trigger}': {e}")

        result = valid_triggers

        # Final result
        if verbose:
            print("\nFINAL DECISION:")
            print(f"   - Returning explicit triggers for state '{state}': {sorted(result)}")
            print("   - Filtered out auto-generated triggers like 'to_*'")
            print(f"{'=' * 60}\n")
        return result

    except Exception as e:
        if verbose:
            print("\nUNEXPECTED ERROR in _get_allowed_triggers:")
            print(f"   - Exception type: {type(e).__name__}")
            print(f"   - Exception message: {str(e)}")
            import traceback
            print("   - Stack trace:")
            traceback.print_exc()
            print(f"{'=' * 60}\n")
        return set()



def tool_get_fsm_reply(user: UserContext):
    snap = user.get_fsm_snapshot()
    reply = user.reply_for_state(snap)
    return json.dumps({"reply": reply, "fsm": snap})


# Additional helper function for debugging
def debug_fsm_state(user, context=""):
    """Call this anywhere to get detailed FSM state info"""
    print(f"=== FSM STATE DEBUG {context} ===")
    print(f"Current state: {user.get_current_state()}")
    print(f"Allowed triggers: {sorted(_get_allowed_triggers(user.get_current_state(), user))}")
    print(f"FSM snapshot: {user.get_fsm_snapshot()}")
    print(f"=== END DEBUG ===")
