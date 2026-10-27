import asyncio
import struct
import unittest

from bluetti_bt_lib.bluetooth import DeviceWriter, DeviceWriterConfig
from bluetti_bt_lib.bluetooth.device_reader import DeviceReader, DeviceReaderConfig
from bluetti_bt_lib.devices.charger2 import CHARGER2, CHARGER2_CONTROL_STATES
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption


class TestCharger2Writer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = ClientMockNoEncryption()
        self.device = CHARGER2()
        self.lock = asyncio.Lock()

        self.reader = DeviceReader(
            mac=self.client.address,
            bluetti_device=self.device,
            future_builder_method=asyncio.get_running_loop().create_future,
            config=DeviceReaderConfig(timeout=10),
            lock=self.lock,
            ble_client=self.client,
        )

        self.writer = DeviceWriter(
            self.client,
            self.device,
            DeviceWriterConfig(timeout=10),
            self.lock,
            reader=self.reader,
        )

        self.writes = []
        original_write = self.client.write_gatt_char

        async def capture_write(char_specifier, data, response=None):
            command = bytes(data)
            if len(command) >= 2 and command[1] == 6:
                self.writes.append(command)

                # Simulate the device applying the register write without
                # sending a notification to the reader.
                address, value = struct.unpack_from("!HH", command, 2)
                self.client.add_r_int(address, value)
                return

            await original_write(char_specifier, data, response)

        self.client.write_gatt_char = capture_write

    async def asyncTearDown(self):
        await self.reader.disconnect()

    async def test_controls_preserve_other_control_for_all_valid_states(self):
        controls = {
            "ctrl_system_on_off": 0,
            "ctrl_silent_charging": 1,
        }

        for initial_word, initial_state in CHARGER2_CONTROL_STATES.items():
            for field, state_index in controls.items():
                for desired_value in (False, True):
                    with self.subTest(
                        initial_word=f"0x{initial_word:04X}",
                        field=field,
                        desired_value=desired_value,
                    ):
                        self.client.add_r_int(15600, initial_word)
                        self.writes.clear()

                        result = await self.writer.write(field, desired_value)

                        self.assertTrue(result)
                        self.assertEqual(len(self.writes), 1)

                        command = struct.unpack("!HHHH", self.writes[0][:8])
                        self.assertEqual(command[0], 0x0106)  # Device ID + function code
                        self.assertEqual(command[1], 15600)   # Register address

                        expected_state = list(initial_state)
                        expected_state[state_index] = desired_value
                        expected_word = next(
                            word
                            for word, state in CHARGER2_CONTROL_STATES.items()
                            if state == tuple(expected_state)
                        )

                        self.assertEqual(command[2], expected_word)  # Register value

    async def test_unknown_register_value_is_rejected_without_writing(self):
        self.client.add_r_int(15600, 0x1234)

        result = await self.writer.write("ctrl_system_on_off", True)

        self.assertFalse(result)
        self.assertEqual(self.writes, [])

    async def test_control_write_requires_reader(self):
        writer = DeviceWriter(
            self.client,
            self.device,
            DeviceWriterConfig(timeout=10),
            asyncio.Lock(),
        )

        result = await writer.write("ctrl_system_on_off", True)

        self.assertFalse(result)
        self.assertEqual(self.writes, [])

    async def test_non_boolean_control_value_is_rejected(self):
        result = await self.writer.write("ctrl_system_on_off", 1)

        self.assertFalse(result)
        self.assertEqual(self.writes, [])

    async def test_verified_control_write_returns_readback_state(self):
        self.client.add_r_int(15600, 0xAA2A)

        result = await self.writer.write_verified(
            "ctrl_system_on_off", True
        )

        self.assertTrue(result.write_succeeded)
        self.assertTrue(result.readback_succeeded)
        self.assertTrue(result.verified)
        self.assertIsNotNone(result.values)
        self.assertTrue(result.values["ctrl_system_on_off"])
        self.assertFalse(result.values["ctrl_silent_charging"])
        self.assertEqual(len(self.writes), 1)

    async def test_verified_write_reports_readback_failure(self):
        self.client.add_r_int(15600, 0xAA2A)

        original_read_locked = self.reader.read_locked
        read_count = 0

        async def fail_readbacks(*args, **kwargs):
            nonlocal read_count
            read_count += 1
            if read_count >= 2:
                return {}
            return await original_read_locked(*args, **kwargs)

        self.reader.read_locked = fail_readbacks

        result = await self.writer.write_verified(
            "ctrl_system_on_off", True
        )

        self.assertTrue(result.write_succeeded)
        self.assertFalse(result.readback_succeeded)
        self.assertFalse(result.verified)
        self.assertIsNone(result.values)
        self.assertEqual(len(self.writes), 1)

    async def test_readback_that_fails_once_is_retried(self):
        # A read-back that gets no answer is not the end of verification: the
        # window leaves room to ask again, which is what makes a slow or busy
        # station confirm instead of reporting a failed write.
        self.client.add_r_int(15600, 0xAA2A)

        original_read_locked = self.reader.read_locked
        read_count = 0

        async def fail_second_read(*args, **kwargs):
            nonlocal read_count
            read_count += 1
            if read_count == 2:
                return {}
            return await original_read_locked(*args, **kwargs)

        self.reader.read_locked = fail_second_read

        result = await self.writer.write_verified(
            "ctrl_system_on_off", True
        )

        self.assertTrue(result.write_succeeded)
        self.assertTrue(result.readback_succeeded)
        self.assertTrue(result.verified)
        self.assertTrue(result.values["ctrl_system_on_off"])
        self.assertEqual(len(self.writes), 1)

    async def test_verified_write_reports_state_mismatch(self):
        self.client.add_r_int(15600, 0xAA2A)

        # Allow the initial register value to be set, but simulate the device
        # not applying the subsequent write.
        self.client.add_r_int = lambda register, value: None

        result = await self.writer.write_verified(
            "ctrl_system_on_off", True
        )

        self.assertTrue(result.write_succeeded)
        self.assertTrue(result.readback_succeeded)
        self.assertFalse(result.verified)
        self.assertFalse(result.values["ctrl_system_on_off"])
        self.assertFalse(result.values["ctrl_silent_charging"])
        self.assertEqual(len(self.writes), 1)

    async def test_reverse_charging_switch_keeps_the_mode(self):
        for initial, desired, expected in (
            (0x046A, True, 0x0469),
            (0x0469, False, 0x046A),
            (0x0169, False, 0x016A),
        ):
            with self.subTest(initial=f"0x{initial:04X}", desired=desired):
                self.client.add_r_int(15626, initial)
                self.writes.clear()

                result = await self.writer.write("ctrl_reverse_charging", desired)

                self.assertTrue(result)
                self.assertEqual(len(self.writes), 1)
                command = struct.unpack("!HHHH", self.writes[0][:8])
                self.assertEqual(command[1], 15626)
                self.assertEqual(command[2], expected)

    async def test_reverse_charging_mode_keeps_the_switch(self):
        self.client.add_r_int(15626, 0x0169)

        result = await self.writer.write_verified(
            "ctrl_reverse_charging_mode", "TOP_UP"
        )

        self.assertTrue(result.verified)
        self.assertEqual(len(self.writes), 1)
        command = struct.unpack("!HHHH", self.writes[0][:8])
        self.assertEqual(command[1], 15626)
        self.assertEqual(command[2], 0x0469)
        self.assertTrue(result.values["ctrl_reverse_charging"])

    async def test_reverse_charging_rejects_bad_values_without_writing(self):
        self.client.add_r_int(15626, 0x046A)

        self.assertFalse(await self.writer.write("ctrl_reverse_charging", 1))
        self.assertFalse(
            await self.writer.write("ctrl_reverse_charging_mode", "TURBO")
        )
        self.assertEqual(self.writes, [])

    async def test_reverse_charging_unknown_register_is_not_written(self):
        self.client.add_r_int(15626, 0x0468)

        result = await self.writer.write("ctrl_reverse_charging", True)

        self.assertFalse(result)
        self.assertEqual(self.writes, [])


if __name__ == "__main__":
    unittest.main()