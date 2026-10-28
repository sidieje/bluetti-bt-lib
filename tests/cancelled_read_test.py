import asyncio
import unittest

from bleak.exc import BleakError

from bluetti_bt_lib import DeviceReader
from bluetti_bt_lib.base_devices import BaseDeviceV1
from bluetti_bt_lib.fields import FieldName
from bluetti_bt_lib.registers import ReadableRegisters
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption


class StrictNotifyMock(ClientMockNoEncryption):
    """Refuses a second subscription while still notifying, like bleak-esphome."""

    def __init__(self):
        super().__init__()
        self.deaf = True

    async def start_notify(self, char, callback, **kwargs):
        if self._callback is not None:
            raise BleakError("Notifications are already enabled")
        await super().start_notify(char, callback, **kwargs)

    async def write_gatt_char(self, char, data, response=None):
        if not self.deaf:
            await super().write_gatt_char(char, data, response)


class TestCancelledRead(unittest.IsolatedAsyncioTestCase):
    def _reader(self, station):
        return DeviceReader("00:11:00:11:00:11", BaseDeviceV1(), asyncio.Future,
                            ble_client=station, lock=asyncio.Lock())

    async def test_a_read_cancelled_by_the_caller_does_not_wedge_the_reader(self):
        station = StrictNotifyMock()
        station.add_r_int(36, 10)
        reader = self._reader(station)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read([ReadableRegisters(36, 1)]), timeout=0.1)
        station.deaf = False
        data = await reader.read([ReadableRegisters(36, 1)])
        self.assertIsNotNone(data)
        self.assertEqual(data.get(FieldName.DC_INPUT_POWER.value), 10)

    async def test_forgetting_a_live_link_unsubscribes_first(self):
        station = StrictNotifyMock()
        station.deaf = False
        station.add_r_int(36, 10)
        reader = self._reader(station)
        self.assertIsNotNone(await reader.read([ReadableRegisters(36, 1)]))
        await reader._mark_disconnected()  # e.g. a BleakError while the link stayed up
        data = await reader.read([ReadableRegisters(36, 1)])
        self.assertEqual(data.get(FieldName.DC_INPUT_POWER.value), 10)


if __name__ == "__main__":
    unittest.main()
