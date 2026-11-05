import struct
import unittest

from bluetti_bt_lib.fields import FieldName, PulseSwitchField


class TestPulseSwitchField(unittest.TestCase):
    def setUp(self):
        # Shaped like the Apex 300's standby: asleep is reported by bit 3 of the
        # word at 174, and the write that moves it is 2 to 2013.
        self.field = PulseSwitchField(
            FieldName.CTRL_SLEEP_MODE,
            174,
            pulse_address=2013,
            pulse_value=2,
            on_bit=8,
        )

    def test_parse_reads_the_on_bit(self):
        for word, expected in (
            (0x0000, False),
            (0x0008, True),
            (0x2008, True),  # another flag set beside the standby bit
            (0x0002, False),  # a flag that is not the standby bit
            (0x2000, False),
        ):
            with self.subTest(word=hex(word)):
                self.assertIs(self.field.parse(struct.pack("!H", word)), expected)

    def test_parse_stays_usable_for_words_it_has_never_seen(self):
        # A BoolField gives up on any word that is not 0 or 1. This one has to
        # keep answering as the station sets other bits in the same word.
        self.assertIsNotNone(self.field.parse(struct.pack("!H", 0x8008)))
        self.assertIsNotNone(self.field.parse(struct.pack("!H", 0xFFFF)))

    def test_the_write_goes_to_a_different_register_than_the_read(self):
        self.assertEqual((self.field.address, self.field.size), (174, 1))
        self.assertEqual(self.field.write_address, 2013)

    def test_is_writeable(self):
        self.assertTrue(self.field.is_writeable())

    def test_only_a_direction_can_be_asked_for(self):
        self.assertTrue(self.field.allowed_write_type(True))
        self.assertTrue(self.field.allowed_write_type(False))
        for value in (1, 2, "on", None):
            with self.subTest(value=value):
                self.assertFalse(self.field.allowed_write_type(value))


if __name__ == "__main__":
    unittest.main()
