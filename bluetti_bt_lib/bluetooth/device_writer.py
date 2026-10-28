import asyncio
import logging
import struct
from time import monotonic
from typing import Any
from dataclasses import dataclass

import async_timeout
from bleak import BleakClient
from bleak.exc import BleakError

from .encryption import AES_BLOCK_SIZE, BluettiEncryption, Message, MessageType
from ..const import NOTIFY_UUID, WRITE_UUID
from ..base_devices import BluettiDevice
from ..fields import EnumField
from ..utils.privacy import mac_loggable
from .device_reader import DeviceReader, HANDSHAKE_TIMEOUT_SECONDS
from ..registers import ReadableRegisters, WriteableRegister
from ..devices.charger2 import CHARGER2, CHARGER2_CONTROL_STATES


# A verification delay is a *window*, not a pause: the first read-back goes out
# VERIFICATION_FIRST_READ_SECONDS after the write and is repeated every
# VERIFICATION_REREAD_SECONDS until the register shows the new value or the
# window runs out. A station that applies a write is confirmed by the first
# read-back; one whose register lags is confirmed as soon as it catches up; only
# a write that never shows up uses the whole window.
VERIFICATION_FIRST_READ_SECONDS = 1.0
VERIFICATION_REREAD_SECONDS = 0.5

# Window used when a field does not declare its own. The AC300's registers can
# lag a control by more than three seconds, and the extra half second only costs
# a write that has already failed five read-backs.
DEFAULT_VERIFICATION_WINDOW_SECONDS = 3.5


def _expected_readback(field_obj, value: Any) -> Any:
    """Put a requested write value in the form the field parses back to.

    A select option can be requested by its Enum member, by the option name
    that Home Assistant hands over, or by the raw number, while the read-back
    always parses to the Enum member. Comparing the two as they were made a
    verified select write report itself as not applied.
    """
    if isinstance(field_obj, EnumField):
        option = field_obj.to_option(value)
        if option is not None:
            return option

    return value


@dataclass
class WriteVerificationResult:
    """Result of writing a field and reading its state back from the device.

    ``verified`` is False when none of the read-backs across the verification
    window matched. The device can still have applied the change - a station
    whose registers lag the control it applied only shows the new value on a
    later read - so callers should read that as "not confirmed yet" rather than
    "the write failed".
    """

    write_succeeded: bool
    readback_succeeded: bool
    values: dict[str, Any] | None = None
    verified: bool = False

class DeviceWriterConfig:
    def __init__(self, timeout: int = 15, use_encryption: bool = False):
        self.timeout = timeout
        self.use_encryption = use_encryption


def _select_verification_delay(field_obj) -> float:
    """Pick how long a write's verification read-back may keep trying.

    The value is a window, not a pause: the first read-back goes out
    VERIFICATION_FIRST_READ_SECONDS after the write and is repeated every
    VERIFICATION_REREAD_SECONDS until the register agrees or the window runs
    out. A field can declare its own window for a setting that is genuinely
    slower to apply than the rest; everything else - switches, selects and
    numbers alike, Charger 2 shared controls included - uses
    DEFAULT_VERIFICATION_WINDOW_SECONDS.
    """
    if field_obj is not None and field_obj.verification_delay is not None:
        return field_obj.verification_delay
    return DEFAULT_VERIFICATION_WINDOW_SECONDS


class DeviceWriter:
    def __init__(
        self,
        bleak_client: BleakClient,
        bluetti_device: BluettiDevice,
        config: DeviceWriterConfig = DeviceWriterConfig(),
        lock: asyncio.Lock | None = None,
        reader: DeviceReader | None = None,
    ):
        self.client = bleak_client
        self.reader = reader
        self.bluetti_device = bluetti_device
        self.config = config
        self.polling_lock = lock or asyncio.Lock()
        self.encryption = BluettiEncryption()
        self.encrypted_buffer = bytearray()
        self.encryption_ready = asyncio.Event()
        self.has_notifier = False
        self._connected = False
        self._owns_notifier = False
        self._borrowed_encryption = False
        self._attached = False
        """True when the client belongs to a DeviceReader's shared connection."""
        self._write_future: asyncio.Future[bytes] | None = None
        self._write_command: WriteableRegister | None = None
        """The write _write_future waits for; only its echo or exception settles it."""

        self.logger = logging.getLogger(
            f"{__name__}.{mac_loggable(bleak_client.address).replace(':', '_')}"
        )

    def attach(self, encryption: BluettiEncryption | None = None) -> None:
        """Attach to a client that is already connected and notifying.

        Use this when a DeviceReader and a DeviceWriter share a single
        persistent connection. The notifier (and with it the encryption
        handshake) belongs to the reader, so the writer must not start a
        second one on the same characteristic and cannot wait for write
        acknowledgements it never receives.

        The client itself belongs to the reader as well: disconnect() on
        an attached writer leaves the shared connection alone for the
        reader to tear down.
        """
        self._connected = True
        self._attached = True
        if encryption is not None:
            # Share the session instead of copying keys, so both sides stay
            # in sync for the lifetime of the connection. It is not ours to
            # reset on disconnect.
            self.encryption = encryption
            self._borrowed_encryption = True
        self.encryption_ready.set()

    async def _await_encryption_ready(self) -> bool:
        """Wait for the handshake to finish before a verified write.

        The wait is bounded. An unbounded loop holds the polling lock until the
        whole write times out, which leaves the caller with a bare timeout and
        no explanation, and stops the reader from polling in the meantime.
        """
        # Only part of the write budget goes to the wait: a wait that runs the
        # whole timeout leaves the caller with a bare timeout instead of the
        # reason, and the write itself still needs time to be sent.
        wait_for = min(HANDSHAKE_TIMEOUT_SECONDS, self.config.timeout / 2)
        deadline = monotonic() + wait_for

        while not self.reader.encryption.is_ready_for_commands:
            if monotonic() >= deadline:
                self.logger.warning(
                    "Encryption session was not ready within %ss, so the write "
                    "was not sent",
                    wait_for,
                )
                return False

            await asyncio.sleep(0.1)

        return True

    async def connect(self) -> bool:
        """Establish a persistent connection to the device.
        
        Note: This method should be called while holding the polling_lock
        to avoid race conditions. The write() method handles this automatically.
        """
        if self._connected and self.client.is_connected:
            return True

        if self._attached and self.config.use_encryption:
            # The link and its session belong to the reader, which reconnects
            # it and redoes the key exchange. Reconnecting here would bring the
            # link up with no subscription and the old session: the write
            # would go out encrypted with dead keys and look successful.
            if not self.client.is_connected:
                self.logger.warning(
                    "Shared connection is down; the reader reconnects it on its next poll"
                )
                return False
            self._connected = True
            return True

        try:
            if not self.client.is_connected:
                self.logger.debug("Connecting to device")
                await self.client.connect()

            if (
                self.config.use_encryption
                and not self._attached
                and not self._owns_notifier
            ):
                # An attached writer never owns a notifier: the reader
                # owns the subscription and the handshake on the shared
                # connection. A standalone writer must (re-)subscribe on
                # every new connection: reconnecting reopens the GATT
                # connection on the same client object, so the previous
                # subscription and handshake are gone even though the
                # Python object identity has not changed.
                self.encryption_ready.clear()
                await self.client.start_notify(
                    NOTIFY_UUID, self._notification_handler
                )
                self.has_notifier = True
                self._owns_notifier = True
                await asyncio.wait_for(
                    self.encryption_ready.wait(),
                    timeout=self.config.timeout,
                )

            self.logger.debug("Connected to device")
            self._connected = True
            return True

        except BleakError as err:
            self.logger.warning("Bleak error during connect: %s", err)
            await self._mark_disconnected()
            return False
        except asyncio.CancelledError:
            # Propagate cancellation (HA reload/unload) after tearing
            # down whatever this connect attempt started.
            await self._mark_disconnected()
            raise
        except BaseException as err:
            self.logger.warning("Unknown error during connect: %s", err)
            await self._mark_disconnected()
            return False

    async def _mark_disconnected(self) -> None:
        """Forget a dropped or half-established connection.

        Reconnecting reopens the GATT connection on the same client
        object, so the notify subscription and the encryption handshake
        are gone with it even though the Python object identity has not
        changed. A standalone writer must therefore re-subscribe and
        re-run the handshake on the next connect(), so its notifier
        ownership state and session are cleared here. An attached
        writer leaves the reader's notifier, session and client alone.
        """
        self._connected = False

        if self._attached:
            return

        self.has_notifier = False
        self._owns_notifier = False
        if not self._borrowed_encryption:
            self.encryption.reset()
        self.encrypted_buffer.clear()
        self.encryption_ready.clear()

        if self.client and self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    async def disconnect(self):
        """Disconnect from the device."""
        if not self._connected:
            return

        async with self.polling_lock:
            if self.has_notifier:
                try:
                    await self.client.stop_notify(NOTIFY_UUID)
                except BleakError:
                    pass
                self.has_notifier = False
            # Without this the next connect() skips the subscription and the
            # key exchange, and every later write fails on the reset session.
            self._owns_notifier = False

            # Only disconnect clients that this writer established.
            # When attached, the client belongs to the reader: pulling
            # it down here would kill the reader's connection while the
            # reader still believes it is connected.
            if (
                not self._attached
                and self.client
                and self.client.is_connected
            ):
                await self.client.disconnect()
                self.logger.debug("Disconnected from device")

            if not self._borrowed_encryption:
                self.encryption.reset()
            self.encrypted_buffer.clear()
            self.encryption_ready.clear()
            self._connected = False
            self._attached = False


    async def write(self, field: str, value: Any) -> bool:
        """Write a field, retaining the existing boolean API."""
        return await self._write(field, value, verify=False)

    async def write_verified(
        self, field: str, value: Any
    ) -> WriteVerificationResult:
        """Write a field and read back its actual device state."""
        result = await self._write(field, value, verify=True)
        assert isinstance(result, WriteVerificationResult)
        return result

    async def _write(
        self, field: str, value: Any, verify: bool
    ) -> bool | WriteVerificationResult:
        available_fields = {f.name: f for f in self.bluetti_device.fields}
        field_obj = available_fields.get(field)

        if field_obj is None:
            self.logger.error("Field not supported: %s", field)
            return (
                WriteVerificationResult(False, False)
                if verify
                else False
            )

        if verify and self.reader is None:
            self.logger.error("Verified writes require a DeviceReader")
            return WriteVerificationResult(False, False)

        control_fields = {
            "ctrl_system_on_off": 0,
            "ctrl_silent_charging": 1,
        }
        is_shared_control = (
            isinstance(self.bluetti_device, CHARGER2)
            and field in control_fields
        )

        if is_shared_control:
            if not isinstance(value, bool):
                self.logger.error("Charger 2 control value must be bool")
                return (
                    WriteVerificationResult(False, False)
                    if verify
                    else False
                )
            if self.reader is None:
                self.logger.error(
                    "Charger 2 control requires a reader for register 15600"
                )
                return (
                    WriteVerificationResult(False, False)
                    if verify
                    else False
                )
            command = None
        else:
            command = self.bluetti_device.build_write_command(field, value)
            if command is None:
                self.logger.error(
                    "Write rejected (field not writeable or value out of "
                    "range): %s=%r",
                    field,
                    value,
                )
                return (
                    WriteVerificationResult(False, False)
                    if verify
                    else False
                )

        write_succeeded = False

        async with self.polling_lock:
            try:
                async with async_timeout.timeout(self.config.timeout):
                    if not self._connected:
                        if not await self.connect():
                            return (
                                WriteVerificationResult(False, False)
                                if verify
                                else False
                            )

                    if verify and self.config.use_encryption:
                        if not await self._await_encryption_ready():
                            return WriteVerificationResult(False, False)

                    if is_shared_control:
                        raw_data = await self.reader.read_locked(
                            [ReadableRegisters(15600, 1)],
                            raw=True,
                        )
                        raw_word = raw_data.get(15600) if raw_data else None

                        if raw_word is None or len(raw_word) != 2:
                            self.logger.warning(
                                "Unable to read valid Charger 2 register 15600"
                            )
                            return (
                                WriteVerificationResult(False, False)
                                if verify
                                else False
                            )

                        current_word = struct.unpack("!H", raw_word)[0]
                        current_state = CHARGER2_CONTROL_STATES.get(current_word)

                        if current_state is None:
                            self.logger.warning(
                                "Unknown Charger 2 control value: 0x%04X",
                                current_word,
                            )
                            return (
                                WriteVerificationResult(False, False)
                                if verify
                                else False
                            )

                        state = list(current_state)
                        state[control_fields[field]] = value
                        new_state = tuple(state)

                        new_word = next(
                            (
                                word
                                for word, mapped_state
                                in CHARGER2_CONTROL_STATES.items()
                                if mapped_state == new_state
                            ),
                            None,
                        )
                        if new_word is None:
                            self.logger.error(
                                "No Charger 2 register value for state %s",
                                new_state,
                            )
                            return (
                                WriteVerificationResult(False, False)
                                if verify
                                else False
                            )

                        command = WriteableRegister(15600, new_word)

                    command_bytes = bytes(command)
                    if self.config.use_encryption:
                        command_bytes = self.encryption.aes_encrypt(
                            command_bytes,
                            self.encryption.secure_aes_key,
                            None,
                        )

                    self.logger.debug("Writing command: %s", command)

                    if self._owns_notifier:
                        self._write_command = command
                        self._write_future = (
                            asyncio.get_running_loop().create_future()
                        )

                    await self.client.write_gatt_char(WRITE_UUID, command_bytes)
                    write_succeeded = True

                    if self._write_future is not None:
                        try:
                            response = await asyncio.wait_for(
                                self._write_future, timeout=5
                            )
                            self.logger.debug(
                                "Write response received: %s", response.hex()
                            )
                            if not command.is_valid_response(response):
                                # A MODBUS exception: the station refused it.
                                self.logger.warning(
                                    "Write of %s refused: %s", command, response.hex()
                                )
                                write_succeeded = False
                        except asyncio.TimeoutError:
                            self.logger.warning("Write response timeout")
                            write_succeeded = False

                    if not verify:
                        return write_succeeded

                    if not write_succeeded:
                        return WriteVerificationResult(False, False)

                    # The station needs a moment to reflect a control write in
                    # its own register, and how long that takes varies. Spend
                    # the verification window on repeated read-backs rather than
                    # one fixed pause: the first read-back goes out a second
                    # after the write, then every 0.5s until the register agrees
                    # or the window runs out (see _select_verification_delay).
                    window = _select_verification_delay(field_obj)
                    self.logger.debug(
                        "Write verification: device_class=%s field=%s "
                        "is_shared_control=%s window=%.1fs",
                        type(self.bluetti_device).__name__,
                        field,
                        is_shared_control,
                        window,
                    )

                    async def read_back() -> dict | None:
                        """One read-back of the register(s) holding the field.

                        None when the attempt produced no usable answer.
                        """
                        if is_shared_control:
                            raw_data = await self.reader.read_locked(
                                [ReadableRegisters(15600, 1)],
                                raw=True,
                            )
                            raw_word = raw_data.get(15600) if raw_data else None

                            if raw_word is None or len(raw_word) != 2:
                                return None

                            actual_word = struct.unpack("!H", raw_word)[0]

                            self.logger.debug(
                                "Charger 2 register 15600 read-back: "
                                "raw=%s decoded=0x%04X expected_field=%s "
                                "requested=%r",
                                raw_word.hex(),
                                actual_word,
                                field,
                                value,
                            )

                            if actual_word not in CHARGER2_CONTROL_STATES:
                                self.logger.warning(
                                    "Unknown Charger 2 read-back value: 0x%04X",
                                    actual_word,
                                )
                                return None

                            return self.bluetti_device.parse(15600, raw_word)

                        return await self.reader.read_locked(
                            [
                                ReadableRegisters(
                                    field_obj.address, field_obj.size
                                )
                            ],
                        )

                    expected = _expected_readback(field_obj, value)
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + window
                    step = min(VERIFICATION_FIRST_READ_SECONDS, window)
                    values: dict[str, Any] | None = None
                    attempts = 0
                    verified = False

                    while True:
                        attempts += 1
                        await asyncio.sleep(step)
                        attempt_values = await read_back()

                        if attempt_values and field in attempt_values:
                            values = attempt_values
                            if values.get(field) == expected:
                                verified = True
                                break
                        elif attempts == 1:
                            self.logger.warning(
                                "Write sent, but %s could not be read back",
                                "register 15600"
                                if is_shared_control
                                else f"field {field}",
                            )

                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            break
                        step = min(VERIFICATION_REREAD_SECONDS, remaining)

                    actual_value = values.get(field) if values else None

                    self.logger.debug(
                        "Write verification result for %s: requested %r, "
                        "device reports %r after %d read-back(s)",
                        field,
                        value,
                        actual_value,
                        attempts,
                    )

                    if not verified:
                        # The device accepted the write; the read-backs across
                        # the window still disagreed. The station's own copy of
                        # the register lags the control it has already applied,
                        # so the next poll usually shows the change.
                        self.logger.warning(
                            "Write of %s=%r was accepted, but %d read-back%s "
                            "across %.1fs still reported %r; the change may "
                            "only show up on the next poll",
                            field,
                            value,
                            attempts,
                            "" if attempts == 1 else "s",
                            window,
                            actual_value,
                        )

                    return WriteVerificationResult(
                        write_succeeded=True,
                        readback_succeeded=values is not None,
                        values=values,
                        verified=verified,
                    )

            except TimeoutError:
                self.logger.warning("Timeout during write or verification")
                return (
                    WriteVerificationResult(write_succeeded, False)
                    if verify
                    else False
                )
            except BleakError as err:
                self.logger.warning("Bleak error: %s", err)
                # Drop all per-connection state (standalone writer) so
                # the next write() reconnects, re-subscribes the
                # notifier and re-runs the encryption handshake.
                await self._mark_disconnected()
                return (
                    WriteVerificationResult(write_succeeded, False)
                    if verify
                    else False
                )
            except Exception as err:
                self.logger.warning("Error during write or verification: %s", err)
                return (
                    WriteVerificationResult(write_succeeded, False)
                    if verify
                    else False
                )
            finally:
                self._write_future = None
                self._write_command = None

    def _calculate_expected_encrypted_length(
        self, buffer: bytearray
    ) -> int | None:
        if len(buffer) < 2:
            return None

        data_len = (buffer[0] << 8) + buffer[1]
        _, iv = self.encryption.getKeyIv()
        header_size = 2 if iv is not None else 6
        padded_len = (
            (data_len + AES_BLOCK_SIZE - 1) // AES_BLOCK_SIZE
        ) * AES_BLOCK_SIZE
        return header_size + padded_len

    async def _notification_handler(self, _: int, data: bytearray):
        message = Message(data)

        if message.is_pre_key_exchange:
            message.verify_checksum()

            if message.type == MessageType.CHALLENGE:
                response = self.encryption.msg_challenge(message)
                await self.client.write_gatt_char(WRITE_UUID, response)
            return

        if self.encryption.unsecure_aes_key is None:
            self.logger.error("Received encrypted message before key initialization")
            return

        self.encrypted_buffer.extend(data)
        expected_len = self._calculate_expected_encrypted_length(
            self.encrypted_buffer
        )
        if expected_len is None or len(self.encrypted_buffer) < expected_len:
            return

        complete_message = bytes(self.encrypted_buffer[:expected_len])
        self.encrypted_buffer = self.encrypted_buffer[expected_len:]
        key, iv = self.encryption.getKeyIv()

        try:
            decrypted = Message(self.encryption.aes_decrypt(complete_message, key, iv))
        except ValueError as err:
            self.logger.error("Decryption failed: %s", err)
            self.encrypted_buffer.clear()
            return

        if not decrypted.is_pre_key_exchange:
            # This is a regular response (e.g., write acknowledgment)
            pending = self._write_command
            answer = bytes(decrypted.buffer)
            if (
                self._write_future
                and not self._write_future.done()
                and pending is not None
                and (pending.is_valid_response(answer) or pending.is_valid_exception(answer))
            ):
                self._write_future.set_result(answer)
            else:
                # A late answer or other stray frame must not pass for the ack.
                self.logger.debug("Ignoring frame that does not answer the write: %s", answer.hex())
            return

        decrypted.verify_checksum()
        if decrypted.type == MessageType.PEER_PUBKEY:
            response = self.encryption.msg_peer_pubkey(decrypted)
            await self.client.write_gatt_char(WRITE_UUID, response)
        elif decrypted.type == MessageType.PUBKEY_ACCEPTED:
            self.encryption.msg_key_accepted(decrypted)
            self.encryption_ready.set()
