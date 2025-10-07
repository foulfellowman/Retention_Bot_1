from __future__ import annotations

import atexit
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Union

from dotenv import load_dotenv
from sqlalchemy import create_engine, exists, func, inspect, or_, select
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from models import (
    Base,
    Contact,
    FSMState,
    Phone,
    Message,
    TestCase,
    TestRun,
    TwilioMessage,
    Usage,
)

load_dotenv()


_engine_lock = threading.Lock()
_engine: Optional[Engine] = None
_session_lock = threading.Lock()
_SessionFactory: Optional[sessionmaker[Session]] = None


def _env_flag(name: str) -> Optional[bool]:
    value = os.getenv(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "t", "yes", "on"}


def _build_engine_url() -> Union[str, URL]:
    url = (
        os.getenv("SQLALCHEMY_DATABASE_URI")
        or os.getenv("PG_DSN")
        or os.getenv("PG_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("PGBOUNCER_DSN")
    )
    if url:
        return url

    driver = os.getenv("PG_DRIVER", "postgresql+psycopg2")
    username = os.getenv("PG_USER")
    password = os.getenv("PG_PASS") or os.getenv("PG_PASSWORD")
    host = os.getenv("PG_HOST", "localhost")
    port_raw = os.getenv("PG_PORT")
    database = os.getenv("PG_DB")

    port: Optional[int] = None
    if port_raw:
        try:
            port = int(port_raw)
        except ValueError:
            port = None

    return URL.create(
        drivername=driver,
        username=username,
        password=password,
        host=host,
        port=port,
        database=database,
    )


def _build_connect_args() -> Dict[str, Any]:
    optional_env_map = {
        "PG_SSLMODE": "sslmode",
        "PG_TARGET_SESSION_ATTRS": "target_session_attrs",
        "PG_CONNECT_TIMEOUT": "connect_timeout",
        "PG_APPLICATION_NAME": "application_name",
        "PG_APP_NAME": "application_name",
        "PG_OPTIONS": "options",
    }

    connect_args: Dict[str, Any] = {}
    for env_name, param_name in optional_env_map.items():
        value = os.getenv(env_name)
        if value:
            if param_name == "connect_timeout":
                try:
                    connect_args[param_name] = int(value)
                except ValueError:
                    connect_args[param_name] = value
            else:
                connect_args[param_name] = value

    return connect_args


_POOL_CLASS_ALIASES = {
    "null": None,
    "nullpool": None,
    "queue": "queue",
    "queuepool": "queue",
    "static": "static",
    "staticpool": "static",
}


def _resolve_pool_class():
    from sqlalchemy.pool import NullPool, QueuePool, StaticPool

    choice = (
        os.getenv("SQLALCHEMY_POOL_CLASS")
        or os.getenv("DB_POOL_CLASS")
        or ""
    ).strip().lower()
    if choice:
        return {
            "queue": QueuePool,
            "static": StaticPool,
            "null": NullPool,
        }.get(choice, NullPool)

    if any(
        flag is True
        for flag in (
            _env_flag("SQLALCHEMY_POOL_ENABLED"),
            _env_flag("SQLALCHEMY_QUEUE_POOL"),
            _env_flag("DB_POOL_ENABLED"),
        )
    ):
        return QueuePool

    return NullPool


def get_engine() -> Engine:
    global _engine
    with _engine_lock:
        if _engine is not None:
            return _engine

        url = _build_engine_url()
        connect_args = _build_connect_args()
        pool_class = _resolve_pool_class()
        engine_kwargs: Dict[str, Any] = {"future": True}
        if pool_class is not None:
            engine_kwargs["poolclass"] = pool_class

        echo_flag = _env_flag("SQLALCHEMY_ECHO")
        if echo_flag is True:
            engine_kwargs["echo"] = True

        pre_ping_flag = _env_flag("SQLALCHEMY_POOL_PRE_PING")
        if pre_ping_flag is not None:
            engine_kwargs["pool_pre_ping"] = pre_ping_flag

        if connect_args:
            engine_kwargs["connect_args"] = connect_args

        if pool_class and pool_class.__name__ == "QueuePool":
            pool_size = os.getenv("SQLALCHEMY_POOL_SIZE") or os.getenv("PG_POOL_MIN") or "5"
            max_overflow = os.getenv("SQLALCHEMY_MAX_OVERFLOW") or os.getenv("PG_POOL_MAX_OVERFLOW") or "10"
            pool_timeout = os.getenv("SQLALCHEMY_POOL_TIMEOUT") or "30"
            try:
                engine_kwargs["pool_size"] = int(pool_size)
            except ValueError:
                engine_kwargs["pool_size"] = 5
            try:
                engine_kwargs["max_overflow"] = int(max_overflow)
            except ValueError:
                engine_kwargs["max_overflow"] = 10
            try:
                engine_kwargs["pool_timeout"] = int(pool_timeout)
            except ValueError:
                engine_kwargs["pool_timeout"] = 30

        _engine = create_engine(url, **engine_kwargs)
        return _engine


def dispose_engine() -> None:
    global _engine, _SessionFactory
    with _engine_lock:
        engine = _engine
        _engine = None
        _SessionFactory = None
    if engine is not None:
        engine.dispose()


def _get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    with _session_lock:
        if _SessionFactory is None:
            engine = get_engine()
            _SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    return _SessionFactory


def get_session() -> Session:
    return _get_session_factory()()


class DB:
    def __init__(self) -> None:
        self.session: Session = get_session()

    def __enter__(self) -> "DB":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type:
            try:
                self.session.rollback()
            except SQLAlchemyError:
                pass
        self.close()
        return False

    def close(self) -> None:
        if getattr(self, "session", None) is not None:
            self.session.close()
            self.session = None  # type: ignore[assignment]

    def _ensure_phone(self, phone: str) -> Phone:
        phone = (phone or "").strip()
        if not phone:
            raise ValueError("phone number is required")
        phone_row = self.session.get(Phone, phone)
        if phone_row is None:
            phone_row = Phone(phone_number=phone)
            self.session.add(phone_row)
            self.session.flush()
        return phone_row
    def quick_query(self) -> None:
        bind = self.session.get_bind()
        if bind is None:
            print([])
            return
        inspector = inspect(bind)
        tables: List[tuple[str, str]] = []
        for schema in inspector.get_schema_names():
            if schema in {"pg_catalog", "information_schema"}:
                continue
            for table in inspector.get_table_names(schema=schema):
                tables.append((schema, table))
        print(tables)

    def insert_message(self, phone: str, user_input: str, twilio_sid: Optional[str] = None) -> None:
        self._ensure_phone(phone)
        message = Message(
            phone_number=phone,
            twilio_sid=twilio_sid,
            direction="inbound",
            body=user_input,
            message_data={"role": "user", "content": user_input},
            sent_at=datetime.utcnow(),
        )
        self.session.add(message)
        self.session.commit()

    def SQL_latest_message_per_phone(self):
        stmt = (
            select(
                Message.phone_number,
                func.max(Message.sent_at).label("last_message_at"),
            )
            .group_by(Message.phone_number)
            .order_by(Message.phone_number)
        )
        return self.session.execute(stmt).all()

    def SQL_full_conversation_per_phone(self, phone: str) -> List[Dict[str, Any]]:
        stmt = (
            select(Message)
            .where(Message.phone_number == phone)
            .order_by(Message.message_id.asc())
        )
        messages = self.session.execute(stmt).scalars().all()

        out: List[Dict[str, Any]] = []
        for message in messages:
            payload = message.message_data if isinstance(message.message_data, dict) else {}
            role = payload.get("role") if isinstance(payload, dict) else None
            content = payload.get("content") if isinstance(payload, dict) else None
            if not role:
                role = "assistant" if message.direction == "outbound" else "user"
            if not content:
                content = message.body
            out.append({"role": role, "content": content, "sent_at": message.sent_at})
        return out

    def fetch_conversations(
        self,
        q: Optional[str] = None,
        sort: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        latest_messages = (
            select(
                Message.message_id,
                Message.phone_number,
                Message.direction,
                Message.body,
                Message.message_data,
                Message.sent_at,
                func.row_number().over(
                    partition_by=Message.phone_number,
                    order_by=[Message.sent_at.desc(), Message.message_id.desc()],
                ).label("rank"),
            )
        ).subquery()

        stmt = (
            select(
                latest_messages.c.phone_number,
                latest_messages.c.direction,
                latest_messages.c.body,
                latest_messages.c.message_data,
                latest_messages.c.sent_at,
                FSMState.statename,
                FSMState.was_interested,
                Contact.first_name,
                Contact.last_name,
            )
            .outerjoin(FSMState, FSMState.phone_number == latest_messages.c.phone_number)
            .outerjoin(Contact, Contact.phone_number == latest_messages.c.phone_number)
            .where(latest_messages.c.rank == 1)
        )

        if q:
            pattern = f"%{q}%"
            stmt = stmt.where(
                or_(
                    latest_messages.c.phone_number.ilike(pattern),
                    latest_messages.c.message_data["content"].astext.ilike(pattern),
                    Contact.first_name.ilike(pattern),
                    Contact.last_name.ilike(pattern),
                )
            )

        rows = self.session.execute(stmt).all()

        items: List[Dict[str, Any]] = []
        for row in rows:
            message_data = row.message_data if isinstance(row.message_data, dict) else {}
            role = None
            text = None
            if isinstance(message_data, dict):
                role = message_data.get("role")
                text = message_data.get("content")
            if not role:
                role = row.direction
            if not text:
                text = row.body or ""

            name_parts: List[str] = []
            if row.first_name and row.first_name.strip():
                name_parts.append(row.first_name.strip())
            if row.last_name and row.last_name.strip():
                name_parts.append(row.last_name.strip())

            items.append(
                {
                    "phone_number": row.phone_number,
                    "display_name": " ".join(name_parts),
                    "status": (str(row.statename).upper() if row.statename else "None"),
                    "last_message_at": row.sent_at,
                    "last_snippet": (text or "")[:80],
                    "last_role": role,
                    "was_interested": bool(row.was_interested),
                }
            )

        sort_key = (sort or "").lower()
        direction_key = (direction or "asc").lower()
        if direction_key not in ("asc", "desc"):
            direction_key = "asc"
        reverse = direction_key == "desc"

        if sort_key == "name":
            items.sort(key=lambda item: (item["display_name"] or "").lower(), reverse=reverse)
        elif sort_key == "number":
            items.sort(key=lambda item: item["phone_number"], reverse=reverse)
        elif sort_key == "status":
            items.sort(key=lambda item: item["status"] or "", reverse=reverse)

        return items

    def _lookup_contact_names(self, phones: List[str]) -> Dict[str, str]:
        if not phones:
            return {}

        stmt = select(Contact.phone_number, Contact.first_name, Contact.last_name).where(
            Contact.phone_number.in_(phones)
        )
        rows = self.session.execute(stmt).all()

        name_map: Dict[str, str] = {}
        for phone, first, last in rows:
            parts: List[str] = []
            if first:
                parts.append(first.strip())
            if last:
                parts.append(last.strip())
            if parts:
                name_map[phone] = " ".join(parts)
        return name_map


def insert_message(db_connection: "DB", phone: str, user_input: str) -> None:
    message = Message(
        phone_number=phone,
        direction="inbound",
        body=user_input,
        message_data={"role": "user", "content": user_input},
        sent_at=datetime.utcnow(),
    )
    db_connection.session.add(message)
    db_connection.session.commit()


def insert_message_from_gpt(db_connection: "DB", phone: str, gpt_input: str) -> None:
    message = Message(
        phone_number=phone,
        direction="outbound",
        body=gpt_input,
        message_data={"role": "developer", "content": gpt_input},
        sent_at=datetime.utcnow(),
    )
    db_connection.session.add(message)
    db_connection.session.commit()


def ensure_test_run_tables(db_connection: "DB") -> None:
    bind = db_connection.session.get_bind()
    if bind is not None:
        Base.metadata.create_all(bind=bind, tables=[TestRun.__table__, TestCase.__table__])


def insert_test_run(
    db_connection: "DB",
    started_at: Optional[datetime] = None,
    total_passed: int = 0,
    total_failed: int = 0,
) -> int:
    run = TestRun(
        started_at=started_at or datetime.utcnow(),
        total_passed=total_passed,
        total_failed=total_failed,
    )
    db_connection.session.add(run)
    db_connection.session.commit()
    db_connection.session.refresh(run)
    return run.run_id


def update_test_run(
    db_connection: "DB",
    run_id: int,
    finished_at: Optional[datetime] = None,
    total_passed: Optional[int] = None,
    total_failed: Optional[int] = None,
) -> None:
    run = db_connection.session.get(TestRun, run_id)
    if run is None:
        raise ValueError(f"test_run {run_id} does not exist")

    run.finished_at = finished_at or datetime.utcnow()
    if total_passed is not None:
        run.total_passed = total_passed
    if total_failed is not None:
        run.total_failed = total_failed

    db_connection.session.commit()


def insert_test_case(
    db_connection: "DB",
    run_id: int,
    name: str,
    result: str,
    steps_verified: int,
    total_steps: int,
    duration_seconds: Optional[float] = None,
    finished_at: Optional[datetime] = None,
) -> int:
    case = TestCase(
        run_id=run_id,
        name=name,
        result=result,
        steps_verified=steps_verified,
        total_steps=total_steps,
        duration_seconds=duration_seconds,
        finished_at=finished_at or datetime.utcnow(),
    )
    db_connection.session.add(case)
    db_connection.session.commit()
    db_connection.session.refresh(case)
    return case.case_id


atexit.register(dispose_engine)
