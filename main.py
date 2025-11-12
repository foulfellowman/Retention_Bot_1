from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Callable

from dotenv import load_dotenv

from db import DB
from gpt import GPTClient
from logging_config import configure_logging
from models import FSMState, Message
from sqlalchemy import delete
from user_context import UserContext


configure_logging()
logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "t", "yes", "on"}


# ---------- config / wiring ----------
def load_config() -> dict:
    load_dotenv()
    max_tokens = (
        os.getenv("max_tokens")
        or os.getenv("MAX_TOKENS")
        or "300"
    )
    return {
        "openai_api_key": os.environ["OPENAI_API_KEY"],
        "default_phone": os.getenv("DEFAULT_PHONE", "4802982031"),
        "max_tokens": int(max_tokens),
        "temperature": float(os.getenv("OPENAI_TEMPERATURE", "0")),
        "intro_message": os.getenv(
            "CONSOLE_INTRO_MESSAGE",
            "Hey! Quick check-in -- are you still seeing any pest activity?",
        ),
        "reset_on_start": _env_flag("CONSOLE_RESET_STATE", True),
    }


def build_gpt_client(cfg: dict) -> GPTClient:
    return GPTClient(
        temperature=cfg["temperature"],
        max_tokens=cfg["max_tokens"],
    )


def build_user(phone: str) -> UserContext:
    return UserContext(phone)


# ---------- app core ----------
@dataclass
class ConversationApp:
    phone: str
    gpt: GPTClient
    user: UserContext
    db_factory: Callable[[], DB] = DB
    intro_message: str | None = None

    def reset_state(self) -> None:
        """Reset persisted state for this phone and clear GPT context."""
        with self.db_factory() as db:
            session = db.session
            session.execute(delete(Message).where(Message.phone_number == self.phone))
            session.execute(delete(FSMState).where(FSMState.phone_number == self.phone))
            session.commit()

        self.gpt.set_context(self.phone, [])
        self.user = UserContext(self.phone)

    def setup(self) -> None:
        # ensure context is empty and FSM row exists if needed
        self.gpt.set_context(self.phone, [])
        try:
            self.user.get_current_state()
        except Exception:
            pass

    def should_exit_stateful(self) -> bool:
        return self.user.get_current_state() in {"pause", "complete_flow", "user_stopped"}

    def handle_stop(self, text: str) -> None:
        self.user.trigger_event("user_stopped", verbose=True)
        with self.db_factory() as db:
            db.insert_message(self.phone, text)

    def handle_user_turn(self, text: str) -> str:
        with self.db_factory() as db:
            db.insert_message(self.phone, text)
            reply = self.gpt.generate_response(text, self.user, db)
            self.gpt.insert_with_db_instance(db, reply, self.user)
        return reply

    def loop(self) -> None:
        logger.info("")
        logger.info("--- GPT SMS Conversation Simulator ---")

        if self.intro_message:
            with self.db_factory() as db:
                db.insert_message_from_gpt(self.phone, self.intro_message)
            logger.info("GPT: %s", self.intro_message)

        while True:
            if self.should_exit_stateful():
                break

            user_input = input("You: ").strip()
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit", "stop"}:
                self.handle_stop(user_input)
                break

            reply = self.handle_user_turn(user_input)
            logger.info("Snapshot: %s", self.user.get_fsm_snapshot())
            logger.info("GPT: %s", reply)
            logger.info("")


# ---------- entrypoint ----------
def main() -> None:
    cfg = load_config()
    phone = cfg["default_phone"]
    if len(sys.argv) > 1:
        phone = sys.argv[1]

    gpt = build_gpt_client(cfg)
    user = build_user(phone)

    app = ConversationApp(
        phone=phone,
        gpt=gpt,
        user=user,
        intro_message=cfg["intro_message"],
    )

    if cfg["reset_on_start"]:
        app.reset_state()
    app.setup()

    try:
        app.loop()
    except KeyboardInterrupt:
        logger.info("")
        logger.info("Exiting.")


if __name__ == "__main__":
    main()
