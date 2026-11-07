import struct
import unittest

from bluetti_bt_lib.fields import FieldName, SharedRegisterControlField

# Shaped like Charger 2 register 15600 without being it: the station answers
# whole state words, and each word stands for a pair of controls.
STATES = {
    0x1234: (False, False),
    0x2345: (True, False),
    0x3456: (True, True),
    0x4567: (False, True),
}


class TestSharedRegisterControlField(unittest.TestCase):
    def setUp(self):
        self.first = SharedRegisterControlField(
            FieldName.CTRL_AC, 200, STATES, state_index=0
        )
        self.second = SharedRegisterControlField(
            FieldName.CTRL_DC, 200, STATES, state_index=1
        )

    def test_each_field_reads_its_own_element_of_the_state(self):
        for word, (first, second) in STATES.items():
            with self.subTest(word=hex(word)):
                data = struct.pack("!H", word)
                self.assertIs(self.first.parse(data), first)
                self.assertIs(self.second.parse(data), second)

    def test_a_word_outside_the_table_is_refused(self):
        # Guessing here would report a state the station never had, and a
        # refused read only costs this field: the device skips it and says why.
        with self.assertRaises(ValueError) as ctx:
            self.first.parse(struct.pack("!H", 0x0000))

        self.assertIn("Register 200", str(ctx.exception))
        self.assertIn("0x0000", str(ctx.exception))

    def test_it_is_one_register_wide_at_the_shared_address(self):
        # The read-back the writer does for any field reads address/size, and
        # that is the same register both controls live in.
        self.assertEqual((self.first.address, self.first.size), (200, 1))
        self.assertEqual(self.second.address, self.first.address)

    def test_it_can_be_written_but_only_with_a_direction(self):
        self.assertTrue(self.first.is_writeable())
        self.assertTrue(self.first.allowed_write_type(True))
        self.assertTrue(self.first.allowed_write_type(False))
        for value in (1, 0, "on", None):
            with self.subTest(value=value):
                self.assertFalse(self.first.allowed_write_type(value))

    def test_a_write_of_the_field_alone_is_not_a_valid_word(self):
        # What the writer has to work around: a SwitchField write is a plain
        # 1 or 0, and neither is a word this station reports.
        self.assertNotIn(1, STATES)
        self.assertNotIn(0, STATES)

    def test_it_can_carry_its_own_verification_delay(self):
        field = SharedRegisterControlField(
            FieldName.CTRL_AC, 200, STATES, state_index=0, verification_delay=5
        )
        self.assertEqual(field.verification_delay, 5)


if __name__ == "__main__":
    unittest.main()
