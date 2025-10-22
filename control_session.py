import json
import os

from dotenv import load_dotenv
from sqlalchemy import select

from models import Message


def _get_session(db):
    return getattr(db, "session", db)


def get_session_messages(db, phone, base_prompt, verbose=False):
    session = _get_session(db)

    stmt = (
        select(Message.message_data)
        .where(Message.phone_number == phone)
        .order_by(Message.sent_at.asc(), Message.message_id.asc())
    )
    rows = session.execute(stmt).scalars().all()

    user_message_array = [{"role": "system", "content": base_prompt}]

    for message_json in rows:
        if isinstance(message_json, str):
            message_json = json.loads(message_json)
        user_message_array.append(message_json)

    if verbose:
        print(user_message_array)

    return user_message_array


def get_session_messages_no_base_prompt(db, phone, verbose=False):
    load_dotenv()  # This loads variables from .env into os.environ

    readback_limit = int(os.getenv("readback_limit", 20))
    session = _get_session(db)

    stmt = (
        select(Message.message_data)
        .where(Message.phone_number == phone)
        .order_by(Message.message_id.desc())
        .limit(readback_limit)
    )
    rows = session.execute(stmt).scalars().all()

    user_message_array = []

    for message_json in rows:
        if isinstance(message_json, str):
            message_json = json.loads(message_json)
        user_message_array.append(message_json)

    if verbose:
        print(user_message_array)

    return user_message_array
