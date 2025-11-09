import unittest

from sqlalchemy import Float, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB

from models import (
    Base,
    Contact,
    FSMState,
    Message,
    Phone,
    TestCase,
    TestRun,
    TwilioMessage,
    Usage,
)


class TestModelsMetadata(unittest.TestCase):
    def test_tables_registered(self):
        expected_tables = {
            "phone",
            "contact",
            "fsm_state",
            "test_run",
            "test_case",
            "twilio_message",
            "message",
            "usage",
            "reach_out_run"
        }
        self.assertEqual(set(Base.metadata.tables.keys()), expected_tables)

    def test_phone_columns(self):
        phone_table = Phone.__table__
        phone_number = phone_table.c.phone_number
        self.assertTrue(phone_number.primary_key)
        self.assertIsInstance(phone_number.type, String)
        self.assertEqual(phone_number.type.length, 15)
        self.assertEqual(len(phone_table.foreign_keys), 0)

    def test_contact_foreign_key(self):
        contact_table = Contact.__table__
        phone_fk = list(contact_table.c.phone_number.foreign_keys)
        self.assertEqual(len(phone_fk), 1)
        self.assertEqual(phone_fk[0].column.table.name, "phone")
        # self.assertEqual(contact_table.columns['customer_id'].type.__class__, Integer)

    def test_message_columns_and_fks(self):
        message_table = Message.__table__
        phone_fk = list(message_table.c.phone_number.foreign_keys)
        twilio_fk = list(message_table.c.twilio_sid.foreign_keys)
        self.assertEqual(phone_fk[0].column.table.name, "phone")
        self.assertEqual(twilio_fk[0].column.table.name, "twilio_message")
        self.assertIsInstance(message_table.c.message_data.type, JSONB)

    def test_numeric_and_float_columns(self):
        usage_table = Usage.__table__
        price_col = usage_table.c.price
        self.assertIsInstance(price_col.type, Numeric)
        self.assertEqual(price_col.type.precision, 6)
        self.assertEqual(price_col.type.scale, 2)

        test_case_table = TestCase.__table__
        duration_col = test_case_table.c.duration_seconds
        self.assertIsInstance(duration_col.type, Float)

    def test_relationship_mappings(self):
        self.assertIs(Phone.messages.property.mapper.class_, Message)
        self.assertFalse(Phone.fsm_state.property.uselist)
        self.assertIs(Message.phone.property.mapper.class_, Phone)
        self.assertIs(TwilioMessage.messages.property.mapper.class_, Message)
        self.assertIs(Usage.twilio_message.property.mapper.class_, TwilioMessage)

    def test_testcase_foreign_key(self):
        test_case_table = TestCase.__table__
        run_fk = list(test_case_table.c.run_id.foreign_keys)
        self.assertEqual(run_fk[0].column.table.name, "test_run")

    def test_testrun_defaults(self):
        test_run_table = TestRun.__table__
        self.assertIsInstance(test_run_table.c.total_passed.type, Integer)
        self.assertEqual(str(test_run_table.c.total_passed.server_default.arg), "0")
        self.assertEqual(str(test_run_table.c.total_failed.server_default.arg), "0")


if __name__ == "__main__":
    unittest.main()