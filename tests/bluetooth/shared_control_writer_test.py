"""Read-modify-write of a shared state word, on a device nobody has heard of.

The Charger 2 is the only station whose two controls share a state word today,
and the writer used to name it to find out. These tests drive the same code
through a made-up profile, so the read-modify-write can only work the way it is
meant to: by asking the field.
"""

import asyncio
import struct
import unittest

from bluetti_bt_lib.base_devices import BluettiDevice
from bluetti_bt_lib.bluetooth import DeviceWriter, DeviceWriterConfig
from bluetti_bt_lib.bluetooth.device_reader import DeviceReader, DeviceReaderConfig
from bluetti_bt_lib.fields import FieldName, SharedRegisterControlField
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption

# Deliberately unlike the Charger 2's words, and on another register, so that
# nothing here can be a Charger 2 in disguise.
STATES = {
    0x1234: (False, False),
    0x2345: (True, False),
    0x3456: (True, True),
    0x4567: (False, True),
}
SHARED_ADDRESS = 200

FIRST = FieldName.CTRL_AC.value
SECOND = FieldName.CTRL_DC.value


def build_device() -> BluettiDevice:
    return BluettiDevice(
        fields=[
            SharedRegisterControlField(
                FieldName.CTRL_AC, SHARED_ADDRESS, STATES, state_index=0
            ),
            SharedRegisterControlField(
                FieldName.CTRL_DC, SHARED_ADDRESS, STATES, state_index=1
            ),
        ],
        pack_fields=[],
        max_packs=0,
    )


class TestSharedControlWriter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = ClientMockNoEncryption()
        self.device = build_device()
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

                address, value = struct.unpack_from("!HH", command, 2)
                self.client.add_r_int(address, value)
                return

            await original_write(char_specifier, data, response)

        self.client.write_gatt_char = capture_write

    async def asyncTearDown(self):
        await self.reader.disconnect()

    def word_on_the_wire(self) -> int:
        command = struct.unpack("!HHHH", self.writes[0][:8])
        self.assertEqual(command[0], 0x0106)
        self.assertEqual(command[1], SHARED_ADDRESS)
        return command[2]

    async def test_either_half_can_be_written(self):
        for initial_word, initial_state in STATES.items():
            for name, state_index in ((FIRST, 0), (SECOND, 1)):
                for desired in (False, True):
                    with self.subTest(
                        initial_word=f"0x{initial_word:04X}",
                        field=name,
                        desired=desired,
                    ):
                        self.client.add_r_int(SHARED_ADDRESS, initial_word)
                        self.writes.clear()

                        result = await self.writer.write(name, desired)

                        self.assertTrue(result)
                        self.assertEqual(len(self.writes), 1)

                        expected = list(initial_state)
                        expected[state_index] = desired
                        expected_word = next(
                            word
                            for word, state in STATES.items()
                            if state == tuple(expected)
                        )
                        self.assertEqual(self.word_on_the_wire(), expected_word)

    async def test_a_word_outside_the_table_is_not_written(self):
        # Half way through its own change the station answers something the
        # table has never reported. Rewriting the word from a guess would drop
        # the other control, so the write is refused instead.
        self.client.add_r_int(SHARED_ADDRESS, 0x0000)

        result = await self.writer.write(FIRST, True)

        self.assertFalse(result)
        self.assertEqual(self.writes, [])

    async def test_without_a_reader_there_is_nothing_to_modify(self):
        writer = DeviceWriter(
            self.client,
            self.device,
            DeviceWriterConfig(timeout=10),
            asyncio.Lock(),
        )

        result = await writer.write(FIRST, True)

        self.assertFalse(result)
        self.assertEqual(self.writes, [])

    async def test_only_a_direction_can_be_asked_for(self):
        result = await self.writer.write(FIRST, 1)

        self.assertFalse(result)
        self.assertEqual(self.writes, [])

    def test_a_plain_write_of_the_field_would_corrupt_the_word(self):
        # What the read-modify-write is protecting against. The field is a
        # SwitchField, so building its command the ordinary way is honest and
        # wrong: 1 is not a word this station has ever reported.
        command = self.device.build_write_command(FIRST, True)

        self.assertEqual(command.address, SHARED_ADDRESS)
        self.assertNotIn(command.value, STATES)

    async def test_verified_write_reads_both_halves_back(self):
        self.client.add_r_int(SHARED_ADDRESS, 0x1234)

        result = await self.writer.write_verified(SECOND, True)

        self.assertTrue(result.write_succeeded)
        self.assertTrue(result.verified)
        self.assertFalse(result.values[FIRST])
        self.assertTrue(result.values[SECOND])


if __name__ == "__main__":
    unittest.main()
