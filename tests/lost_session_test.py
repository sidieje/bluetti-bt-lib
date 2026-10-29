import asyncio
import unittest
from unittest.mock import patch

from bluetti_bt_lib import DeviceReader
from bluetti_bt_lib.bluetooth import device_reader
from bluetti_bt_lib.bluetooth.device_reader import DeviceReaderConfig
from bluetti_bt_lib.devices import AC60, AC300
from bluetti_bt_lib.registers import ReadableRegisters

from response_validation_test import PACK_NUM_REGISTER, ScriptedMock


class TestLostSession(unittest.IsolatedAsyncioTestCase):
    def _reader(self, station, device=None):
        reader = DeviceReader("00:11:00:11:00:11", device or AC60(), asyncio.get_running_loop().create_future,
                              DeviceReaderConfig(use_encryption=True), ble_client=station)
        reader.client = station
        # What the InvalidSignature branch of the notification handler leaves behind.
        reader.encryption.reset()
        return reader

    async def test_no_session_is_a_missing_answer_not_an_empty_one(self):
        station = ScriptedMock()
        station.add_r_int(PACK_NUM_REGISTER, 4)
        reader = self._reader(station)
        with patch.object(device_reader, "RETRY_PAUSE_SECONDS", 0):
            with self.assertRaises(device_reader.ResponseTimeout):
                await reader._async_send_command(ReadableRegisters(PACK_NUM_REGISTER, 1))
        self.assertIsNone(reader.notify_future)
        self.assertEqual(station.commands, [])  # nothing sent that could never be answered
        # Counted as the cycle's abort, and not asked for a second time.
        self.assertEqual(reader.link_stats.failure, "session")
        self.assertTrue(reader._cycle_aborted)
        self.assertEqual(reader.link_stats.retried, 0)

    async def test_a_timeout_on_a_lost_session_drops_the_link(self):
        station = ScriptedMock()
        reader = self._reader(station)
        reader.has_notifier = True
        await reader._recover_from_timeout()
        # Only a new connection makes the peer issue a fresh challenge.
        self.assertFalse(station.is_connected)
        self.assertEqual(station.unsubscribed, 1)

    async def test_a_lost_session_during_the_selector_restore_drops_the_link(self):
        # The last pack answered, then the session went: the restore cannot be
        # sent, and resyncing would leave the next connect() waiting out the
        # whole handshake timeout on a link that will never start one.
        station = ScriptedMock()
        device = AC300()
        reader = self._reader(station, device)
        reader.has_notifier = True
        await reader._restore_pack_selector(device.get_pack_selector_register(), 1)
        self.assertEqual(station.commands, [])
        self.assertFalse(station.is_connected)


if __name__ == "__main__":
    unittest.main()
