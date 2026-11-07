import asyncio
import re
import unittest
from unittest import mock

from bleak.exc import BleakError
from cryptography.hazmat.primitives.asymmetric import ec

from bluetti_bt_lib import DeviceReader
from bluetti_bt_lib.base_devices import BaseDeviceV1
from bluetti_bt_lib.bluetooth import DeviceWriter, DeviceWriterConfig
from bluetti_bt_lib.bluetooth import encryption as encryption_mod
from bluetti_bt_lib.bluetooth.device_reader import DeviceReaderConfig
from bluetti_bt_lib.bluetooth.encryption import (
    BluettiEncryption,
    KEX_MAGIC,
    Message,
    MessageType,
    hexsum,
    pubkey_to_bytes,
)
from bluetti_bt_lib.devices.ac180 import AC180


def encrypted_frame(encryption, body: bytes) -> bytearray:
    """Wrap a key exchange body the way an established session would."""
    plain = KEX_MAGIC + body + hexsum(body, 2)
    key, _ = encryption.getKeyIv()
    return bytearray(encryption.aes_encrypt(plain, key, None))


def plaintext_frame(body: bytes) -> bytearray:
    return bytearray(KEX_MAGIC + body + hexsum(body, 2))


class WriteCapturingClient:
    is_connected = True
    # A GATT peer always has an address, and the writer names its link with one.
    address = "00:11:00:11:00:11"

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


def open_session(encryption: BluettiEncryption) -> None:
    """Put a session in the state a finished key exchange leaves behind."""
    encryption.unsecure_aes_key = bytes(16)
    encryption.unsecure_aes_iv = bytes(16)
    encryption.secure_aes_key = bytes(range(16))
    encryption.peer_pubkey = object()


def encrypted_reader(client=None) -> DeviceReader:
    """A reader with a session already up, so the encrypted path is reachable."""
    reader = DeviceReader(
        "00:11:00:11:00:11",
        BaseDeviceV1(),
        asyncio.Future,
        config=DeviceReaderConfig(use_encryption=True),
    )
    reader.client = client or WriteCapturingClient()
    open_session(reader.encryption)
    return reader


# A signed pubkey is 64 bytes of key plus a 64 byte signature, and
# verify_and_extract_signed_data() rejects anything else. These are the lengths
# a truncated or padded frame really arrives with, and every one of them used to
# raise ValueError out of the notification callback.
MALFORMED_PUBKEY_LENGTHS = (0, 10, 127)


def malformed_pubkey_frame(
    encryption: BluettiEncryption, payload_len: int
) -> bytearray:
    """A correctly encrypted PEER_PUBKEY whose payload cannot hold a pubkey."""
    return encrypted_frame(
        encryption,
        bytes([MessageType.PEER_PUBKEY.value, 0]) + bytes(payload_len),
    )


class TestKeyExchange(unittest.IsolatedAsyncioTestCase):
    def make_reader(self, client=None):
        return encrypted_reader(client)

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

    async def test_corrupt_frame_still_reaches_the_exchange(self):
        # Issue #51 pins today's behaviour: a bad checksum is logged, but the
        # frame is used anyway. This test exists to fail the day we decide to
        # reject it, so that the change has to be a deliberate one.
        reader = self.make_reader()
        frame = plaintext_frame(
            bytes([MessageType.CHALLENGE.value, 0]) + b"\x01\x02\x03\x04"
        )
        frame[-3] ^= 0xFF

        with mock.patch.object(
            reader.encryption, "msg_challenge", return_value=b"\x00"
        ) as msg_challenge:
            await reader._notification_handler(0, frame)

        msg_challenge.assert_called_once()
        self.assertEqual(len(reader.client.writes), 1)

    async def test_repeated_frame_is_answered_once(self):
        # Before the dedupe, an AP300 re-delivered 3 distinct pubkeys 25 times.
        # Answering a repeat twice is what leaves the peer sharing a secret with
        # a key we threw away, after which it ignores every command.
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


class TestMalformedPeerPubkey(unittest.IsolatedAsyncioTestCase):
    """A malformed pubkey frame has to fail the handshake, not the callback.

    ``msg_peer_pubkey()`` only ever caught ``InvalidSignature``, but the length
    guard in ``verify_and_extract_signed_data()`` raises ``ValueError`` for any
    frame that is not exactly 128 bytes, and a key that will not parse raises it
    too. That escaped ``_notification_handler``, and bleak dispatches the
    callback as a detached task: the exception was dropped, ``handshake_failed``
    stayed clear, and the read sat out its timeout on a link that could never
    carry a command -- strictly worse than the signature path, which at least
    reports a failure.
    """

    async def test_a_malformed_pubkey_does_not_kill_the_callback(self):
        for length in MALFORMED_PUBKEY_LENGTHS:
            with self.subTest(payload_length=length):
                reader = encrypted_reader()
                frame = malformed_pubkey_frame(reader.encryption, length)

                await reader._notification_handler(0, frame)

                self.assertEqual(reader.client.writes, [])
                self.assertEqual(reader.notify_response, bytearray())

    async def test_a_malformed_pubkey_fails_the_handshake(self):
        for length in MALFORMED_PUBKEY_LENGTHS:
            with self.subTest(payload_length=length):
                reader = encrypted_reader()
                frame = malformed_pubkey_frame(reader.encryption, length)

                await reader._notification_handler(0, frame)

                self.assertTrue(reader.handshake_failed)

    async def test_a_malformed_pubkey_drops_the_session(self):
        # The session has to go with the failure. Keeping the keys would let
        # the link carry on encrypting commands with a session the peer has no
        # reason to still hold, which is why the signature path resets too.
        for length in MALFORMED_PUBKEY_LENGTHS:
            with self.subTest(payload_length=length):
                reader = encrypted_reader()
                frame = malformed_pubkey_frame(reader.encryption, length)

                await reader._notification_handler(0, frame)

                self.assertFalse(reader.encryption.is_ready_for_commands)
                self.assertIsNone(reader.encryption.unsecure_aes_key)
                self.assertIsNone(reader.encryption.unsecure_aes_iv)
                self.assertIsNone(reader.encryption.secure_aes_key)
                self.assertIsNone(reader.encryption.peer_pubkey)
                self.assertEqual(reader.encrypted_buffer, bytearray())

    async def test_a_malformed_pubkey_names_the_reason(self):
        # Returning without a word would leave a stalled read with nothing
        # naming the cause, which is how this went unnoticed in the field.
        for length in MALFORMED_PUBKEY_LENGTHS:
            with self.subTest(payload_length=length):
                reader = encrypted_reader()
                frame = malformed_pubkey_frame(reader.encryption, length)

                with self.assertLogs(reader.logger, level="WARNING") as logs:
                    await reader._notification_handler(0, frame)

                self.assertTrue(
                    any(
                        "Peer pubkey rejected (Unexpected message length)" in line
                        for line in logs.output
                    ),
                    logs.output,
                )

    async def test_the_reader_keeps_working_after_a_malformed_pubkey(self):
        # One unusable frame must not end the exchange: the station retries, and
        # the challenge that restarts the session still gets its answer.
        reader = encrypted_reader()
        await reader._notification_handler(
            0, malformed_pubkey_frame(reader.encryption, 10)
        )

        challenge = plaintext_frame(
            bytes([MessageType.CHALLENGE.value, 0]) + b"\x01\x02\x03\x04"
        )
        await reader._notification_handler(0, challenge)

        self.assertEqual(len(reader.client.writes), 1)
        self.assertIsNotNone(reader.encryption.unsecure_aes_key)

    async def test_a_full_length_bad_signature_still_reports_a_bad_signature(self):
        # The catch widened, the diagnosis must not blur: 128 bytes of garbage
        # reaches the crypto and comes back InvalidSignature, which has no
        # message of its own to log.
        reader = encrypted_reader()
        frame = encrypted_frame(
            reader.encryption,
            bytes([MessageType.PEER_PUBKEY.value, 0])
            + bytes([1]) * 64
            + bytes([1]) * 64,
        )

        with self.assertLogs(reader.logger, level="WARNING") as logs:
            await reader._notification_handler(0, frame)

        self.assertTrue(
            any(
                "Peer pubkey rejected (bad signature)" in line
                for line in logs.output
            ),
            logs.output,
        )
        self.assertTrue(reader.handshake_failed)
        self.assertIsNone(reader.encryption.unsecure_aes_key)

    async def test_a_signed_pubkey_is_still_answered(self):
        # The frame this test feeds is the shape a real station sends; only the
        # signature check is stood in for, since the station's signing key lives
        # in the library and no test can produce a signature it accepts.
        reader = encrypted_reader()
        frame = encrypted_frame(
            reader.encryption,
            bytes([MessageType.PEER_PUBKEY.value, 0]) + bytes(64) + bytes(64),
        )
        # A real key, because the reply has to agree on a curve point: an all
        # zero payload is not on secp256r1 and would fail for the wrong reason.
        peer_bytes = pubkey_to_bytes(
            ec.generate_private_key(ec.SECP256R1()).public_key()
        )

        with mock.patch.object(
            encryption_mod,
            "verify_and_extract_signed_data",
            return_value=memoryview(peer_bytes),
        ):
            await reader._notification_handler(0, frame)

        self.assertEqual(len(reader.client.writes), 1)
        self.assertFalse(reader.handshake_failed)
        self.assertIsNotNone(reader.encryption.my_privkey)

    async def test_reader_and_writer_fail_the_same_way(self):
        # Two handlers over two code paths, one defect. The writer owns an extra
        # event and a ready flag, but the outcome the caller depends on -- a
        # failed handshake and no session left behind -- has to be identical.
        reader = encrypted_reader()
        writer = DeviceWriter(
            WriteCapturingClient(),
            AC180(),
            DeviceWriterConfig(timeout=5, use_encryption=True),
            asyncio.Lock(),
        )
        open_session(writer.encryption)
        # One frame has to be validly encrypted for both of them.
        self.assertEqual(
            writer.encryption.secure_aes_key, reader.encryption.secure_aes_key
        )
        frame = malformed_pubkey_frame(reader.encryption, 127)

        await reader._notification_handler(0, bytearray(frame))
        await writer._notification_handler(0, bytearray(frame))

        self.assertTrue(reader.handshake_failed)
        self.assertTrue(writer.handshake_failed)
        self.assertTrue(writer._handshake_failed_event.is_set())
        self.assertFalse(writer.encryption_ready.is_set())
        self.assertFalse(reader.encryption.is_ready_for_commands)
        self.assertFalse(writer.encryption.is_ready_for_commands)
        self.assertEqual(reader.client.writes, [])
        self.assertEqual(writer.client.writes, [])


class TestRepeatedPeerPubkey(unittest.TestCase):
    """Stations retry their pubkey until they see our answer, and the Apex 300
    re-delivers each pubkey frame several times. Each repeat must not make a new
    keypair: the peer shares a secret with whichever key it accepted, so a later
    keypair leaves the two ends agreeing on different keys and the device stops
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


class TestChecksum(unittest.TestCase):
    """Issue #51: verify_checksum() logged "Checksum error!" and then logged
    "Checksum OK" for the same frame, and returned nothing, so every caller read
    a clean verification out of a frame that had failed. This covers the report;
    whether a mismatching frame should be rejected at all is still open."""

    @staticmethod
    def challenge_frame(body: bytes = b"\x01\x02") -> bytes:
        return bytes(plaintext_frame(bytes([MessageType.CHALLENGE.value, 0]) + body))

    @staticmethod
    def corrupt(frame: bytes) -> bytes:
        # Flip the last body byte, leaving the checksum itself untouched.
        broken = bytearray(frame)
        broken[-3] ^= 0xFF
        return bytes(broken)

    def test_valid_frame_verifies(self):
        message = Message(self.challenge_frame())

        with self.assertLogs(encryption_mod._LOGGER, level="DEBUG") as logs:
            self.assertTrue(message.verify_checksum())

        self.assertEqual(len(logs.output), 1)
        self.assertIn("Checksum OK", logs.output[0])

    def test_corrupt_body_fails(self):
        message = Message(self.corrupt(self.challenge_frame()))

        with self.assertLogs(encryption_mod._LOGGER, level="ERROR") as logs:
            self.assertFalse(message.verify_checksum())

        # The misleading success line is what made the failure invisible.
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("Checksum OK", logs.output[0])

    def test_mismatch_names_the_link_and_both_checksums(self):
        message = Message(self.corrupt(self.challenge_frame()))

        with self.assertLogs(encryption_mod._LOGGER, level="ERROR") as logs:
            message.verify_checksum("BaseDeviceV1 00:11:00:11:00:11 plaintext")

        detail = logs.output[0]
        self.assertIn("[BaseDeviceV1 00:11:00:11:00:11 plaintext]", detail)
        self.assertIn("type=CHALLENGE", detail)
        self.assertIn("received=", detail)
        self.assertIn("computed=", detail)
        # Length replaces the frame: enough to tie a bad frame to a link and a
        # moment, without carrying anything that decrypts.
        self.assertIn("length=8", detail)
        self.assertNotIn("frame=", detail)

    def test_mismatch_omits_the_frame_and_the_iv_seed(self):
        # A plaintext CHALLENGE body is type, flags, then the 4-byte IV seed,
        # and msg_challenge() turns that seed into the unsecure session key with
        # md5(seed[::-1]) XOR a constant from the library. Logging the frame at
        # ERROR therefore used to hand over a working key on a corrupted frame.
        seed = b"\xde\xa0\xbe\xef"
        frame = bytearray(
            self.challenge_frame(bytes([MessageType.CHALLENGE.value, 0]) + seed)
        )
        # Corrupt the flags byte, not the seed: TestChecksum.corrupt() flips the
        # last body byte, which here is part of the seed, and a test that only
        # ever sees the seed mangled passes against the leak it exists to catch.
        frame[3] ^= 0xFF
        message = Message(bytes(frame))

        with self.assertLogs(encryption_mod._LOGGER, level="ERROR") as logs:
            message.verify_checksum("BaseDeviceV1 00:11:00:11:00:11 plaintext")

        detail = logs.output[0]
        self.assertNotIn(seed.hex(), detail)
        self.assertNotIn(seed[::-1].hex(), detail)
        self.assertNotIn(bytes(frame).hex(), detail)

    def test_mismatch_leaks_no_blob_a_frame_could_hide_in(self):
        # Guards the shape rather than one seed: the two checksums are 4 hex
        # characters each and the MAC is colon-separated, so anything long
        # enough here is frame or payload material that has crept back in.
        frame = self.challenge_frame(
            bytes([MessageType.CHALLENGE.value, 0]) + bytes(range(12))
        )
        message = Message(self.corrupt(frame))

        with self.assertLogs(encryption_mod._LOGGER, level="ERROR") as logs:
            message.verify_checksum("BaseDeviceV1 00:11:00:11:00:11 decrypted")

        self.assertIsNone(re.search(r"[0-9a-f]{12,}", logs.output[0]), logs.output[0])

    def test_unknown_type_is_labelled_without_raising(self):
        # The log line runs before the caller has accepted the type byte, so it
        # cannot be the thing that raises out of a notification callback.
        message = Message(bytes(plaintext_frame(bytes([9, 0]) + b"\x01\x02")))

        self.assertEqual(message.type_label(), "UNKNOWN(9)")

    def test_empty_body_is_labelled_without_raising(self):
        message = Message(KEX_MAGIC + hexsum(b"", 2))

        self.assertEqual(message.type_label(), "EMPTY")


if __name__ == "__main__":
    unittest.main()
