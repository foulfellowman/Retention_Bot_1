import json
import pytest
from types import SimpleNamespace
from transitions.core import MachineError

from main_intent import _coerce_event, tool_update_fsm


# ---------- fakes ----------
class FakeMachine:
    def __init__(self, triggers):
        self._triggers = triggers

    def get_triggers(self, state):
        return list(self._triggers)


class FakeUser:
    def __init__(self, state, triggers):
        self._state = state
        self.machine = FakeMachine(triggers)
        self.fsm = SimpleNamespace(machine=self.machine)
        self.phone_number = "4802982000"
        self.user_data = {}
        self.twilio_data = {}
        self._last_event = None

    def get_current_state(self):
        return self._state

    def get_fsm_snapshot(self):
        return {"state": self._state, "last_event": self._last_event}

    def trigger_event(self, event_name, verbose=False, **kwargs):
        # record and advance state
        self._last_event = event_name
        self._state = "after"
        return {"state": self._state, "applied_event": event_name}


# 1) Coercion rule: follow_up + (ack-ish) -> polite_ack
def test_coerce_event_follow_up_maps_ack():
    final, reason = _coerce_event("follow_up", "receive_positive_response")
    assert final == "polite_ack"
    assert reason == "coerced_from_follow_up_ack"


# 2) tool_update_fsm should APPLY the *coerced* event (polite_ack) when allowed
def test_tool_update_fsm_applies_coerced_event_when_allowed():
    user = FakeUser(state="follow_up", triggers={"polite_ack", "complete_flow"})
    out = json.loads(tool_update_fsm(user, "retry_confused"))
    # Expect success using the coerced event
    assert out["applied"] is True
    assert user._last_event == "polite_ack"
    assert out["event"] == "polite_ack"  # <- ensures function fires the coerced event
    assert out["from_state"] == "follow_up"
    assert out["to_state"] == "after"


# 3) Pause state: invalid triggers should NO-OP with reason and coercion noted
def test_tool_update_fsm_invalid_in_pause_returns_noop_with_reason():
    # In pause, only 'user_stopped' is allowed
    user = FakeUser(state="pause", triggers={"user_stopped"})
    out = json.loads(tool_update_fsm(user, "receive_positive_response"))
    assert out["applied"] is False
    assert out["reason"] == "invalid_trigger_for_state"
    assert out["coercion"] == "noop_if_invalid_in_pause"
    assert out["state_before"] == "pause" and out["state_after"] == "pause"
    assert "allowed_triggers" in out and "user_stopped" in out["allowed_triggers"]
