import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bluetti_bt_lib import DeviceReader
from bluetti_bt_lib.base_devices import BaseDeviceV1
from bluetti_bt_lib.bluetooth import device_reader
from bluetti_bt_lib.bluetooth.device_reader import DeviceReaderConfig
from bluetti_bt_lib.devices import AC60, AC300
from bluetti_bt_lib.registers import ReadableRegisters
from bluetti_bt_lib.utils.bleak_client_mock import BleakClientMock, ClientMockNoEncryption

from response_validation_test import PACK_NUM_REGISTER, ScriptedMock

STATION = SimpleNamespace(name="AC60", address="00:11:00:11:00:11")
READ = [ReadableRegisters(PACK_NUM_REGISTER, 1)]
DC_IN, AC_IN = 36, 37  # dc_input_power, ac_input_power: same-size answers


class LateStation(ClientMockNoEncryption):
    """Answers each command after the next of ``delays`` (None drops it), to
    whoever is subscribed by then."""

    def __init__(self, delays):
        super().__init__()
        self.delays = list(delays)
        self.pending = []

    async def write_gatt_char(self, char, data, response=None) -> None:
        answers = []

        async def keep(_char, frame):
            answers.append(bytes(frame))

        subscriber, self._callback = self._callback, keep
        await super().write_gatt_char(char, data, response)
        self._callback = subscriber
        delay = self.delays.pop(0) if self.delays else 0
        if delay is not None:
            self.pending.append(asyncio.create_task(self._answer(char, answers, delay)))

    async def _answer(self, char, answers, delay):
        await asyncio.sleep(delay)
        for frame in answers:
            if self._callback is not None:
                await self._callback(char, bytearray(frame))


class DeafOnWrite(ScriptedMock):
    """Answers reads until it is sent a write (function 06), then goes silent."""

    async def write_gatt_char(self, char, data, response=None) -> None:
        if bytes(data)[1] == 6:
            self.deaf = True
        await super().write_gatt_char(char, data, response)


class TestResumeKeptLink(unittest.IsolatedAsyncioTestCase):
    def _patches(self, find, establish):
        return (
            patch.object(device_reader.BleakScanner, "find_device_by_address", find),
            patch.object(device_reader, "establish_connection", establish),
            patch.object(device_reader, "RESPONSE_TIMEOUT_SECONDS", 0.05),
            patch.object(device_reader, "RETRY_PAUSE_SECONDS", 0),
            patch.object(device_reader, "STRAY_ANSWER_SECONDS", 0.1),
        )

    async def test_a_resync_resumes_the_link_it_kept(self):
        # A connected station that stops advertising: only the first scan sees it.
        station = ScriptedMock(deaf=True)
        station.add_r_int(PACK_NUM_REGISTER, 4)
        find = AsyncMock(side_effect=[STATION, None])
        establish = AsyncMock(return_value=station)
        p1, p2, p3, p4, p5 = self._patches(find, establish)
        with p1, p2, p3, p4, p5:
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
        p1, p2, p3, p4, p5 = self._patches(find, establish)
        with p1, p2, p3, p4, p5:
            reader = DeviceReader(STATION.address, AC60(), asyncio.get_running_loop().create_future)
            for _ in range(device_reader.MAX_RESYNCS_ON_ONE_LINK + 1):
                self.assertIsNone(await reader.read_locked(READ, raw=True))
        self.assertEqual(establish.await_count, 2)
        self.assertFalse(stations[0].is_connected)

    async def test_a_late_answer_to_the_abandoned_request_is_not_taken(self):
        # The second ask for 36 is answered 0.05 s after the read gave up on
        # it, while the resumed link is waiting for 37's answer (due 0.1 s in).
        station = LateStation([None, 0.25, 0.1])
        station.add_r_int(DC_IN, 10)
        station.add_r_int(AC_IN, 99)
        find = AsyncMock(return_value=STATION)
        establish = AsyncMock(return_value=station)
        p1, p2, p3, p4, p5 = self._patches(find, establish)
        with p1, p2, p3, p4, p5, patch.object(device_reader, "RESPONSE_TIMEOUT_SECONDS", 0.2):
            reader = DeviceReader(STATION.address, BaseDeviceV1(), asyncio.get_running_loop().create_future)
            self.assertIsNone(await reader.read([ReadableRegisters(DC_IN, 1)]))
            data = await reader.read([ReadableRegisters(AC_IN, 1)])
        self.assertEqual(data, {"ac_input_power": 99})
        self.assertEqual(establish.await_count, 1)

    async def test_the_cycle_that_drops_the_link_still_reports_the_startup_fields(self):
        first, second = ScriptedMock(), ScriptedMock()
        for station in (first, second):
            station.add_r_int(DC_IN, 10)
            station.add_r_sn(17, 1234567)
        current = [first]
        find = AsyncMock(return_value=STATION)
        establish = AsyncMock(side_effect=lambda *a, **k: current[0])
        p1, p2, p3, p4, p5 = self._patches(find, establish)
        with p1, p2, p3, p4, p5:
            reader = DeviceReader(STATION.address, BaseDeviceV1(), asyncio.get_running_loop().create_future)
            self.assertEqual((await reader.read())["device_sn"], 1234567)
            first.deaf = True
            for _ in range(device_reader.MAX_RESYNCS_ON_ONE_LINK):
                self.assertIsNone(await reader.read())
            current[0] = second
            data = await reader.read()
        self.assertEqual(data["device_sn"], 1234567)

    async def test_a_new_link_asks_for_the_startup_fields_again(self):
        first, second = ScriptedMock(), ScriptedMock()
        first.add_r_sn(17, 111)
        second.add_r_sn(17, 222)
        current = [first]
        find = AsyncMock(return_value=STATION)
        establish = AsyncMock(side_effect=lambda *a, **k: current[0])
        p1, p2, p3, p4, p5 = self._patches(find, establish)
        with p1, p2, p3, p4, p5:
            reader = DeviceReader(STATION.address, BaseDeviceV1(), asyncio.get_running_loop().create_future)
            self.assertEqual((await reader.read())["device_sn"], 111)
            first.deaf = True
            self.assertIsNone(await reader.read())  # resync, link kept
            await first.disconnect()  # then the station drops it
            current[0] = second
            data = await reader.read()
        self.assertEqual(data["device_sn"], 222)

    async def test_one_failed_pack_walk_counts_one_resync(self):
        # The selector restore resyncs, then read() resyncs again.
        station = DeafOnWrite()
        station.add_r_int(96, 1)  # one pack installed
        station.add_r_int(3006, 1)  # selector on slot 1, so it gets restored
        find = AsyncMock(return_value=STATION)
        establish = AsyncMock(return_value=station)
        p1, p2, p3, p4, p5 = self._patches(find, establish)
        with p1, p2, p3, p4, p5:
            reader = DeviceReader(STATION.address, AC300(), asyncio.get_running_loop().create_future)
            self.assertIsNone(await reader.read())
        self.assertEqual(reader._resyncs, 1)


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

    async def test_a_supplied_client_its_owner_reconnects_needs_a_new_session(self):
        client = BleakClientMock()
        reader = DeviceReader(STATION.address, BaseDeviceV1(), asyncio.Future,
                              config=DeviceReaderConfig(use_encryption=True), ble_client=client)
        reader.client = reader._notified_client = client
        reader.has_notifier = True
        reader._connected = True
        reader.encryption.secure_aes_key = bytes(32)
        reader.encryption.peer_pubkey = object()
        await client.disconnect()  # the link died while a request waited
        await reader._resync_link()
        await client.connect()  # the owner brings it back up
        with patch.object(device_reader, "HANDSHAKE_TIMEOUT_SECONDS", 0):
            connected = await reader.connect()
        self.assertFalse(connected)

    async def test_a_kept_link_whose_session_was_lost_is_not_resumed(self):
        kept, fresh = BleakClientMock(), BleakClientMock()
        reader = DeviceReader(STATION.address, BaseDeviceV1(), asyncio.Future,
                              config=DeviceReaderConfig(use_encryption=True))
        reader.client = reader._notified_client = kept
        reader.has_notifier = True
        reader._connected = True
        await reader._resync_link()  # the session is already gone (never set here)
        scanner = MagicMock()
        scanner.find_device_by_address = AsyncMock(return_value=STATION)
        establish = AsyncMock(return_value=fresh)
        with patch.object(device_reader, "BleakScanner", scanner), \
             patch.object(device_reader, "establish_connection", establish), \
             patch.object(device_reader, "HANDSHAKE_TIMEOUT_SECONDS", 0):
            await reader.connect()
        # Waiting on the kept link would only time out: the peer starts a new
        # exchange on a new link.
        self.assertEqual(establish.await_count, 1)
        self.assertFalse(kept.is_connected)


if __name__ == "__main__":
    unittest.main()
