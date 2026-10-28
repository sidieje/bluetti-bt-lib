import asyncio
import struct
import unittest

from crcmod.predefined import mkCrcFun

from bluetti_bt_lib.bluetooth import DeviceWriter, DeviceWriterConfig
from bluetti_bt_lib.bluetooth.encryption import BluettiEncryption
from bluetti_bt_lib.devices.ac180 import AC180
from bluetti_bt_lib.fields import FieldName
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption

modbus_crc = mkCrcFun("modbus")


class EncryptedStation:
    """Answers each encrypted write with `answer(request)`, encrypted."""

    address = "00:11:00:11:00:11"
    is_connected = True

    def __init__(self, answer):
        self.answer = answer
        self.writer = None

    async def write_gatt_char(self, _uuid, data, response=None):
        s = self.writer.encryption
        request = s.aes_decrypt(bytes(data), s.secure_aes_key, None)
        for frame in self.answer(bytes(request)):
            await self.writer._notification_handler(0, bytearray(s.aes_encrypt(frame, s.secure_aes_key, None)))


def refuse(request):
    frame = bytearray([request[0], request[1] | 0x80, 0x03])
    return [bytes(frame + struct.pack("<H", modbus_crc(bytes(frame))))]


def echo(request):
    return [request[:8]]


def stray_then_echo(request):
    frame = bytearray([request[0], 0x03, 0x02, 0x00, 0x07])  # a late read answer
    return [bytes(frame + struct.pack("<H", modbus_crc(bytes(frame)))), request[:8]]


class TestStandaloneWriterAck(unittest.IsolatedAsyncioTestCase):
    def _writer(self, answer):
        station = EncryptedStation(answer)
        writer = DeviceWriter(station, AC180(), DeviceWriterConfig(timeout=10, use_encryption=True), asyncio.Lock())
        station.writer = writer
        writer.encryption.unsecure_aes_key = bytes(16)
        writer.encryption.unsecure_aes_iv = bytes(16)
        writer.encryption.secure_aes_key = bytes(32)
        writer._owns_notifier = writer.has_notifier = writer._connected = True
        return writer

    async def test_an_exception_answer_is_not_success(self):
        self.assertFalse(await self._writer(refuse).write(FieldName.CTRL_AC.value, True))

    async def test_the_echo_is_success(self):
        self.assertTrue(await self._writer(echo).write(FieldName.CTRL_AC.value, True))

    async def test_a_stray_frame_does_not_pass_for_the_ack(self):
        writer = self._writer(stray_then_echo)
        self.assertTrue(await writer.write(FieldName.CTRL_AC.value, True))


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
