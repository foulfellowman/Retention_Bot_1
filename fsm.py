from transitions import Machine, State


class IntentionFlow:
    states = [
        State(name='start'),
        State(name='interested', on_enter=['mark_interested']),
        State(name='action_sqft', on_enter=['mark_interested']),
        State(name='confused'),
        State(name='not_interested'),
        State(name='follow_up'),
        State(name='pause'),
        State(name='stop'),
        State(name='done'),
    ]

    def __init__(self, name):
        self.name = name
        self.confused_count = 0
        self.was_ever_interested = False
        self.flow_version = 1
        self.machine = Machine(model=self, states=IntentionFlow.states, initial='start')

        # Define transitions
        self.machine.add_transition(trigger='receive_positive_response', source=['start', 'confused', 'pause'], dest='interested')
        self.machine.add_transition(trigger='go_to_sqft', source=['interested', 'start', 'confused'], dest='action_sqft')
        self.machine.add_transition(trigger='receive_followup', source=['action_sqft','interested'], dest='follow_up')
        self.machine.add_transition(trigger='complete_flow', source='follow_up', dest='done')
        self.machine.add_transition(trigger='receive_negative_response', source='*', dest='not_interested')
        self.machine.add_transition(trigger='user_stopped', source='*', dest='stop')
        self.machine.add_transition(trigger='retry_confused', source='*', dest='confused', after='increment_confused')
        self.machine.add_transition(trigger='pause_flow', source='confused', dest='pause', conditions='max_confused')
        self.machine.add_transition(trigger='resume_flow', source='pause', dest='start')

        #
        self.machine.add_transition(trigger='polite_ack', source='follow_up', dest='done')

    def mark_interested(self):
        self.was_ever_interested = True

    def increment_confused(self):
        self.confused_count += 1
        if self.max_confused():
            # auto-pause if limit reached
            self.pause_flow()

    def max_confused(self):
        return self.confused_count >= 3

    # in your IntentionFlow class, add:
    def snapshot(self) -> dict:
        return {
            "flow_state": self.state,
            "confused_count": getattr(self, "confused_count", 0),
            "was_ever_interested": getattr(self, "was_ever_interested", False),
        }


def simulate_all_paths():
    print("=== 🟢 Positive Happy Path ===")
    flow = IntentionFlow("User-A")
    print(f"Start in: {flow.state}")

    flow.receive_positive_response()
    print(f"→ receive_positive_response → {flow.state}")

    flow.go_to_sqft()
    print(f"→ go_to_sqft → {flow.state}")

    flow.receive_followup()
    print(f"→ receive_followup → {flow.state}")

    flow.complete_flow()
    print(f"→ complete_flow → {flow.state}")

    print("\n=== 🟡 Confused → Pause Path ===")
    flow = IntentionFlow("User-B")
    print(f"Start in: {flow.state}")

    flow.retry_confused()
    print(f"→ retry_confused → {flow.state} (count: {flow.confused_count})")

    flow.retry_confused()
    print(f"→ retry_confused → {flow.state} (count: {flow.confused_count})")

    flow.retry_confused()
    print(f"→ retry_confused → {flow.state} (count: {flow.confused_count})")

    flow.pause_flow()
    print(f"→ pause_flow → {flow.state}")

    flow.resume_flow()
    print(f"→ resume_flow → {flow.state}")

    print("\n=== 🔴 User Opted Out Early ===")
    flow = IntentionFlow("User-C")
    print(f"Start in: {flow.state}")
    flow.receive_negative_response()
    print(f"→ receive_negative_response → {flow.state}")

    print("\n=== 🔕 User Sent STOP ===")
    flow = IntentionFlow("User-D")
    print(f"Start in: {flow.state}")
    flow.user_stopped()
    print(f"→ user_stopped → {flow.state}")


if __name__ == "__main__":
    simulate_all_paths()
