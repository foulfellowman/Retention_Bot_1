import logging
from typing import Dict, List, Optional

from db import DB
from fsm import IntentionFlow
from models import FSMState, Phone


logger = logging.getLogger(__name__)


class UserContext:
    def __init__(self, phone_number: str):
        self.phone_number = phone_number
        self.twilio_data: Dict[str, Optional[str]] = {
            "last_sid": None,
            "last_message": None
        }
        self.gpt_history: List[Dict[str, str]] = []  # [{role: ..., content: ...}]
        self.user_data: Dict[str, Optional[str]] = {
            "name": None,
            "previous_services": None,
            "days_since_cancelled": None,
            "last_service": None
        }
        self._phone_number = phone_number
        self._ensure_phone_in_db()
        self.fsm = IntentionFlow(name=phone_number)
        self._load_existing_fsm_flags()

    def trigger_event(self, event_name: str, verbose=False, **kwargs):
        """
        Trigger an FSM event if it exists.
        event_name should match the trigger name in IntentionFlow.
        Example: 'receive_positive_response', 'go_to_sqft', etc.
        """
        if hasattr(self.fsm, event_name):
            trigger_fn = getattr(self.fsm, event_name)
            if verbose:
                logger.info("FSM event triggered: %s", event_name)
            # Fire transition on the in-memory FSM
            trigger_fn(**kwargs)
            # Persist the resulting state to the DB immediately for reliable tracking
            try:
                self.set_current_state(self.fsm.state)
            except Exception:
                # Avoid blocking on persistence errors here; callers may retry/state will sync on next read
                pass
            return True
        else:
            raise ValueError(f"No FSM trigger named '{event_name}'")

    def get_fsm_snapshot(self) -> dict:
        return self.fsm.snapshot()

    def change_state_from_intent(self, intent: str, **kwargs):
        """
        Map a high-level intent string to an FSM trigger.
        """
        intent_map = {
            "yes": "receive_positive_response",
            "no": "receive_negative_response",
            "stop": "user_stopped",
            "confused": "retry_confused",
            "resume": "resume_flow",
            "sqft_ready": "go_to_sqft",
            "followup": "receive_followup",
            "complete": "complete_flow",
        }

        trigger = intent_map.get(intent)
        if not trigger:
            raise ValueError(f"No trigger mapped for intent '{intent}'")

        return self.trigger_event(trigger, **kwargs)

    def set_current_state(self, state_name: str) -> None:
        """
        Update the FSM's current state for this phone number.
        If the phone number doesn't exist in fsm_state, insert it.
        """
        was_interested_flag = bool(getattr(self.fsm, "was_ever_interested", False))
        db_connection = DB()
        session = db_connection.session

        try:
            state = session.get(FSMState, self._phone_number)
            if state:
                state.statename = state_name
                state.was_interested = bool(state.was_interested) or was_interested_flag
            else:
                state = FSMState(
                    phone_number=self._phone_number,
                    statename=state_name,
                    was_interested=was_interested_flag,
                )
                session.add(state)
            session.commit()
        finally:
            db_connection.close()

    def get_current_state(self) -> str:
        """
        Return the current FSM state and ensure the DB reflects it.
        If the row doesn't exist it is inserted; if it exists but is
        different from the in-memory FSM state, it is updated.
        """
        current_mem_state = self.fsm.state
        current_interest = bool(getattr(self.fsm, "was_ever_interested", False))

        db_connection = DB()
        session = db_connection.session

        try:
            state = session.get(FSMState, self._phone_number)
            if state is None:
                state = FSMState(
                    phone_number=self._phone_number,
                    statename=current_mem_state,
                    was_interested=current_interest,
                )
                session.add(state)
                session.commit()
                return current_mem_state

            if state.was_interested:
                self.fsm.was_ever_interested = True

            updated = False
            if state.statename != current_mem_state:
                state.statename = current_mem_state
                updated = True

            if current_interest and not state.was_interested:
                state.was_interested = True
                updated = True

            if updated:
                session.commit()

            return state.statename
        finally:
            db_connection.close()

    def _ensure_phone_in_db(self):
        db_connection = DB()
        session = db_connection.session

        try:
            if session.get(Phone, self._phone_number) is None:
                session.add(Phone(phone_number=self._phone_number))
                session.commit()
        finally:
            db_connection.close()

    def _load_existing_fsm_flags(self):
        db_connection = DB()
        session = db_connection.session
        try:
            state = session.get(FSMState, self._phone_number)
            if state and state.was_interested:
                self.fsm.was_ever_interested = True
        finally:
            db_connection.close()

    # -------- TWILIO --------
    def set_twilio_sid(self, sid: str):
        self.twilio_data["last_sid"] = sid

    def set_twilio_message(self, message: str):
        self.twilio_data["last_message"] = message

    def get_twilio_data(self) -> Dict[str, Optional[str]]:
        return self.twilio_data

    # -------- OPENAI / GPT --------
    def add_gpt_message(self, role: str, content: str):
        self.gpt_history.append({"role": role, "content": content})

    def get_gpt_history(self) -> List[Dict[str, str]]:
        return self.gpt_history

    def clear_gpt_history(self):
        self.gpt_history = []

    # -------- USER DATA --------
    def set_user_info(self, name: str, services: List[str], days_since: int, last_service: str):
        self.user_data["name"] = name
        self.user_data["previous_services"] = ", ".join(services)
        self.user_data["days_since_cancelled"] = str(days_since)
        self.user_data["last_service"] = last_service

    def get_user_context_string(self) -> str:
        return (
            f"Customer name: {self.user_data['name']}\n"
            f"Previous services: {self.user_data['previous_services']}\n"
            f"Days since cancellation: {self.user_data['days_since_cancelled']}\n"
            f"Last service: {self.user_data['last_service']}"
        )

    def turn_into_gpt_context(self, incoming_sms: str) -> List[Dict[str, str]]:
        context = self.get_user_context_string()
        return [{"role": "user", "content": f"{context}\n\nCustomer says: {incoming_sms}"}]

    def get_user_data(self) -> Dict[str, Optional[str]]:
        return self.user_data

    def reply_for_state(self, snap: dict) -> str:
        state = snap.get("flow_state", "start")
        if state == "start":
            return "Hey! Quick check-in—are you still seeing any pest activity?"
        if state == "interested":
            return "Great—roughly how many square feet is the area you want serviced?"
        if state == "action_sqft":
            return "Please let me know the square footage of your property."
        if state == "follow_up":
            return "Thanks I've noted those details. We will reach out with a booking"
        if state == "done":
            return "All set—thanks! We will reach out if anything is needed"
        if state == "not_interested":
            return "Thank you, no problem. Bye"
        if state == "pause":
            return "Let's pause for now. Ping me 'resume' when you're ready."
        if state == "stop":
            return "You're opted out"
        if state == "confused":
            count = snap.get("confused_count", 0)
            return f"Sorry, could you clarify?"
        return "I didn't catch that, mind rephrasing?"
