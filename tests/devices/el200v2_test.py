import unittest

from bluetti_bt_lib.base_devices import BaseDeviceV2
from bluetti_bt_lib.utils.device_builder import build_device
from bluetti_bt_lib.utils.device_info import get_type_by_bt_name
from bluetti_bt_lib.devices.el100v2 import EL100V2
from bluetti_bt_lib.devices.el200v2 import EL200V2


def _field_signature(device):
    return [(f.name, f.address) for f in device.fields]


class TestEL200V2(unittest.TestCase):
    def setUp(self):
        self.device = EL200V2()

    def test_it_is_a_clone_of_the_el100v2_profile(self):
        # The EL200V2 shares the Elite V2 register map, so the profiles are
        # expected to stay identical apart from the class name.
        self.assertEqual(_field_signature(self.device), _field_signature(EL100V2()))

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
