import unittest

from bluetti_bt_lib.devices import DEVICE_NAME_RE, DEVICES, DEVICE_ALIASES
from bluetti_bt_lib.devices.charger2 import CHARGER2
from bluetti_bt_lib.devices.handsfree1 import Handsfree1
from bluetti_bt_lib.devices.handsfree2 import Handsfree2
from bluetti_bt_lib.utils.device_builder import build_device
from bluetti_bt_lib.utils.device_info import get_type_by_bt_name


class TestEveryAcceptedNameBuilds(unittest.TestCase):
    def test_charger2_builds_from_both_spellings(self):
        # The recognizer accepts "CHARGER 2" from the type register; building
        # it must not then return None.
        for name in ("CHARGER2", "CHARGER 2"):
            with self.subTest(name=name):
                self.assertIsNotNone(get_type_by_bt_name(name + "12345678"))
                self.assertIsInstance(build_device(name + "12345678"), CHARGER2)

    def test_handsfree_builds_from_the_normalised_name(self):
        # The HA config flow keeps only capitals and digits, like E200V2.
        for name, cls in (("Handsfree 1", Handsfree1), ("H1", Handsfree1),
                          ("Handsfree 2", Handsfree2), ("H2", Handsfree2)):
            with self.subTest(name=name):
                self.assertIsInstance(build_device(name + "2539111584127"), cls)

    def test_every_alias_points_at_a_registered_device(self):
        for alias, target in DEVICE_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIn(target, DEVICES)
                self.assertIsNotNone(DEVICE_NAME_RE.match(alias + "12345678"))


if __name__ == "__main__":
    unittest.main()
