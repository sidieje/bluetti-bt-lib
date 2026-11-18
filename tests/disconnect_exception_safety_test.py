"""Teardown must finish even when the BLE backend refuses to cooperate.

bleak only wraps its own failures in BleakError. A backend that has already
dropped the link answers stop_notify() and disconnect() with COMError, OSError
or RuntimeError instead, and both disconnect() implementations used to let
those escape. That aborted the teardown part way through, which left the
object reporting a live connection over a subscription that no longer existed:
the next connect() took its fast path and skipped the re-subscribe, and every
later disconnect() repeated the same failing call first, so it raised forever
and the writer could never be torn down at all.

Ownership is the part these tests hold fixed alongside the recovery: an
attached writer must not drop the reader's client or reset a borrowed session,
and a reader must not drop a client its caller supplied.
"""

import asyncio
import unittest
from unittest.mock import patch

from bleak.exc import BleakError
from bluetti_bt_lib import DeviceReader
from bluetti_bt_lib.bluetooth import DeviceWriter, DeviceWriterConfig
from bluetti_bt_lib.bluetooth.encryption import BluettiEncryption
from bluetti_bt_lib.base_devices import BaseDeviceV1
from bluetti_bt_lib.devices.ac180 import AC180
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption

ADDRESS = "00:11:00:11:00:11"


class Station:
    """GATT peer that can refuse each teardown call on demand."""

    address = ADDRESS

    def __init__(self):
        self.is_connected = True
        self.events = []
        self.callback = None
        self.fail_stop_notify = None
        self.fail_disconnect = None

    async def connect(self):
        self.events.append("connect")
        self.is_connected = True

    async def disconnect(self):
        self.events.append("disconnect")
        if self.fail_disconnect is not None:
            raise self.fail_disconnect
        self.is_connected = False

    async def start_notify(self, _uuid, callback):
        self.events.append("start_notify")
        self.callback = callback

    async def stop_notify(self, _uuid):
        self.events.append("stop_notify")
        if self.fail_stop_notify is not None:
            raise self.fail_stop_notify
        self.callback = None


async def handshake_ok(self):
    """Stand in for a completed key exchange; teardown is what is tested."""
    return True


def build_writer(station, timeout: int = 1) -> DeviceWriter:
    return DeviceWriter(
        station,
        AC180(),
        DeviceWriterConfig(timeout=timeout, use_encryption=True),
        asyncio.Lock(),
    )


class TestWriterDisconnectSurvivesBackendErrors(unittest.IsolatedAsyncioTestCase):
    async def connect_writer(self, station) -> DeviceWriter:
        writer = build_writer(station)
        with patch.object(DeviceWriter, "_await_handshake", handshake_ok):
            self.assertTrue(await writer.connect())
        self.assertTrue(writer.has_notifier)
        self.assertTrue(writer._owns_notifier)
        return writer

    async def test_a_refused_unsubscribe_still_finishes_the_teardown(self):
        station = Station()
        station.fail_stop_notify = RuntimeError("characteristic busy")
        writer = await self.connect_writer(station)

        await writer.disconnect()

        self.assertFalse(writer.has_notifier)
        self.assertFalse(writer._owns_notifier)
        self.assertFalse(writer._connected)
        self.assertFalse(writer.encryption_ready.is_set())
        # The link is still torn down even though the unsubscribe failed.
        self.assertFalse(station.is_connected)
        self.assertEqual(station.events, ["start_notify", "stop_notify", "disconnect"])

    async def test_a_refused_client_disconnect_still_finishes_the_teardown(self):
        station = Station()
        station.fail_disconnect = OSError("link already gone")
        writer = await self.connect_writer(station)

        await writer.disconnect()

        self.assertFalse(writer._connected)
        self.assertFalse(writer.has_notifier)
        self.assertFalse(writer._owns_notifier)
        self.assertFalse(writer.encryption_ready.is_set())

    async def test_disconnect_can_be_called_again_after_a_failure(self):
        # The stuck case this fixes: before, _connected was never cleared, so
        # every later disconnect() ran the same failing call and raised again.
        station = Station()
        station.fail_disconnect = RuntimeError("link already gone")
        writer = await self.connect_writer(station)

        await writer.disconnect()
        await writer.disconnect()

        self.assertFalse(writer._connected)

    async def test_a_reconnect_after_a_failed_disconnect_resubscribes(self):
        station = Station()
        station.fail_disconnect = RuntimeError("link already gone")
        writer = await self.connect_writer(station)

        await writer.disconnect()
        with patch.object(DeviceWriter, "_await_handshake", handshake_ok):
            self.assertTrue(await writer.connect())

        # Two subscriptions, not one: the second connect() may not reuse the
        # notifier the first teardown lost track of.
        self.assertEqual(station.events.count("start_notify"), 2)
        self.assertTrue(writer._owns_notifier)

    async def test_a_bleak_error_is_still_tolerated(self):
        station = Station()
        station.fail_stop_notify = BleakError("dropped")
        writer = await self.connect_writer(station)

        await writer.disconnect()

        self.assertFalse(writer._connected)
        self.assertFalse(station.is_connected)

    async def test_an_attached_writer_leaves_the_readers_link_alone(self):
        station = Station()
        writer = build_writer(station)
        session = BluettiEncryption()
        session.secure_aes_key = bytes(32)
        writer.attach(session)

        await writer.disconnect()

        # The client and the borrowed session both belong to the reader.
        self.assertTrue(station.is_connected)
        self.assertIs(writer.encryption, session)
        self.assertEqual(writer.encryption.secure_aes_key, bytes(32))
        self.assertNotIn("disconnect", station.events)
        self.assertNotIn("stop_notify", station.events)


class ExplodingMock(ClientMockNoEncryption):
    """Reader mock that refuses one of the two teardown calls."""

    def __init__(self, fail_on):
        super().__init__()
        self.fail_on = fail_on
        self.events = []

    async def stop_notify(self, char_specifier):
        self.events.append("stop_notify")
        if self.fail_on == "stop_notify":
            raise RuntimeError("characteristic busy")
        await super().stop_notify(char_specifier)

    async def disconnect(self):
        self.events.append("disconnect")
        if self.fail_on == "disconnect":
            raise OSError("link already gone")
        await super().disconnect()


class TestReaderDisconnectSurvivesBackendErrors(unittest.IsolatedAsyncioTestCase):
    async def connect_reader(self, ble_mock) -> DeviceReader:
        reader = DeviceReader(
            ADDRESS,
            BaseDeviceV1(),
            asyncio.Future,
            ble_client=ble_mock,
            lock=asyncio.Lock(),
        )
        self.assertTrue(await reader.connect())
        self.assertTrue(reader.has_notifier)
        return reader

    async def test_a_refused_unsubscribe_still_finishes_the_teardown(self):
        reader = await self.connect_reader(ExplodingMock("stop_notify"))
        reader._startup_values["warmup"] = 1

        await reader.disconnect()

        self.assertFalse(reader.has_notifier)
        self.assertIsNone(reader._notified_client)
        self.assertFalse(reader._connected)
        self.assertEqual(reader._startup_values, {})

    async def test_a_refused_client_disconnect_still_finishes_the_teardown(self):
        ble_mock = ExplodingMock("disconnect")
        reader = await self.connect_reader(ble_mock)
        # Stand in for the production case where the reader established the
        # client itself through establish_connection() rather than borrowing
        # one from the caller.
        reader.ble_client = None

        await reader.disconnect()

        self.assertIn("disconnect", ble_mock.events)
        self.assertFalse(reader._connected)
        self.assertFalse(reader.has_notifier)

    async def test_a_supplied_client_is_still_left_to_its_owner(self):
        ble_mock = ExplodingMock("none")
        reader = await self.connect_reader(ble_mock)

        await reader.disconnect()

        self.assertNotIn("disconnect", ble_mock.events)
        self.assertTrue(ble_mock.is_connected)
        self.assertFalse(reader._connected)


if __name__ == "__main__":
    unittest.main()
