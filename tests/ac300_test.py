import unittest

from bluetti_bt_lib.devices.ac300 import AC300
from bluetti_bt_lib.devices.ac500 import AC500
from bluetti_bt_lib.devices.el100v2 import EL100V2
from bluetti_bt_lib.fields import NumberField


class TestAC300(unittest.TestCase):
    def setUp(self):
        self.device = AC300()

    def test_pack_fields_trimmed_to_voltage_and_soc(self):
        # The per-pack voltage and SOC stay; the read-only selected flag and
        # the 16 cell-voltage fields are dropped.
        self.assertEqual(
            [f.name for f in self.device.pack_fields], ["voltage", "battery_soc"]
        )
        self.assertEqual([f.address for f in self.device.pack_fields], [98, 99])
        self.assertEqual(self.device.max_packs, 4)

    def test_power_generation_reads_register_41(self):
        # Register 41 is the generated-energy counter (kWh, only ever
        # increasing). Register 43 is the SOC: reading it as
        # power_generation made the sensor track stored energy and
        # decrease on discharge, breaking HA's total_increasing state
        # class.
        gen = next(
            f for f in self.device.fields if f.name == "power_generation"
        )
        self.assertEqual(gen.address, 41)

    def test_polling_blocks(self):
        # One request per field put the AC300 at 28 reads plus two per pack
        # slot, which is most of a poll cycle on the radio. Neighbouring
        # main registers are read together instead; the pack reads stay as
        # they were. This profile merges harder than the default limits, which
        # takes the cycle from the seven requests the defaults produce down
        # to four.
        blocks = self.device.get_polling_registers()
        self.assertEqual(
            [(b.starting_address, b.quantity) for b in blocks],
            [(36, 8), (70, 11), (86, 11), (3001, 16)],
        )

        # A block covers a range, so the merge has to leave no field outside
        # one, or a wider block would silently drop it from the cycle.
        covered = set()
        for block in blocks:
            covered.update(
                range(block.starting_address, block.starting_address + block.quantity)
            )
        for field in self.device.polling_fields:
            self.assertTrue(
                all(
                    address in covered
                    for address in range(field.address, field.address + field.size)
                ),
                f"{field.name} is not covered by a polling request",
            )

        # The identity and firmware registers are not part of the cycle; they
        # are read once per connection (see STARTUP_FIELDS), and they keep the
        # request size that has always answered them.
        startup = self.device.get_startup_registers()
        self.assertEqual(len(startup), 2)
        self.assertEqual(startup[0].starting_address, 10)
        self.assertEqual(startup[1].starting_address, 17)

        # A cycle that is not walking the pack slots does not ask for the pack
        # count, which is the only thing holding 89-95 onto the 86 block.
        self.assertEqual(
            [
                (b.starting_address, b.quantity)
                for b in self.device.get_packless_polling_registers()
            ],
            [(36, 8), (70, 11), (86, 3), (3001, 16)],
        )

        pack_blocks = self.device.get_pack_polling_registers()
        self.assertEqual(len(pack_blocks), 2)
        self.assertEqual(pack_blocks[0].starting_address, 98)
        self.assertEqual(pack_blocks[0].quantity, 1)
        self.assertEqual(pack_blocks[1].starting_address, 99)
        self.assertEqual(pack_blocks[1].quantity, 1)

    def test_soc_range_takes_the_default_verification_window(self):
        # The SOC range thresholds used to declare a longer delay because the
        # old write check was one read-back after a fixed pause. The window
        # retries inside itself now, so they need nothing special.
        start = next(
            f for f in self.device.fields if f.name == "soc_range_start"
        )
        end = next(f for f in self.device.fields if f.name == "soc_range_end")
        self.assertIsNone(start.verification_delay)
        self.assertIsNone(end.verification_delay)

    def test_soc_range_is_writable_number(self):
        start = next(
            f for f in self.device.fields if f.name == "soc_range_start"
        )
        end = next(f for f in self.device.fields if f.name == "soc_range_end")
        self.assertIsInstance(start, NumberField)
        self.assertIsInstance(end, NumberField)
        self.assertTrue(start.is_writeable())
        self.assertTrue(end.is_writeable())
        self.assertEqual(start.step, 1)
        self.assertEqual(end.step, 1)

        self.assertEqual(self.device.build_write_command("soc_range_start", 30).value, 30)
        self.assertEqual(self.device.build_write_command("soc_range_end", 90).value, 90)

    def test_soc_range_accepts_off_step_values(self):
        # step is the increment we advertise to clients, not a hardware
        # limit: the device accepts 1%, so off-step values must write.
        self.assertEqual(
            self.device.build_write_command("soc_range_start", 32).value, 32
        )
        self.assertEqual(
            self.device.build_write_command("soc_range_end", 91).value, 91
        )

    def test_soc_range_rejects_out_of_range_and_bool(self):
        self.assertIsNone(self.device.build_write_command("soc_range_start", 150))
        self.assertIsNone(self.device.build_write_command("soc_range_start", -1))
        self.assertIsNone(self.device.build_write_command("soc_range_start", True))

    def test_switches_take_the_default_verification_window(self):
        # The tower is slower than the newer stations to show a control write in
        # its own register, and how much slower varies from write to write. That
        # is what the writer's re-read window is for, so the switches need no
        # delay of their own: a fixed pause could never cover the slow writes.
        switches = self.device.get_switch_fields()
        self.assertEqual(len(switches), 4)
        for field in switches:
            with self.subTest(field=field.name):
                self.assertIsNone(field.verification_delay)

    def test_other_profiles_take_the_default_switch_delay(self):
        # No profile needs a switch delay of its own, slow tower included: the
        # writer's window is what covers a station that lags.
        for device_cls in (EL100V2, AC500):
            switches = device_cls().get_switch_fields()
            self.assertTrue(switches)
            for field in switches:
                with self.subTest(device=device_cls.__name__, field=field.name):
                    self.assertIsNone(field.verification_delay)

    def test_ups_switches_present_and_writable(self):
        names = {f.name for f in self.device.get_switch_fields()}
        self.assertIn("ctrl_grid_charge_on", names)
        self.assertIn("ctrl_time_control_on", names)

        self.assertEqual(self.device.build_write_command("ctrl_grid_charge_on", True).address, 3011)
        self.assertEqual(self.device.build_write_command("ctrl_time_control_on", False).address, 3013)


if __name__ == "__main__":
    unittest.main()
