import unittest

from decimal import Decimal

from bluetti_bt_lib.base_devices.bluetti_device import PERSISTENT_SKIP_POLLS
from bluetti_bt_lib.devices.charger2 import (
    CHARGER2,
    Charger2ReverseChargingField,
    Charger2ReverseChargingModeField,
    Charger2SilentChargingField,
    Charger2SystemSwitchField,
)
from bluetti_bt_lib.enums import ReverseChargingMode
from bluetti_bt_lib.fields import FieldName, SignedDecimalField


class TestCharger2(unittest.TestCase):
    def setUp(self):
        self.system_field = Charger2SystemSwitchField(
            FieldName.CTRL_SYSTEM_ON_OFF,
            15600,
        )
        self.silent_field = Charger2SilentChargingField(
            FieldName.CTRL_SILENT_CHARGING,
            15600,
        )
        self.device = CHARGER2()

    def joined(self, logs) -> str:
        return "\n".join(r.getMessage() for r in logs.records)

    def test_all_four_register_states(self):
        expected = {
            "5529": (True, False),
            "AA2A": (False, False),
            "AA26": (False, True),
            "5525": (True, True),
        }

        for hex_value, (system_on, silent_charging) in expected.items():
            with self.subTest(register_value=hex_value):
                data = bytes.fromhex(hex_value)
                self.assertEqual(self.system_field.parse(data), system_on)
                self.assertEqual(
                    self.silent_field.parse(data),
                    silent_charging,
                )

    def test_both_fields_reject_unknown_register_value(self):
        data = bytes.fromhex("1234")

        with self.assertRaises(ValueError):
            self.system_field.parse(data)

        with self.assertRaises(ValueError):
            self.silent_field.parse(data)

    def test_unknown_control_word_skips_only_the_controls(self):
        # A station that reads 0x001E at 15600 for a minute after a hard
        # disconnect must not lose the whole cycle, only the two controls.
        with self.assertLogs(
            "bluetti_bt_lib.base_devices.bluetti_device", "DEBUG"
        ) as logs:
            parsed = self.device.parse(15600, bytes.fromhex("001E"))

        self.assertEqual(parsed, {})
        skips = [r for r in logs.records if "Skipping" in r.getMessage()]
        self.assertEqual(len(skips), 1)
        self.assertIn("0x001E", skips[0].getMessage())

    def test_two_fields_on_one_register_report_once(self):
        # ctrl_system_on_off and ctrl_silent_charging are the same register, so
        # one bad word there is one fault. It used to cost two warnings.
        with self.assertLogs(
            "bluetti_bt_lib.base_devices.bluetti_device", "DEBUG"
        ) as logs:
            self.device.parse(15600, bytes.fromhex("0089"))

        skips = [r for r in logs.records if "Skipping" in r.getMessage()]
        self.assertEqual(len(skips), 1)

    def test_a_brief_unknown_value_stays_quiet(self):
        # A Charger 2 that wakes with solar input reads 0x0089 for a cycle or
        # two. That is worth a debug line, not a warning in the HA log.
        logger = "bluetti_bt_lib.base_devices.bluetti_device"
        with self.assertLogs(logger, "DEBUG") as logs:
            for _ in range(PERSISTENT_SKIP_POLLS - 1):
                self.assertEqual(
                    self.device.parse(15600, bytes.fromhex("0089")), {}
                )

        self.assertEqual(
            [r.levelname for r in logs.records if "Skipping" in r.getMessage()],
            ["DEBUG"] * (PERSISTENT_SKIP_POLLS - 1),
        )

    def test_an_unknown_value_that_persists_warns_once(self):
        # Polled every 20 s, a station that sits on 0x001E keeps saying the same
        # thing. The streak is what warns, and it warns once.
        logger = "bluetti_bt_lib.base_devices.bluetti_device"
        with self.assertLogs(logger, "DEBUG") as logs:
            for _ in range(PERSISTENT_SKIP_POLLS + 1):
                self.assertEqual(
                    self.device.parse(15600, bytes.fromhex("001E")), {}
                )

        skips = [r for r in logs.records if "Skipping" in r.getMessage()]
        self.assertEqual(
            [r.levelname for r in skips],
            ["DEBUG"] * (PERSISTENT_SKIP_POLLS - 1) + ["WARNING", "DEBUG"],
        )
        warning = next(r for r in skips if r.levelname == "WARNING")
        self.assertIn(f"{PERSISTENT_SKIP_POLLS} poll(s) in a row", warning.getMessage())

    def test_a_different_unknown_value_starts_the_streak_over(self):
        logger = "bluetti_bt_lib.base_devices.bluetti_device"
        with self.assertLogs(logger, "DEBUG") as logs:
            for _ in range(PERSISTENT_SKIP_POLLS - 1):
                self.device.parse(15600, bytes.fromhex("001E"))
            self.device.parse(15600, bytes.fromhex("001F"))

        skips = [r for r in logs.records if "Skipping" in r.getMessage()]
        self.assertEqual([r.levelname for r in skips], ["DEBUG"] * len(skips))
        self.assertIn("1 poll", skips[-1].getMessage())
        self.assertIn("0x001F", skips[-1].getMessage())

    def test_the_streak_restarts_after_a_known_value(self):
        # Once the field parses again it starts over: the same fault later on
        # has to build a streak again before it speaks up.
        logger = "bluetti_bt_lib.base_devices.bluetti_device"
        with self.assertLogs(logger, "DEBUG") as logs:
            for _ in range(PERSISTENT_SKIP_POLLS):
                self.device.parse(15600, bytes.fromhex("001E"))
            parsed = self.device.parse(15600, bytes.fromhex("5529"))
            for _ in range(PERSISTENT_SKIP_POLLS - 1):
                self.device.parse(15600, bytes.fromhex("001E"))

        self.assertEqual(parsed[FieldName.CTRL_SYSTEM_ON_OFF.value], True)
        self.assertEqual(
            [r.levelname for r in logs.records if r.levelname == "WARNING"],
            ["WARNING"],
        )
        self.assertIn("parses again", self.joined(logs))

    def test_known_control_word_still_parses_both_controls(self):
        parsed = self.device.parse(15600, bytes.fromhex("5529"))
        self.assertEqual(
            parsed,
            {
                FieldName.CTRL_SYSTEM_ON_OFF.value: True,
                FieldName.CTRL_SILENT_CHARGING.value: False,
            },
        )

    def test_both_controls_are_classified_as_switches(self):
        switch_fields = [
            field.name for field in self.device.get_switch_fields()
        ]

        self.assertIn(FieldName.CTRL_SYSTEM_ON_OFF.value, switch_fields)
        self.assertIn(FieldName.CTRL_SILENT_CHARGING.value, switch_fields)

    def test_both_controls_are_not_classified_as_sensors(self):
        sensor_fields = [
            field.name for field in self.device.get_sensor_fields()
        ]

        self.assertNotIn(FieldName.CTRL_SYSTEM_ON_OFF.value, sensor_fields)
        self.assertNotIn(FieldName.CTRL_SILENT_CHARGING.value, sensor_fields)

    def test_both_controls_require_read_modify_write(self):
        for field_name in (
            FieldName.CTRL_SYSTEM_ON_OFF.value,
            FieldName.CTRL_SILENT_CHARGING.value,
        ):
            with self.subTest(field=field_name):
                with self.assertRaises(ValueError):
                    self.device.build_write_command(field_name, True)

    def test_reverse_charging_top_up_power(self):
        # The app's Top-up -> Power setting: 100 W, then 150 W (26 Oct 2026).
        name = FieldName.REVERSE_CHARGING_TOP_UP_POWER.value
        for hex_value, watts in (("0064", 100), ("0096", 150)):
            with self.subTest(register_value=hex_value):
                parsed = self.device.parse(15631, bytes.fromhex(hex_value))
                self.assertEqual(parsed[name], watts)

    def test_top_up_power_is_read_only(self):
        name = FieldName.REVERSE_CHARGING_TOP_UP_POWER.value
        sensors = [field.name for field in self.device.get_sensor_fields()]
        self.assertIn(name, sensors)
        self.assertIsNone(self.device.build_write_command(name, 120))

    def test_top_up_power_reads_the_whole_word(self):
        # The app allows 100-800 W, so the value passes 255 and the high byte is
        # in use: 800 W reads 0x0320, not 0x0020.
        name = FieldName.REVERSE_CHARGING_TOP_UP_POWER.value
        parsed = self.device.parse(15631, bytes.fromhex("0320"))
        self.assertEqual(parsed[name], 800)

    def test_reverse_charging_states_seen_on_a_device(self):
        # Five dumps from a Charger 2 on 26 Oct 2026, taken while the Bluetti
        # app changed the mode. Pulse with the switch off has never been dumped.
        expected = {
            "0169": (True, ReverseChargingMode.TRICKLE),
            "0269": (True, ReverseChargingMode.PULSE),
            "0469": (True, ReverseChargingMode.TOP_UP),
            "016A": (False, ReverseChargingMode.TRICKLE),
            "046A": (False, ReverseChargingMode.TOP_UP),
        }

        for hex_value, (on, mode) in expected.items():
            with self.subTest(register_value=hex_value):
                parsed = self.device.parse(15626, bytes.fromhex(hex_value))
                self.assertEqual(
                    parsed[FieldName.CTRL_REVERSE_CHARGING.value], on
                )
                self.assertEqual(
                    parsed[FieldName.CTRL_REVERSE_CHARGING_MODE.value], mode
                )

    def test_unknown_reverse_charging_word_skips_only_those_fields(self):
        # 0x0368 has two mode bits set and no switch state, which is not a
        # layout the app has ever produced. The rest of the cycle stays.
        with self.assertLogs(
            "bluetti_bt_lib.base_devices.bluetti_device", "DEBUG"
        ) as logs:
            parsed = self.device.parse(15626, bytes.fromhex("0368"))

        self.assertEqual(parsed, {})
        skips = [r for r in logs.records if "Skipping" in r.getMessage()]
        self.assertEqual(len(skips), 1)
        self.assertIn("0x0368", skips[0].getMessage())

    def test_late_answer_from_15600_is_not_read_as_on(self):
        # 0x5529 ends in 0b01, which is the "on" pair, but 0x55 is not a mode.
        # A shifted answer from register 15600 has landed on 15626 before, so
        # the high byte has to agree before the switch bits mean anything.
        for hex_value in ("5529", "AA2A", "0001"):
            with self.subTest(register_value=hex_value):
                with self.assertLogs(
                    "bluetti_bt_lib.base_devices.bluetti_device", "DEBUG"
                ):
                    parsed = self.device.parse(15626, bytes.fromhex(hex_value))
                self.assertEqual(parsed, {})

    def test_reverse_charging_is_reported_but_not_writeable(self):
        # Reading 15626 is safe; writing it pushes current into a car's starter
        # battery, so it stays out of reach until the write side is proven.
        switches = [f.name for f in self.device.get_switch_fields()]
        selects = [f.name for f in self.device.get_select_fields()]
        bools = [f.name for f in self.device.get_bool_fields()]
        sensors = [f.name for f in self.device.get_sensor_fields()]

        self.assertIn(FieldName.CTRL_REVERSE_CHARGING.value, bools)
        self.assertIn(FieldName.CTRL_REVERSE_CHARGING_MODE.value, sensors)
        self.assertNotIn(FieldName.CTRL_REVERSE_CHARGING.value, switches)
        self.assertNotIn(FieldName.CTRL_REVERSE_CHARGING_MODE.value, selects)

        for field_name, value in (
            (FieldName.CTRL_REVERSE_CHARGING.value, True),
            (FieldName.CTRL_REVERSE_CHARGING_MODE.value, "PULSE"),
        ):
            with self.subTest(field=field_name):
                self.assertIsNone(self.device.build_write_command(field_name, value))

    def test_reverse_charging_compose_keeps_the_rest_of_the_word(self):
        # Groundwork for the write side: each field changes only its own half of
        # 15626 and refuses a word it does not recognise, so a write can never
        # zero the mode byte or the switch along with it.
        switch = Charger2ReverseChargingField(
            FieldName.CTRL_REVERSE_CHARGING, 15626
        )
        mode = Charger2ReverseChargingModeField(
            FieldName.CTRL_REVERSE_CHARGING_MODE, 15626
        )

        self.assertEqual(switch.compose(0x046A, True), 0x0469)
        self.assertEqual(switch.compose(0x0469, False), 0x046A)
        self.assertEqual(mode.compose(0x046A, "PULSE"), 0x026A)
        self.assertEqual(mode.compose(0x0169, ReverseChargingMode.TOP_UP), 0x0469)
        # The pulse/off word the app has never been dumped producing.
        self.assertEqual(mode.compose(0x016A, "PULSE"), 0x026A)
        self.assertEqual(switch.compose(0x026A, True), 0x0269)

        self.assertIsNone(switch.compose(0x0468, True))
        self.assertIsNone(switch.compose(0x046A, 1))
        self.assertIsNone(mode.compose(0x046A, "TURBO"))
        self.assertIsNone(switch.compose(0x5529, True))
        self.assertIsNone(mode.compose(0x5529, "PULSE"))

    def test_starter_battery_values_are_signed(self):
        # Raw dumps from a Charger 2: 0x1760/0x030A while the alternator
        # charges through it, 0xFCE0/0xFF9C while reverse charging feeds the
        # starter battery. multiplier=-1 flips the raw polarity so the sign
        # matches the other signed Charger 2 fields.
        cases = {
            (15540, "FCE0"): Decimal("8.00"),
            (15542, "FF9C"): Decimal("100"),
            (15540, "1760"): Decimal("-59.84"),
            (15542, "030A"): Decimal("-778"),
        }
        names = {
            15540: FieldName.B_ALT_CURRENT.value,
            15542: FieldName.B_ALT_IO_POWER.value,
        }

        for (address, hex_value), expected in cases.items():
            with self.subTest(address=address, register_value=hex_value):
                parsed = self.device.parse(address, bytes.fromhex(hex_value))
                self.assertEqual(parsed[names[address]], expected)

    def test_signed_fields_share_one_sign_convention(self):
        # A power flow card treats the sign as a direction, so every signed
        # Charger 2 reading has to mean the same direction of flow. The raw
        # registers do not agree: 15540/15542 are the other way round to
        # 15536/15538/15546, so all five carry multiplier=-1.
        signed = {
            field.address: field
            for field in self.device.get_sensor_fields()
            if isinstance(field, SignedDecimalField)
        }

        self.assertEqual(
            set(signed),
            {15536, 15538, 15540, 15542, 15546},
        )
        for address, field in signed.items():
            with self.subTest(address=address):
                self.assertEqual(field.multiplier, -1)


if __name__ == "__main__":
    unittest.main()
