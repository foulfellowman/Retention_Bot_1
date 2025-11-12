import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

"""
Scenario coverage (quick table)
Test                                                Model response 1                                Model response 2    Expected path
test_happy_tool_short_circuit_on_get_fsm_reply      get_user_context, update_fsm, get_fsm_reply     —                   Short-circuit on get_fsm_reply, return template
test_no_tool_calls_forces_template_reply            no tools                                        —                   Force tool_get_fsm_reply, return template
test_unknown_tool_then_auto_second_turn             not_a_real_tool                                 no tools            Handle unknown tool, second call, then forced template
test_context_appended_on_reply                      get_user_context, get_fsm_reply                 —                   Reply appended to _context, logged
test_update_fsm_is_passed_kwargs_if_present         update_fsm with kwargs, get_fsm_reply           —                   kwargs flow into tool_update_fsm
"""
"""
test_happy_tool_short_circuit_on_get_fsm_reply:
Scripts the first LLM turn to call get_user_context, update_fsm, and get_fsm_reply,
testing the short-circuit path; success = returns TEMPLATE_REPLY, tools executed, reply logged/appended.

test_no_tool_calls_forces_template_reply:
Scripts the first LLM turn with no tool calls, testing the forced fallback to tool_get_fsm_reply; 
success = returns TEMPLATE_REPLY and no other tools are invoked.

test_unknown_tool_then_auto_second_turn:
Scripts an unknown tool on the first LLM turn and no tools on the second,
testing resilience and the two-call loop; success = two client calls occur and final result is
TEMPLATE_REPLY via tool_get_fsm_reply.

test_context_appended_on_reply:
Scripts get_user_context then get_fsm_reply, testing that the assistant’s template reply is appended to _context and
logged; success = _context ends with the template and DB log called.

test_update_fsm_is_passed_kwargs_if_present:
Scripts update_fsm with kwargs={"payload":123} followed by get_fsm_reply, testing argument plumbing to the tool;
success = tool_update_fsm receives the kwargs payload and the method returns TEMPLATE_REPLY.
"""


# ---------- helpers to mimic OpenAI chat response shape ----------
def mk_tool_call(name, args=None, call_id="tool_1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args or {}))
    )


def mk_message_with_tool_calls(tool_calls):
    # Mimic openai response: choices[0].message.tool_calls
    msg = SimpleNamespace(tool_calls=tool_calls, content=None)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


def mk_message_no_tools():
    msg = SimpleNamespace(tool_calls=None, content="assistant says hi")
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


# ---------- fakes / fixtures ----------
class FakeUser:
    def __init__(self, phone="4802982000"):
        self.phone_number = phone


class FakeDB:
    pass


@pytest.fixture
def fake_dependencies(monkeypatch):
    # --- stub env key(s) used elsewhere ---
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    # --- stub tool loader so we don't import TOOLS constant anywhere ---
    test_tools = [
        {"type": "function",
         "function": {"name": "get_user_context", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "update_fsm", "parameters": {
            "type": "object",
            "properties": {"event_name": {"type": "string"}, "kwargs": {"type": "object"}},
            "required": ["event_name"]
        }}},
        {"type": "function", "function": {"name": "get_fsm_reply", "parameters": {"type": "object", "properties": {}}}},
    ]

    import gpt as gpt_module
    monkeypatch.setattr(gpt_module, "load_tools", lambda: test_tools, raising=True)

    # stubs your GPTClient calls
    get_session = Mock(return_value=[])
    tool_get_user_context = Mock(return_value=json.dumps({"ok": True}))
    tool_update_fsm = Mock(return_value=json.dumps({"fsm": {"state": "whatever"}}))
    tool_get_fsm_reply = Mock(return_value=json.dumps({"reply": "TEMPLATE_REPLY"}))
    log_message_to_db = Mock()

    # patch where referenced (gpt module namespace)
    monkeypatch.setattr(gpt_module, "get_session_messages_no_base_prompt", get_session, raising=True)
    monkeypatch.setattr(gpt_module, "tool_get_user_context", tool_get_user_context, raising=True)
    monkeypatch.setattr(gpt_module, "tool_update_fsm", tool_update_fsm, raising=True)
    monkeypatch.setattr(gpt_module, "tool_get_fsm_reply", tool_get_fsm_reply, raising=True)
    monkeypatch.setattr(gpt_module, "log_message_to_db", log_message_to_db, raising=True)

    return SimpleNamespace(
        gpt_module=gpt_module,
        get_session=get_session,
        tool_get_user_context=tool_get_user_context,
        tool_update_fsm=tool_update_fsm,
        tool_get_fsm_reply=tool_get_fsm_reply,
        log_message_to_db=log_message_to_db,
    )


# ---------- client scriptor ----------
class ScriptedClient:
    """
    Fake OpenAI client that returns a sequence of pre-baked responses
    each time .chat.completions.create() is called.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

        class _Chat:
            def __init__(self, outer):
                self.outer = outer

                class _Completions:
                    def __init__(self, outer):
                        self.outer = outer

                    def create(_self, **kwargs):
                        outer.calls.append(kwargs)
                        if not outer._responses:
                            raise AssertionError("Ran out of scripted responses")
                        return outer._responses.pop(0)

                self.completions = _Completions(self)

        self.chat = _Chat(self)


# ---------- tests ----------
def test_happy_tool_short_circuit_on_get_fsm_reply(fake_dependencies, monkeypatch):
    # First LLM turn: asks for get_user_context and update_fsm and get_fsm_reply together
    first = mk_message_with_tool_calls([
        mk_tool_call("get_user_context", {}, "tc1"),
        mk_tool_call("update_fsm", {"event_name": "go_to_sqft"}, "tc2"),
        mk_tool_call("get_fsm_reply", {}, "tc3"),  # Should SHORT-CIRCUIT and return immediately
    ])

    client = ScriptedClient([first])

    # Build GPTClient with our fake client
    GPTClient = fake_dependencies.gpt_module.GPTClient
    gpt = GPTClient()
    gpt._client = client  # inject

    user = FakeUser()
    db = FakeDB()

    out = gpt.generate_response("user says hi", user, db)
    assert out == "TEMPLATE_REPLY"

    # tool execution order
    assert fake_dependencies.tool_get_user_context.called
    assert fake_dependencies.tool_update_fsm.called
    assert fake_dependencies.tool_get_fsm_reply.called
    # generate_response no longer logs automatically
    fake_dependencies.log_message_to_db.assert_not_called()


def test_no_tool_calls_forces_template_reply(fake_dependencies, monkeypatch):
    # First LLM turn: no tools → code forces tool_get_fsm_reply
    first = mk_message_no_tools()
    client = ScriptedClient([first])

    GPTClient = fake_dependencies.gpt_module.GPTClient
    gpt = GPTClient()
    gpt._client = client

    user = FakeUser()
    db = FakeDB()

    out = gpt.generate_response("anything", user, db)
    assert out == "TEMPLATE_REPLY"

    # Should NOT call other tools
    fake_dependencies.tool_get_user_context.assert_not_called()
    fake_dependencies.tool_update_fsm.assert_not_called()
    fake_dependencies.tool_get_fsm_reply.assert_called_once()


def test_unknown_tool_then_auto_second_turn(fake_dependencies, monkeypatch):
    # First LLM turn: returns an unknown tool + a known one
    first = mk_message_with_tool_calls([
        mk_tool_call("not_a_real_tool", {"x": 1}, "t1"),
    ])
    # After we handle tools, code does a second LLM call (tool_choice="auto")
    # Make that second call return *no tools*, so we fall back to template reply
    second = mk_message_no_tools()

    client = ScriptedClient([first, second])

    GPTClient = fake_dependencies.gpt_module.GPTClient
    gpt = GPTClient()
    gpt._client = client

    user = FakeUser()
    db = FakeDB()

    out = gpt.generate_response("go!", user, db)
    assert out == "TEMPLATE_REPLY"

    # Unknown tool path hit
    # (we can’t assert internal messages, but we can assert the second call happened)
    assert len(client.calls) == 2
    fake_dependencies.tool_get_fsm_reply.assert_called_once()


def test_context_appended_on_reply(fake_dependencies, monkeypatch):
    first = mk_message_with_tool_calls([
        mk_tool_call("get_user_context", {}, "tc1"),
        mk_tool_call("get_fsm_reply", {}, "tc2"),
    ])
    client = ScriptedClient([first])

    GPTClient = fake_dependencies.gpt_module.GPTClient
    gpt = GPTClient()
    gpt._client = client

    user = FakeUser()
    db = FakeDB()

    assert gpt.get_context(user.phone_number) == []
    out = gpt.generate_response("hello", user, db)
    assert out == "TEMPLATE_REPLY"
    # No auto append until we log the message
    assert gpt.get_context(user.phone_number) == []

    gpt.insert_with_db_instance(db, out, user, twilio_sid="SM123")

    convo = gpt.get_context(user.phone_number)
    assert convo[-1] == {"role": "assistant", "content": "TEMPLATE_REPLY"}
    fake_dependencies.log_message_to_db.assert_called_once_with(
        db, user.phone_number, "TEMPLATE_REPLY", twilio_sid="SM123"
    )


def test_update_fsm_is_passed_kwargs_if_present(fake_dependencies, monkeypatch):
    # Make the model ask update_fsm with kwargs
    first = mk_message_with_tool_calls([
        mk_tool_call("update_fsm", {"event_name": "receive_followup", "kwargs": {"payload": 123}}, "tc1"),
        mk_tool_call("get_fsm_reply", {}, "tc2"),
    ])
    client = ScriptedClient([first])

    GPTClient = fake_dependencies.gpt_module.GPTClient
    gpt = GPTClient()
    gpt._client = client

    user = FakeUser()
    db = FakeDB()

    out = gpt.generate_response("run", user, db)
    assert out == "TEMPLATE_REPLY"

    # Verify kwargs made it through
    fake_dependencies.tool_update_fsm.assert_called_once()
    _, kwargs = fake_dependencies.tool_update_fsm.call_args
    # call_args = (args, kwargs); kwargs should include the parsed 'kwargs'
    assert kwargs["kwargs"] == {"payload": 123}
