# test_fsm_tools_get_user_context.py
import json
from types import SimpleNamespace
import pytest

from main_intent import tool_get_user_context, _coerce_event, tool_update_fsm


class FakeMachine:
    def __init__(self, triggers):
        self._triggers = triggers

    def get_triggers(self, state):
        return self._triggers


class FakeUser:
    def __init__(self, state, triggers, phone="4802982000"):
        self._state = state
        self.phone_number = phone
        self.user_data = {"name": "Ryan"}
        self.twilio_data = {"sid": "SM123"}
        self._snap = {"state": state, "meta": {"k": 1}}
        self.fsm = SimpleNamespace(machine=FakeMachine(triggers))

    def get_current_state(self):
        return self._state

    def get_fsm_snapshot(self):
        return self._snap


def test_get_user_context_happy_path_sorted_triggers():
    user = FakeUser(state="active", triggers=["b", "a", "c"])
    out = json.loads(tool_get_user_context(user))
    assert out["current_state"] == "active"
    assert out["phone_number"] == "4802982000"
    assert out["user_data"] == {"name": "Ryan"}
    assert out["twilio_data"] == {"sid": "SM123"}
    assert out["fsm"] == {"state": "active", "meta": {"k": 1}}
    # sorted() guarantee
    assert out["allowed_triggers"] == ["a", "b", "c"]


def test_get_user_context_follow_up_has_expected_hint():
    user = FakeUser(state="follow_up", triggers=["retry_confused", "polite_ack"])
    out = json.loads(tool_get_user_context(user))
    # Exact hint text required by contract
    assert out["nlu_hint"] == (
        "If current_state is 'follow_up', map acknowledgements like 'ok/thanks/got it' "
        "to 'polite_ack' or 'complete_flow', not 'retry_confused'."
    )


def test_get_user_context_handles_various_iterables_for_triggers():
    # Triggers as a set (order-unstable) → function must still return a sorted list
    user = FakeUser(state="active", triggers={"z", "m", "a"})
    out = json.loads(tool_get_user_context(user))
    assert out["allowed_triggers"] == ["a", "m", "z"]
