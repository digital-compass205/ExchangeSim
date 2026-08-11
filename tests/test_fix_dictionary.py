"""Field-level validation: data types, enumerations and length limits.

The Japannext specification constrains field lengths and decimal places
precisely (Price 8 whole digits and 1 decimal, AvgPx 8 and 4, quantities 9
digits, ClOrdID 32 characters), so these checks carry real venue meaning.
"""

import unittest

from exchangesim.fix import constants as C
from exchangesim.fix.dictionary import Dictionary, FieldDef, MessageDef
from exchangesim.fix.message import Message

T = C.FieldType
R = C.SessionRejectReason


def check(field, value):
    """Return the reject reason for a value, or None when acceptable."""
    failure = field.validate(value)
    return failure.reason if failure else None


class IntFieldTest(unittest.TestCase):

    def setUp(self):
        self.field = FieldDef(34, "MsgSeqNum", T.INT)

    def test_accepts_digits(self):
        self.assertIsNone(check(self.field, "42"))

    def test_accepts_signed(self):
        self.assertIsNone(check(self.field, "-1"))

    def test_rejects_text(self):
        self.assertEqual(R.INCORRECT_DATA_FORMAT, check(self.field, "abc"))

    def test_rejects_decimal(self):
        self.assertEqual(R.INCORRECT_DATA_FORMAT, check(self.field, "1.5"))

    def test_rejects_empty(self):
        self.assertEqual(R.TAG_WITHOUT_VALUE, check(self.field, ""))


class CharFieldTest(unittest.TestCase):

    def setUp(self):
        self.field = FieldDef(54, "Side", T.CHAR, values=("1", "2", "5", "6"))

    def test_accepts_a_listed_value(self):
        self.assertIsNone(check(self.field, "5"))

    def test_rejects_an_unlisted_value(self):
        self.assertEqual(R.VALUE_INCORRECT, check(self.field, "3"))

    def test_rejects_more_than_one_character(self):
        self.assertEqual(R.INCORRECT_DATA_FORMAT, check(self.field, "12"))


class BooleanFieldTest(unittest.TestCase):

    def setUp(self):
        self.field = FieldDef(43, "PossDupFlag", T.BOOLEAN)

    def test_accepts_y_and_n(self):
        self.assertIsNone(check(self.field, "Y"))
        self.assertIsNone(check(self.field, "N"))

    def test_rejects_anything_else(self):
        self.assertEqual(R.INCORRECT_DATA_FORMAT, check(self.field, "true"))
        self.assertEqual(R.INCORRECT_DATA_FORMAT, check(self.field, "y"))


class PriceFieldTest(unittest.TestCase):
    """Price(44): 8 whole number digits, 1 decimal place."""

    def setUp(self):
        self.field = FieldDef(44, "Price", T.PRICE, max_digits=8, max_decimals=1)

    def test_accepts_a_one_decimal_price(self):
        self.assertIsNone(check(self.field, "2845.5"))

    def test_accepts_a_whole_price(self):
        self.assertIsNone(check(self.field, "2845"))

    def test_accepts_the_maximum_width(self):
        self.assertIsNone(check(self.field, "99999999.9"))

    def test_rejects_two_decimal_places(self):
        self.assertEqual(R.VALUE_INCORRECT, check(self.field, "2845.55"))

    def test_rejects_too_many_whole_digits(self):
        self.assertEqual(R.VALUE_INCORRECT, check(self.field, "999999999.0"))

    def test_leading_zeros_do_not_count_towards_the_digit_limit(self):
        self.assertIsNone(check(self.field, "000002845.5"))

    def test_rejects_non_numeric(self):
        self.assertEqual(R.INCORRECT_DATA_FORMAT, check(self.field, "12x.5"))

    def test_rejects_two_decimal_points(self):
        self.assertEqual(R.INCORRECT_DATA_FORMAT, check(self.field, "1.2.3"))


class QtyFieldTest(unittest.TestCase):
    """OrderQty(38): 9 whole number digits."""

    def setUp(self):
        self.field = FieldDef(38, "OrderQty", T.QTY, max_digits=9)

    def test_accepts_a_normal_quantity(self):
        self.assertIsNone(check(self.field, "100"))

    def test_accepts_nine_digits(self):
        self.assertIsNone(check(self.field, "999999999"))

    def test_rejects_ten_digits(self):
        self.assertEqual(R.VALUE_INCORRECT, check(self.field, "1000000000"))


class AvgPxFieldTest(unittest.TestCase):
    """AvgPx(6): 8 whole number digits, 4 decimal places."""

    def setUp(self):
        self.field = FieldDef(6, "AvgPx", T.PRICE, max_digits=8, max_decimals=4)

    def test_accepts_four_decimals(self):
        self.assertIsNone(check(self.field, "2845.5556"))

    def test_rejects_five_decimals(self):
        self.assertEqual(R.VALUE_INCORRECT, check(self.field, "2845.55556"))


class TimestampFieldTest(unittest.TestCase):

    def setUp(self):
        self.field = FieldDef(52, "SendingTime", T.UTC_TIMESTAMP)

    def test_accepts_millisecond_form(self):
        self.assertIsNone(check(self.field, "20260808-01:02:03.456"))

    def test_accepts_second_form(self):
        self.assertIsNone(check(self.field, "20260808-01:02:03"))

    def test_rejects_a_missing_separator(self):
        self.assertEqual(R.INCORRECT_DATA_FORMAT,
                         check(self.field, "20260808 01:02:03"))

    def test_rejects_a_non_numeric_date(self):
        self.assertEqual(R.INCORRECT_DATA_FORMAT,
                         check(self.field, "2026AUG8-01:02:03"))

    def test_rejects_single_digit_time_components(self):
        self.assertEqual(R.INCORRECT_DATA_FORMAT,
                         check(self.field, "20260808-1:02:03"))

    def test_rejects_microsecond_precision(self):
        self.assertEqual(R.INCORRECT_DATA_FORMAT,
                         check(self.field, "20260808-01:02:03.456789"))


class StringLengthTest(unittest.TestCase):

    def test_within_the_limit_is_accepted(self):
        field = FieldDef(11, "ClOrdID", T.STRING, max_length=32)
        self.assertIsNone(check(field, "X" * 32))

    def test_over_the_limit_is_rejected(self):
        field = FieldDef(11, "ClOrdID", T.STRING, max_length=32)
        self.assertEqual(R.VALUE_INCORRECT, check(field, "X" * 33))

    def test_symbol_limit_matches_the_specification(self):
        field = FieldDef(55, "Symbol", T.STRING, max_length=9)
        self.assertIsNone(check(field, "7203"))
        self.assertEqual(R.VALUE_INCORRECT, check(field, "1234567890"))


class MultiValueTest(unittest.TestCase):
    """ExecInst(18) is a space-delimited MultipleValueString."""

    def setUp(self):
        self.field = FieldDef(18, "ExecInst", T.MULTI_VALUE_STRING,
                              values=("6", "x"))

    def test_accepts_a_single_value(self):
        self.assertIsNone(check(self.field, "6"))

    def test_accepts_several_space_delimited_values(self):
        self.assertIsNone(check(self.field, "6 x"))

    def test_rejects_when_any_value_is_unsupported(self):
        self.assertEqual(R.VALUE_INCORRECT, check(self.field, "6 Z"))


class MessageValidationTest(unittest.TestCase):

    def setUp(self):
        self.dictionary = Dictionary(
            fields=[
                FieldDef(C.MSG_TYPE, "MsgType", T.STRING),
                FieldDef(C.MSG_SEQ_NUM, "MsgSeqNum", T.INT),
                FieldDef(11, "ClOrdID", T.STRING, max_length=32),
                FieldDef(54, "Side", T.CHAR, values=("1", "2")),
                FieldDef(38, "OrderQty", T.QTY, max_digits=9),
            ],
            messages=[MessageDef("D", "NewOrderSingle",
                                 required=(11, 54), optional=(38,))],
            header=(C.MSG_TYPE, C.MSG_SEQ_NUM),
        )

    def _message(self, **tags):
        message = Message.create("D")
        for tag, value in tags.items():
            message.set(int(tag[1:]), value)
        return message

    def test_a_complete_message_validates(self):
        self.assertIsNone(self.dictionary.validate(
            self._message(t11="ORD-1", t54="1", t38="100")))

    def test_optional_fields_may_be_omitted(self):
        self.assertIsNone(self.dictionary.validate(
            self._message(t11="ORD-1", t54="1")))

    def test_a_missing_required_field_names_its_tag(self):
        failure = self.dictionary.validate(self._message(t11="ORD-1"))
        self.assertEqual(R.REQUIRED_TAG_MISSING, failure.reason)
        self.assertEqual(54, failure.tag)

    def test_an_unsupported_message_type_is_reported(self):
        failure = self.dictionary.validate(Message.create("ZZ"))
        self.assertEqual(R.INVALID_MSGTYPE, failure.reason)

    def test_an_undefined_tag_is_reported(self):
        message = self._message(t11="ORD-1", t54="1")
        message.set(9999, "x")
        failure = self.dictionary.validate(message)
        self.assertEqual(R.INVALID_TAG_NUMBER, failure.reason)
        self.assertEqual(9999, failure.tag)

    def test_a_tag_defined_but_not_for_this_message_is_reported(self):
        self.dictionary.add_field(FieldDef(44, "Price", T.PRICE))
        message = self._message(t11="ORD-1", t54="1")
        message.set(44, "100")

        failure = self.dictionary.validate(message)

        self.assertEqual(R.TAG_NOT_DEFINED_FOR_MESSAGE, failure.reason)
        self.assertEqual(44, failure.tag)

    def test_unknown_tag_checking_can_be_relaxed(self):
        message = self._message(t11="ORD-1", t54="1")
        message.set(9999, "x")
        self.assertIsNone(self.dictionary.validate(message, check_unknown=False))

    def test_header_tags_are_allowed_in_any_message(self):
        message = self._message(t11="ORD-1", t54="1")
        message.set(C.MSG_SEQ_NUM, "5")
        self.assertIsNone(self.dictionary.validate(message))

    def test_field_errors_are_reported_before_missing_required_fields(self):
        # Side is present but invalid, and OrderQty is absent but optional;
        # the value error is the one that matters.
        message = self._message(t11="ORD-1", t54="9")
        failure = self.dictionary.validate(message)
        self.assertEqual(R.VALUE_INCORRECT, failure.reason)


if __name__ == "__main__":
    unittest.main()
