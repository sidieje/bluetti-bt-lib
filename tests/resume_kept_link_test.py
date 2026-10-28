import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bluetti_bt_lib import DeviceReader
from bluetti_bt_lib.base_devices import BaseDeviceV1
from bluetti_bt_lib.bluetooth import device_reader
from bluetti_bt_lib.bluetooth.device_reader import DeviceReaderConfig
from bluetti_bt_lib.devices import AC60
from bluetti_bt_lib.registers import ReadableRegisters
from bluetti_bt_lib.utils.bleak_client_mock import BleakClientMock

from response_validation_test import PACK_NUM_REGISTER, ScriptedMock

STATION = SimpleNamespace(name="AC60", address="00:11:00:11:00:11")
READ = [ReadableRegisters(PACK_NUM_REGISTER, 1)]


class TestResumeKeptLink(unittest.IsolatedAsyncioTestCase):
    def _patches(self, find, establish):
        return (
            patch.object(device_reader.BleakScanner, "find_device_by_address", find),
            patch.object(device_reader, "establish_connection", establish),
            patch.object(device_reader, "RESPONSE_TIMEOUT_SECONDS", 0.05),
            patch.object(device_reader, "RETRY_PAUSE_SECONDS", 0),
        )

    async def test_a_resync_resumes_the_link_it_kept(self):
        # A connected station that stops advertising: only the first scan sees it.
        station = ScriptedMock(deaf=True)
        station.add_r_int(PACK_NUM_REGISTER, 4)
        find = AsyncMock(side_effect=[STATION, None])
        establish = AsyncMock(return_value=station)
        p1, p2, p3, p4 = self._patches(find, establish)
        with p1, p2, p3, p4:
            reader = DeviceReader(STATION.address, AC60(), asyncio.get_running_loop().create_future)
            self.assertIsNone(await reader.read_locked(READ, raw=True))
            station.deaf = False
            values = await reader.read_locked(READ, raw=True)
        self.assertEqual(values, {PACK_NUM_REGISTER: b"\x00\x04"})
        self.assertEqual(establish.await_count, 1)

    async def test_a_kept_link_that_never_answers_is_eventually_dropped(self):
        stations = [ScriptedMock(deaf=True) for _ in range(2)]
        find = AsyncMock(return_value=STATION)
        establish = AsyncMock(side_effect=stations)
        p1, p2, p3, p4 = self._patches(find, establish)
        with p1, p2, p3, p4:
            reader = DeviceReader(STATION.address, AC60(), asyncio.get_running_loop().create_future)
            for _ in range(device_reader.MAX_RESYNCS_ON_ONE_LINK + 1):
                self.assertIsNone(await reader.read_locked(READ, raw=True))
        self.assertEqual(establish.await_count, 2)
        self.assertFalse(stations[0].is_connected)


class TestNewLinkNewSession(unittest.IsolatedAsyncioTestCase):
    async def test_the_session_of_a_dead_link_is_not_trusted(self):
        reader = DeviceReader(STATION.address, BaseDeviceV1(), asyncio.Future,
                              config=DeviceReaderConfig(use_encryption=True))
        dead = BleakClientMock()
        reader.client = reader._notified_client = dead
        reader.has_notifier = True
        reader._connected = True
        reader.encryption.secure_aes_key = bytes(32)
        reader.encryption.peer_pubkey = object()
        # The link died while a request waited, which surfaces as a timeout.
        await dead.disconnect()
        await reader._resync_link()
        scanner = MagicMock()
        scanner.find_device_by_address = AsyncMock(return_value=STATION)
        with patch.object(device_reader, "BleakScanner", scanner), \
             patch.object(device_reader, "establish_connection", AsyncMock(return_value=BleakClientMock())), \
             patch.object(device_reader, "HANDSHAKE_TIMEOUT_SECONDS", 0):
            connected = await reader.connect()
        # The new link has not exchanged keys yet: the old session must not count.
        self.assertFalse(connected)
        self.assertIsNone(reader.encryption.secure_aes_key)


if __name__ == "__main__":
    unittest.main()
