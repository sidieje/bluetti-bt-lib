import struct
import unittest

from bluetti_bt_lib.base_devices import BaseDeviceV2
from bluetti_bt_lib.utils.device_builder import build_device
from bluetti_bt_lib.utils.device_info import get_type_by_bt_name
from bluetti_bt_lib.devices.el100v2 import EL100V2
from bluetti_bt_lib.devices.el200v2 import EL200V2


def _field_signature(device):
    return [(f.name, f.address) for f in device.fields]


# The standby switch is this profile's own addition to the Elite V2 map: the
# control was traced here first, but the state word was only ever measured on an
# Apex 300, so the other portables keep the map without it.
STANDBY_SWITCH = ("ctrl_sleep_mode", 174)


class TestEL200V2(unittest.TestCase):
    def setUp(self):
        self.device = EL200V2()

    def test_it_is_a_clone_of_the_el100v2_profile_apart_from_the_switch(self):
        # The EL200V2 shares the Elite V2 register map, so the profiles are
        # expected to stay identical apart from the class name and the standby
        # switch.
        mine = [f for f in _field_signature(self.device) if f != STANDBY_SWITCH]
        self.assertEqual(mine, _field_signature(EL100V2()))

    def test_the_standby_switch_reads_174_and_pulses_2_to_2013(self):
        # An owner of this model watched the official integration's sleep switch
        # write 4 to 2013 to enter sleep and 3 to leave it, but the pulse that
        # was watched moving the state on real hardware is the 2 an Apex 300
        # takes, and it moves the station whichever way it is asked. The state
        # comes back from 174: 0 awake, 8 in standby.
        field = next(f for f in self.device.fields if f.name == "ctrl_sleep_mode")
        self.assertEqual((field.address, field.write_address), (174, 2013))
        self.assertIs(field.parse(struct.pack("!H", 0)), False)
        self.assertIs(field.parse(struct.pack("!H", 8)), True)

        for state in (True, False):
            with self.subTest(state=state):
                command = self.device.build_write_command("ctrl_sleep_mode", state)
                self.assertEqual((command.address, command.value), (2013, 2))

        self.assertIn(
            "ctrl_sleep_mode", [f.name for f in self.device.get_switch_fields()]
        )

    def test_the_switch_state_word_costs_one_read(self):
        # 174 sits 28 above the 140+7 block, past the gap this profile merges,
        # so it adds a request of its own. 2013 is already inside the block the
        # profile asks for at 2005, so the pulse register needs no read.
        blocks = [
            (r.starting_address, r.quantity)
            for r in self.device.get_polling_registers()
        ]
        self.assertIn((174, 1), blocks)
        self.assertTrue(
            any(start <= 2013 < start + quantity for start, quantity in blocks)
        )

    def test_it_builds_from_every_reported_name(self):
        # The unit reports "Elite 200 V2" in register 110 and advertises that
        # over BLE; the HA config flow stores the normalised "E200V2" instead.
        for name in ("Elite 200 V2", "E200V2"):
            with self.subTest(name=name):
                device = build_device(name + "2539111584127")
                self.assertIsInstance(device, EL200V2)

    def test_the_advertised_name_is_recognised(self):
        # DeviceRecognizerResult and the BLE scan both hand this string on.
        self.assertEqual(
            get_type_by_bt_name("Elite 200 V22539111584127"), "Elite 200 V2"
        )
        self.assertEqual(get_type_by_bt_name("E200V22539111584127"), "E200V2")

    def test_the_type_register_fits_the_advertised_name(self):
        # Register 110 is read as 6 registers (12 bytes), which is exactly the
        # length of the name this model reports.
        self.assertEqual(len("Elite 200 V2"), 12)
        self.assertEqual(
            next(f for f in BaseDeviceV2().fields if f.name == "device_type").size,
            6,
        )

    def test_shared_controls_are_writeable(self):
        for name in ("ctrl_ups_mode", "ctrl_grid_charge_on", "ctrl_charging_mode"):
            with self.subTest(field=name):
                field = next(f for f in self.device.fields if f.name == name)
                self.assertTrue(field.is_writeable())


if __name__ == "__main__":
    unittest.main()
