import unittest

from bluetti_bt_lib.base_devices.bluetti_device import (
    MAX_POLL_BLOCK,
    MAX_POLL_GAP,
    _polling_blocks,
)
from bluetti_bt_lib.devices import DEVICES

# Profiles that merge harder than the module default, with the block layout the
# wider limits produce. A profile belongs here once a probe run has shown that
# station answers the wider reads; the comment on each profile says how firm
# the evidence is. Pinning the layout here is what keeps a later change to the
# defaults from quietly rewriting a device's poll order.
WIDENED = {
    "AC300": [(36, 8), (70, 11), (86, 11), (3001, 16)],
    "AC500": [(36, 8), (70, 11), (86, 11), (3001, 16), (3061, 1)],
    "EP500": [(36, 8), (70, 11), (86, 3), (3001, 16), (3061, 1)],
    "EP500P": [(36, 8), (70, 11), (86, 3), (3001, 16), (3061, 1)],
    "AP300": [(102, 3), (140, 7), (1314, 2), (1431, 1), (2005, 16), (2021, 3), (2067, 1)],
    "EL100V2": [(102, 3), (140, 7), (1314, 2), (1431, 1), (2005, 16), (2021, 3), (2067, 1)],
}

# The same profiles with the pack-count register left out, which is what a
# cycle that is not walking the pack slots asks for. Register 96 sits inside
# the 86+11 block, so dropping it shortens that block rather than removing a
# request.
PACKLESS = {
    "AC300": [(36, 8), (70, 11), (86, 3), (3001, 16)],
    "AC500": [(36, 8), (70, 11), (86, 3), (3001, 16), (3061, 1)],
}


def _layout(registers):
    return [(r.starting_address, r.quantity) for r in registers]


class TestPollBlockLayout(unittest.TestCase):
    def test_widened_profiles_produce_the_pinned_layout(self):
        for name, expected in WIDENED.items():
            with self.subTest(profile=name):
                device = DEVICES[name]()
                self.assertLess(MAX_POLL_BLOCK, device.max_poll_block)
                self.assertLess(MAX_POLL_GAP, device.max_poll_gap)
                self.assertEqual(_layout(device.get_polling_registers()), expected)

    def test_widening_covers_every_polling_field(self):
        for name in WIDENED:
            with self.subTest(profile=name):
                device = DEVICES[name]()
                covered = set()
                for block in device.get_polling_registers():
                    covered.update(
                        range(block.starting_address, block.starting_address + block.quantity)
                    )
                for field in device.polling_fields:
                    for address in range(field.address, field.address + field.size):
                        self.assertIn(address, covered)

    def test_pack_count_leaves_a_widened_block(self):
        for name, expected in PACKLESS.items():
            with self.subTest(profile=name):
                device = DEVICES[name]()
                self.assertEqual(_layout(device.get_packless_polling_registers()), expected)

    def test_startup_blocks_stay_on_the_default_limits(self):
        # The startup reads are asked for once per connection, so a wider block
        # saves no request there and would put device identity behind a request
        # size that has not been proven on that station.
        for name in WIDENED:
            with self.subTest(profile=name):
                device = DEVICES[name]()
                self.assertEqual(
                    _layout(device.get_startup_registers()),
                    _layout(_polling_blocks(device.startup_fields, MAX_POLL_BLOCK, MAX_POLL_GAP)),
                )

    def test_every_other_profile_keeps_the_default_limits(self):
        for name, device_class in sorted(DEVICES.items()):
            if name in WIDENED:
                continue
            with self.subTest(profile=name):
                device = device_class()
                self.assertEqual(device.max_poll_block, MAX_POLL_BLOCK)
                self.assertEqual(device.max_poll_gap, MAX_POLL_GAP)


if __name__ == "__main__":
    unittest.main()
