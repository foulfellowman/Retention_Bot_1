import json
import pathlib
from datetime import datetime
from collections import defaultdict

from dotenv import load_dotenv
import os
from openai import OpenAI

import main_intent
from control_session import get_session_messages, get_session_messages_no_base_prompt
from main_intent import tool_get_fsm_reply, tool_get_user_context, tool_update_fsm
from models import Message
from user_context import UserContext

load_dotenv()  # This loads variables from .env into os.environ


class GPTServiceError(RuntimeError):
    """Raised when the upstream GPT service fails or is unavailable."""


class GPTClient:
    def __init__(self, temperature: float = 0.0, max_tokens: int = 512):
        path = pathlib.Path(os.getenv("BASE_PROMPT_FILE", "./base_prompt.txt"))
        base_prompt = path.read_text(encoding="utf-8") if path.exists() else ""

        self._api_key = os.getenv("OPENAI_API_KEY")
        self._organization = os.getenv("OPENAI_ORG")
        self._client = OpenAI(api_key=self._api_key, organization=self._organization)
        self._model = "gpt-4o-mini"
        self._base_instructions = base_prompt
        self._contexts = defaultdict(list)
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._tools = load_tools()

    # Getters
    def get_base_instructions(self):
        return self._base_instructions

    def get_temperature(self):
        return self._temperature

    def get_max_tokens(self):
        return self._max_tokens

    def get_context(self, phone=None):
        if phone is None:
            return self._contexts
        return list(self._contexts.get(phone, []))

    def get_client(self):
        return self._client

    # Setters
    def set_base_instructions(self, instructions: str):
        self._base_instructions = instructions

    def set_temperature(self, temperature: float):
        self._temperature = temperature

    def set_max_tokens(self, max_tokens: int):
        self._max_tokens = max_tokens

    def set_context(self, phone: str, context: list):
        self._contexts[phone] = list(context)

    def add_to_context(self, phone: str, role: str, content: str):
        self._contexts[phone].append({"role": role, "content": content})

    def generate_reasons(self, text: str, state: str) -> str:
        """
        Generate a natural-language explanation of why the input maps to a state.
        """
        prompt = f"""
        You are an intention classifier for a customer service SMS bot.
        The user's message was:

        "{text}"

        The system mapped this to the state: {state}

        Explain briefly, in natural conversational language (not technical jargon),
        why this message fits that state.
        """
        response = self._client.chat.completions.create(
            model=self._model,  # or your existing model
            messages=[{"role": "user", "content": prompt}],
            max_tokens=120
        )
        return response.choices[0].message["content"].strip()

    # Call GPT model
    def generate_response(self, user_input: str, user: UserContext, db_instance):
        phone = user.phone_number
        context_messages = list(self._contexts.get(phone, []))
        messages = [{"role": "system", "content": self._base_instructions}] + context_messages
        try:
            if db_instance:
                previous_messages = get_session_messages_no_base_prompt(db_instance, user.phone_number)
                messages.extend(previous_messages)
        except Exception as e:
            print(e)

        messages.append({"role": "user", "content": user_input})

        force_tool_next = False  # <- set when we reject/no-op a transition

        # first turn: must use tools
        response = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            tools=self._tools,
            tool_choice="required",
        )

        while True:
            choice = response.choices[0]
            msg = choice.message

            # If the model requested tool calls
            if msg.tool_calls:
                # record the assistant "tool request" message
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.function.name, "arguments": c.function.arguments}
                        } for c in msg.tool_calls
                    ]
                })

                # reset for this round
                force_tool_next = False

                for call in msg.tool_calls:
                    name = call.function.name
                    args = json.loads(call.function.arguments or "{}")

                    if name == "get_user_context":
                        result = tool_get_user_context(user)

                    elif name == "update_fsm":
                        # IMPORTANT: tool_update_fsm enforces allowed triggers and uses the coerced event
                        result = tool_update_fsm(user, args["event_name"], kwargs=args.get("kwargs"), verbose=False)
                        data = json.loads(result)
                        # If the update was rejected or no-op, don't advance to template; force another tool turn
                        if not data.get("applied", False):
                            force_tool_next = True

                    elif name == "get_fsm_reply":
                        result = tool_get_fsm_reply(user)
                        data = json.loads(result)
                        reply = data["reply"]
                        self.insert_with_db_instance(db_instance, reply, user)

                        return reply

                    else:
                        result = json.dumps({"error": f"unknown tool {name}"})

                    # append the tool result so the model can see allowed_triggers/state/etc.
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": name,
                        "content": result
                    })

                # Ask again after tools:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    tools=self._tools,
                    # If last update failed/no-op, REQUIRE a tool (e.g., read context, pick a valid trigger)
                    tool_choice="required" if force_tool_next else "auto",
                )
                continue

            # No tool calls returned by the model
            if force_tool_next:
                # Model skipped tools right after a rejected/no-op update: force a tool pass
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    tools=self._tools,
                    tool_choice="required",
                )
                # clear after forcing a retry
                force_tool_next = False
                continue

            # Otherwise: FORCE template reply as a safe fallback
            data = json.loads(tool_get_fsm_reply(user))
            reply = data["reply"]
            # self._contexts[phone].append({"role": "assistant", "content": reply})  # DOUBLE MESSAGE ISSUE
            self.insert_with_db_instance(db_instance, reply, user)
            return reply

    def insert_with_db_instance(self, db_instance, reply, user):
        phone = user.phone_number
        self._contexts[phone].append({"role": "assistant", "content": reply})
        try:
            if db_instance:
                log_message_to_db(db_instance, phone, reply)
        except Exception as e:
            print(e)


def log_message_to_db(db_instance, phone_number: str, reply: str):
    """Log assistant reply to the message table."""
    session = getattr(db_instance, "session", db_instance)

    message = Message(
        phone_number=phone_number,
        direction='outbound',
        body=reply,
        message_data={'role': 'assistant', 'content': reply},
        sent_at=datetime.utcnow(),
    )

    session.add(message)
    session.commit()


def load_tools():
    """

        tools_path = pathlib.Path(os.getenv("OPENAI_TOOLS_JSON", "OPENAI_TOOLS_JSON.json"))
        with open(tools_path, encoding="utf-8") as f:
            TOOLS = json.load(f)
            f.close()

    """
    # Prefer file path
    p = os.getenv("OPENAI_TOOLS_JSON")
    if p:
        path = pathlib.Path(p)
        if not path.is_absolute():
            path = pathlib.Path(__file__).resolve().parent / path
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    # Fallback to inline JSON
    raw = os.getenv("OPENAI_TOOLS_JSON")
    if raw:
        return json.loads(raw)
    return []
