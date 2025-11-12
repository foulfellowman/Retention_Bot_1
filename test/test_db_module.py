import pytest
from unittest.mock import MagicMock

import db
from db import DB
from models import Message, Phone


@pytest.fixture
def build_db(monkeypatch):
    """Return a factory that yields (DB instance, mocked SQLAlchemy session)."""
    sessions: list[MagicMock] = []

    def make_session():
        session = MagicMock(name=f"session-{len(sessions) + 1}")
        sessions.append(session)
        return session

    monkeypatch.setattr(db, "get_session", make_session)

    def factory():
        instance = DB()
        return instance, sessions[-1]

    return factory


def test_close_closes_session_and_clears_reference(build_db):
    db_instance, session = build_db()

    db_instance.close()

    session.close.assert_called_once()
    assert db_instance.session is None


def test_context_manager_rolls_back_on_exception(build_db):
    db_instance, session = build_db()

    with pytest.raises(RuntimeError):
        with db_instance:
            raise RuntimeError("boom")

    session.rollback.assert_called_once()
    session.close.assert_called_once()


def test_quick_query_without_bind_skips_inspect(build_db, monkeypatch):
    db_instance, session = build_db()
    session.get_bind.return_value = None
    inspect_mock = MagicMock()
    monkeypatch.setattr(db, "inspect", inspect_mock)

    db_instance.quick_query()

    inspect_mock.assert_not_called()


def test_quick_query_with_bind_uses_inspect(build_db, monkeypatch):
    db_instance, session = build_db()
    fake_bind = object()
    session.get_bind.return_value = fake_bind

    class FakeInspector:
        def get_schema_names(self):
            return ["public", "pg_catalog"]

        def get_table_names(self, schema: str):
            if schema == "public":
                return ["message", "phone"]
            return ["ignored"]

    inspect_mock = MagicMock(return_value=FakeInspector())
    monkeypatch.setattr(db, "inspect", inspect_mock)

    db_instance.quick_query()

    inspect_mock.assert_called_once_with(fake_bind)


def test_insert_message_persists_inbound_payload(build_db, monkeypatch):
    db_instance, session = build_db()
    ensure_mock = MagicMock()
    monkeypatch.setattr(DB, "_ensure_phone", ensure_mock)

    db_instance.insert_message("1234567890", "hello world", twilio_sid="SM123")

    ensure_mock.assert_called_once_with("1234567890")
    added_message = session.add.call_args[0][0]
    assert isinstance(added_message, Message)
    assert added_message.phone_number == "1234567890"
    assert added_message.direction == "inbound"
    assert added_message.body == "hello world"
    assert added_message.twilio_sid == "SM123"
    assert added_message.message_data == {"role": "user", "content": "hello world"}
    session.commit.assert_called_once()


def test_insert_message_from_gpt_records_outbound(build_db):
    db_instance, session = build_db()

    db_instance.insert_message_from_gpt("1112223333", "bot reply")

    added_message = session.add.call_args[0][0]
    assert isinstance(added_message, Message)
    assert added_message.phone_number == "1112223333"
    assert added_message.direction == "outbound"
    assert added_message.body == "bot reply"
    assert added_message.twilio_sid is None
    assert added_message.message_data == {"role": "developer", "content": "bot reply"}
    session.commit.assert_called_once()


def test_ensure_phone_inserts_when_missing(build_db):
    db_instance, session = build_db()
    session.get.return_value = None

    phone = db_instance._ensure_phone("15558675309")

    assert isinstance(phone, Phone)
    assert phone.phone_number == "15558675309"
    session.add.assert_called_once()
    session.flush.assert_called_once()


def test_ensure_phone_returns_existing_without_inserting(build_db):
    db_instance, session = build_db()
    existing = Phone(phone_number="18005551212")
    session.get.return_value = existing

    phone = db_instance._ensure_phone("18005551212")

    assert phone is existing
    session.add.assert_not_called()
    session.flush.assert_not_called()
