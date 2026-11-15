"""Regression tests for the BLE cleanup in bluetti-write (audit finding H1).

The script builds the BleakClient itself, so it owns the disconnect.  It used
to disconnect only when ``reader.connect()`` returned false, which left the
powerstation connected -- and therefore not advertising -- after a BleakError
during setup, a failed write, a successful write, or any exception.

The cleanup splits three ways: the reader stops its notification subscription,
the writer clears its own state, and the script drops the client.  Neither
library component drops a caller-supplied client, so the script has to do it on
every path.
"""

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from bluetti_bt_lib.scripts import bluetti_write

MAC = "00:11:22:33:44:55"

# Patched per test.  Holders keep the fakes free of constructor plumbing, since
# the script builds both of them positionally.
CONNECT_OK = SimpleNamespace(value=True)
CONNECT_ERROR = SimpleNamespace(value=None)
WRITE_ERROR = SimpleNamespace(value=None)
READER_ERROR = SimpleNamespace(value=None)
READER_CONNECT_ERROR = SimpleNamespace(value=None)
WRITER_ERROR = SimpleNamespace(value=None)
CLIENT_ERROR = SimpleNamespace(value=None)


class FakeClient:
    """Stand-in for BleakClient that records the connection lifecycle."""

    def __init__(self, address, events):
        self.address = address
        self.events = events
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self):
        if CONNECT_ERROR.value is not None:
            raise CONNECT_ERROR.value
        self._connected = True
        self.events.append("client.connect")

    async def disconnect(self):
        self.events.append("client.disconnect")
        if CLIENT_ERROR.value is not None:
            raise CLIENT_ERROR.value
        self._connected = False


class FakeReader:
    """Stands in for DeviceReader on the shared-link path."""

    events = []

    def __init__(self, address, device, future_builder, config, **kwargs):
        self.encryption = object()
        self.lock = kwargs.get("lock")
        self.events = FakeReader.events

    async def connect(self):
        if READER_CONNECT_ERROR.value is not None:
            raise READER_CONNECT_ERROR.value
        return CONNECT_OK.value

    async def disconnect(self):
        # Records whether the script still holds polling_lock here.  Both
        # disconnect() methods take it, so cleanup written inside the connect
        # block would deadlock; the test asserts this stays "free".
        held = self.lock is not None and self.lock.locked()
        self.events.append("reader.lock_held" if held else "reader.lock_free")
        self.events.append("reader.disconnect")
        if READER_ERROR.value is not None:
            raise READER_ERROR.value


class FakeWriter:
    """Stands in for DeviceWriter on either path."""

    def __init__(self, client, device, config, lock=None, reader=None):
        self.client = client
        self.events = FakeReader.events
        self.attached = False

    def attach(self, encryption):
        self.attached = True

    async def write(self, field, value):
        # The real DeviceWriter connects its own client from _write() when it
        # is not attached, so the standalone path ends up with a live link.
        if not self.client.is_connected:
            await self.client.connect()
        self.events.append("writer.write")
        if WRITE_ERROR.value is not None:
            raise WRITE_ERROR.value
        return True

    async def disconnect(self):
        self.events.append("writer.disconnect")
        if WRITER_ERROR.value is not None:
            raise WRITER_ERROR.value


class TestWriteConnectionCleanup(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeReader.events = []
        CONNECT_OK.value = True
        CONNECT_ERROR.value = None
        WRITE_ERROR.value = None
        READER_ERROR.value = None
        READER_CONNECT_ERROR.value = None
        WRITER_ERROR.value = None
        CLIENT_ERROR.value = None

    async def run_write(self, needs_reader=True):
        """Runs async_write() against fakes and records the event log."""
        events = FakeReader.events
        client = FakeClient(MAC, events)
        device = SimpleNamespace(requires_read_modify_write=lambda field: needs_reader)

        out = io.StringIO()
        # Recorded before the await: the writer-raise case leaves async_write()
        # by exception, and the cleanup still has to be inspectable.
        self.client = client
        self.events = events
        with (
            patch.object(bluetti_write, "BleakClient", lambda a: client),
            patch.object(bluetti_write, "build_device", lambda _: device),
            patch.object(bluetti_write, "DeviceReader", FakeReader),
            patch.object(bluetti_write, "DeviceWriter", FakeWriter),
            redirect_stdout(out),
        ):
            await bluetti_write.async_write(MAC, "EB3A", False, "ctrl_ac", True)

        self.output = out.getvalue()

    def assert_disconnected_once(self):
        self.assertFalse(self.client.is_connected)
        self.assertEqual(self.events.count("client.disconnect"), 1)

    async def test_successful_write_disconnects(self):
        for needs_reader in (True, False):
            with self.subTest(shared_reader=needs_reader):
                self.setUp()
                await self.run_write(needs_reader=needs_reader)
                self.assertIn("writer.write", self.events)
                self.assert_disconnected_once()

    async def test_reader_connect_failure_disconnects(self):
        CONNECT_OK.value = False
        await self.run_write()
        self.assertNotIn("writer.write", self.events)
        self.assert_disconnected_once()

    async def test_bleak_error_during_setup_disconnects(self):
        # The handler used to return without disconnecting even though
        # client.connect() had already succeeded, so the branch leaked despite
        # looking like it cleaned up.
        READER_CONNECT_ERROR.value = bluetti_write.BleakError("notify failed")
        await self.run_write()
        self.assertNotIn("writer.write", self.events)
        self.assert_disconnected_once()

    async def test_client_connect_failure_leaves_nothing_connected(self):
        # Nothing ever connected, so there is no link to drop -- but the script
        # must still unwind cleanly rather than raise out of the finally.
        CONNECT_ERROR.value = bluetti_write.BleakError("no radio")
        await self.run_write()
        self.assertFalse(self.client.is_connected)
        self.assertNotIn("writer.write", self.events)
        self.assertEqual(self.events.count("client.disconnect"), 0)

    async def test_writer_exception_disconnects(self):
        for needs_reader in (True, False):
            with self.subTest(shared_reader=needs_reader):
                self.setUp()
                WRITE_ERROR.value = RuntimeError("write failed")
                with self.assertRaises(RuntimeError):
                    await self.run_write(needs_reader=needs_reader)
                self.assert_disconnected_once()

    async def test_reader_cleanup_failure_still_cleans_up(self):
        READER_ERROR.value = RuntimeError("stop_notify failed")
        await self.run_write()
        self.assertIn("writer.disconnect", self.events)
        self.assert_disconnected_once()

    async def test_writer_cleanup_failure_still_disconnects(self):
        WRITER_ERROR.value = RuntimeError("disconnect failed")
        await self.run_write()
        self.assert_disconnected_once()

    async def test_client_cleanup_failure_is_reported_not_raised(self):
        # Nothing is left to fall back on, but a stuck link must not turn into
        # a traceback from the finally block.
        CLIENT_ERROR.value = RuntimeError("link stuck")
        await self.run_write()
        self.assertIn("Error disconnecting client", self.output)

    async def test_cleanup_order_is_reader_writer_client(self):
        await self.run_write()
        cleanup = [
            event
            for event in self.events
            if event
            in ("reader.disconnect", "writer.disconnect", "client.disconnect")
        ]
        # stop_notify needs a live link, so the client has to go down last.
        self.assertEqual(
            cleanup,
            ["reader.disconnect", "writer.disconnect", "client.disconnect"],
        )

    async def test_cleanup_runs_outside_the_polling_lock(self):
        await self.run_write()
        self.assertIn("reader.lock_free", self.events)
        self.assertNotIn("reader.lock_held", self.events)

    async def test_unsupported_type_builds_nothing(self):
        with (
            patch.object(bluetti_write, "BleakClient", lambda a: FakeClient(a, [])),
            patch.object(bluetti_write, "build_device", lambda _: None),
        ):
            await bluetti_write.async_write(MAC, "NOPE", False, "ctrl_ac", True)
        self.assertEqual(FakeReader.events, [])


if __name__ == "__main__":
    unittest.main()
