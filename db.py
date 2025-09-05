import json
import os
import psycopg2
from typing import List, Dict, Optional
from dotenv import load_dotenv

load_dotenv()


class DB:
    def __init__(self):
        self.conn = psycopg2.connect(
            host=os.getenv("PG_HOST"),
            port=os.getenv("PG_PORT"),
            dbname=os.getenv("PG_DB"),
            user=os.getenv("PG_USER"),
            password=os.getenv("PG_PASS")
        )

    def quick_query(self):
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT schemaname, tablename
                FROM pg_tables
                WHERE schemaname NOT IN ('pg_catalog', 'information_schema');
            """)
            print(cur.fetchall())

    def close(self):
        self.conn.close()

    def insert_message(self, phone, user_input, twilio_sid=None):
        cur = self.conn.cursor()

        if twilio_sid:
            cur.execute(
                "INSERT INTO message (phone_number, message_data, direction, body, twilio_sid) VALUES (%s, %s, %s, %s)",
                (phone, json.dumps({"role": "user", "content": user_input}), 'inbound', user_input, twilio_sid)
            )
            self.conn.commit()
        elif twilio_sid is None:
            cur.execute(
                "INSERT INTO message (phone_number, message_data, direction, body) VALUES (%s, %s, %s, %s)",
                (phone, json.dumps({"role": "user", "content": user_input}), 'inbound', user_input)
            )
            self.conn.commit()

    def SQL_latest_message_per_phone(self):
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (m.phone_number)
                       m.phone_number,
                       MAX(m.sent_at) OVER (PARTITION BY m.phone_number) AS last_message_at
                FROM public.message AS m
                ORDER BY m.phone_number, last_message_at DESC;
            """)
            rows = cur.fetchall()
            # print(rows)
            return rows

    def SQL_full_conversation_per_phone(self, phone):
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT message_data, body, direction, sent_at
                FROM public.message
                WHERE phone_number = %s
                ORDER BY message_id ASC;
            """,
                        (phone,)
                        )
            out = []
            for message_data, body, direction, sent_at in cur.fetchall():
                role = None
                content = None
                if isinstance(message_data, dict):
                    role = message_data.get('role')
                    content = message_data.get('content')
                if not role:
                    role = 'assistant' if direction == 'outbound' else 'user'
                if not content:
                    content = body
                out.append({'role': role, 'content': content, 'sent_at': sent_at})
            return out

    def fetch_conversations(self, q: Optional[str] = None) -> List[Dict]:
        """
        Return one row per phone_number (latest message), optionally filtered by query `q`.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (m.phone_number)
                       m.phone_number,
                       COALESCE((m.message_data->>'role'), m.direction) AS last_role,
                       COALESCE((m.message_data->>'content'), m.body) AS last_text,
                       MAX(m.sent_at) OVER (PARTITION BY m.phone_number) AS last_at,
                       fs.statename AS fsm_state
                FROM public.message AS m
                LEFT JOIN public.fsm_state AS fs
                  ON fs."phone_number" = m.phone_number
                WHERE (%s IS NULL
                       OR m.phone_number ILIKE '%%' || %s || '%%'
                       OR COALESCE((m.message_data->>'content'), '') ILIKE '%%' || %s || '%%')
                ORDER BY m.phone_number, last_at DESC;
                """,
                (q, q, q),
            )
            rows = cur.fetchall()

        items: List[Dict] = []

        for phone, last_role, last_text, last_at, fsm_state in rows:
            items.append({
                "phone_number": phone,
                "display_name": phone,  # TODO: replace w/ CRM name lookup
                "status": str(fsm_state).upper() or "None",
                "last_message_at": last_at,
                "last_snippet": (last_text or "")[:80],
                "last_role": last_role,
            })
        return items


def insert_message(db_connection, phone, user_input):
    cur = db_connection.conn.cursor()

    cur.execute(
        "INSERT INTO message (phone_number, message_data, direction, body) VALUES (%s, %s, %s, %s)",
        (phone, json.dumps({"role": "user", "content": user_input}), 'inbound', user_input)
    )
    db_connection.conn.commit()


def insert_message_from_gpt(db_connection, phone, gpt_input):
    cur = db_connection.conn.cursor()

    cur.execute(
        "INSERT INTO message (phone_number, message_data, direction, body) VALUES (%s, %s, %s, %s)",
        (phone, json.dumps({"role": "developer", "content": gpt_input}), 'outbound', gpt_input)
    )
    db_connection.conn.commit()
