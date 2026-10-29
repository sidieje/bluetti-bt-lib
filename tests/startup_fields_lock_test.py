import asyncio
import unittest

from bluetti_bt_lib import DeviceReader
from bluetti_bt_lib.base_devices import BaseDeviceV1
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption


class TestStartupFieldsUnderLock(unittest.IsolatedAsyncioTestCase):
    async def test_a_cache_dropped_while_waiting_for_the_lock_is_asked_again(self):
        station = ClientMockNoEncryption()
        station.add_r_int(36, 10)
        lock = asyncio.Lock()
        reader = DeviceReader("00:11:00:11:00:11", BaseDeviceV1(), asyncio.Future,
                              ble_client=station, lock=lock)
        first = await reader.read()
        startup = {f.name for f in reader.bluetti_device.startup_fields} & set(first)
        self.assertTrue(startup)
        async with lock:
            poll = asyncio.create_task(reader.read())
            await asyncio.sleep(0)  # the poll is now waiting for the lock
            await reader._mark_disconnected()  # e.g. a write's read-back hit a BleakError
        data = await poll
        self.assertLessEqual(startup, set(data))

    async def test_an_abandoned_handshake_forgets_the_startup_fields(self):
        station = ClientMockNoEncryption()
        reader = DeviceReader("00:11:00:11:00:11", BaseDeviceV1(), asyncio.Future, ble_client=station)
        await reader.read()
        self.assertTrue(reader._startup_values)
        await reader._abandon_handshake()
        self.assertEqual(reader._startup_values, {})


if __name__ == "__main__":
    unittest.main()
