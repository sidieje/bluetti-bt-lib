import asyncio
import logging
import struct
from time import monotonic
from typing import Any
from dataclasses import dataclass

import async_timeout
from bleak import BleakClient
from bleak.exc import BleakError
from cryptography.exceptions import InvalidSignature

from .encryption import AES_BLOCK_SIZE, BluettiEncryption, Message, MessageType
from .bleak_errors import describe_bleak_error
from ..const import NOTIFY_UUID, WRITE_UUID
from ..base_devices import BluettiDevice
from ..fields import EnumField, PulseSwitchField, SharedRegisterControlField
from ..utils.privacy import mac_loggable
from .device_reader import DeviceReader, HANDSHAKE_TIMEOUT_SECONDS
from ..registers import ReadableRegisters, WriteableRegister


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

# How long a standalone writer waits for the station to answer a write.
WRITE_ACK_TIMEOUT_SECONDS = 5


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
        """Create a writer.

        One link, one lock: when a reader is supplied the writer shares that
        reader's connection, so it must serialise on the reader's polling_lock.
        _write() calls DeviceReader.read_locked(), which is only sound while the
        reader's own lock is held, and an encrypted link carries a single
        session whose stream any interleaved frame breaks. A reader without a
        polling_lock is not a DeviceReader, so that is a TypeError; a lock that
        is not the reader's is refused rather than left to corrupt frames.
        """
        self.client = bleak_client
        self.reader = reader
        self.bluetti_device = bluetti_device
        self.config = config
        if reader is None:
            self.polling_lock = lock or asyncio.Lock()
        else:
            try:
                reader_lock = reader.polling_lock
            except AttributeError:
                raise TypeError("reader must provide a polling_lock") from None
            if lock is not None and lock is not reader_lock:
                raise ValueError(
                    "lock must be the reader's polling_lock: one link, one lock"
                )
            self.polling_lock = reader_lock
        self.encryption = BluettiEncryption()
        self.encrypted_buffer = bytearray()
        self.encryption_ready = asyncio.Event()
        self.handshake_failed = False
        """Set when a key exchange cannot complete; only a new link fixes it."""
        self._handshake_failed_event = asyncio.Event()
        """Wakes _await_handshake() when the exchange failed instead of hung."""
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
        reader to tear down, and the writer stays attached, so a later
        write never subscribes on that client or drops it.

        Attaching is not what establishes the shared lock: pass the reader to
        the constructor (or that reader's polling_lock as lock) so a poll and a
        write can never be on the wire at the same time. A writer attached with
        no reader in sight cannot be checked, so it is the caller's job to hand
        it the reader's lock.

        Under DeviceWriterConfig(use_encryption=True) the session argument is
        not optional in practice: pass the reader's own BluettiEncryption so
        the writer encrypts commands with the keys the reader negotiated. The
        argument stays optional because a shared link may not use encryption at
        all. Attaching without a session on an encrypted link leaves this
        writer holding a fresh un-keyed session, and every write is then
        refused by _await_encryption_ready() with the reason in the log rather
        than sent out unencrypted.
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
        """Wait for the handshake to finish before an encrypted write.

        The wait is bounded. An unbounded loop holds the polling lock until the
        whole write times out, which leaves the caller with a bare timeout and
        no explanation, and stops the reader from polling in the meantime.
        """
        # Only part of the write budget goes to the wait: a wait that runs the
        # whole timeout leaves the caller with a bare timeout instead of the
        # reason, and the write itself still needs time to be sent.
        wait_for = min(HANDSHAKE_TIMEOUT_SECONDS, self.config.timeout / 2)
        deadline = monotonic() + wait_for

        # Wait on the session this writer actually encrypts with, not on the
        # reader's. On a shared connection attach() borrowed that very object,
        # so the two are one and the same whenever a session was handed over;
        # checking self.encryption additionally refuses a writer that was
        # attached without one, which otherwise passed here and then failed
        # inside aes_encrypt() with a key of None.
        while not self.encryption.is_ready_for_commands:
            if monotonic() >= deadline:
                self.logger.warning(
                    "Encryption session was not ready within %ss, so the write "
                    "was not sent",
                    wait_for,
                )
                return False

            await asyncio.sleep(0.1)

        return True

    async def _await_handshake(self) -> bool:
        """Wait for the key exchange of a fresh standalone connection.

        Returns as soon as the exchange either completes or fails. A failed
        exchange never recovers over a live link: the peer only sends a new
        challenge on a new connection, so sitting out the whole timeout would
        burn the caller's write budget and leave nothing in the log naming
        the cause.
        """
        ready = asyncio.ensure_future(self.encryption_ready.wait())
        failed = asyncio.ensure_future(self._handshake_failed_event.wait())
        try:
            done, _ = await asyncio.wait(
                {ready, failed},
                timeout=self.config.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            ready.cancel()
            failed.cancel()

        if self.encryption_ready.is_set():
            return True

        if self.handshake_failed:
            self.logger.warning("Key exchange failed, dropping the connection")
        elif not done:
            self.logger.warning(
                "Key exchange did not complete within %ss", self.config.timeout
            )
        return False

    def _fail_handshake(self) -> None:
        """Stop waiting for a key exchange that cannot complete.

        Called from the notification callback, where raising is not an option
        because bleak runs it as a detached task and the exception is lost.
        """
        self.handshake_failed = True
        self._handshake_failed_event.set()

    async def _write_handshake(self, payload: bytes, what: str) -> bool:
        """Send a key exchange reply.

        A BleakError raised inside a notification callback is swallowed by
        bleak's dispatch, which leaves connect() waiting its full timeout for
        an event that can no longer arrive with nothing in the log naming the
        cause. Catching it here and flagging the handshake lets the writer
        give the connection up instead.
        """
        try:
            await self.client.write_gatt_char(WRITE_UUID, payload)
            return True
        except BleakError as err:
            self.logger.warning("Failed to send the %s: %s", what, err)
            self._fail_handshake()
            return False

    async def connect(self) -> bool:
        """Establish a persistent connection to the device.
        
        Note: This method should be called while holding the polling_lock
        to avoid race conditions. The write() method handles this automatically.
        """
        if self._connected and self.client.is_connected:
            if (
                self.config.use_encryption
                and not self._attached
                and not self.encryption.is_ready_for_commands
            ):
                # The link is up but its session is not. A key exchange that
                # failed after the session was already up leaves nothing to
                # encrypt a command with, and the peer only restarts the
                # exchange on a new connection, so rebuilding here is what
                # keeps one bad re-exchange from wedging the writer for the
                # rest of its life.
                self.logger.warning(
                    "Connection carries no encryption session, reconnecting"
                )
                await self._mark_disconnected()
            else:
                return True

        if self._attached and self.config.use_encryption:
            # The link and its session belong to the reader: whoever owns the
            # client reconnects it, and the reader redoes the key exchange.
            # Reconnecting here would bring the link up with no subscription
            # and the old session: the write would go out encrypted with dead
            # keys and look successful.
            if not self.client.is_connected:
                self.logger.warning(
                    "Shared connection is down; the client's owner reconnects "
                    "it (a reader with its own connection reconnects on a new "
                    "client, which needs a new writer)"
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
                # Cleared here rather than in _mark_disconnected(), so the
                # flag still records why the last attempt failed after that
                # attempt has been torn down.
                self.handshake_failed = False
                self._handshake_failed_event.clear()
                await self.client.start_notify(
                    NOTIFY_UUID, self._notification_handler
                )
                self.has_notifier = True
                self._owns_notifier = True
                if not await self._await_handshake():
                    await self._mark_disconnected()
                    return False

            self.logger.debug("Connected to device")
            self._connected = True
            return True

        except BleakError as err:
            self.logger.warning("BLE connect failed: %s", describe_bleak_error(err))
            await self._mark_disconnected()
            return False
        except asyncio.CancelledError:
            # Propagate cancellation (HA reload/unload) after tearing
            # down whatever this connect attempt started.
            await self._mark_disconnected()
            raise
        except Exception as err:
            # Exception, not BaseException: KeyboardInterrupt and SystemExit
            # end the process, and GeneratorExit discards this coroutine. None
            # of them is a connection failure, and awaiting teardown inside one
            # turns the shutdown into "coroutine ignored GeneratorExit".
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

        # The link may still be up: a key exchange that failed after the
        # session was established leaves the subscription in place. Drop it
        # before the link goes, or the next connect() subscribes a second time,
        # which some backends (bleak-esphome) refuse for good. The reader
        # unsubscribes the same way in its own _mark_disconnected().
        if self.has_notifier and self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(NOTIFY_UUID)
            except Exception as err:
                # Cleanup must not hold up the reconnect, but a subscription
                # the backend kept is exactly what a later double subscribe
                # would come from, so leave a trace of who refused.
                self.logger.debug("Failed to unsubscribe: %s", err)
        self.has_notifier = False
        self._owns_notifier = False
        if not self._borrowed_encryption:
            self.encryption.reset()
        self.encrypted_buffer.clear()
        self.encryption_ready.clear()

        if self.client and self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception as err:
                # Raising is normal on a link the stack already dropped, hence
                # debug rather than warning.
                self.logger.debug("Failed to disconnect: %s", err)

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
            self.handshake_failed = False
            self._handshake_failed_event.clear()
            self._connected = False


    async def write(self, field: str, value: Any) -> bool:
        """Write a field, retaining the existing boolean API."""
        return await self._write(field, value, verify=False)

    async def write_verified(
        self, field: str, value: Any
    ) -> WriteVerificationResult:
        """Write a field and read back its actual device state.

        A pulse control whose state already matches the request is reported as
        verified without sending the pulse; see the skip in _write().
        """
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

        # A SharedRegisterControlField is one boolean half of a state word, so
        # writing it is a read-modify-write of the whole register: the word to
        # send depends on what is already there, which needs a reader. Which
        # register, which boolean in the state tuple, and which words are legal
        # all come from the field itself, so nothing here names a product.
        shared_control = (
            field_obj
            if isinstance(field_obj, SharedRegisterControlField)
            else None
        )
        is_shared_control = shared_control is not None
        # A pulse control has to be checked against the state before it is sent;
        # see the skip inside the lock below.
        pulse = field_obj if isinstance(field_obj, PulseSwitchField) else None
        device_label = type(self.bluetti_device).__name__

        if is_shared_control:
            if not isinstance(value, bool):
                self.logger.error(
                    "%s control value must be bool: %s=%r", device_label, field, value
                )
                return (
                    WriteVerificationResult(False, False)
                    if verify
                    else False
                )
            if self.reader is None:
                self.logger.error(
                    "%s control %s requires a reader for register %d",
                    device_label,
                    field,
                    shared_control.address,
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

                    if self.config.use_encryption:
                        # Any encrypted write needs a session, not only a
                        # verified one. An unverified write used to skip this
                        # and reach aes_encrypt() with no key, which surfaced
                        # as a TypeError logged as a generic write failure.
                        if not await self._await_encryption_ready():
                            return (
                                WriteVerificationResult(False, False)
                                if verify
                                else False
                            )

                    if is_shared_control:
                        address = shared_control.address
                        raw_data = await self.reader.read_locked(
                            [ReadableRegisters(address, 1)],
                            raw=True,
                        )
                        raw_word = raw_data.get(address) if raw_data else None

                        if raw_word is None or len(raw_word) != 2:
                            self.logger.warning(
                                "Unable to read valid %s register %d",
                                device_label,
                                address,
                            )
                            return (
                                WriteVerificationResult(False, False)
                                if verify
                                else False
                            )

                        current_word = struct.unpack("!H", raw_word)[0]
                        current_state = shared_control.state_table.get(current_word)

                        if current_state is None:
                            self.logger.warning(
                                "Unknown %s control value: 0x%04X",
                                device_label,
                                current_word,
                            )
                            return (
                                WriteVerificationResult(False, False)
                                if verify
                                else False
                            )

                        state = list(current_state)
                        state[shared_control.state_index] = value
                        new_state = tuple(state)

                        new_word = next(
                            (
                                word
                                for word, mapped_state
                                in shared_control.state_table.items()
                                if mapped_state == new_state
                            ),
                            None,
                        )
                        if new_word is None:
                            self.logger.error(
                                "No %s register value for state %s",
                                device_label,
                                new_state,
                            )
                            return (
                                WriteVerificationResult(False, False)
                                if verify
                                else False
                            )

                        command = WriteableRegister(address, new_word)

                    if pulse is not None and self.reader is not None:
                        # A pulse moves the station whichever way it is asked, so
                        # a request that already matches the state it reports must
                        # not send one: the station would land in the other state,
                        # and the read-backs below would report that change as a
                        # write that had not taken effect. Home Assistant retries
                        # a switch service call, so the redundant request is the
                        # common case rather than a corner of it.
                        current_values = await self.reader.read_locked(
                            [ReadableRegisters(pulse.address, pulse.size)]
                        )
                        current = (
                            current_values.get(field) if current_values else None
                        )

                        if current is None:
                            # Nothing to condition the pulse on. The state word is
                            # the only one these controls answer, so fall back to
                            # the pulse rather than refuse the control outright.
                            self.logger.warning(
                                "Unable to read the %s state before pulsing %d: "
                                "sending it anyway",
                                field,
                                pulse.write_address,
                            )
                        elif current == value:
                            self.logger.debug(
                                "%s already reports %r: skipping the pulse to %d",
                                field,
                                value,
                                pulse.write_address,
                            )
                            return (
                                WriteVerificationResult(
                                    write_succeeded=True,
                                    readback_succeeded=True,
                                    values={field: current},
                                    verified=True,
                                )
                                if verify
                                else True
                            )

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
                                self._write_future,
                                timeout=WRITE_ACK_TIMEOUT_SECONDS,
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
                            # The answer may still arrive and be taken for the
                            # next write's: a refusal names no register. Drop
                            # the link so the next write starts on a fresh one.
                            await self._mark_disconnected()

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

                        A shared control word needs no special case here: the
                        field's own address and size name exactly the register
                        the read-modify-write targeted, and its parse() decodes
                        every half of the word. An unknown word makes parse()
                        raise, which leaves the field out of the answer and
                        surfaces as a skipped-field streak on the device.
                        """
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
                                f"register {shared_control.address}"
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
                self.logger.warning(
                    "BLE write failed: %s; the link will be rebuilt on the"
                    " next write",
                    describe_bleak_error(err),
                )
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
                # Not only a BleakError can leave a link untrustworthy. An
                # error part way through a write means a frame may have gone
                # out whose fate is unknown, and a station that answered with
                # something this code could not parse usually answers the next
                # write the same way. Rebuild like the BleakError branch, which
                # leaves an attached writer's shared client alone: for one of
                # those, _mark_disconnected only clears this writer's state and
                # the client's owner reconnects.
                self.logger.warning("Error during write or verification: %s", err)
                await self._mark_disconnected()
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

    def _kex_context(self, kind: str) -> str:
        """Name the link a key exchange frame arrived on, for the checksum log."""
        address = getattr(self.client, "address", "unknown")
        return f"{type(self.bluetti_device).__name__} {address} {kind}"

    async def _notification_handler(self, _: int, data: bytearray):
        """Handle notifications on a connection this writer owns itself.

        Only a standalone writer ever subscribes this callback. A writer
        attached to a reader's shared connection does not subscribe at all
        (see attach()), so on the persistent link the reader's own handler is
        the only one that runs and the two are not twins to keep in step.
        This handler therefore carries no connection lifecycle and no
        reconnect logic, but it does have to survive a bad frame: bleak runs
        the callback as a detached task, so anything raised here is lost and
        the writer is left waiting for a handshake that can no longer arrive.
        """
        message = Message(data)

        if message.is_pre_key_exchange:
            message.verify_checksum(self._kex_context("plaintext"))

            try:
                message_type = message.type
            except (ValueError, IndexError):
                # Unknown type byte, or a frame too short to carry one.
                self.logger.warning("Unknown key exchange message type")
                return

            if message_type == MessageType.CHALLENGE:
                response = self.encryption.msg_challenge(message)
                if response is None:
                    # A challenge we cannot turn into a reply means there is
                    # no session to wait for: give up now rather than sit out
                    # the caller's timeout on a handshake that cannot happen.
                    self._fail_handshake()
                    return
                await self._write_handshake(response, "challenge response")
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
            # Same guard, same reasoning as the reader's handler: ValueError
            # covers the block-alignment check and cryptography's complaints
            # about a key or IV of the wrong size, and a TypeError for a
            # missing key is not reachable because getKeyIv() only returns
            # bytes or a key that was already checked for None. The frame was
            # taken out of the buffer before the decrypt, so the clear() here
            # is belt-and-braces rather than the recovery.
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

        decrypted.verify_checksum(self._kex_context("decrypted"))
        try:
            decrypted_type = decrypted.type
        except (ValueError, IndexError):
            self.logger.warning("Unknown key exchange message type")
            return

        if decrypted_type == MessageType.CHALLENGE:
            # A station can restart the exchange inside the encrypted channel,
            # and this is the only place a standalone writer sees it. Answering
            # is not a courtesy: the reply is what refreshes the unsecure IV
            # that the peer pubkey signature is later verified against, so
            # ignoring the challenge makes the pubkey that follows look like it
            # carries a bad signature.
            challenge_response = self.encryption.msg_challenge(decrypted)
            if challenge_response is not None:
                await self._write_handshake(
                    self.encryption.aes_encrypt(challenge_response, key, None),
                    "challenge response",
                )
            return

        if decrypted_type == MessageType.CHALLENGE_ACCEPTED:
            self.logger.debug("Challenge accepted (encrypted)")
            return

        if decrypted_type == MessageType.PEER_PUBKEY:
            try:
                response = self.encryption.msg_peer_pubkey(decrypted)
            except (InvalidSignature, ValueError) as err:
                # Usually the stale IV the branch above exists to prevent. A
                # ValueError instead means the frame never got that far: it was
                # too short or too long to hold a signed pubkey at all
                # (verify_and_extract_signed_data() wants exactly 128 bytes), or
                # the pubkey inside it will not parse. It used to escape into
                # bleak's detached callback dispatch, where it was lost with the
                # handshake left marked healthy, so connect() waited out its
                # whole timeout for keys that could no longer arrive.
                # The session cannot be salvaged either way, so report the
                # handshake as failed instead of waiting on keys that never
                # settle, and drop the session the link would otherwise keep
                # encrypting with. Only a new connection makes the peer challenge
                # again; tearing the link down is not this writer's job, since
                # the reader owns it wherever this handler does not run at all.
                self.logger.warning(
                    "Peer pubkey rejected (%s)", str(err) or "bad signature"
                )
                self.encryption.reset()
                self.encrypted_buffer.clear()
                self.encryption_ready.clear()
                self._fail_handshake()
                return
            await self._write_handshake(response, "peer pubkey reply")
            return

        if decrypted_type == MessageType.PUBKEY_ACCEPTED:
            try:
                self.encryption.msg_key_accepted(decrypted)
            except (ValueError, AttributeError) as err:
                # A refused acceptance, or one that arrives with no local
                # keypair left to finish, leaves no session to mark ready.
                self.logger.warning("Key exchange rejected: %s", err)
                self._fail_handshake()
                return
            # A finished exchange supersedes any earlier failure on this link,
            # so the writer never reports a working session and a failed
            # handshake at the same time.
            self.handshake_failed = False
            self._handshake_failed_event.clear()
            self.encryption_ready.set()
