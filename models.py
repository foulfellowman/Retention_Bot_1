from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.associationproxy import association_proxy


class Base(DeclarativeBase):
    """Base class for all ORM models."""


class Phone(Base):
    __tablename__ = "phone"

    phone_number: Mapped[str] = mapped_column(String(15), primary_key=True)

    contacts = relationship("Contact", back_populates="phone")
    fsm_state = relationship("FSMState", back_populates="phone", uselist=False)
    messages = relationship("Message", back_populates="phone")
    twilio_messages = relationship("TwilioMessage", back_populates="phone")


class Contact(Base):
    __tablename__ = "contact"

    phone_number: Mapped[str] = mapped_column(
        String(15), ForeignKey("phone.phone_number"), primary_key=True
    )
    customer_id: Mapped[Optional[int]] = mapped_column("CustomerID", Integer, nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    phone = relationship("Phone", back_populates="contacts")
    fsm_state = association_proxy("phone", "fsm_state")


class FSMState(Base):
    __tablename__ = "fsm_state"

    phone_number: Mapped[str] = mapped_column(
        String(15), ForeignKey("phone.phone_number"), primary_key=True
    )
    statename: Mapped[str] = mapped_column(String(30), nullable=False)
    was_interested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    phone = relationship("Phone", back_populates="fsm_state")


class TestRun(Base):
    __tablename__ = "test_run"

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    total_passed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    total_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    test_cases = relationship("TestCase", back_populates="test_run")


class TestCase(Base):
    __tablename__ = "test_case"

    case_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("test_run.run_id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    steps_verified: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    total_steps: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    test_run = relationship("TestRun", back_populates="test_cases")


class TwilioMessage(Base):
    __tablename__ = "twilio_message"

    twilio_sid: Mapped[str] = mapped_column(String(64), primary_key=True)
    phone_number: Mapped[str] = mapped_column(
        String(15), ForeignKey("phone.phone_number"), nullable=False
    )
    direction: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    phone = relationship("Phone", back_populates="twilio_messages")
    messages = relationship("Message", back_populates="twilio_message")
    usage_records = relationship("Usage", back_populates="twilio_message")


class Message(Base):
    __tablename__ = "message"

    message_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone_number: Mapped[str] = mapped_column(
        String(15), ForeignKey("phone.phone_number"), nullable=False
    )
    twilio_sid: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("twilio_message.twilio_sid"), nullable=True
    )
    direction: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    message_data = mapped_column(JSONB, nullable=True)

    phone = relationship("Phone", back_populates="messages")
    twilio_message = relationship("TwilioMessage", back_populates="messages")


class Usage(Base):
    __tablename__ = "usage"

    usage_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    twilio_sid: Mapped[str] = mapped_column(
        String(64), ForeignKey("twilio_message.twilio_sid"), nullable=False
    )
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    tokens_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    twilio_message = relationship("TwilioMessage", back_populates="usage_records")


class ReachOutRun(Base):
    __tablename__ = "reach_out_run"

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    requested: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    processed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sent: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    throttled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    errors: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    context: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
