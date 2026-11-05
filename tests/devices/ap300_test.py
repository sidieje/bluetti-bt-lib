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
# The 2013 standby register and the two words that hold the standby state are
# read on the Apex 300 alone. An Elite 200 V2 owner traced the official
# integration's sleep switch to 2013, but the Elite 100 V2 has no sleep switch in
# its app, so this is the unit that could answer it.
AP300_ONLY_PROBE = ("ctrl_sleep_mode", 2013, 1)
STANDBY_WORDS = {
    ("standby_flags", 124, 1),
    ("standby_state", 174, 1),
}
AP300_ONLY_REGISTERS = {AP300_ONLY_PROBE} | STANDBY_WORDS


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

    def test_standby_register_2013_keeps_the_word_and_writes_a_pulse(self):
        # An owner's Apex 300 reads 0 at 2013 while awake and 2 in standby, so
        # the word is reported as it reads: a switch field would turn both into
        # a plain on/off and lose which state the station is actually in.
        field = next(f for f in AP300().fields if f.name == "ctrl_sleep_mode")
        self.assertEqual(field.address, 2013)
        self.assertTrue(field.is_writeable())

        for value in (0, 1, 2, 3, 4):
            with self.subTest(value=value):
                self.assertEqual(field.parse(struct.pack("!H", value)), value)

        device = AP300()
        for value in (0, 2, 3, 4):
            with self.subTest(value=value):
                command = device.build_write_command("ctrl_sleep_mode", value)
                self.assertEqual((command.address, command.value), (2013, value))

        # Writing 2 both wakes and sleeps the station, and 2013 reads 0 again
        # afterwards, so it is a pulse rather than a state. The range stops at
        # the 4 the official integration uses: nothing wider has been seen on
        # any station, and the True/False a switch would send is not a word.
        for value in (-1, 5, 65535, True, "0"):
            with self.subTest(value=value):
                self.assertIsNone(device.build_write_command("ctrl_sleep_mode", value))

    def test_standby_register_adds_no_poll_request(self):
        # 2013 sits inside the 2005+16 block the profile already asks for, so
        # probing it costs no extra round trip.
        blocks = [
            (r.starting_address, r.quantity) for r in AP300().get_polling_registers()
        ]
        self.assertIn((2005, 16), blocks)

    def test_standby_words_report_what_the_station_shows(self):
        # Sweeping the registers around 2013 and the 100-199 block while standby
        # was switched by hand in the middle of a run (tools/standby_probe.py
        # --switch) left two words whose values never overlapped between the two
        # halves, in both directions: 124 went 0x0800 -> 0x2800 and 174 went
        # 0 -> 8 going into standby, and both came back when standby was
        # switched off again. They stay plain read-only words, because only the
        # one bit that moved in each has been worked out.
        device = AP300()
        for name, address, awake, asleep in (
            ("standby_flags", 124, 0x0800, 0x2800),
            ("standby_state", 174, 0, 8),
        ):
            with self.subTest(name=name):
                field = next(f for f in device.fields if f.name == name)
                self.assertEqual(field.address, address)
                self.assertFalse(field.is_writeable())
                self.assertEqual(field.parse(struct.pack("!H", awake)), awake)
                self.assertEqual(field.parse(struct.pack("!H", asleep)), asleep)
                self.assertIsNone(device.build_write_command(name, asleep))

    def test_standby_words_cost_one_read_each(self):
        # 124 and 174 sit outside every block the profile already asks for: 124
        # is 20 above the 102+3 block and 174 is 28 above 140+7, both past the
        # gap this station merges, so each adds a request of its own.
        blocks = [
            (r.starting_address, r.quantity) for r in AP300().get_polling_registers()
        ]
        self.assertIn((124, 1), blocks)
        self.assertIn((174, 1), blocks)
        self.assertEqual(len(blocks), 9)

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
