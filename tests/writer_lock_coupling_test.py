import asyncio
import unittest
from types import SimpleNamespace

from bluetti_bt_lib.bluetooth import DeviceWriter
from bluetti_bt_lib.bluetooth.device_reader import DeviceReader
from bluetti_bt_lib.devices.ac180 import AC180
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption


class TestWriterLockCoupling(unittest.TestCase):
    """One link, one lock.

    A writer built on a reader drives that reader's connection, and _write()
    calls DeviceReader.read_locked() while holding its own polling_lock. That
    is only sound while the lock is the reader's: read_locked() exists because
    read() would otherwise take the same lock twice. A writer holding a
    different lock lets a poll and a write go out at the same time, and on an
    encrypted link the two then disagree about one AES stream.
    """

    def _reader(self, lock=None):
        return DeviceReader(
            "00:11:00:11:00:11",
            AC180(),
            asyncio.Future,
            lock=lock,
            ble_client=ClientMockNoEncryption(),
        )

    def test_a_writer_with_a_reader_adopts_the_readers_lock(self):
        reader = self._reader()
        reader_lock = reader.polling_lock

        writer = DeviceWriter(ClientMockNoEncryption(), AC180(), reader=reader)

        self.assertIs(writer.polling_lock, reader_lock)
        # Adopting is not swapping: the reader still serialises on its own lock.
        self.assertIs(reader.polling_lock, reader_lock)

    def test_the_readers_lock_may_be_passed_explicitly(self):
        lock = asyncio.Lock()
        reader = self._reader(lock=lock)

        writer = DeviceWriter(
            ClientMockNoEncryption(), AC180(), lock=lock, reader=reader
        )

        self.assertIs(writer.polling_lock, lock)
        self.assertIs(writer.polling_lock, reader.polling_lock)

    def test_a_different_lock_is_refused(self):
        reader = self._reader()

        with self.assertRaises(ValueError) as ctx:
            DeviceWriter(
                ClientMockNoEncryption(),
                AC180(),
                lock=asyncio.Lock(),
                reader=reader,
            )

        self.assertIn("one link, one lock", str(ctx.exception))

    def test_a_reader_without_a_polling_lock_is_a_type_error(self):
        # A caller who duck-types a reader would otherwise end up with a writer
        # whose writes are guarded by a lock nobody else knows about.
        with self.assertRaises(TypeError):
            DeviceWriter(ClientMockNoEncryption(), AC180(), reader=SimpleNamespace())

    def test_a_writer_with_no_reader_keeps_its_own_lock(self):
        # Standalone writers own the link, so they keep owning the lock.
        own = DeviceWriter(ClientMockNoEncryption(), AC180())
        given = DeviceWriter(ClientMockNoEncryption(), AC180(), lock=asyncio.Lock())

        self.assertIsInstance(own.polling_lock, asyncio.Lock)
        self.assertIsNot(own.polling_lock, given.polling_lock)


if __name__ == "__main__":
    unittest.main()
