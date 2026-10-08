import unittest

from bluetti_bt_lib.devices.charger2 import CHARGER2, Charger2SystemSwitchField
from bluetti_bt_lib.fields import FieldName


class TestCharger2(unittest.TestCase):
    def setUp(self):
        self.field = Charger2SystemSwitchField(
            FieldName.CTRL_SYSTEM_ON_OFF,
            15600,
        )
        self.device = CHARGER2()

    def test_system_switch_parse_on(self):
        self.assertTrue(self.field.parse(bytes.fromhex("5529")))

    def test_system_switch_parse_off(self):
        self.assertFalse(self.field.parse(bytes.fromhex("AA2A")))

    def test_system_switch_parse_invalid(self):
        with self.assertRaises(ValueError):
            self.field.parse(bytes.fromhex("1234"))

    def test_system_switch_write_command(self):
        on_command = self.device.build_write_command(
            FieldName.CTRL_SYSTEM_ON_OFF.value,
            True,
        )
        off_command = self.device.build_write_command(
            FieldName.CTRL_SYSTEM_ON_OFF.value,
            False,
        )

        self.assertEqual(on_command.address, 15600)
        self.assertEqual(on_command.value, 0x5529)

        self.assertEqual(off_command.address, 15600)
        self.assertEqual(off_command.value, 0xAA2A)


if __name__ == "__main__":
    unittest.main()
