import asyncio
import struct
import unittest
from unittest.mock import patch

from crcmod.predefined import mkCrcFun

from bluetti_bt_lib.bluetooth import DeviceWriter, DeviceWriterConfig, device_writer
from bluetti_bt_lib.bluetooth.encryption import BluettiEncryption
from bluetti_bt_lib.const import NOTIFY_UUID
from bluetti_bt_lib.devices.ac180 import AC180
from bluetti_bt_lib.fields import FieldName
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption

modbus_crc = mkCrcFun("modbus")


def open_session(writer):
    """Stand-in for a completed key exchange."""
    writer.encryption.unsecure_aes_key = bytes(16)
    writer.encryption.unsecure_aes_iv = bytes(16)
    writer.encryption.secure_aes_key = bytes(32)


class EncryptedStation:
    """Answers each encrypted write with `answer(request)`, encrypted.

    Frames in `in_flight` are still on their way over the link: they land ahead
    of the next write's answer unless the link drops first.
    """

    address = "00:11:00:11:00:11"
    is_connected = True

    def __init__(self, answer):
        self.answer = answer
        self.writer = None
        self.in_flight = []

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False
        self.in_flight = []

    async def start_notify(self, _uuid, _callback):
        open_session(self.writer)  # every new link gets a key exchange
        self.writer.encryption_ready.set()

    async def write_gatt_char(self, _uuid, data, response=None):
        s = self.writer.encryption
        request = s.aes_decrypt(bytes(data), s.secure_aes_key, None)
        frames, self.in_flight = self.in_flight, []
        for frame in frames + self.answer(bytes(request)):
            await self.writer._notification_handler(0, bytearray(s.aes_encrypt(frame, s.secure_aes_key, None)))


def refuse(request):
    frame = bytearray([request[0], request[1] | 0x80, 0x03])
    return [bytes(frame + struct.pack("<H", modbus_crc(bytes(frame))))]


def echo(request):
    return [request[:8]]


def stray(request):
    frame = bytearray([request[0], 0x03, 0x02, 0x00, 0x07])  # a late read answer
    return [bytes(frame + struct.pack("<H", modbus_crc(bytes(frame))))]


class TestStandaloneWriterAck(unittest.IsolatedAsyncioTestCase):
    def _writer(self, answer):
        station = EncryptedStation(answer)
        writer = DeviceWriter(station, AC180(), DeviceWriterConfig(timeout=10, use_encryption=True), asyncio.Lock())
        station.writer = writer
        open_session(writer)
        writer._owns_notifier = writer.has_notifier = writer._connected = True
        return writer

    async def test_an_exception_answer_is_not_success(self):
        self.assertFalse(await self._writer(refuse).write(FieldName.CTRL_AC.value, True))

    async def test_the_echo_is_success(self):
        self.assertTrue(await self._writer(echo).write(FieldName.CTRL_AC.value, True))

    async def test_a_stray_frame_does_not_pass_for_the_ack(self):
        with patch.object(device_writer, "WRITE_ACK_TIMEOUT_SECONDS", 0.1):
            self.assertFalse(await self._writer(stray).write(FieldName.CTRL_AC.value, True))

    async def test_a_late_refusal_does_not_fail_the_next_write(self):
        writer = self._writer(echo)
        station = writer.client

        def refuse_late(request):
            # Still on its way when the writer gives up waiting for it.
            station.in_flight += refuse(request)
            station.answer = echo
            return []

        station.answer = refuse_late
        with patch.object(device_writer, "WRITE_ACK_TIMEOUT_SECONDS", 0.1):
            self.assertFalse(await writer.write(FieldName.CTRL_AC.value, True))
            self.assertTrue(await writer.write(FieldName.CTRL_DC.value, True))


class TestWriterConnection(unittest.IsolatedAsyncioTestCase):
    async def test_an_attached_writer_does_not_bring_the_shared_link_back(self):
        station = ClientMockNoEncryption()
        session = BluettiEncryption()
        session.secure_aes_key = bytes(32)
        session.peer_pubkey = object()
        writer = DeviceWriter(station, AC180(), DeviceWriterConfig(timeout=5, use_encryption=True), asyncio.Lock())
        writer.attach(session)
        await station.disconnect()  # the shared link dropped
        await writer._mark_disconnected()  # the previous write saw the BleakError
        self.assertFalse(await writer.write(FieldName.CTRL_AC.value, True))
        self.assertFalse(station.is_connected)

    async def test_an_attached_writer_stays_off_the_shared_link_after_disconnect(self):
        station = ClientMockNoEncryption()

        async def reader_handler(*_):
            pass

        await station.start_notify(NOTIFY_UUID, reader_handler)  # the reader's subscription
        session = BluettiEncryption()
        session.secure_aes_key = bytes(32)
        session.peer_pubkey = object()
        writer = DeviceWriter(station, AC180(), DeviceWriterConfig(timeout=1, use_encryption=True), asyncio.Lock())
        writer.attach(session)
        await writer.disconnect()
        written = await writer.write(FieldName.CTRL_AC.value, True)
        self.assertIs(station._callback, reader_handler)
        self.assertTrue(station.is_connected)
        self.assertTrue(written)

    async def test_reconnect_after_disconnect_redoes_the_key_exchange(self):
        station = ClientMockNoEncryption()
        writer = DeviceWriter(station, AC180(), DeviceWriterConfig(timeout=5, use_encryption=True), asyncio.Lock())
        subscriptions = 0
        original = station.start_notify

        async def start_notify_completes_handshake(char, callback, **kwargs):
            nonlocal subscriptions
            subscriptions += 1
            await original(char, callback, **kwargs)
            writer.encryption.secure_aes_key = b"0" * 16
            writer.encryption_ready.set()

        station.start_notify = start_notify_completes_handshake
        self.assertTrue(await writer.connect())
        await writer.disconnect()
        self.assertTrue(await writer.connect())
        self.assertEqual(subscriptions, 2)
        self.assertIsNotNone(writer.encryption.secure_aes_key)


if __name__ == "__main__":
    unittest.main()
