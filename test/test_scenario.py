import json


def test_action_sqft_numeric_input_advances_to_follow_up(monkeypatch):
    # Fake flow allows receive_followup and moves to follow_up
    class Flow:
        def __init__(self, name): self.state = "action_sqft"

        def snapshot(self): return {"flow_state": self.state}

        def receive_followup(self, **kwargs):
            assert kwargs.get("sqft") == 1500
            self.state = "follow_up"

    import main_intent as ft
    import user_context as uc

    monkeypatch.setattr(uc, "IntentionFlow", Flow, raising=True)

    # Build user and stash last inbound
    monkeypatch.setattr(uc, "DB", type("D", (), {"__init__": lambda s: None, "close": lambda s: None,
                                                 "conn": type("C", (), {"cursor": lambda s: type("K", (), {
                                                     "execute": lambda *a, **k: None, "fetchone": lambda s: (1,),
                                                     "close": lambda s: None})(), "commit": lambda s: None})()}),
                        raising=True)
    user = uc.UserContext("4802982000")
    user.set_twilio_message("1500")

    out = json.loads(ft.tool_update_fsm(user, "go_to_sqft"))
    assert out["applied"] is True
    assert out["event"] == "receive_followup"
    assert out["to_state"] == "follow_up"
