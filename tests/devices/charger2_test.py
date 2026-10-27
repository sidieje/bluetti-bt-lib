import unittest

from decimal import Decimal

from bluetti_bt_lib.devices.charger2 import (
    CHARGER2,
    Charger2ReverseChargingField,
    Charger2ReverseChargingModeField,
    Charger2SilentChargingField,
    Charger2SystemSwitchField,
)
from bluetti_bt_lib.enums import ReverseChargingMode
from bluetti_bt_lib.fields import FieldName


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
        with self.assertLogs("bluetti_bt_lib.base_devices.bluetti_device", "WARNING") as logs:
            parsed = self.device.parse(15600, bytes.fromhex("001E"))
        self.assertEqual(parsed, {})
        self.assertEqual(len(logs.records), 2)
        self.assertIn("0x001E", logs.output[0])

    def test_the_same_unknown_value_warns_once(self):
        # Polled every 20 s, a station that sits on 0x001E for a minute would
        # repeat the same two warnings on every poll. The first poll warns,
        # the ones after it are DEBUG.
        logger = "bluetti_bt_lib.base_devices.bluetti_device"
        with self.assertLogs(logger, "DEBUG") as logs:
            for _ in range(3):
                self.assertEqual(self.device.parse(15600, bytes.fromhex("001E")), {})

        skips = [r for r in logs.records if "Skipping" in r.getMessage()]
        self.assertEqual(
            [r.levelname for r in skips], ["WARNING"] * 2 + ["DEBUG"] * 4
        )

    def test_a_different_unknown_value_warns_again(self):
        logger = "bluetti_bt_lib.base_devices.bluetti_device"
        with self.assertLogs(logger, "DEBUG") as logs:
            self.device.parse(15600, bytes.fromhex("001E"))
            self.device.parse(15600, bytes.fromhex("001E"))
            self.device.parse(15600, bytes.fromhex("001F"))

        warnings = [r.getMessage() for r in logs.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 4)
        self.assertIn("0x001F", warnings[2])
        self.assertIn("1 more skipped reads of the previous value", warnings[2])

    def test_an_unknown_value_warns_again_after_a_known_one(self):
        # Once the field parses again it starts over: the same fault later on
        # is news again.
        logger = "bluetti_bt_lib.base_devices.bluetti_device"
        with self.assertLogs(logger, "DEBUG") as logs:
            self.device.parse(15600, bytes.fromhex("001E"))
            parsed = self.device.parse(15600, bytes.fromhex("5529"))
            self.device.parse(15600, bytes.fromhex("001E"))

        self.assertEqual(parsed[FieldName.CTRL_SYSTEM_ON_OFF.value], True)
        warnings = [r for r in logs.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 4)
        self.assertIn("parses again", logs.output[2])

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

    def test_reverse_charging_states_seen_on_a_device(self):
        # 0x0169 trickle on, 0x0269 pulse on, 0x0469 top-up on,
        # 0x046A top-up selected but off (dumps of 26 Oct 2026).
        expected = {
            "0169": (True, ReverseChargingMode.TRICKLE),
            "0269": (True, ReverseChargingMode.PULSE),
            "0469": (True, ReverseChargingMode.TOP_UP),
            "046A": (False, ReverseChargingMode.TOP_UP),
            "016A": (False, ReverseChargingMode.TRICKLE),
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

    def test_unknown_reverse_charging_value_skips_only_that_field(self):
        with self.assertLogs("bluetti_bt_lib.base_devices.bluetti_device", "WARNING"):
            parsed = self.device.parse(15626, bytes.fromhex("0368"))
        self.assertEqual(parsed, {})

    def test_late_answer_from_15600_is_not_read_as_on(self):
        # 0x5529 ends in 0b01, but 0x55 is no mode: a late answer, not "on".
        for hex_value in ("5529", "AA2A", "0001"):
            with self.subTest(register_value=hex_value):
                with self.assertLogs("bluetti_bt_lib.base_devices.bluetti_device", "WARNING"):
                    parsed = self.device.parse(15626, bytes.fromhex(hex_value))
                self.assertEqual(parsed, {})

    def test_reverse_charging_compose_keeps_the_other_bits(self):
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
        self.assertIsNone(switch.compose(0x0468, True))
        self.assertIsNone(switch.compose(0x046A, 1))
        self.assertIsNone(mode.compose(0x046A, "TURBO"))
        self.assertIsNone(switch.compose(0x5529, True))
        self.assertIsNone(mode.compose(0x5529, "PULSE"))

    def test_reverse_charging_controls_require_read_modify_write(self):
        for field_name, value in (
            (FieldName.CTRL_REVERSE_CHARGING.value, True),
            (FieldName.CTRL_REVERSE_CHARGING_MODE.value, "PULSE"),
        ):
            with self.subTest(field=field_name):
                with self.assertRaises(ValueError):
                    self.device.build_write_command(field_name, value)

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

    def test_starter_battery_values_are_signed(self):
        # Reverse charging read 0xFCE0 (15540) and 0xFF9C (15542): -8 A, -100 W.
        # From the alternator they are positive: 59.84 A, 778 W.
        cases = {
            (15540, "FCE0"): Decimal("-8.00"),
            (15542, "FF9C"): Decimal("-100"),
            (15540, "1760"): Decimal("59.84"),
            (15542, "030A"): Decimal("778"),
        }
        names = {
            15540: FieldName.B_ALT_CURRENT.value,
            15542: FieldName.B_ALT_IO_POWER.value,
        }

        for (address, hex_value), expected in cases.items():
            with self.subTest(address=address, register_value=hex_value):
                parsed = self.device.parse(address, bytes.fromhex(hex_value))
                self.assertEqual(parsed[names[address]], expected)


if __name__ == "__main__":
    unittest.main()
