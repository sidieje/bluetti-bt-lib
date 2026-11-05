import struct
import unittest

from bluetti_bt_lib.devices import AP300, EL100V2, EL100mini, EL200V2, EL300

# The Apex 300 shares the Elite V2 register map, so its profile is a copy of
# the EL100V2 layout apart from the one register recorded below. An owner's
# Apex 300 reads 0 at 2008 while the app shows grid charging enabled, and a
# write does not come back in the read-back; the same register works on their
# EL100 V2, so the register is right and the model is the exception.
GRID_CHARGE_SWITCH = "ctrl_grid_charge_on"
EL100V2_ONLY_DIFFERENCE = (GRID_CHARGE_SWITCH, 2008, 1)
# The standby switch reads its state from 174 and pulses 2 to 2013. It belongs
# to the Apex 300 alone among the portables: an Elite 200 V2 owner traced the
# official integration's sleep switch to 2013, but the Elite 100 V2 has no sleep
# switch in its app, so the Apex 300 is the unit that could be asked.
SLEEP_SWITCH = ("ctrl_sleep_mode", 174, 1)
AP300_ONLY_REGISTERS = {SLEEP_SWITCH}


def _layout(device_cls):
    device = device_cls()
    return [(f.name, f.address, f.size) for f in device.fields]


class TestAP300(unittest.TestCase):
    def test_it_omits_the_grid_charge_switch(self):
        # A switch that cannot be turned on is worse than no switch: it shows
        # off while the app says grid charging is running.
        names = {name for name, _, _ in _layout(AP300)}
        self.assertNotIn(GRID_CHARGE_SWITCH, names)

    def test_layout_matches_the_el100v2_apart_from_the_recorded_registers(self):
        reference = set(_layout(EL100V2))
        ap300 = set(_layout(AP300))

        self.assertEqual(reference - ap300, {EL100V2_ONLY_DIFFERENCE})
        self.assertEqual(ap300 - reference, AP300_ONLY_REGISTERS)

    def test_the_sleep_switch_reads_174_and_pulses_2_to_2013(self):
        # Sweeping the registers while standby was switched by hand on 5 Nov left
        # 174 as the word that says whether the station is asleep: 0 awake, 8 in
        # standby, and back again in the run that switched standby off. The write
        # that moves it is 2 to 2013, which toggles whichever way it is asked, so
        # the switch sends the same pulse for on and for off and reads the result
        # back from 174.
        device = AP300()
        field = next(f for f in device.fields if f.name == "ctrl_sleep_mode")
        self.assertEqual((field.address, field.write_address), (174, 2013))
        self.assertTrue(field.is_writeable())

        for word, expected in (
            (0, False),  # awake
            (8, True),  # standby
            (0x2008, True),  # standby with another bit set beside it
            (2, False),  # a bit that is not the standby one
        ):
            with self.subTest(word=word):
                self.assertIs(field.parse(struct.pack("!H", word)), expected)

        # The pulse is the same in both directions: the station moves to the
        # other state, and which state that is depends on where it already was.
        for state in (True, False):
            with self.subTest(state=state):
                command = device.build_write_command("ctrl_sleep_mode", state)
                self.assertEqual((command.address, command.value), (2013, 2))

        # The words the station has shown at 2013 are not what this field takes:
        # a bool is the only direction it can be asked to move in.
        for value in (0, 2, 3, 4, "on", None):
            with self.subTest(value=value):
                self.assertIsNone(device.build_write_command("ctrl_sleep_mode", value))

    def test_the_switch_lands_in_the_platform_ha_asks_for(self):
        # Home Assistant builds switches from get_switch_fields() and sensors
        # from get_sensor_fields(), so the standby control has to arrive in the
        # first and 174 must not also show as a sensor beside it.
        device = AP300()
        switches = [f.name for f in device.get_switch_fields()]
        sensors = [f.name for f in device.get_sensor_fields()]
        self.assertIn("ctrl_sleep_mode", switches)
        self.assertNotIn("ctrl_sleep_mode", sensors)
        self.assertNotIn("standby_state", sensors)

    def test_the_flag_word_at_124_is_left_off_the_profile(self):
        # The sweep that found 174 also found 124: 0x0800 awake, 0x2800 in
        # standby, and back again when standby was switched off. It is not
        # reported, because the word answers for more than the standby state -
        # it read 0x0802 with the AC inverter on and 0x0806 with the AC and DC
        # inverters on - so it stays off the profile rather than being decoded
        # bit by bit.
        device = AP300()
        self.assertNotIn("standby_flags", [f.name for f in device.fields])
        for block in device.get_polling_registers():
            with self.subTest(block=block.starting_address):
                end = block.starting_address + block.quantity
                self.assertFalse(block.starting_address <= 124 < end)

    def test_the_switch_costs_one_read_and_2013_costs_none(self):
        # 174, the switch's state word, sits 28 above the 140+7 block, past the
        # gap this station merges, so it adds a request of its own. The 2013 the
        # switch pulses is already inside the 2005+16 block, so the control
        # itself needs no read at all.
        blocks = [
            (r.starting_address, r.quantity) for r in AP300().get_polling_registers()
        ]
        self.assertIn((174, 1), blocks)
        self.assertIn((2005, 16), blocks)
        self.assertEqual(len(blocks), 8)

    def test_the_other_elite_portables_keep_the_switch(self):
        # The exclusion belongs to the Apex 300 alone; the Elite portables are
        # verified against register 2008.
        for device_cls in (EL100V2, EL100mini, EL200V2, EL300):
            with self.subTest(device=device_cls.__name__):
                field = next(
                    f for f in device_cls().fields if f.name == GRID_CHARGE_SWITCH
                )
                self.assertEqual((field.address, field.size), (2008, 1))
                self.assertTrue(field.is_writeable())


if __name__ == "__main__":
    unittest.main()
