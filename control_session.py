import json
import os

from dotenv import load_dotenv


def get_session_messages(db, phone, base_prompt, verbose=False):
    db_connection = db
    cur = db_connection.conn.cursor()

    query = f"""
      SELECT m.message_data
      FROM public.message m
      JOIN public.phone p ON p.phone_number = m.phone_number
      WHERE p.phone_number = %s
      ORDER BY m.sent_at, m.message_id ASC
    """

    cur.execute(query, (phone,))
    rows = cur.fetchall()

    user_message_array = [{"role": "system", "content": base_prompt}]

    for row in rows:
        message_json = row[0]
        if isinstance(message_json, str):
            # In case data was stored as JSON string
            message_json = json.loads(message_json)
        user_message_array.append(message_json)

    if verbose:
        print(user_message_array)

    return user_message_array


def get_session_messages_no_base_prompt(db, phone, verbose=False):
    load_dotenv()  # This loads variables from .env into os.environ

    # read env, fallback to 15
    readback_limit = int(os.getenv("readback_limit"))

    db_connection = db
    cur = db_connection.conn.cursor()

    query = f"""
      SELECT m.message_data
      FROM public.message m
      JOIN public.phone p ON p.phone_number = m.phone_number
      WHERE p.phone_number = %s
      ORDER BY m.message_id DESC
      LIMIT %s
    """

    cur.execute(query, (phone, readback_limit))
    rows = cur.fetchall()

    user_message_array = []

    for row in rows[:readback_limit]:
        message_json = row[0]
        if isinstance(message_json, str):
            # In case data was stored as JSON string
            message_json = json.loads(message_json)
        user_message_array.append(message_json)

    if verbose:
        print(user_message_array)

    return user_message_array
