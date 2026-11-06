"""Key exchange on a connection the writer owns.

Both operating modes are covered on purpose:

* An attached writer shares the reader's persistent link and must stay off its
  notification path entirely -- the reader's handler is the only one that runs.
* A standalone encrypted writer runs its own key exchange, and has to survive a
  dropped link or a malformed frame, because bleak dispatches notification
  callbacks as detached tasks: an exception raised there is simply lost, and
  the writer is left waiting for a handshake that can no longer arrive.
"""

import asyncio
import time
import unittest
from unittest.mock import patch

from bleak.exc import BleakError
from cryptography.hazmat.primitives.asymmetric import ec

from bluetti_bt_lib.bluetooth import DeviceWriter, DeviceWriterConfig
from bluetti_bt_lib.bluetooth import encryption as encryption_mod
from bluetti_bt_lib.bluetooth.encryption import (
    KEX_MAGIC,
    BluettiEncryption,
    hexsum,
    pubkey_to_bytes,
)
from bluetti_bt_lib.devices.ac180 import AC180
from bluetti_bt_lib.fields import FieldName
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption

NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

CHALLENGE = 1
PEER_PUBKEY = 4
PUBKEY_ACCEPTED = 6
UNKNOWN_TYPE = 99


def kex_frame(type_byte: int, payload: bytes) -> bytes:
    """Build a frame in the pre-key-exchange format the station sends."""
    body = bytes([type_byte, 0]) + payload
    return KEX_MAGIC + body + hexsum(body, 2)


def challenge(payload: bytes = bytes(range(4))) -> bytes:
    """A challenge carrying a seed of the length msg_challenge() expects."""
    return kex_frame(CHALLENGE, payload)


class Station:
    """Minimal GATT peer for a standalone writer."""

    address = "00:11:00:11:00:11"

    def __init__(self):
        self.is_connected = True
        self.written = []
        self.drops_writes = False
        self.callback = None

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False
        self.callback = None

    async def start_notify(self, _uuid, callback):
        self.callback = callback

    async def stop_notify(self, _uuid):
        self.callback = None

    async def write_gatt_char(self, _uuid, data, response=None):
        if self.drops_writes:
            raise BleakError("connection dropped")
        self.written.append(bytes(data))


def build_writer(station, timeout: int = 5) -> DeviceWriter:
    return DeviceWriter(
        station,
        AC180(),
        DeviceWriterConfig(timeout=timeout, use_encryption=True),
        asyncio.Lock(),
    )


class TestStandaloneKeyExchange(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.station = Station()
        self.writer = build_writer(self.station)

    def open_session(self):
        """Stand in for a completed key exchange."""
        session = self.writer.encryption
        session.unsecure_aes_key = bytes(16)
        session.unsecure_aes_iv = bytes(16)
        session.secure_aes_key = bytes(32)

    def encrypt(self, frame: bytes) -> bytearray:
        session = self.writer.encryption
        return bytearray(session.aes_encrypt(frame, session.secure_aes_key, None))

    async def deliver(self, frame: bytes):
        await self.writer._notification_handler(0, bytearray(frame))

    async def test_a_valid_challenge_is_answered(self):
        await self.deliver(challenge())

        self.assertEqual(len(self.station.written), 1)
        self.assertTrue(self.station.written[0].startswith(KEX_MAGIC))
        self.assertFalse(self.writer.handshake_failed)

    async def test_a_malformed_challenge_is_not_sent_as_a_reply(self):
        # msg_challenge() returns None for a seed it cannot use. Passing that
        # straight to write_gatt_char() raised TypeError inside the callback,
        # which bleak swallowed, so the writer just waited for nothing.
        await self.deliver(challenge(payload=bytes(3)))

        self.assertEqual(self.station.written, [])
        self.assertTrue(self.writer.handshake_failed)

    async def test_an_unknown_key_exchange_type_does_not_kill_the_callback(self):
        await self.deliver(kex_frame(UNKNOWN_TYPE, bytes(4)))

        self.assertEqual(self.station.written, [])
        self.assertFalse(self.writer.handshake_failed)

    async def test_a_truncated_key_exchange_frame_does_not_kill_the_callback(self):
        # Short enough that the frame carries no type byte at all.
        await self.deliver(KEX_MAGIC + hexsum(b"", 2))

        self.assertEqual(self.station.written, [])
        self.assertFalse(self.writer.handshake_failed)

    async def test_an_unknown_type_inside_the_encrypted_channel_is_dropped(self):
        self.open_session()

        await self.deliver(bytes(self.encrypt(kex_frame(UNKNOWN_TYPE, bytes(4)))))

        self.assertEqual(self.station.written, [])
        self.assertFalse(self.writer.handshake_failed)

    async def test_connect_gives_up_when_the_reply_cannot_be_sent(self):
        self.station.drops_writes = True

        connect = asyncio.create_task(self.writer.connect())
        await asyncio.sleep(0.01)  # let connect() subscribe
        self.assertIsNotNone(self.station.callback)

        start = time.monotonic()
        await self.station.callback(0, bytearray(challenge()))
        connected = await connect
        elapsed = time.monotonic() - start

        self.assertFalse(connected)
        self.assertTrue(self.writer.handshake_failed)
        # The point of the failure flag: the wait ends on the error instead of
        # running the whole timeout.
        self.assertLess(elapsed, 1)
        self.assertFalse(self.writer._owns_notifier)

    async def test_connect_still_waits_out_a_silent_peer(self):
        writer = build_writer(self.station, timeout=1)

        self.assertFalse(await writer.connect())
        # A peer that never sends anything is a timeout, not a failed exchange.
        self.assertFalse(writer.handshake_failed)
        self.assertFalse(writer._owns_notifier)

    async def test_a_retried_connect_starts_the_exchange_again(self):
        self.station.drops_writes = True
        first = asyncio.create_task(self.writer.connect())
        await asyncio.sleep(0.01)
        await self.station.callback(0, bytearray(challenge()))

        self.assertFalse(await first)
        self.assertTrue(self.writer.handshake_failed)

        self.station.drops_writes = False
        second = asyncio.create_task(self.writer.connect())
        await asyncio.sleep(0.01)
        await self.station.callback(0, bytearray(challenge()))

        # A challenge on its own is not a session, so this attempt is still
        # waiting: it must not have inherited the previous failure, and it
        # must have re-subscribed and answered on the new link.
        self.assertFalse(second.done())
        self.assertFalse(self.writer.handshake_failed)
        self.assertEqual(len(self.station.written), 1)

        second.cancel()
        await asyncio.gather(second, return_exceptions=True)


class TestEncryptedChannelReExchange(unittest.IsolatedAsyncioTestCase):
    """A station that restarts the exchange after the session is up.

    The frames are driven as a whole sequence rather than one per test: the
    peer pubkey is verified against the IV that answering a challenge refreshes,
    so a challenge the writer ignores leaves the next pubkey looking like it
    carries a bad signature. Only the sequence shows that.
    """

    # The peer's side of the exchange. Its signing key is well-known and lives
    # in the library, so the test only needs a real ECDH key to agree with.
    peer = ec.generate_private_key(ec.SECP256R1())
    peer_pubkey_bytes = pubkey_to_bytes(peer.public_key())
    # A station that restarts the exchange brings a fresh key of its own.
    other_peer = ec.generate_private_key(ec.SECP256R1())
    other_peer_pubkey_bytes = pubkey_to_bytes(other_peer.public_key())

    SEED_A = bytes([1, 2, 3, 4])
    SEED_B = bytes([9, 8, 7, 6])

    async def asyncSetUp(self):
        self.station = Station()
        self.writer = build_writer(self.station)
        # What the signature check was really asked to verify against.
        self.suffixes = []

    def session(self):
        return self.writer.encryption

    def encrypt(self, frame: bytes) -> bytearray:
        """Wrap a frame the way the station would at the current step."""
        session = self.session()
        key, iv = session.getKeyIv()
        return bytearray(session.aes_encrypt(frame, key, iv))

    def decrypt(self, frame: bytes) -> bytes:
        session = self.session()
        key, iv = session.getKeyIv()
        return session.aes_decrypt(bytes(frame), key, iv)

    async def deliver(self, frame):
        await self.writer._notification_handler(0, bytearray(frame))

    def accept_pubkey(self, pubkey_bytes: bytes | None = None):
        """Stand in for the signature check, recording the IV it used.

        The station signs with a key whose counterpart lives in the library, so
        a test cannot produce a signature the real check accepts. What matters
        here is which IV the check was handed.
        """
        agreed = self.peer_pubkey_bytes if pubkey_bytes is None else pubkey_bytes

        def verify(_data, suffix):
            self.suffixes.append(bytes(suffix))
            return agreed

        return patch.object(
            encryption_mod, "verify_and_extract_signed_data", side_effect=verify
        )

    async def finish_exchange(self, pubkey_bytes: bytes | None = None):
        """Send the pubkey and its acceptance, as the station does."""
        agreed = self.peer_pubkey_bytes if pubkey_bytes is None else pubkey_bytes
        with self.accept_pubkey(agreed):
            await self.deliver(self.encrypt(kex_frame(PEER_PUBKEY, agreed + bytes(64))))
            await self.deliver(self.encrypt(kex_frame(PUBKEY_ACCEPTED, b"\x00")))

    async def test_a_first_exchange_completes_over_a_live_connect(self):
        connect = asyncio.create_task(self.writer.connect())
        await asyncio.sleep(0.01)  # let connect() subscribe

        await self.station.callback(0, bytearray(challenge(self.SEED_A)))
        with self.accept_pubkey():
            await self.station.callback(
                0,
                bytearray(
                    self.encrypt(
                        kex_frame(PEER_PUBKEY, self.peer_pubkey_bytes + bytes(64))
                    )
                ),
            )
            await self.station.callback(
                0, bytearray(self.encrypt(kex_frame(PUBKEY_ACCEPTED, b"\x00")))
            )

        self.assertTrue(await asyncio.wait_for(connect, 1))
        self.assertTrue(self.session().is_ready_for_commands)
        self.assertFalse(self.writer.handshake_failed)

    async def test_a_restarted_exchange_completes(self):
        await self.deliver(challenge(self.SEED_A))
        await self.finish_exchange()
        self.assertTrue(self.writer.encryption_ready.is_set())
        first_key = self.session().secure_aes_key

        self.station.written.clear()
        await self.deliver(self.encrypt(challenge(self.SEED_B)))

        # The challenge was answered, and the answer carries the fresh IV the
        # station will sign the next pubkey with.
        self.assertEqual(len(self.station.written), 1)
        self.assertIn(
            self.session().unsecure_aes_iv[8:12],
            self.decrypt(self.station.written[0]),
        )

        await self.finish_exchange(self.other_peer_pubkey_bytes)

        self.assertFalse(self.writer.handshake_failed)
        self.assertTrue(self.writer.encryption_ready.is_set())
        self.assertTrue(self.session().is_ready_for_commands)
        self.assertNotEqual(self.session().secure_aes_key, first_key)
        # Both pubkeys were checked, the second one against the IV the second
        # challenge derived. That is the coupling the writer used to miss.
        self.assertEqual(len(self.suffixes), 2)
        self.assertNotEqual(self.suffixes[0], self.suffixes[1])
        self.assertEqual(self.suffixes[1], self.session().unsecure_aes_iv)

    async def test_a_rejected_pubkey_drops_the_session(self):
        await self.deliver(challenge(self.SEED_A))
        await self.finish_exchange()

        await self.deliver(self.encrypt(challenge(self.SEED_B)))
        # A signature the station cannot back up: the session is unusable, and
        # leaving it in place would have the writer encrypt commands with keys
        # the peer no longer holds.
        await self.deliver(
            self.encrypt(kex_frame(PEER_PUBKEY, bytes([1]) * 64 + bytes(64)))
        )

        self.assertTrue(self.writer.handshake_failed)
        self.assertTrue(self.writer._handshake_failed_event.is_set())
        self.assertFalse(self.writer.encryption_ready.is_set())
        self.assertFalse(self.session().is_ready_for_commands)
        self.assertIsNone(self.session().secure_aes_key)
        self.assertIsNone(self.session().unsecure_aes_iv)

    async def test_a_refused_acceptance_fails_the_handshake(self):
        await self.deliver(challenge(self.SEED_A))
        with self.accept_pubkey():
            await self.deliver(
                self.encrypt(kex_frame(PEER_PUBKEY, self.peer_pubkey_bytes + bytes(64)))
            )
            # Acceptance code other than 0: msg_key_accepted() rejects it.
            await self.deliver(self.encrypt(kex_frame(PUBKEY_ACCEPTED, b"\x01")))

        self.assertTrue(self.writer.handshake_failed)
        self.assertFalse(self.writer.encryption_ready.is_set())
        self.assertFalse(self.session().is_ready_for_commands)

    async def test_connect_gives_up_when_the_pubkey_is_rejected(self):
        connect = asyncio.create_task(self.writer.connect())
        await asyncio.sleep(0.01)

        start = time.monotonic()
        await self.station.callback(0, bytearray(challenge(self.SEED_A)))
        await self.station.callback(
            0,
            bytearray(
                self.encrypt(kex_frame(PEER_PUBKEY, bytes([1]) * 64 + bytes(64)))
            ),
        )
        connected = await connect
        elapsed = time.monotonic() - start

        self.assertFalse(connected)
        self.assertTrue(self.writer.handshake_failed)
        self.assertLess(elapsed, 1)
        self.assertFalse(self.writer._owns_notifier)


class TestAttachedWriterStaysOffTheReadPath(unittest.IsolatedAsyncioTestCase):
    async def test_an_attached_writer_never_runs_its_own_handler(self):
        station = ClientMockNoEncryption()

        async def reader_handler(*_):
            pass

        # The reader owns the subscription on the shared link.
        await station.start_notify(NOTIFY_UUID, reader_handler)

        session = BluettiEncryption()
        session.secure_aes_key = bytes(32)
        session.peer_pubkey = object()

        writer = DeviceWriter(
            station,
            AC180(),
            DeviceWriterConfig(timeout=5, use_encryption=True),
            asyncio.Lock(),
            reader=None,
        )
        writer.attach(session)

        with patch.object(writer, "_notification_handler") as own_handler:
            self.assertTrue(await writer.write(FieldName.CTRL_AC.value, True))
            own_handler.assert_not_called()

        # The reader is still the sole subscriber and still owns the session.
        self.assertIs(station._callback, reader_handler)
        self.assertIs(writer.encryption, session)
        self.assertFalse(writer._owns_notifier)
        self.assertFalse(writer.has_notifier)
        self.assertFalse(writer.handshake_failed)
        self.assertTrue(session.is_ready_for_commands)

    async def test_an_attached_writer_does_not_wait_for_a_handshake(self):
        station = ClientMockNoEncryption()
        session = BluettiEncryption()
        session.secure_aes_key = bytes(32)
        session.peer_pubkey = object()

        writer = DeviceWriter(
            station,
            AC180(),
            DeviceWriterConfig(timeout=5, use_encryption=True),
            asyncio.Lock(),
        )
        writer.attach(session)

        start = time.monotonic()
        self.assertTrue(await writer.connect())
        self.assertLess(time.monotonic() - start, 1)
        self.assertTrue(writer.encryption_ready.is_set())
        self.assertFalse(writer._handshake_failed_event.is_set())


if __name__ == "__main__":
    unittest.main()
