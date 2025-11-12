import types
from datetime import datetime

import pytest

import reach_out
from reach_out import ReachOut


class DummyTwilio:
    def __init__(self, fail_numbers=None):
        self.sent = []
        self.fail_numbers = set(fail_numbers or [])

    def send_sms(self, to_phone: str, message: str):
        if to_phone in self.fail_numbers:
            raise RuntimeError("twilio failure")
        self.sent.append((to_phone, message))
        return f"SM{len(self.sent):05d}"


class DummyGPT:
    def __init__(self):
        self.context = {}
        self.logged = []

    def set_context(self, phone, value):
        self.context[phone] = list(value)

    def insert_with_db_instance(self, db_instance, body, user, twilio_sid=None):
        self.logged.append((db_instance, body, user.phone_number, twilio_sid))


class DummyUserContext:
    def __init__(self, phone, reply="reply"):
        self.phone_number = phone
        self._reply = reply
        self.info_calls = []

    def set_user_info(self, name, services, days, last_service):
        self.info_calls.append((name, services, days, last_service))

    def get_current_state(self):
        return "start"

    def get_fsm_snapshot(self):
        return {"flow_state": "start"}

    def reply_for_state(self, snap):
        return self._reply


class DummyExecuteResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class FakeSession:
    def __init__(self):
        self.objects = []
        self._next_run_id = 1
        self.executed = []

    def add(self, obj):
        if obj not in self.objects:
            self.objects.append(obj)

    def flush(self):
        for obj in self.objects:
            if hasattr(obj, "run_id") and getattr(obj, "run_id") is None:
                obj.run_id = self._next_run_id
                self._next_run_id += 1

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def execute(self, stmt):
        self.executed.append(stmt)
        return DummyExecuteResult(2)


class FakeDB:
    def __init__(self, session=None):
        self.session = session or FakeSession()
        self.closed = False

    def close(self):
        self.closed = True


class DBFactory:
    def __init__(self):
        self.instances = []

    def __call__(self):
        db = FakeDB()
        self.instances.append(db)
        return db


class DummyReachOutRun:
    def __init__(self, **kwargs):
        self.run_id = None
        self.started_at = kwargs.get("started_at")
        self.finished_at = kwargs.get("finished_at")
        self.requested = kwargs.get("requested", 0)
        self.processed = kwargs.get("processed", 0)
        self.sent = kwargs.get("sent", 0)
        self.skipped = kwargs.get("skipped", 0)
        self.throttled = kwargs.get("throttled", 0)
        self.errors = kwargs.get("errors", 0)
        self.context = kwargs.get("context", {})


def make_reach_out(monkeypatch, gpt=None, twilio=None, db_factory=None, max_active=None):
    gpt = gpt or DummyGPT()
    twilio = twilio or DummyTwilio()
    db_factory = db_factory or DBFactory()
    ro = ReachOut(gpt, twilio, db_factory=db_factory, max_active_conversations=max_active)
    ro._count_active_conversations = lambda db: 0  # default no throttling
    monkeypatch.setattr(reach_out, "ReachOutRun", DummyReachOutRun)
    monkeypatch.setenv("OUTBOUND_LIVE_TOGGLE", "1")
    return ro, gpt, twilio, db_factory


def test_extract_phone_prefers_known_keys():
    row = {"mobile": " 123 ", "phone": "456", "phone_number": "789"}
    assert ReachOut._extract_phone(row) == "789"

    class Obj:
        phone = "111"

    assert ReachOut._extract_phone(Obj()) == "111"


def test_value_from_mapping_or_attr():
    row = {"first_name": "Alice"}
    assert ReachOut._value(row, "first_name") == "Alice"

    class Obj:
        last_name = "Smith"

    assert ReachOut._value(Obj(), "last_name") == "Smith"


def test_to_mapping_collects_attrs():
    class Obj:
        a = 1
        b = 2
        _private = "x"

    mapping = ReachOut._to_mapping(Obj())
    assert mapping["a"] == 1 and mapping["b"] == 2
    assert "_private" not in mapping


def test_coerce_services_handles_types():
    assert ReachOut._coerce_services(None) == []
    assert ReachOut._coerce_services("a, b") == ["a", "b"]
    assert ReachOut._coerce_services(["x", "y"]) == ["x", "y"]


def test_coerce_int_with_invalid():
    assert ReachOut._coerce_int("5") == 5
    assert ReachOut._coerce_int("bad") == 0


def test_resolve_message_uses_template():
    ro = ReachOut(DummyGPT(), DummyTwilio(), db_factory=DBFactory())
    user = DummyUserContext("123", reply="fallback")
    row = {"name": "Alice"}
    msg = ro._resolve_message(user, row, "Hello {name}")
    assert msg == "Hello Alice"


def test_resolve_message_falls_back_on_error():
    ro = ReachOut(DummyGPT(), DummyTwilio(), db_factory=DBFactory())
    user = DummyUserContext("123", reply="fallback")
    row = {"missing": "field"}
    msg = ro._resolve_message(user, row, "Hi {name}")
    assert msg == "fallback"


def test_build_user_context_sets_info(monkeypatch):
    captured = {}

    def fake_user_context(phone):
        ctx = DummyUserContext(phone)
        captured["ctx"] = ctx
        return ctx

    monkeypatch.setattr(reach_out, "UserContext", fake_user_context)
    ro, *_ = make_reach_out(monkeypatch)
    row = {
        "first_name": "Jane",
        "last_name": "Doe",
        "services": "A,B",
        "days_since_cancelled": "7",
        "last_service": "Pest Control",
    }
    user = ro._build_user_context("555", row)
    assert user.info_calls[0] == ("Jane Doe", ["A", "B"], 7, "Pest Control")


def test_load_throttle_from_env_prioritizes_concurrency(monkeypatch):
    monkeypatch.setenv("REACH_OUT_CONCURRENCY", "5")
    monkeypatch.setenv("REACH_OUT_CONCURRENCY_MAX", "10")
    assert ReachOut._load_throttle_from_env() == 5
    monkeypatch.delenv("REACH_OUT_CONCURRENCY")
    assert ReachOut._load_throttle_from_env() == 10
    monkeypatch.delenv("REACH_OUT_CONCURRENCY_MAX", raising=False)


def test_resolve_max_active_with_ceiling(monkeypatch):

    # clear any fallback throttle envs that may be set in the environment
    monkeypatch.delenv("REACH_OUT_CONCURRENCY_MAX", raising=False)
    monkeypatch.delenv("REACH_OUT_THROTTLE", raising=False)
    monkeypatch.delenv("REACH_OUT_MAX_ACTIVE", raising=False)

    ro, *_ = make_reach_out(monkeypatch, max_active=8)
    ro._apply_ceiling = ReachOut._apply_ceiling

    assert ro._resolve_max_active(override=5) == 5
    assert ro._resolve_max_active(override=12) == 8

    monkeypatch.setenv("REACH_OUT_CONCURRENCY", "6")
    assert ro._resolve_max_active(override=None) == 6
    monkeypatch.setenv("REACH_OUT_CONCURRENCY", "12")
    assert ro._resolve_max_active(override=None) == 8
    monkeypatch.delenv("REACH_OUT_CONCURRENCY")

    assert ro._resolve_max_active(override=None) == 8


def test_apply_ceiling_limits():
    assert ReachOut._apply_ceiling(10, None) == 10
    assert ReachOut._apply_ceiling(10, 8) == 8


def test_count_active_conversations_uses_scalar():
    db = FakeDB()
    result = ReachOut._count_active_conversations(db)
    assert result == 2


def test_send_bulk_returns_summary(monkeypatch):
    db_factory = DBFactory()
    ro, gpt, twilio, factory = make_reach_out(monkeypatch, db_factory=db_factory, max_active=1)

    def build_user(_, row):
        phone = ReachOut._extract_phone(row)
        return DummyUserContext(phone, reply=f"msg-{phone}")

    ro._build_user_context = build_user  # type: ignore[assignment]
    counts = iter([1, 0, 0, 0])
    ro._count_active_conversations = lambda db: next(counts, 0)  # type: ignore[assignment]

    twilio.fail_numbers.add("333")

    rows = [
        {"phone_number": "111", "name": "A"},
        {"phone_number": "222", "name": "B"},
        {"phone_number": "333", "name": "C"},
        {"name": "NoPhone"},
    ]

    outcome = ro.send_bulk(rows, max_active=1)
    summary = outcome["summary"]
    assert summary == {
        "requested": 4,
        "processed": 4,
        "sent": 1,
        "skipped": 1,
        "throttled": 1,
        "errors": 1,
        "max_active_limit": 1,
    }
    results = outcome["results"]
    statuses = [item["status"] for item in results]
    assert statuses == ["skipped", "sent", "error", "skipped"]
    assert results[0]["reason"] == "throttled"
    assert results[1]["message"] == "msg-222"
    assert results[2]["status"] == "error"
    assert results[3]["reason"] == "missing phone"

    run_db = factory.instances[0]
    run_log = next(obj for obj in run_db.session.objects if isinstance(obj, DummyReachOutRun))
    assert run_log.sent == 1
    assert run_log.skipped == 1
    assert run_log.throttled == 1
    assert run_log.errors == 1
    assert isinstance(run_log.finished_at, datetime)
    assert run_db.closed


def test_send_bulk_respects_outbound_toggle(monkeypatch):
    ro, _, twilio, _ = make_reach_out(monkeypatch)
    monkeypatch.setenv("OUTBOUND_LIVE_TOGGLE", "0")

    ro._build_user_context = lambda _self, row: DummyUserContext(row["phone_number"])  # type: ignore[assignment]

    rows = [{"phone_number": "555"}]
    outcome = ro.send_bulk(rows)
    summary = outcome["summary"]

    assert summary["sent"] == 0
    assert summary["skipped"] == 1
    assert twilio.sent == []

    result = outcome["results"][0]
    assert result["reason"] == "outbound disabled"
