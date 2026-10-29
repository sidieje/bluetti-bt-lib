import unittest
from decimal import Decimal

from bluetti_bt_lib.devices import AC60, AC60P, AC180T, EP600, EP760


class TestV2DecimalScales(unittest.TestCase):
    def test_ac_input_voltage_decodes_like_the_rest_of_the_v2_family(self):
        # 1314 is 0.1 V on AC180T, AC70, the Elite family, PR30V2, ...
        raw = (2301).to_bytes(2, "big")
        reference = AC180T().parse(1314, raw)["ac_input_voltage"]
        self.assertEqual(reference, Decimal("230.1"))
        for cls in (AC60, AC60P):
            with self.subTest(device=cls.__name__):
                self.assertEqual(cls().parse(1314, raw)["ac_input_voltage"], reference)

    def test_sm_current_scales_like_every_other_current(self):
        # The neighbouring voltages and the grid/AC currents are all 0.1 units.
        # On the EP760, 1228-1230 is most likely PV string 3, not a smart meter
        # (hassio-bluetti-bt#250); the SM_P1 names stay to keep entity IDs.
        for cls, addresses in ((EP600, (1230, 1238, 1246)), (EP760, (1230,))):
            for address in addresses:
                with self.subTest(device=cls.__name__, address=address):
                    parsed = cls().parse(address, b"\x00\x34")
                    self.assertEqual(list(parsed.values()), [Decimal("5.2")])


if __name__ == "__main__":
    unittest.main()
