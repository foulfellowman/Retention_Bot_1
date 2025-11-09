import pytest
from fsm import IntentionFlow  # your FSM module


def test_initial_state():
    flow = IntentionFlow("user")
    assert flow.state == "start"
    assert flow.confused_count == 0
    assert flow.was_ever_interested is False


def test_positive_path_to_done():
    flow = IntentionFlow("user")

    flow.receive_positive_response()
    assert flow.state == "interested"
    assert flow.was_ever_interested is True

    flow.go_to_sqft()
    assert flow.state == "action_sqft"

    flow.receive_followup()
    assert flow.state == "follow_up"

    flow.complete_flow()
    assert flow.state == "done"


def test_negative_response_from_anywhere():
    states = [
        "start", "interested", "action_sqft", "confused",
        "follow_up", "pause"
    ]

    for state in states:
        flow = IntentionFlow("user")
        flow.state = state
        flow.receive_negative_response()
        assert flow.state == "not_interested"


def test_user_stopped_from_anywhere():
    states = [
        "start", "interested", "action_sqft", "confused",
        "pause", "follow_up"
    ]

    for state in states:
        flow = IntentionFlow("user")
        flow.state = state
        flow.user_stopped()
        assert flow.state == "stop"


def test_confused_retry_loop_and_pause():
    flow = IntentionFlow("user")

    # Retry three times
    for i in range(3):
        flow.retry_confused()
        # assert flow.state == "confused"
        assert flow.confused_count == i + 1

    # Now flow is eligible to pause
    # flow.pause_flow()
    assert flow.state == "pause"

    # Resume back to start
    flow.resume_flow()
    assert flow.state == "start"


def test_resume_flow_only_from_pause():
    flow = IntentionFlow("user")
    flow.state = "pause"
    flow.resume_flow()
    assert flow.state == "start"

    # Should fail from other states (optional guard test)
    flow.state = "start"
    with pytest.raises(Exception):
        flow.resume_flow()


def test_mark_interested_only_sets_flag_once():
    flow = IntentionFlow("user")

    assert flow.was_ever_interested is False
    flow.receive_positive_response()
    assert flow.was_ever_interested is True

    # Ensure flag doesn't get unset
    flow.go_to_sqft()
    assert flow.was_ever_interested is True

    # Even if we go to confused and pause
    flow.retry_confused()
    flow.retry_confused()
    flow.retry_confused()
    # we are already in pause flow
    # flow.pause_flow()
    assert flow.was_ever_interested is True


def test_invalid_transition_raises():
    flow = IntentionFlow("user")
    # go_to_sqft isn't allowed from start
    with pytest.raises(Exception):
        flow.go_to_sqft()

    # complete_flow isn't allowed from start
    with pytest.raises(Exception):
        flow.complete_flow()
