import asyncio
import struct
import time
import unittest
from enum import Enum
from types import SimpleNamespace

from bleak.exc import BleakError

from bluetti_bt_lib.base_devices import BluettiDevice
from bluetti_bt_lib.bluetooth import DeviceWriter, DeviceWriterConfig
from bluetti_bt_lib.bluetooth.device_reader import DeviceReader, DeviceReaderConfig
from bluetti_bt_lib.bluetooth.device_writer import (
    DEFAULT_VERIFICATION_WINDOW_SECONDS,
    VERIFICATION_FIRST_READ_SECONDS,
    VERIFICATION_REREAD_SECONDS,
    _select_verification_delay,
)
from bluetti_bt_lib.bluetooth.encryption import BluettiEncryption
from bluetti_bt_lib.devices.ac180 import AC180
from bluetti_bt_lib.fields import (
    FieldName,
    NumberField,
    SelectField,
    SwitchField,
    UIntField,
)
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption

NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"


class TestDeviceWriter(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ble_mock = ClientMockNoEncryption()
        self.device = AC180()

    def build_writer(self, use_encryption: bool = False) -> DeviceWriter:
        return DeviceWriter(
            self.ble_mock,
            self.device,
            DeviceWriterConfig(timeout=15, use_encryption=use_encryption),
            asyncio.Lock(),
        )

    async def test_write_without_notifier_does_not_wait_for_ack(self):
        writer = self.build_writer()

        start = time.monotonic()
        result = await writer.write(FieldName.CTRL_AC.value, True)
        elapsed = time.monotonic() - start

        self.assertTrue(result)
        # No notifier means no acknowledgement, so the write must not block
        self.assertLess(elapsed, 1)
        self.assertTrue(self.ble_mock.is_connected)

    async def test_attach_shares_connection_without_second_notifier(self):
        async def on_notify(*_):
            return None

        notify_calls = 0
        original_start_notify = self.ble_mock.start_notify

        async def counting_start_notify(*args, **kwargs):
            nonlocal notify_calls
            notify_calls += 1
            await original_start_notify(*args, **kwargs)

        self.ble_mock.start_notify = counting_start_notify

        # A reader owns the notifier of the shared connection
        await self.ble_mock.start_notify(NOTIFY_UUID, on_notify)
        self.assertEqual(notify_calls, 1)

        writer = self.build_writer()
        writer.attach()

        start = time.monotonic()
        result = await writer.write(FieldName.CTRL_AC.value, True)
        elapsed = time.monotonic() - start

        self.assertTrue(result)
        self.assertLess(elapsed, 1)
        # The writer must not register a notifier of its own
        self.assertEqual(notify_calls, 1)

    async def test_disconnect_keeps_borrowed_session(self):
        encryption = BluettiEncryption()
        encryption.secure_aes_key = b"0" * 16

        writer = self.build_writer()
        writer.attach(encryption=encryption)

        await writer.disconnect()

        self.assertIs(writer.encryption, encryption)
        self.assertEqual(encryption.secure_aes_key, b"0" * 16)

    async def test_disconnect_attached_keeps_shared_client_connected(self):
        writer = self.build_writer()
        writer.attach()

        await writer.disconnect()

        # The client belongs to the reader's shared connection; the
        # writer must not pull it down and leave the reader stranded.
        self.assertTrue(self.ble_mock.is_connected)
        self.assertFalse(writer._connected)

    async def test_disconnect_resets_own_session(self):
        writer = self.build_writer()
        await writer.connect()
        writer.encryption.secure_aes_key = b"0" * 16

        await writer.disconnect()

        self.assertIsNone(writer.encryption.secure_aes_key)
        self.assertFalse(self.ble_mock.is_connected)

    async def test_standalone_reconnect_resubscribes_notifier(self):
        notify_calls = 0
        original_start_notify = self.ble_mock.start_notify

        async def counting_start_notify(*args, **kwargs):
            nonlocal notify_calls
            notify_calls += 1
            await original_start_notify(*args, **kwargs)

        self.ble_mock.start_notify = counting_start_notify

        # The mock never completes the key exchange, so each connect()
        # attempt fails at the handshake wait. Every attempt must still
        # (re-)subscribe the notifier instead of trusting stale
        # ownership state left over from the previous connection.
        writer = DeviceWriter(
            self.ble_mock,
            self.device,
            DeviceWriterConfig(timeout=1, use_encryption=True),
            asyncio.Lock(),
        )

        self.assertFalse(await writer.connect())
        self.assertFalse(writer._owns_notifier)
        self.assertFalse(await writer.connect())

        self.assertEqual(notify_calls, 2)

    def test_verification_delay_selection(self):
        fast = UIntField(FieldName.DC_INPUT_POWER, 36)
        # Fields with no window of their own take the default, shared
        # controls included.
        self.assertEqual(
            _select_verification_delay(fast),
            DEFAULT_VERIFICATION_WINDOW_SECONDS,
        )
        self.assertEqual(DEFAULT_VERIFICATION_WINDOW_SECONDS, 3.5)

        # A field-declared window wins over the default.
        slow = NumberField(
            FieldName.BATTERY_SOC_RANGE_START,
            3015,
            1,
            0,
            100,
            5,
            verification_delay=5.0,
        )
        self.assertEqual(_select_verification_delay(slow), 5.0)

    async def test_write_bleak_error_clears_notifier_ownership(self):
        writer = DeviceWriter(
            self.ble_mock,
            self.device,
            DeviceWriterConfig(timeout=5, use_encryption=True),
            asyncio.Lock(),
        )

        original_start_notify = self.ble_mock.start_notify

        async def start_notify_completes_handshake(char, callback, **kwargs):
            await original_start_notify(char, callback, **kwargs)
            # Pretend the key exchange completed so connect() proceeds.
            writer.encryption.secure_aes_key = b"0" * 16
            writer.encryption_ready.set()

        self.ble_mock.start_notify = start_notify_completes_handshake

        self.assertTrue(await writer.connect())
        self.assertTrue(writer._owns_notifier)

        async def write_drops_connection(*args, **kwargs):
            raise BleakError("connection dropped")

        self.ble_mock.write_gatt_char = write_drops_connection

        result = await writer.write(FieldName.CTRL_AC.value, True)

        self.assertFalse(result)
        self.assertFalse(writer._connected)
        # Ownership must be forgotten so the next connect() re-subscribes
        # the notifier for the new connection lifecycle.
        self.assertFalse(writer._owns_notifier)
        self.assertFalse(writer.has_notifier)


class DummyMode(Enum):
    OFF = 0
    ECO = 1
    ON = 2


class TestVerifiedSelectWrite(unittest.IsolatedAsyncioTestCase):
    """A select write has to be checked against what the field parses back."""

    async def asyncSetUp(self):
        self.client = ClientMockNoEncryption()
        self.original_write = self.client.write_gatt_char
        self.device = BluettiDevice(
            fields=[
                SelectField(
                    FieldName.CTRL_AC, 150, DummyMode, verification_delay=0.05
                )
            ]
        )
        self.lock = asyncio.Lock()

        self.reader = DeviceReader(
            mac=self.client.address,
            bluetti_device=self.device,
            future_builder_method=asyncio.get_running_loop().create_future,
            config=DeviceReaderConfig(timeout=10),
            lock=self.lock,
            ble_client=self.client,
        )
        self.writer = DeviceWriter(
            self.client,
            self.device,
            DeviceWriterConfig(timeout=10),
            self.lock,
            reader=self.reader,
        )

        async def apply_write(char_specifier, data, response=None):
            command = bytes(data)
            if len(command) >= 4 and command[1] == 6:
                # The station applies the write to the register.
                address, value = struct.unpack_from("!HH", command, 2)
                self.client.add_r_int(address, value)
                return

            await self.original_write(char_specifier, data, response)

        self.client.write_gatt_char = apply_write

    async def asyncTearDown(self):
        await self.reader.disconnect()

    async def test_option_name_is_checked_against_the_parsed_option(self):
        # Home Assistant hands over the option name while the read-back parses
        # to the Enum member. Comparing the two as they were made every
        # verified select write look like it had not taken effect.
        result = await self.writer.write_verified(FieldName.CTRL_AC.value, "ECO")

        self.assertTrue(result.write_succeeded)
        self.assertTrue(result.readback_succeeded)
        self.assertTrue(result.verified)
        self.assertEqual(result.values[FieldName.CTRL_AC.value], DummyMode.ECO)

    async def test_option_member_and_number_are_checked_too(self):
        member = await self.writer.write_verified(
            FieldName.CTRL_AC.value, DummyMode.ON
        )
        self.assertTrue(member.verified)
        self.assertEqual(member.values[FieldName.CTRL_AC.value], DummyMode.ON)

        number = await self.writer.write_verified(FieldName.CTRL_AC.value, 0)
        self.assertTrue(number.verified)
        self.assertEqual(number.values[FieldName.CTRL_AC.value], DummyMode.OFF)

    async def test_a_write_the_station_ignored_is_not_verified(self):
        # The station answers the write but leaves the register alone, so the
        # read-back really does disagree and verification has to say so.
        self.client.write_gatt_char = self.original_write

        result = await self.writer.write_verified(FieldName.CTRL_AC.value, "ECO")

        self.assertFalse(result.verified)

    async def test_an_option_the_field_cannot_hold_is_rejected(self):
        result = await self.writer.write_verified(FieldName.CTRL_AC.value, "NOPE")

        self.assertFalse(result.write_succeeded)
        self.assertFalse(result.verified)


class TestSlowStationIsReRead(unittest.IsolatedAsyncioTestCase):
    """The verification delay is a window, and the read-back is retried in it.

    The AC300 tower applies a control write but only reflects it in its own
    register some time later, by an amount that varies from write to write. A
    single read-back after one fixed pause races that refresh and loses about
    half the time, which reads as a failed write check.
    """

    WINDOW = DEFAULT_VERIFICATION_WINDOW_SECONDS

    async def asyncSetUp(self):
        self.client = ClientMockNoEncryption()
        self.original_write = self.client.write_gatt_char
        self.device = BluettiDevice(
            fields=[SwitchField(FieldName.CTRL_AC, 150)]
        )
        self.lock = asyncio.Lock()
        self.reader = DeviceReader(
            mac=self.client.address,
            bluetti_device=self.device,
            future_builder_method=asyncio.get_running_loop().create_future,
            config=DeviceReaderConfig(timeout=10),
            lock=self.lock,
            ble_client=self.client,
        )
        self.writer = DeviceWriter(
            self.client,
            self.device,
            DeviceWriterConfig(timeout=10),
            self.lock,
            reader=self.reader,
        )
        self.reads = 0

    def lag_station(self, lag: float):
        """Station that only shows a write in its register `lag` seconds on."""
        original = self.original_write

        async def lagged_write(char_specifier, data, response=None):
            command = bytes(data)
            if len(command) >= 4 and command[1] == 6:
                address, value = struct.unpack_from("!HH", command, 2)
                asyncio.get_running_loop().call_later(
                    lag, self.client.add_r_int, address, value
                )
                return

            if len(command) >= 4 and command[1] == 3:
                self.reads += 1

            await original(char_specifier, data, response)

        self.client.write_gatt_char = lagged_write

    async def asyncTearDown(self):
        await self.reader.disconnect()

    async def test_a_quick_station_is_confirmed_on_the_first_read_back(self):
        self.lag_station(0.0)
        self.client.add_r_int(150, 0)

        start = time.monotonic()
        result = await self.writer.write_verified(FieldName.CTRL_AC.value, True)
        elapsed = time.monotonic() - start

        self.assertTrue(result.verified)
        self.assertEqual(self.reads, 1)
        # The first read-back waits out the initial pause, which is what keeps
        # it from answering a request the write itself was still waiting on.
        self.assertGreaterEqual(elapsed, VERIFICATION_FIRST_READ_SECONDS)
        # The window is a ceiling rather than a wait, so a station that applies
        # the write is confirmed after one read-back instead of after the delay.
        self.assertLess(elapsed, self.WINDOW)

    async def test_a_station_that_lags_is_confirmed_once_it_catches_up(self):
        # The refresh lands between the first read-back and the next one.
        self.lag_station(VERIFICATION_FIRST_READ_SECONDS + 0.4)
        self.client.add_r_int(150, 0)

        result = await self.writer.write_verified(FieldName.CTRL_AC.value, True)

        # The first read-back still reports the value from before the write, so
        # verification tries again instead of calling the write a failure.
        self.assertTrue(result.verified)
        self.assertEqual(self.reads, 2)

    async def test_a_station_that_never_catches_up_is_not_verified(self):
        self.lag_station(5.0)
        self.client.add_r_int(150, 0)

        start = time.monotonic()
        result = await self.writer.write_verified(FieldName.CTRL_AC.value, True)
        elapsed = time.monotonic() - start

        self.assertTrue(result.write_succeeded)
        self.assertTrue(result.readback_succeeded)
        self.assertFalse(result.verified)
        self.assertFalse(result.values[FieldName.CTRL_AC.value])
        # The window bounds the work: the retries stop when it runs out.
        self.assertLess(elapsed, self.WINDOW + VERIFICATION_REREAD_SECONDS)


class TestEncryptionReadyWait(unittest.IsolatedAsyncioTestCase):
    async def test_verified_write_does_not_wait_out_the_whole_budget(self):
        # A session that never becomes ready used to hold the polling lock
        # until the entire write timed out, leaving the caller with a bare
        # timeout and no explanation of why nothing was sent.
        client = ClientMockNoEncryption()
        # A writer handed a reader has to share that reader's polling_lock, so
        # the fake carries one and the writer is built with it.
        reader_lock = asyncio.Lock()
        fake_reader = SimpleNamespace(
            encryption=BluettiEncryption(), polling_lock=reader_lock
        )
        writer = DeviceWriter(
            client,
            AC180(),
            DeviceWriterConfig(timeout=4, use_encryption=True),
            reader_lock,
            reader=fake_reader,
        )
        writer.attach()

        start = time.monotonic()
        result = await writer.write_verified(FieldName.CTRL_AC.value, True)
        elapsed = time.monotonic() - start

        self.assertFalse(result.write_succeeded)
        self.assertFalse(result.verified)
        # The wait gives up inside its share of the budget rather than
        # running the whole timeout.
        self.assertGreater(elapsed, 0.5)
        self.assertLess(elapsed, 3.5)
