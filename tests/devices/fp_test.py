import asyncio
from decimal import Decimal
import unittest

from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption
from bluetti_bt_lib.devices import FP
from bluetti_bt_lib.enums import ChargingMode, EcoMode, UpsMode, WorkingModeV2
from bluetti_bt_lib import DeviceReader, FieldName


class TestFP(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._createMock()

    def _createMock(self):
        # Raw values read from a FridgePower with one BC200 pack attached.
        self.ble_mock = ClientMockNoEncryption()

        self.ble_mock.add_r_int(102, 90)
        # 2292 minutes to empty
        self.ble_mock.add_r_int(104, 2292)
        self.ble_mock.add_r_sstr(110, "FP", 6)
        self.ble_mock.add_r_sn(116, 2600000000001)

        self.ble_mock.add_r_int(140, 0)
        self.ble_mock.add_r_int(142, 64)
        self.ble_mock.add_r_int(144, 0)
        # -64 while passing the grid through, which reads as 64 W in
        self.ble_mock.add_r_int(146, 0xFFC0)

        self.ble_mock.add_r_int(1114, 0x0452)
        self.ble_mock.add_r_int(1115, 0x05F8)
        self.ble_mock.add_r_int(1117, 0xDD3E)
        self.ble_mock.add_r_int(1118, 0x05F7)

        self.ble_mock.add_r_int(1213, 365)
        self.ble_mock.add_r_int(1214, 82)

        self.ble_mock.add_r_int(1300, 600)
        self.ble_mock.add_r_int(1314, 1202)
        # -9, so 0.9 A drawn from the grid
        self.ble_mock.add_r_int(1315, 0xFFF7)

        self.ble_mock.add_r_int(1500, 600)
        self.ble_mock.add_r_int(1511, 1209)

        self.ble_mock.add_r_int(2005, 4)
        self.ble_mock.add_r_int(2011, 1)
        self.ble_mock.add_r_int(2012, 1)
        self.ble_mock.add_r_int(2014, 0)
        self.ble_mock.add_r_int(2015, 4)
        self.ble_mock.add_r_int(2016, 5)
        self.ble_mock.add_r_int(2017, 1)
        self.ble_mock.add_r_int(2018, 4)
        self.ble_mock.add_r_int(2019, 10)
        self.ble_mock.add_r_int(2020, 0)
        self.ble_mock.add_r_int(2021, 1)
        self.ble_mock.add_r_int(2022, 20)
        self.ble_mock.add_r_int(2023, 80)
        self.ble_mock.add_r_int(2029, 1)
        self.ble_mock.add_r_int(2225, 1)

        self.ble_mock.add_r_int(6175, 0xE170)
        self.ble_mock.add_r_int(6176, 0x05F5)

        self.ble_mock.add_r_sstr(12002, "MyHomeSSID", 16)

    async def _read(self):
        reader = DeviceReader(
            "00:11:00:11:00:11",
            FP(),
            asyncio.Future,
            ble_client=self.ble_mock,
        )

        return await reader.read()

    async def test_fp(self):
        data = await self._read()

        self.assertIsNotNone(data)

        self.assertEqual(data.get(FieldName.BATTERY_SOC.value), 90)
        self.assertEqual(data.get(FieldName.DEVICE_TYPE.value), "FP")
        self.assertEqual(data.get(FieldName.DEVICE_SN.value), 2600000000001)

        self.assertEqual(
            data.get(FieldName.TIME_REMAINING.value),
            Decimal(2292) * (1 / Decimal(60)),
        )

        self.assertEqual(data.get(FieldName.DC_OUTPUT_POWER.value), 0)
        self.assertEqual(data.get(FieldName.AC_OUTPUT_POWER.value), 64)
        self.assertEqual(data.get(FieldName.DC_INPUT_POWER.value), 0)

        self.assertEqual(data.get(FieldName.DC_INPUT_VOLTAGE.value), Decimal("36.5"))
        self.assertEqual(data.get(FieldName.DC_INPUT_CURRENT.value), Decimal("8.2"))

        self.assertEqual(data.get(FieldName.AC_INPUT_FREQUENCY.value), Decimal("60.0"))
        self.assertEqual(data.get(FieldName.AC_INPUT_VOLTAGE.value), Decimal("120.2"))
        self.assertEqual(data.get(FieldName.AC_OUTPUT_FREQUENCY.value), Decimal("60.0"))
        self.assertEqual(data.get(FieldName.AC_OUTPUT_VOLTAGE.value), Decimal("120.9"))

        self.assertEqual(
            data.get(FieldName.CTRL_UPS_MODE.value), WorkingModeV2.STANDARD
        )
        self.assertIs(data.get(FieldName.CTRL_AC.value), True)
        self.assertIs(data.get(FieldName.CTRL_DC.value), True)
        self.assertIs(data.get(FieldName.CTRL_ECO_DC.value), False)
        self.assertEqual(
            data.get(FieldName.CTRL_ECO_TIME_MODE_DC.value), EcoMode.HOURS4
        )
        self.assertEqual(data.get(FieldName.CTRL_ECO_MIN_POWER_DC.value), 5)
        self.assertIs(data.get(FieldName.CTRL_ECO_AC.value), True)
        self.assertEqual(
            data.get(FieldName.CTRL_ECO_TIME_MODE_AC.value), EcoMode.HOURS4
        )
        self.assertEqual(data.get(FieldName.CTRL_ECO_MIN_POWER_AC.value), 10)
        self.assertEqual(
            data.get(FieldName.CTRL_CHARGING_MODE.value), ChargingMode.STANDARD
        )
        self.assertIs(data.get(FieldName.CTRL_POWER_LIFTING.value), True)
        self.assertIs(data.get(FieldName.CTRL_TIME_CONTROL_ON.value), True)
        self.assertIs(data.get(FieldName.CTRL_GRID_STABILIZING.value), True)

        self.assertEqual(data.get(FieldName.BATTERY_SOC_RANGE_START.value), 20)
        self.assertEqual(data.get(FieldName.BATTERY_SOC_RANGE_END.value), 80)

        self.assertEqual(data.get(FieldName.VER_ARM.value), Decimal("1001401.14"))
        self.assertEqual(data.get(FieldName.VER_DSP.value), Decimal("1001301.10"))
        self.assertEqual(data.get(FieldName.VER_BMS.value), Decimal("1000001.12"))

        self.assertEqual(data.get(FieldName.WIFI_NAME.value), "MyHomeSSID")

    async def test_grid_side_readings_report_the_power_flowing_in(self):
        data = await self._read()

        self.assertEqual(data.get(FieldName.AC_INPUT_POWER.value), Decimal(64))
        self.assertEqual(data.get(FieldName.AC_INPUT_CURRENT.value), Decimal("0.9"))

    def test_the_working_mode_is_the_v2_one_not_the_v1_ups_mode(self):
        # The FP keeps the working mode at 2005 with the V2 values, where the
        # V1 UpsMode would read the same 4 as TIME_CONTROL rather than STANDARD.
        field = {f.name: f for f in FP().fields}["ctrl_ups_mode"]

        self.assertEqual(field.address, 2005)
        self.assertIs(field.e, WorkingModeV2)
        self.assertEqual(WorkingModeV2(4), WorkingModeV2.STANDARD)
        self.assertEqual(UpsMode(4), UpsMode.TIME_CONTROL)

    def test_the_thresholds_stay_read_only(self):
        # The app's accepted ranges are unconfirmed on this unit, so nothing
        # here can write a charge limit or an eco threshold.
        fields = {f.name: f for f in FP().fields}
        for name in (
            "soc_range_start",
            "soc_range_end",
            "ctrl_eco_min_power_dc",
            "ctrl_eco_min_power_ac",
        ):
            with self.subTest(field=name):
                self.assertFalse(fields[name].is_writeable())


if __name__ == "__main__":
    unittest.main()
