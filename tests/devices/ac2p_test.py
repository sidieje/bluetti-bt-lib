import unittest

from bluetti_bt_lib.devices.ac2p import AC2P
from bluetti_bt_lib.fields import FieldName

SWITCHES = ((2011, FieldName.CTRL_AC, FieldName.AC_OUTPUT_ON),
            (2012, FieldName.CTRL_DC, FieldName.DC_OUTPUT_ON),
            (2021, FieldName.CTRL_POWER_LIFTING, FieldName.POWER_LIFTING_ON))


class TestAC2P(unittest.TestCase):
    def setUp(self):
        self.device = AC2P()

    def test_switches_read_off_when_the_station_reports_3(self):
        for address, switch, sensor in SWITCHES:
            with self.subTest(address=address):
                parsed = self.device.parse(address, b"\x00\x03")
                self.assertIs(parsed[sensor.value], False)
                self.assertIs(parsed[switch.value], False)
                self.assertIs(self.device.parse(address, b"\x00\x00")[switch.value], False)
                self.assertIs(self.device.parse(address, b"\x00\x01")[switch.value], True)

    def test_switches_skip_other_values_instead_of_reading_them_as_off(self):
        for address, switch, _ in SWITCHES:
            with self.subTest(address=address):
                with self.assertLogs("bluetti_bt_lib.base_devices.bluetti_device", "WARNING") as logs:
                    parsed = self.device.parse(address, b"\x00\x02")
                self.assertNotIn(switch.value, parsed)
                self.assertIn("0x0002", logs.output[0])

    def test_switches_still_write_0_and_1(self):
        for value, raw in ((True, 1), (False, 0)):
            command = self.device.build_write_command(FieldName.CTRL_AC.value, value)
            self.assertEqual((command.address, command.value), (2011, raw))


if __name__ == "__main__":
    unittest.main()
