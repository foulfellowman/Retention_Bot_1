import json
import os
import psycopg2

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


def insert_message(db_connection, phone, user_input):

    cur = db_connection.conn.cursor()

    cur.execute(
        "INSERT INTO message (phone_number, message_data, direction, body) VALUES (%s, %s, %s, %s)",
        (phone, json.dumps({"role": "user", "content": user_input}), 'inbound', user_input)
    )
    db_connection.conn.commit()
