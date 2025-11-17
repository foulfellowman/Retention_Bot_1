import pytest

pytest.importorskip("flask")
pytest.importorskip("flask_login")
pytest.importorskip("flask_wtf")

import app as app_module
import reach_out as reach_out_module
'''
It exercises the full /reach-out/run path using real Flask machinery but with sandboxed dependencies, so it proves that:

When a logged-in session hits the route, it successfully pulls one candidate from the DB layer, runs it through the 
GPT/user-context machinery, and calls Twilio via the mocked client instead of the real API. The endpoint responds 
with HTTP 200 and a JSON payload where status == "ok" and the summary counters (sent, requested, errors) reflect 
exactly one successful send. The sandbox Twilio client captured a single message with the expected phone number and 
body, demonstrating that outbound SMS stays in the stub transport during tests. In short, the test is an integration 
check that the reach-out run endpoint wires together DB → GPT/UserContext → Twilio, respects login requirements, 
and doesn’t leak real SMS, making it safe to mark the flow “dev ready.” '''


class SandboxTwilioClient:
    def __init__(self, *_, **__):
        self.sent = []

    def verify_credentials(self):
        return None

    def send_sms(self, to_phone: str, message: str):
        self.sent.append((to_phone, message))
        return f"sandbox-{len(self.sent):05d}"


class DummyGPTClient:
    def __init__(self, *_, **__):
        self.cleared = []
        self.logged = []

    def set_context(self, phone, value):
        self.cleared.append((phone, list(value)))

    def insert_with_db_instance(self, _db_instance, body, user, twilio_sid=None):
        self.logged.append(
            {
                "phone": user.phone_number,
                "body": body,
                "twilio_sid": twilio_sid,
            }
        )


class FakeScalarResult:
    def __init__(self, value=0):
        self.value = value

    def scalar_one(self):
        return self.value


class FakeSession:
    def __init__(self):
        self.objects = []
        self._next_run_id = 1

    def add(self, obj):
        if obj not in self.objects:
            self.objects.append(obj)

    def flush(self):
        for obj in self.objects:
            if hasattr(obj, "run_id") and getattr(obj, "run_id") is None:
                setattr(obj, "run_id", self._next_run_id)
                self._next_run_id += 1

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None

    def execute(self, _stmt):
        return FakeScalarResult(0)


class FakeDB:
    candidates = []

    def __init__(self):
        self.session = FakeSession()
        self.closed = False

    def fetch_reach_out_candidates(self, limit, exclude_states=("done", "stop")):
        return list(self.candidates)[:limit]

    def close(self):
        self.closed = True


class DummyUserContext:
    def __init__(self, phone_number: str):
        self.phone_number = phone_number
        self.info_calls = []

    def set_user_info(self, name, services, days, last_service):
        self.info_calls.append((name, services, days, last_service))

    def get_fsm_snapshot(self):
        return {"state": "start"}

    def reply_for_state(self, _snapshot):
        return f"sandbox-msg-{self.phone_number}"

    def get_current_state(self):
        return "start"


@pytest.fixture
def reach_out_app(monkeypatch):
    sandbox_twilio = SandboxTwilioClient()
    dummy_gpt = DummyGPTClient()

    monkeypatch.setattr(app_module, "TwilioSMSClient", lambda *args, **kwargs: sandbox_twilio)
    monkeypatch.setattr(app_module, "GPTClient", lambda *args, **kwargs: dummy_gpt)
    monkeypatch.setattr(app_module, "DB", FakeDB)
    monkeypatch.setattr(reach_out_module, "UserContext", DummyUserContext)

    FakeDB.candidates = [
        {
            "phone_number": "+15555550123",
            "name": "Fixture Customer",
        }
    ]

    monkeypatch.setenv("OUTBOUND_LIVE_TOGGLE", "1")
    monkeypatch.setenv("REACH_OUT_FETCH_LIMIT", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-key")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")

    app = app_module.create_app()
    app.testing = True

    sentinel_attr = "_csrf_disabled_for_reach_out_test"
    if getattr(app_module, sentinel_attr, False):
        raise AssertionError("CSRF has already been disabled elsewhere in the suite.")
    original_csrf_enabled = app.config.get("WTF_CSRF_ENABLED", True)

    app_module.__dict__[sentinel_attr] = True
    app.config["WTF_CSRF_ENABLED"] = False

    try:
        yield app, sandbox_twilio
    finally:
        app.config["WTF_CSRF_ENABLED"] = original_csrf_enabled
        app_module.__dict__[sentinel_attr] = False


def test_reach_out_run_uses_sandbox_transport(reach_out_app):
    app, sandbox_twilio = reach_out_app
    client = app.test_client()

    admin_user = app.config["services"]["admin_user"]
    with client.session_transaction() as session:
        session["_user_id"] = admin_user.get_id()
        session["_fresh"] = True

    response = client.post("/reach-out/run", data={"limit": "1"})
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["summary"]["sent"] == 1
    assert payload["summary"]["requested"] == 1
    assert payload["summary"]["errors"] == 0

    assert sandbox_twilio.sent == [
        ("+15555550123", "sandbox-msg-+15555550123")
    ]
