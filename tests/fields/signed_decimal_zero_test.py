import unittest
from decimal import Decimal

from bluetti_bt_lib.fields import FieldName, SignedDecimalField


class TestSignedDecimalZero(unittest.TestCase):
    def test_inverted_zero_is_plain_zero(self):
        field = SignedDecimalField(FieldName.B_IO_POWER, 15546, 0, multiplier=-1)
        value = field.parse(b"\x00\x00")
        self.assertEqual(str(value), "0")
        self.assertFalse(value.is_signed())

    def test_inverted_values_keep_their_sign(self):
        field = SignedDecimalField(FieldName.B_IO_POWER, 15546, 1, multiplier=-1)
        self.assertEqual(field.parse((25).to_bytes(2, "big")), Decimal("-2.5"))
        self.assertEqual(field.parse((-25).to_bytes(2, "big", signed=True)), Decimal("2.5"))


if __name__ == "__main__":
    unittest.main()
