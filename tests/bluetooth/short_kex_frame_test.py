"""The reader surviving a key exchange frame with no type byte.

``Message.type`` reads ``self.body[0]``, and ``body`` is ``view[2:-2]``, so any
frame that opens with the key exchange magic and is shorter than five bytes has
an empty body and raises ``IndexError``. ``is_pre_key_exchange`` only compares
the two-byte header, so such a frame reaches the property.

The writer has caught this since 103c67a, which added
``tests/writer_handshake_test.py::TestStandaloneHandshake::test_a_truncated_key_exchange_frame_does_not_kill_the_callback``.
The reader is the twin that was missed, even though its own comment at the
plaintext site records why it matters: bleak runs the callback as a detached
task, so anything raised here is lost and the read sits out its timeout with
nothing naming the cause.
"""

import asyncio
import unittest
from unittest import mock

from bluetti_bt_lib.base_devices import BaseDeviceV1
from bluetti_bt_lib.bluetooth import encryption as encryption_mod
from bluetti_bt_lib.bluetooth.device_reader import DeviceReader, DeviceReaderConfig
from bluetti_bt_lib.bluetooth.encryption import KEX_MAGIC, MessageType, hexsum

from key_exchange_test import (
    WriteCapturingClient,
    encrypted_frame,
    plaintext_frame,
)


def truncated_frames():
    """Every shape that passes the magic check but carries no type byte.

    The four byte frame is the interesting one: its checksum covers an empty
    body and is therefore valid, so it clears verify_checksum() and fails only
    when the type byte is read. That keeps these tests about the truncation
    rather than about the checksum.
    """
    return {
        "magic only": bytearray(KEX_MAGIC),
        "magic plus one byte": bytearray(KEX_MAGIC + b"\x01"),
        "magic plus a valid empty-body checksum": bytearray(
            KEX_MAGIC + hexsum(b"", 2)
        ),
    }


class ShortFrameReaderMixin:
    def make_reader(self, client=None):
        reader = DeviceReader(
            "00:11:00:11:00:11",
            BaseDeviceV1(),
            asyncio.Future,
            config=DeviceReaderConfig(use_encryption=True),
        )
        reader.client = client or WriteCapturingClient()
        # A session partway through, so the encrypted path is reachable too.
        reader.encryption.unsecure_aes_key = bytes(16)
        reader.encryption.unsecure_aes_iv = bytes(16)
        reader.encryption.secure_aes_key = bytes(range(16))
        return reader


class TestTruncatedPlaintextFrame(ShortFrameReaderMixin, unittest.IsolatedAsyncioTestCase):
    async def test_a_truncated_key_exchange_frame_does_not_kill_the_callback(self):
        # Mirrors the writer's test of the same name: the reader has to survive
        # a frame short enough to carry no type byte at all.
        for label, frame in truncated_frames().items():
            with self.subTest(label):
                reader = self.make_reader()

                await reader._notification_handler(0, frame)

                self.assertEqual(reader.client.writes, [])
                self.assertEqual(reader.notify_response, bytearray())
                self.assertFalse(reader.handshake_failed)

    async def test_a_truncated_frame_is_reported_rather_than_swallowed(self):
        # Returning without a word is as hard to diagnose as raising: both leave
        # a read that never completes. The warning is what names the cause.
        for label, frame in truncated_frames().items():
            with self.subTest(label):
                reader = self.make_reader()

                with mock.patch.object(reader.logger, "warning") as warning:
                    await reader._notification_handler(0, frame)

                warning.assert_called_with("Unknown key exchange message type")

    async def test_the_reader_still_answers_after_a_truncated_frame(self):
        # Dropping one frame must not end the exchange: a station that sends a
        # runt on the way to a challenge still gets its challenge answered.
        reader = self.make_reader()
        challenge = plaintext_frame(
            bytes([MessageType.CHALLENGE.value, 0]) + b"\x01\x02\x03\x04"
        )

        await reader._notification_handler(0, bytearray(KEX_MAGIC + hexsum(b"", 2)))
        await reader._notification_handler(0, challenge)

        self.assertEqual(len(reader.client.writes), 1)
        self.assertFalse(reader.handshake_failed)


class TestTruncatedDecryptedFrame(ShortFrameReaderMixin, unittest.IsolatedAsyncioTestCase):
    async def test_a_truncated_frame_inside_the_session_does_not_kill_the_callback(self):
        # The second reader site, reached once the session is up. It caught
        # ValueError alone for the same reason the first one did.
        reader = self.make_reader()
        frame = encrypted_frame(reader.encryption, b"")

        await reader._notification_handler(0, frame)

        self.assertEqual(reader.client.writes, [])
        self.assertEqual(reader.notify_response, bytearray())
        self.assertFalse(reader.handshake_failed)

    async def test_a_truncated_frame_inside_the_session_is_reported(self):
        reader = self.make_reader()
        frame = encrypted_frame(reader.encryption, b"")

        with mock.patch.object(reader.logger, "warning") as warning:
            await reader._notification_handler(0, frame)

        warning.assert_called_with("Unknown key exchange message type")


class TestMessageTypeRaisesIndexError(ShortFrameReaderMixin, unittest.IsolatedAsyncioTestCase):
    """Pins the exception the handlers have to catch.

    ``type`` raises IndexError rather than ValueError on an empty body, which is
    the whole reason the handlers name two exception types. If ``type`` ever
    learns to return a sentinel instead, these fail and the handlers can be
    simplified deliberately rather than by accident.
    """

    def test_empty_body_raises_index_error(self):
        message = encryption_mod.Message(KEX_MAGIC + hexsum(b"", 2))

        with self.assertRaises(IndexError):
            message.type

    def test_a_known_type_byte_still_parses(self):
        # The catch must not become a way to ignore frames that are fine.
        message = encryption_mod.Message(
            plaintext_frame(bytes([MessageType.CHALLENGE.value, 0]) + b"\x01\x02")
        )

        self.assertEqual(message.type, MessageType.CHALLENGE)


if __name__ == "__main__":
    unittest.main()
