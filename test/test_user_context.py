import pytest
from types import SimpleNamespace
from typing import Dict, List

import user_context as uc_mod
from models import FSMState, Phone

PHONE_NUMBER = "4802982000"


class FakeSession:
    def __init__(self, store: "InMemoryStore"):
        self.store = store
        self.closed = False
        self.commits = 0

    def get(self, model, key):
        if model is Phone:
            return self.store.phones.get(key)
        if model is FSMState:
            return self.store.states.get(key)
        return None

    def add(self, obj):
        if isinstance(obj, Phone):
            self.store.phones[obj.phone_number] = obj
        elif isinstance(obj, FSMState):
            self.store.states[obj.phone_number] = obj
        else:
            self.store.other.append(obj)
        return obj

    def flush(self):
        return None

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class InMemoryStore:
    def __init__(self):
        self.phones: Dict[str, Phone] = {}
        self.states: Dict[str, FSMState] = {}
        self.other: List[object] = []
        self.sessions: List[FakeSession] = []


class FakeDB:
    def __init__(self, store: InMemoryStore):
        self.session = FakeSession(store)
        store.sessions.append(self.session)

    def close(self):
        self.session.close()


@pytest.fixture
def db_store(monkeypatch):
    def _make(*, phone_present: bool = False):
        store = InMemoryStore()
        if phone_present:
            store.phones[PHONE_NUMBER] = Phone(phone_number=PHONE_NUMBER)

        def factory():
            return FakeDB(store)

        monkeypatch.setattr(uc_mod, "DB", factory, raising=True)
        return store

    return _make


@pytest.fixture(autouse=True)
def patch_flow(monkeypatch):
    class FakeFlow:
        def __init__(self, name):
            self.name = name
            self.state = "start"
            self.confused_count = 0
            self.was_ever_interested = False

        def receive_positive_response(self, **_):
            self.state = "interested"

        def go_to_sqft(self, **_):
            self.state = "action_sqft"

        def receive_followup(self, **_):
            self.state = "follow_up"

        def complete_flow(self, **_):
            self.state = "done"

        def receive_negative_response(self, **_):
            self.state = "not_interested"

        def user_stopped(self, **_):
            self.state = "stop"

        def retry_confused(self, **_):
            self.confused_count += 1
            self.state = "confused"

        def resume_flow(self, **_):
            self.state = "start"

        def snapshot(self):
            return {"flow_state": self.state, "confused_count": self.confused_count}

        def polite_ack(self):
            self.state = "done"

    monkeypatch.setattr(uc_mod, "IntentionFlow", FakeFlow, raising=True)


def test_init_inserts_phone_when_missing(db_store):
    store = db_store(phone_present=False)

    user = uc_mod.UserContext(PHONE_NUMBER)

    assert PHONE_NUMBER in store.phones
    assert store.sessions[0].commits == 1
    assert user.get_current_state() == "start"


def test_init_skips_insert_when_phone_exists(db_store):
    store = db_store(phone_present=True)

    uc_mod.UserContext(PHONE_NUMBER)

    assert store.phones[PHONE_NUMBER].phone_number == PHONE_NUMBER
    assert store.sessions[0].commits == 0


def test_trigger_event_calls_flow_and_updates_state(db_store):
    db_store(phone_present=True)
    user = uc_mod.UserContext(PHONE_NUMBER)

    ok = user.trigger_event("receive_positive_response", verbose=True)

    assert ok is True
    assert user.get_current_state() == "interested"
    assert user.get_fsm_snapshot()["flow_state"] == "interested"


def test_trigger_event_raises_for_unknown_event(db_store):
    db_store(phone_present=True)
    user = uc_mod.UserContext(PHONE_NUMBER)

    with pytest.raises(ValueError):
        user.trigger_event("not_a_real_trigger")


def test_change_state_from_intent_maps_and_triggers(db_store):
    db_store(phone_present=True)
    user = uc_mod.UserContext(PHONE_NUMBER)

    user.change_state_from_intent("sqft_ready")
    assert user.get_current_state() == "action_sqft"

    user.change_state_from_intent("followup")
    assert user.get_current_state() == "follow_up"


def test_set_user_info_and_context_strings(db_store):
    db_store(phone_present=True)
    user = uc_mod.UserContext(PHONE_NUMBER)

    user.set_user_info("Ryan", ["Rodent Control", "Termite"], 93, "Termite")
    ctx = user.get_user_context_string()

    assert "Customer name: Ryan" in ctx
    assert "Previous services: Rodent Control, Termite" in ctx
    assert "Days since cancellation: 93" in ctx
    assert "Last service: Termite" in ctx

    msg = user.turn_into_gpt_context("Yes please")
    assert isinstance(msg, list)
    assert msg[0]["role"] == "user"
    assert "Customer says: Yes please" in msg[0]["content"]


def test_gpt_history_and_twilio_fields(db_store):
    db_store(phone_present=True)
    user = uc_mod.UserContext(PHONE_NUMBER)

    user.set_twilio_sid("SM123")
    user.set_twilio_message("Hey there")
    tw = user.get_twilio_data()
    assert tw["last_sid"] == "SM123"
    assert tw["last_message"] == "Hey there"

    user.add_gpt_message("assistant", "Hello")
    assert user.get_gpt_history() == [{"role": "assistant", "content": "Hello"}]
    user.clear_gpt_history()
    assert user.get_gpt_history() == []


def test_reply_for_state_variants(db_store):
    db_store(phone_present=True)
    user = uc_mod.UserContext(PHONE_NUMBER)

    snap = user.get_fsm_snapshot()
    assert user.reply_for_state(snap).startswith("Hey! Quick check-in")

    user.trigger_event("receive_positive_response")
    snap = user.get_fsm_snapshot()
    assert "square feet" in user.reply_for_state(snap)

    user.trigger_event("go_to_sqft")
    snap = user.get_fsm_snapshot()
    assert "square footage" in user.reply_for_state(snap)

    user.trigger_event("receive_followup")
    snap = user.get_fsm_snapshot()
    assert "We will reach out with a booking" in user.reply_for_state(snap)

    user.trigger_event("retry_confused")
    snap = user.get_fsm_snapshot()
    assert "clarify" in user.reply_for_state(snap)
