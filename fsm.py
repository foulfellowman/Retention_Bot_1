import logging

from transitions import Machine, State

from logging_config import configure_logging


logger = logging.getLogger(__name__)


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
        self.machine.add_transition(trigger='receive_followup', source=['action_sqft', 'interested'], dest='follow_up')
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
    logger.info("=== Positive Happy Path ===")
    flow = IntentionFlow("User-A")
    logger.info("Start in: %s", flow.state)

    flow.receive_positive_response()
    logger.info("-> receive_positive_response -> %s", flow.state)

    flow.go_to_sqft()
    logger.info("-> go_to_sqft -> %s", flow.state)

    flow.receive_followup()
    logger.info("-> receive_followup -> %s", flow.state)

    flow.complete_flow()
    logger.info("-> complete_flow -> %s", flow.state)

    logger.info("")
    logger.info("=== Confused To Pause Path ===")
    flow = IntentionFlow("User-B")
    logger.info("Start in: %s", flow.state)

    flow.retry_confused()
    logger.info("-> retry_confused -> %s (count: %s)", flow.state, flow.confused_count)

    flow.retry_confused()
    logger.info("-> retry_confused -> %s (count: %s)", flow.state, flow.confused_count)

    flow.retry_confused()
    logger.info("-> retry_confused -> %s (count: %s)", flow.state, flow.confused_count)

    flow.pause_flow()
    logger.info("-> pause_flow -> %s", flow.state)

    flow.resume_flow()
    logger.info("-> resume_flow -> %s", flow.state)

    logger.info("")
    logger.info("=== User Opted Out Early ===")
    flow = IntentionFlow("User-C")
    logger.info("Start in: %s", flow.state)
    flow.receive_negative_response()
    logger.info("-> receive_negative_response -> %s", flow.state)

    logger.info("")
    logger.info("=== User Sent STOP ===")
    flow = IntentionFlow("User-D")
    logger.info("Start in: %s", flow.state)
    flow.user_stopped()
    logger.info("-> user_stopped -> %s", flow.state)


if __name__ == "__main__":
    configure_logging()
    simulate_all_paths()
