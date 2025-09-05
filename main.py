from dataclasses import dataclass

from openai import OpenAI
from dotenv import load_dotenv
import os

from db import DB, insert_message, insert_message_from_gpt
from gpt import GPTClient
from user_context import UserContext


# ---------- config / wiring ----------
def load_config():
    load_dotenv()
    return {
        "OPENAI_API_KEY": os.environ["OPENAI_API_KEY"],
        "READBACK_LIMIT": int(os.getenv("READBACK_LIMIT", "15")),
        "MAX_TOKENS": int(os.getenv("MAX_TOKENS", "300")),
        "DEFAULT_PHONE": os.getenv("DEFAULT_PHONE", "4802982031"),
    }


def build_gpt_client(max_tokens: int) -> GPTClient:
    # if GPTClient accepts an OpenAI client & params, inject here
    gpt = GPTClient()
    gpt.max_tokens = max_tokens  # or pass via constructor if supported
    return gpt


def build_user(phone: str) -> UserContext:
    user = UserContext(phone)
    # seed/demo data; move to a fixture/factory if you don’t want this in prod
    user.set_user_info("Billy", ["Rodent Control", "Termite Treatment"], 93, "Termite Treatment")
    return user


# ---------- app core ----------
@dataclass
class ConversationApp:
    phone: str
    db: DB
    gpt: GPTClient
    user: UserContext

    def setup(self):
        # prime GPT context from current user info

        # String is passed because there is no incoming sms
        self.gpt.set_context(self.user.turn_into_gpt_context(incoming_sms=""))

    def should_exit_stateful(self) -> bool:
        return self.user.get_current_state() in {"pause", "complete_flow", "user_stopped"}

    def handle_stop(self, text: str):
        self.user.trigger_event("user_stopped", verbose=True)
        insert_message(self.db, self.phone, text)

    def handle_user_turn(self, text: str) -> str:
        insert_message(self.db, self.phone, text)

        return self.gpt.generate_response(text, self.user, self.db)

    def loop(self):
        print("\n--- GPT SMS Conversation Simulator ---")
        introg_msg = "\nGPT: Hey! Quick check-in—are you still seeing any pest activity?"
        insert_message_from_gpt(self.db, self.phone, introg_msg)
        print(introg_msg)
        while True:
            if self.should_exit_stateful():
                break

            user_input = input("You: ").strip()
            if user_input.lower() in {"exit", "quit", "stop"}:
                self.handle_stop(user_input)
                break

            reply = self.handle_user_turn(user_input)
            print("Snapshot:")
            print(self.user.get_fsm_snapshot())
            print(f"GPT: {reply}\n")


# ---------- entrypoint ----------
def main():
    cfg = load_config()
    gpt = build_gpt_client(cfg["MAX_TOKENS"])
    user = build_user(cfg["DEFAULT_PHONE"])
    db = DB()

    app = ConversationApp(phone=cfg["DEFAULT_PHONE"], db=db, gpt=gpt, user=user)
    app.setup()

    try:
        app.loop()
    finally:
        # DB() exposes .conn; ensure cursor(s) are context-managed where created
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
