# test_tool_get_fsm_reply_and_usage.py
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from main_intent import tool_get_fsm_reply
import gpt as gpt_module


# ---------- Fakes ----------
class FakeUser:
    def __init__(self):
        self.phone_number = "4802982000"
        self._snap = {"state": "active", "meta": {"k": 1}}

    def get_fsm_snapshot(self):
        return self._snap

    def reply_for_state(self, snap):
        # return a deterministic template based on snapshot
        return f"TEMPLATE::{snap['state']}"


# OpenAI scripted client
class ScriptedClient:
    def __init__(self, responses):
        self._responses = list(responses)

        class _Chat:
            def __init__(self, outer):
                class _Completions:
                    def __init__(self, outer):
                        self.outer = outer

                    def create(self, **kwargs):
                        if not outer._responses:
                            raise AssertionError("No scripted responses left")
                        return outer._responses.pop(0)

                self.completions = _Completions(self)

        self.chat = _Chat(self)


def mk_tool_call(name, args=None, call_id="t1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args or {}))
    )


def mk_message_with_tool_calls(tool_calls):
    msg = SimpleNamespace(tool_calls=tool_calls, content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def mk_message_no_tools():
    msg = SimpleNamespace(tool_calls=None, content="assistant says hi")
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


# ---------- 1) Pure unit test for tool_get_fsm_reply ----------
def test_tool_get_fsm_reply_returns_reply_and_snapshot():
    user = FakeUser()
    out = json.loads(tool_get_fsm_reply(user))
    assert out["reply"] == "TEMPLATE::active"
    assert out["fsm"] == {"state": "active", "meta": {"k": 1}}


# ---------- 2) Usage inside generate_response: short-circuit when tool is called ----------
def test_generate_response_short_circuits_on_get_fsm_reply(monkeypatch):
    # Script model to request get_fsm_reply immediately
    first = mk_message_with_tool_calls([mk_tool_call("get_fsm_reply", {}, "tc1")])
    client = ScriptedClient([first])

    # Stubs
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_session = Mock(return_value=[])
    log_message_to_db = Mock()

    # Patch where GPTClient looks
    monkeypatch.setattr(gpt_module, "get_session_messages_no_base_prompt", get_session, raising=True)
    monkeypatch.setattr(gpt_module, "log_message_to_db", log_message_to_db, raising=True)

    gpt = gpt_module.GPTClient()
    gpt._client = client  # inject scripted client

    user = FakeUser()
    db = SimpleNamespace()

    reply = gpt.generate_response("hi", user, db)
    assert reply == "TEMPLATE::active"  # came from tool_get_fsm_reply
    assert gpt.get_context(user.phone_number) == []

    gpt.insert_with_db_instance(db, reply, user, twilio_sid="SM500")
    assert gpt.get_context(user.phone_number)[-1] == {"role": "assistant", "content": "TEMPLATE::active"}
    log_message_to_db.assert_called_once_with(db, user.phone_number, "TEMPLATE::active", twilio_sid="SM500")


# ---------- 3) Usage inside generate_response: forced fallback when no tools ----------
def test_generate_response_forces_tool_when_no_tools(monkeypatch):
    first = mk_message_no_tools()  # model returns no tool_calls
    client = ScriptedClient([first])

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_session = Mock(return_value=[])
    log_message_to_db = Mock()
    monkeypatch.setattr(gpt_module, "get_session_messages_no_base_prompt", get_session, raising=True)
    monkeypatch.setattr(gpt_module, "log_message_to_db", log_message_to_db, raising=True)

    gpt = gpt_module.GPTClient()
    gpt._client = client

    user = FakeUser()
    db = SimpleNamespace()

    reply = gpt.generate_response("anything", user, db)
    assert reply == "TEMPLATE::active"  # forced path uses tool_get_fsm_reply
    assert gpt.get_context(user.phone_number) == []
    gpt.insert_with_db_instance(db, reply, user, twilio_sid="SM777")
    assert gpt.get_context(user.phone_number)[-1] == {"role": "assistant", "content": "TEMPLATE::active"}
    log_message_to_db.assert_called_once_with(db, user.phone_number, "TEMPLATE::active", twilio_sid="SM777")
