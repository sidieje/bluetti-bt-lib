"""Interpreter control flow is not a link fault.

The connect and read bodies used to end in ``except BaseException``, which also
caught KeyboardInterrupt and SystemExit (reported as a failed connection, and
the process kept running) and GeneratorExit (whose handler then awaited
teardown, turning the shutdown into "coroutine ignored GeneratorExit"). They
now catch ``Exception``; asyncio.CancelledError keeps its own handler, because
cancelling a task *is* expected here and does need the cleanup.
"""

import asyncio
import inspect
import unittest
from unittest.mock import AsyncMock

from bluetti_bt_lib import DeviceReader
from bluetti_bt_lib.base_devices import BaseDeviceV1
from bluetti_bt_lib.bluetooth import DeviceWriter
from bluetti_bt_lib.bluetooth.device_reader import DeviceReaderConfig
from bluetti_bt_lib.bluetooth.device_writer import DeviceWriterConfig
from bluetti_bt_lib.devices.ac180 import AC180
from bluetti_bt_lib.registers import ReadableRegisters
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption

MAC = "00:11:00:11:00:11"
CONTROL_FLOW = (KeyboardInterrupt, SystemExit)


async def _never_resumes(*_args, **_kwargs) -> None:
    """Suspend the caller until its coroutine is closed."""
    await asyncio.Event().wait()


class ReaderConnectTests(unittest.IsolatedAsyncioTestCase):
    """DeviceReader.connect(), with the link already up so the resume branch
    is taken and the first thing awaited is the notify subscription."""

    def _reader(self):
        reader = DeviceReader(
            MAC,
            BaseDeviceV1(),
            asyncio.Future,
            DeviceReaderConfig(timeout=1, use_encryption=False),
        )
        station = ClientMockNoEncryption()
        # No ble_client: the reader owns this link, so the cleanup of the
        # failure paths is observable. ble_client would be left to its owner.
        reader.client = station
        return reader, station

    async def test_keyboard_interrupt_and_system_exit_propagate(self):
        for err in CONTROL_FLOW:
            with self.subTest(err=err.__name__):
                reader, station = self._reader()
                station.start_notify = AsyncMock(side_effect=err)
                with self.assertRaises(err):
                    await reader.connect()
                self.assertFalse(reader._connected)

    async def test_cancellation_still_cleans_up_and_propagates(self):
        reader, station = self._reader()
        station.start_notify = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await reader.connect()
        self.assertFalse(reader._connected)
        self.assertFalse(reader.has_notifier)
        self.assertFalse(station.is_connected)

    async def test_closing_a_live_connect_does_not_ignore_generator_exit(self):
        """The shutdown that used to raise: close a connect() that is
        suspended inside the guarded body, as discarding its task does."""
        reader, station = self._reader()
        station.start_notify = AsyncMock(side_effect=_never_resumes)
        connect = reader.connect()
        connect.send(None)  # runs until the subscription suspends
        self.assertEqual(inspect.getcoroutinestate(connect), inspect.CORO_SUSPENDED)
        try:
            connect.close()
        except RuntimeError as err:  # pragma: no cover - only pre-fix
            self.fail(f"connect() awaited during shutdown: {err}")
        self.assertFalse(reader._connected)


class ReaderReadTests(unittest.IsolatedAsyncioTestCase):
    """read() and read_locked() report an unknown error as a missing answer;
    an interrupted read is not one."""

    def _reader(self):
        reader = DeviceReader(
            MAC,
            BaseDeviceV1(),
            asyncio.Future,
            DeviceReaderConfig(timeout=1, use_encryption=False),
        )
        station = ClientMockNoEncryption()
        reader.client = station
        reader._connected = True
        reader._notified_client = station
        reader._async_send_command = AsyncMock()
        return reader

    async def test_keyboard_interrupt_and_system_exit_propagate_from_read(self):
        for err in CONTROL_FLOW:
            with self.subTest(err=err.__name__):
                reader = self._reader()
                reader._async_send_command.side_effect = err
                with self.assertRaises(err):
                    await reader.read([ReadableRegisters(36, 1)])

    async def test_keyboard_interrupt_and_system_exit_propagate_from_read_locked(
        self,
    ):
        for err in CONTROL_FLOW:
            with self.subTest(err=err.__name__):
                reader = self._reader()
                reader._async_send_command.side_effect = err
                async with reader.polling_lock:
                    with self.assertRaises(err):
                        await reader.read_locked([ReadableRegisters(36, 1)], raw=True)


class WriterConnectTests(unittest.IsolatedAsyncioTestCase):
    """Standalone DeviceWriter.connect(), interrupted during the key exchange."""

    def _writer(self):
        station = ClientMockNoEncryption()
        writer = DeviceWriter(
            station, AC180(), DeviceWriterConfig(timeout=4, use_encryption=True)
        )
        writer._await_handshake = AsyncMock()
        return writer, station

    async def test_keyboard_interrupt_and_system_exit_propagate(self):
        for err in CONTROL_FLOW:
            with self.subTest(err=err.__name__):
                writer, station = self._writer()
                writer._await_handshake.side_effect = err
                with self.assertRaises(err):
                    await writer.connect()
                # Deliberate: the teardown that used to run here is an await
                # inside an interruption, which is what corrupted the
                # shutdown. A caller that catches these closes the link.
                self.assertFalse(writer._connected)
                self.assertTrue(station.is_connected)

    async def test_cancellation_still_cleans_up_and_propagates(self):
        writer, station = self._writer()
        writer._await_handshake.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await writer.connect()
        self.assertFalse(writer._connected)
        self.assertFalse(writer.has_notifier)
        self.assertFalse(writer._owns_notifier)
        self.assertFalse(station.is_connected)

    async def test_closing_a_live_connect_does_not_ignore_generator_exit(self):
        writer, station = self._writer()
        writer._await_handshake.side_effect = _never_resumes
        connect = writer.connect()
        connect.send(None)  # runs until the handshake suspends
        self.assertEqual(inspect.getcoroutinestate(connect), inspect.CORO_SUSPENDED)
        try:
            connect.close()
        except RuntimeError as err:  # pragma: no cover - only pre-fix
            self.fail(f"connect() awaited during shutdown: {err}")
        self.assertFalse(writer._connected)


if __name__ == "__main__":
    unittest.main()
