import json
import os
import psycopg2
from psycopg2 import sql
from typing import List, Dict, Optional
from dotenv import load_dotenv
from datetime import datetime

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

    def fetch_conversations(self, q: Optional[str] = None, sort: Optional[str] = None) -> List[Dict]:
        """
        Return one row per phone_number (latest message), optionally filtered by query `q`.
        Supports optional client-side sorting via `sort` in {"name", "number", "status"}.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (m.phone_number)
                       m.phone_number,
                       COALESCE((m.message_data->>'role'), m.direction) AS last_role,
                       COALESCE((m.message_data->>'content'), m.body) AS last_text,
                       MAX(m.sent_at) OVER (PARTITION BY m.phone_number) AS last_at,
                       fs.statename AS fsm_state,
                       ct.first_name,
                       ct.last_name
                FROM public.message AS m
                LEFT JOIN public.fsm_state AS fs
                  ON fs."phone_number" = m.phone_number
                LEFT JOIN public.contact AS ct
                  ON ct.phone_number = m.phone_number
                WHERE (%s IS NULL
                       OR m.phone_number ILIKE '%%' || %s || '%%'
                       OR COALESCE((m.message_data->>'content'), '') ILIKE '%%' || %s || '%%')
                ORDER BY m.phone_number, last_at DESC;
                """,
                (q, q, q),
            )
            rows = cur.fetchall()

        items: List[Dict] = []

        for phone, last_role, last_text, last_at, fsm_state, first_name, last_name in rows:
            name_parts = []
            if first_name and first_name.strip():
                name_parts.append(first_name.strip())
            if last_name and last_name.strip():
                name_parts.append(last_name.strip())
            display_name = " ".join(name_parts)

            items.append({
                "phone_number": phone,
                "display_name": display_name,
                "status": (str(fsm_state).upper() if fsm_state else "None"),
                "last_message_at": last_at,
                "last_snippet": (last_text or "")[:80],
                "last_role": last_role,
            })

        sort_key = (sort or "").lower()
        if sort_key == "name":
            items.sort(key=lambda item: (item["display_name"] or "").lower())
        elif sort_key == "number":
            items.sort(key=lambda item: item["phone_number"])
        elif sort_key == "status":
            items.sort(key=lambda item: item["status"] or "")

        return items

    def _lookup_contact_names(self, phones: List[str]) -> Dict[str, str]:
        if not phones:
            print('no phones')
            return {}

        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT phone_number, first_name, last_name
                    FROM public.contact
                    WHERE phone_number = ANY(%s)
                    """,
                    (phones,),
                )
                rows = cur.fetchall()
        except psycopg2.Error:
            self.conn.rollback()
            return {}

        name_map: Dict[str, str] = {}
        for phone, first, last in rows:
            parts = []
            if first:
                parts.append(first.strip())
            if last:
                parts.append(last.strip())
            if parts:
                name_map[phone] = " ".join(parts)

        return name_map


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

# ----------------------------
# Test run summary tables/helpers
# ----------------------------


def ensure_test_run_tables(db_connection: "DB") -> None:
    """Create test run summary tables if they do not exist.
    - public.test_run: parent table for a test execution
    - public.test_case: child rows for each scenario result
    """
    cur = db_connection.conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.test_run (
            run_id SERIAL PRIMARY KEY,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ NULL,
            total_passed INT NOT NULL DEFAULT 0,
            total_failed INT NOT NULL DEFAULT 0
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.test_case (
            case_id SERIAL PRIMARY KEY,
            run_id INT NOT NULL REFERENCES public.test_run(run_id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            result TEXT NOT NULL,
            steps_verified INT NOT NULL DEFAULT 0,
            total_steps INT NOT NULL DEFAULT 0,
            duration_seconds DOUBLE PRECISION NULL,
            finished_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    db_connection.conn.commit()


def insert_test_run(db_connection: "DB",
                    started_at: Optional[datetime] = None,
                    total_passed: int = 0,
                    total_failed: int = 0) -> int:
    """Insert a new test_run row and return its id."""
    cur = db_connection.conn.cursor()
    if started_at is None:
        started_at = datetime.utcnow()
    cur.execute(
        """
        INSERT INTO public.test_run (started_at, total_passed, total_failed)
        VALUES (%s, %s, %s)
        RETURNING run_id;
        """,
        (started_at, total_passed, total_failed)
    )
    run_id = cur.fetchone()[0]
    db_connection.conn.commit()
    return run_id


def update_test_run(db_connection: "DB", run_id: int,
                    finished_at: Optional[datetime] = None,
                    total_passed: Optional[int] = None,
                    total_failed: Optional[int] = None) -> None:
    """Update totals and/or finished_at for a test_run."""
    sets = []
    params: list = []
    if finished_at is None:
        finished_at = datetime.utcnow()
    sets.append("finished_at = %s")
    params.append(finished_at)
    if total_passed is not None:
        sets.append("total_passed = %s")
        params.append(total_passed)
    if total_failed is not None:
        sets.append("total_failed = %s")
        params.append(total_failed)
    params.append(run_id)
    sql = f"UPDATE public.test_run SET {', '.join(sets)} WHERE run_id = %s"
    cur = db_connection.conn.cursor()
    cur.execute(sql, tuple(params))
    db_connection.conn.commit()


def insert_test_case(db_connection: "DB", run_id: int, name: str, result: str,
                     steps_verified: int, total_steps: int,
                     duration_seconds: Optional[float] = None,
                     finished_at: Optional[datetime] = None) -> int:
    """Insert a child test_case row and return its id."""
    cur = db_connection.conn.cursor()
    if finished_at is None:
        finished_at = datetime.utcnow()
    cur.execute(
        """
        INSERT INTO public.test_case
            (run_id, name, result, steps_verified, total_steps, duration_seconds, finished_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING case_id;
        """,
        (run_id, name, result, steps_verified, total_steps, duration_seconds, finished_at)
    )
    case_id = cur.fetchone()[0]
    db_connection.conn.commit()
    return case_id
