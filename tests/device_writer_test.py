import asyncio
import time
import unittest

from bluetti_bt_lib.bluetooth import DeviceWriter, DeviceWriterConfig
from bluetti_bt_lib.bluetooth.encryption import BluettiEncryption
from bluetti_bt_lib.devices.ac180 import AC180
from bluetti_bt_lib.fields import FieldName
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption

NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"


class TestDeviceWriter(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ble_mock = ClientMockNoEncryption()
        self.device = AC180()

    def build_writer(self, use_encryption: bool = False) -> DeviceWriter:
        return DeviceWriter(
            self.ble_mock,
            self.device,
            DeviceWriterConfig(timeout=15, use_encryption=use_encryption),
            asyncio.Lock(),
        )

    async def test_write_without_notifier_does_not_wait_for_ack(self):
        writer = self.build_writer()

        start = time.monotonic()
        result = await writer.write(FieldName.CTRL_AC.value, True)
        elapsed = time.monotonic() - start

        self.assertTrue(result)
        # No notifier means no acknowledgement, so the write must not block
        self.assertLess(elapsed, 1)
        self.assertTrue(self.ble_mock.is_connected)

    async def test_attach_shares_connection_without_second_notifier(self):
        async def on_notify(*_):
            return None

        notify_calls = 0
        original_start_notify = self.ble_mock.start_notify

        async def counting_start_notify(*args, **kwargs):
            nonlocal notify_calls
            notify_calls += 1
            await original_start_notify(*args, **kwargs)

        self.ble_mock.start_notify = counting_start_notify

        # A reader owns the notifier of the shared connection
        await self.ble_mock.start_notify(NOTIFY_UUID, on_notify)
        self.assertEqual(notify_calls, 1)

        writer = self.build_writer()
        writer.attach()

        start = time.monotonic()
        result = await writer.write(FieldName.CTRL_AC.value, True)
        elapsed = time.monotonic() - start

        self.assertTrue(result)
        self.assertLess(elapsed, 1)
        # The writer must not register a notifier of its own
        self.assertEqual(notify_calls, 1)

    async def test_disconnect_keeps_borrowed_session(self):
        encryption = BluettiEncryption()
        encryption.secure_aes_key = b"0" * 16

        writer = self.build_writer()
        writer.attach(encryption=encryption)

        await writer.disconnect()

        self.assertIs(writer.encryption, encryption)
        self.assertEqual(encryption.secure_aes_key, b"0" * 16)

    async def test_disconnect_resets_own_session(self):
        writer = self.build_writer()
        await writer.connect()
        writer.encryption.secure_aes_key = b"0" * 16

        await writer.disconnect()

        self.assertIsNone(writer.encryption.secure_aes_key)
        self.assertFalse(self.ble_mock.is_connected)
