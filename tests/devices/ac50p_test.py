import unittest
from decimal import Decimal

from bluetti_bt_lib.devices import AC50P
from bluetti_bt_lib.utils.device_builder import build_device

# The AC50P keeps its power readings in the shared 140-147 block and reports
# the grid voltage in the 1300s, the same split the V2 portables use.
REGISTERS = {
    "dc_output_power": 140,
    "ac_output_power": 142,
    "dc_input_power": 144,
    "ac_input_power": 146,
    "ac_input_voltage": 1314,
    "ctrl_ac": 2011,
    "ctrl_dc": 2012,
    "ctrl_power_lifting": 2021,
}


class TestAC50P(unittest.TestCase):
    def setUp(self):
        self.device = AC50P()
        self.fields = {f.name: f for f in self.device.fields}

    def test_builds_from_advertisement_name(self):
        self.assertIsInstance(build_device("AC50P12345678"), AC50P)

    def test_registers_are_read(self):
        for name, address in REGISTERS.items():
            with self.subTest(field=name):
                self.assertEqual(self.fields[name].address, address)

    def test_grid_voltage_carries_one_decimal(self):
        self.assertEqual(
            self.fields["ac_input_voltage"].parse(b"\x04\xb2"), Decimal("120.2")
        )

    def test_output_switches_are_writable(self):
        for name in ("ctrl_ac", "ctrl_dc", "ctrl_power_lifting"):
            with self.subTest(field=name):
                self.assertTrue(self.fields[name].is_writeable())
                self.assertIs(self.fields[name].parse(b"\x00\x01"), True)


if __name__ == "__main__":
    unittest.main()
