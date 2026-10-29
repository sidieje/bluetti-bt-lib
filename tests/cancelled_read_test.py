import asyncio
import contextlib
import unittest
from unittest.mock import patch

from bleak.exc import BleakError

from bluetti_bt_lib import DeviceReader
from bluetti_bt_lib.base_devices import BaseDeviceV1
from bluetti_bt_lib.bluetooth import device_reader
from bluetti_bt_lib.bluetooth.device_reader import DeviceReaderConfig
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


class LateStation(ClientMockNoEncryption):
    """Answers every read ``latency`` seconds later, to whoever is subscribed by
    then. An unsubscribe takes ``unsub_delay`` and only counts once it is done."""

    def __init__(self, latency, unsub_delay=0.0):
        super().__init__()
        self.latency = latency
        self.unsub_delay = unsub_delay
        self.pending = []

    async def write_gatt_char(self, char, data, response=None):
        answers = []

        async def keep(_char, frame):
            answers.append(bytes(frame))

        subscriber, self._callback = self._callback, keep
        await super().write_gatt_char(char, data, response)
        self._callback = subscriber
        self.pending.append(asyncio.create_task(self._answer(char, answers)))

    async def _answer(self, char, answers):
        await asyncio.sleep(self.latency)
        for frame in answers:
            if self._callback is not None:
                await self._callback(char, bytearray(frame))

    async def stop_notify(self, char):
        await asyncio.sleep(self.unsub_delay)
        await super().stop_notify(char)


# A read cut short resyncs, and the next one waits out a late answer first.
FAST = (
    patch.object(device_reader, "RESPONSE_TIMEOUT_SECONDS", 0.5),
    patch.object(device_reader, "STRAY_ANSWER_SECONDS", 0.1),
)


@FAST[0]
@FAST[1]
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


@FAST[0]
@FAST[1]
class TestCutShortRead(unittest.IsolatedAsyncioTestCase):
    """A read cut short mid-request leaves its answer on the way; the next read
    must not take it for its own (36 and 37 have answers of the same size)."""

    def _reader(self, station, timeout=60):
        station.add_r_int(36, 10)
        station.add_r_int(37, 99)
        return DeviceReader("00:11:00:11:00:11", BaseDeviceV1(), asyncio.Future,
                            DeviceReaderConfig(timeout=timeout), ble_client=station)

    async def test_the_readers_own_timeout_resyncs(self):
        reader = self._reader(LateStation(latency=0.3), timeout=0.2)
        self.assertIsNone(await reader.read([ReadableRegisters(36, 1)]))
        reader.config.timeout = 60
        data = await reader.read([ReadableRegisters(37, 1)])
        self.assertEqual(data, {FieldName.AC_INPUT_POWER.value: 99})

    async def test_a_second_cancel_during_the_resync_still_reconnects(self):
        reader = self._reader(LateStation(latency=0.3, unsub_delay=0.5))
        task = asyncio.create_task(reader.read([ReadableRegisters(36, 1)]))
        await asyncio.sleep(0.1)  # 36 is in flight
        task.cancel()
        await asyncio.sleep(0.05)  # the handler is unsubscribing now
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self.assertFalse(reader._connected)
        data = await reader.read([ReadableRegisters(37, 1)])
        self.assertEqual(data, {FieldName.AC_INPUT_POWER.value: 99})


if __name__ == "__main__":
    unittest.main()
