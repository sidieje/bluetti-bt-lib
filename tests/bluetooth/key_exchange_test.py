import asyncio
import unittest
from unittest import mock

from bleak.exc import BleakError
from cryptography.hazmat.primitives.asymmetric import ec

from bluetti_bt_lib import DeviceReader
from bluetti_bt_lib.base_devices import BaseDeviceV1
from bluetti_bt_lib.bluetooth import encryption as encryption_mod
from bluetti_bt_lib.bluetooth.device_reader import DeviceReaderConfig
from bluetti_bt_lib.bluetooth.encryption import (
    BluettiEncryption,
    KEX_MAGIC,
    Message,
    MessageType,
    hexsum,
)


def encrypted_frame(encryption, body: bytes) -> bytearray:
    """Wrap a key exchange body the way an established session would."""
    plain = KEX_MAGIC + body + hexsum(body, 2)
    key, _ = encryption.getKeyIv()
    return bytearray(encryption.aes_encrypt(plain, key, None))


def plaintext_frame(body: bytes) -> bytearray:
    return bytearray(KEX_MAGIC + body + hexsum(body, 2))


class WriteCapturingClient:
    is_connected = True

    def __init__(self):
        self.writes = []

    async def write_gatt_char(self, _uuid, data):
        self.writes.append(bytes(data))


class FailingWriteClient:
    """BLE client whose writes always fail, as when the peer drops the link."""

    async def write_gatt_char(self, *_args, **_kwargs):
        raise BleakError("Not connected")


class LinkClient:
    def __init__(self):
        self.is_connected = True
        self.stopped = False
        self.disconnected = False

    async def stop_notify(self, _uuid):
        self.stopped = True

    async def disconnect(self):
        self.disconnected = True
        self.is_connected = False


class TestKeyExchange(unittest.IsolatedAsyncioTestCase):
    def make_reader(self, client=None):
        reader = DeviceReader(
            "00:11:00:11:00:11",
            BaseDeviceV1(),
            asyncio.Future,
            config=DeviceReaderConfig(use_encryption=True),
        )
        reader.client = client or WriteCapturingClient()
        # A session partway through: the exchange can restart in-band.
        reader.encryption.unsecure_aes_key = bytes(16)
        reader.encryption.unsecure_aes_iv = bytes(16)
        reader.encryption.secure_aes_key = bytes(range(16))
        return reader

    async def test_challenge_inside_encrypted_channel_refreshes_iv(self):
        # The peer pubkey is verified against unsecure_aes_iv, so a challenge
        # that arrives after the session is up has to be answered.
        reader = self.make_reader()
        stale_iv = reader.encryption.unsecure_aes_iv
        frame = encrypted_frame(
            reader.encryption,
            bytes([MessageType.CHALLENGE.value, 0]) + b"\x09\x08\x07\x06",
        )

        await reader._notification_handler(0, frame)

        self.assertNotEqual(reader.encryption.unsecure_aes_iv, stale_iv)
        self.assertEqual(reader.notify_response, bytearray())
        self.assertEqual(len(reader.client.writes), 1)

    async def test_unknown_key_exchange_type_is_not_forwarded(self):
        # An unknown type byte must not raise out of the callback (bleak runs
        # it as a detached task) and must not reach a pending command.
        reader = self.make_reader()
        frame = encrypted_frame(reader.encryption, bytes([9, 4, 9, 8, 7, 6]))

        await reader._notification_handler(0, frame)

        self.assertEqual(reader.notify_response, bytearray())
        self.assertEqual(reader.client.writes, [])
        self.assertFalse(reader.handshake_failed)

    async def test_rejected_pubkey_flags_the_read_to_reconnect(self):
        reader = self.make_reader()
        pubkey = bytes([1]) * 64
        bad_signature = (bytes([1]) + bytes(31)) * 2
        frame = encrypted_frame(
            reader.encryption,
            bytes([MessageType.PEER_PUBKEY.value, 0]) + pubkey + bad_signature,
        )

        await reader._notification_handler(0, frame)

        self.assertTrue(reader.handshake_failed)
        self.assertIsNone(reader.encryption.unsecure_aes_iv)
        self.assertIsNone(reader.encryption.secure_aes_key)
        self.assertEqual(reader.notify_response, bytearray())
        self.assertEqual(reader.client.writes, [])

    async def test_handshake_write_failure_is_contained(self):
        # A write can fail mid-handshake when the peripheral drops the link.
        # BleakError must not escape into bleak's callback dispatch, where it
        # surfaces without naming the stalled read.
        reader = self.make_reader(FailingWriteClient())
        frame = plaintext_frame(
            bytes([MessageType.CHALLENGE.value, 0]) + b"\x01\x02\x03\x04"
        )

        await reader._notification_handler(0, frame)

        self.assertTrue(reader.handshake_failed)

    async def test_encrypted_challenge_write_failure_is_contained(self):
        reader = self.make_reader(FailingWriteClient())
        frame = encrypted_frame(
            reader.encryption,
            bytes([MessageType.CHALLENGE.value, 0]) + b"\x01\x02\x03\x04",
        )

        await reader._notification_handler(0, frame)

        self.assertTrue(reader.handshake_failed)


    async def test_repeated_frame_is_answered_once(self):
        # Some stations deliver every frame two or three times. Answering a
        # repeat twice is what leaves the peer sharing a secret with a key we
        # threw away, after which it ignores every command.
        reader = self.make_reader()
        frame = plaintext_frame(
            bytes([MessageType.CHALLENGE.value, 0]) + b"\x01\x02\x03\x04"
        )

        await reader._notification_handler(0, frame)
        await reader._notification_handler(0, bytearray(frame))

        self.assertEqual(len(reader.client.writes), 1)

    async def test_identical_chunks_of_a_partial_answer_are_kept(self):
        # A plain answer split over two notifications can have identical
        # halves. Dropping the second would leave the read waiting forever.
        reader = DeviceReader(
            "00:11:00:11:00:11",
            BaseDeviceV1(),
            asyncio.Future,
            config=DeviceReaderConfig(use_encryption=False),
        )
        reader.client = WriteCapturingClient()
        chunk = bytearray(b"\x01\x03\x04\x00\x01\x00\x01")
        reader.notify_response = bytearray(chunk)

        await reader._notification_handler(0, bytearray(chunk))
        await reader._notification_handler(0, bytearray(chunk))

        self.assertEqual(len(reader.notify_response), 3 * len(chunk))

    async def test_resync_forgets_the_last_frame(self):
        # A later request can legitimately get the same answer byte for byte.
        reader = self.make_reader()
        reader.last_notification = b"\x01\x03\x02\x12\x34"

        await reader._resync_link()

        self.assertIsNone(reader.last_notification)


class TestHandshakeAbandoned(unittest.IsolatedAsyncioTestCase):
    async def test_abandon_handshake_drops_a_supplied_client(self):
        # A link without a session can never carry a command, and the peer
        # only restarts the exchange on a new connection, so a client owned by
        # the caller has to go down with it.
        reader = DeviceReader(
            "00:11:00:11:00:11",
            BaseDeviceV1(),
            asyncio.Future,
            config=DeviceReaderConfig(use_encryption=True),
        )
        client = LinkClient()
        reader.ble_client = client
        reader.client = client
        reader.has_notifier = True
        reader._connected = True
        reader.encryption.unsecure_aes_iv = bytes(16)
        reader.handshake_failed = True

        await reader._abandon_handshake()

        self.assertTrue(client.stopped)
        self.assertTrue(client.disconnected)
        self.assertFalse(reader._connected)
        self.assertFalse(reader.has_notifier)
        self.assertFalse(reader.handshake_failed)
        self.assertIsNone(reader.encryption.unsecure_aes_iv)

    async def test_await_handshake_gives_up_on_a_failed_exchange(self):
        reader = DeviceReader(
            "00:11:00:11:00:11",
            BaseDeviceV1(),
            asyncio.Future,
            config=DeviceReaderConfig(use_encryption=True),
        )
        reader.handshake_failed = True

        self.assertFalse(await reader._await_handshake())


class TestRepeatedPeerPubkey(unittest.TestCase):
    """Stations retry their pubkey until they see our answer, and some deliver
    every frame several times. Each repeat must not make a new keypair: the
    peer shares a secret with whichever key it accepted, so a later keypair
    leaves the two ends agreeing on different keys and the device stops
    answering every command."""

    @staticmethod
    def peer_key() -> ec.EllipticCurvePublicKey:
        return ec.generate_private_key(ec.SECP256R1()).public_key()

    def run_exchange(self, peer_pubkeys: list):
        encryption = BluettiEncryption()
        encryption.unsecure_aes_key = bytes(16)
        encryption.unsecure_aes_iv = bytes(16)

        body = bytes([MessageType.PEER_PUBKEY.value, 0]) + bytes(64)
        message = Message(KEX_MAGIC + body + hexsum(body, 2))

        replies: list = []
        keypairs: list = []

        # The peer signs with a well-known key, so a genuinely signed frame
        # cannot be forged here; only the pubkey extraction is stood in for.
        with mock.patch.object(
            encryption_mod, "verify_and_extract_signed_data", return_value=bytes(64)
        ), mock.patch.object(
            encryption_mod, "pubkey_from_bytes", side_effect=peer_pubkeys
        ):
            for _ in peer_pubkeys:
                replies.append(encryption.msg_peer_pubkey(message))
                keypairs.append(encryption.my_pubkey)

        return replies, keypairs, encryption

    @staticmethod
    def plaintext(encryption: BluettiEncryption, reply: bytes) -> bytes:
        return encryption.aes_decrypt(
            reply, encryption.unsecure_aes_key, encryption.unsecure_aes_iv
        )

    def test_repeat_keeps_the_local_keypair(self):
        peer = self.peer_key()
        replies, keypairs, encryption = self.run_exchange([peer, peer, peer])

        self.assertIs(keypairs[0], keypairs[1])
        self.assertIs(keypairs[1], keypairs[2])

        # The signature is randomized, so only the pubkey the station accepts
        # has to be the same in every reply.
        my_pubkey_bytes = encryption_mod.pubkey_to_bytes(keypairs[0])
        for reply in replies:
            self.assertIn(my_pubkey_bytes, self.plaintext(encryption, reply))

    def test_new_peer_pubkey_makes_a_new_keypair(self):
        replies, keypairs, encryption = self.run_exchange(
            [self.peer_key(), self.peer_key()]
        )

        self.assertIsNot(keypairs[0], keypairs[1])
        self.assertNotIn(
            encryption_mod.pubkey_to_bytes(keypairs[0]),
            self.plaintext(encryption, replies[1]),
        )

    def test_confirmation_without_a_keypair_is_rejected(self):
        encryption = BluettiEncryption()
        body = bytes([MessageType.PUBKEY_ACCEPTED.value, 0, 0])
        message = Message(KEX_MAGIC + body + hexsum(body, 2))

        with self.assertRaises(ValueError):
            encryption.msg_key_accepted(message)


class TestEncryptionReset(unittest.TestCase):
    def test_reset_clears_the_unsecure_session(self):
        # The IV is the suffix the peer's signed pubkey is verified against.
        # Carrying it across a reconnect rejects a key that is actually valid.
        encryption = BluettiEncryption()
        encryption.unsecure_aes_iv = bytes(16)
        encryption.unsecure_aes_key = bytes(16)
        encryption.secure_aes_key = bytes(16)
        encryption.peer_pubkey = object()

        encryption.reset()

        self.assertIsNone(encryption.unsecure_aes_iv)
        self.assertIsNone(encryption.unsecure_aes_key)
        self.assertIsNone(encryption.secure_aes_key)
        self.assertIsNone(encryption.peer_pubkey)
        self.assertFalse(encryption.is_ready_for_commands)


if __name__ == "__main__":
    unittest.main()
