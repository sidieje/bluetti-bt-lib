import struct
import unittest

from bluetti_bt_lib.fields import BoolField, BoolFieldNonZero, FieldName


class TestBoolFieldNonZero(unittest.TestCase):
    def setUp(self):
        self.field = BoolFieldNonZero(FieldName.AC_OUTPUT_ON, 2011)

    def parse(self, value: int):
        return self.field.parse(struct.pack("!H", value))

    def test_parse_zero_returns_false(self):
        self.assertIs(self.parse(0), False)

    def test_parse_one_returns_true(self):
        self.assertIs(self.parse(1), True)

    def test_parse_three_returns_false(self):
        """AC2P returns value 3 for OFF state (not 0)."""
        self.assertIs(self.parse(3), False)

    def test_parse_other_values_return_false(self):
        """Only value 1 should return True."""
        for value in [2, 5, 100, 255, 65535]:
            with self.subTest(value=value):
                self.assertIs(self.parse(value), False)

    def test_reports_off_where_bool_field_would_report_nothing(self):
        # The reason this class exists: a plain BoolField has no answer for 3
        # and returns None, which leaves the register out of the poll result.
        # The AC2P does report 3, and it means OFF, so it has to be reported.
        plain = BoolField(FieldName.AC_OUTPUT_ON, 2011)
        self.assertIsNone(plain.parse(struct.pack("!H", 3)))
        self.assertIs(self.parse(3), False)

    def test_address(self):
        self.assertEqual(self.field.address, 2011)

    def test_size(self):
        self.assertEqual(self.field.size, 1)

    def test_name(self):
        self.assertEqual(self.field.name, FieldName.AC_OUTPUT_ON.value)

    def test_is_writeable(self):
        self.assertFalse(self.field.is_writeable())


if __name__ == "__main__":
    unittest.main()
