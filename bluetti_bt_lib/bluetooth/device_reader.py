import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

import async_timeout
from typing import Any, Callable, List, cast
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from cryptography.exceptions import InvalidSignature

from .encryption import BluettiEncryption, Message, MessageType, AES_BLOCK_SIZE
from ..base_devices import BluettiDevice, STARTUP_FIELDS
from ..const import NOTIFY_UUID, WRITE_UUID
from ..fields import FieldName
from ..registers import ReadableRegisters, DeviceRegister
from ..utils.privacy import mac_loggable

# A station needs a moment after the pack selector is written before its pack
# registers hold the data of the newly selected pack.
PACK_SELECTOR_SETTLE_SECONDS = 3

# A slot that holds a pack usually answers long before that wait is over, while
# an empty slot needs all of it. Take an early look and only wait out the
# remainder for a slot that came back empty, so a station with packs in most
# of its slots does not pay the full settle on every one.
PACK_SELECTOR_EARLY_LOOK_SECONDS = 1

# How long a single request waits for its answer.
RESPONSE_TIMEOUT_SECONDS = 5

# A read that goes unanswered is asked again before the cycle is given up. Only
# a read is repeated: the answer to the first attempt, however late it lands,
# is an answer to the same request, so it settles that read correctly. The
# answer to the other attempt can still arrive after the read is done, which is
# what STRAY_ANSWER_SECONDS is for. A write is never resent.
READ_ATTEMPTS = 2

# A station that skipped a request usually skipped it because it was busy, so a
# retry fired off immediately can land in the same busy moment.
RETRY_PAUSE_SECONDS = 0.2

# A read that was asked twice can be answered twice: the first answer settles
# the request, the second one is still on its way. Sent on at once, the next
# request of the same size would take that second answer for its own (a
# Charger 2 filed the answer to 15584 under 15600, and that of 15600 under
# 15626). So after a retried read the reader waits this long for the stray
# answer, discarding it, before it asks anything else. The wait ends early when
# a differing answer is caught; 3 s is not tuned to measured station timings.
STRAY_ANSWER_SECONDS = 3

# How long a fresh connection gets to finish its key exchange. The device
# starts the exchange on its own once notifications are on, so it normally
# takes a second or two.
HANDSHAKE_TIMEOUT_SECONDS = 20

# A link kept by _resync_link() is resumed by the next connect(). After this
# many resyncs in a row without a single answer it is dropped instead, so a
# link the stack still reports as connected cannot hold the reader forever.
MAX_RESYNCS_ON_ONE_LINK = 3

# An answer this late is worth remembering when a later request fails: a station
# that is about to stop answering usually starts by answering slowly.
SLOW_REQUEST_SECONDS = 1.5

# How much of an unusable answer to quote in the log. Enough to tell a late
# reply to an earlier request from noise.
RX_SAMPLE_BYTES = 12


class ResponseTimeout(Exception):
    """No valid answer to a request arrived in time.

    The link is left in an unknown state: an answer may still be on its way and
    would otherwise be taken for the next request, so the caller must stop using
    this connection instead of sending another command.

    ``detail`` says what the station did send, which is the difference between a
    link that went quiet and one that answered something unusable.
    """

    def __init__(self, registers: DeviceRegister, detail: str = ""):
        message = f"no valid response to {registers}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
        self.registers = registers
        self.detail = detail


@dataclass
class LinkStats:
    """What the last read cycle did on the wire.

    A consumer such as Home Assistant only ever sees the warning a failed cycle
    leaves behind, so this is where the evidence about *why* it failed is kept:
    which request was cut short, how many answered before it, how slow the
    slowest answer of the cycle was, and what the station actually sent for the
    request it left unanswered.
    """

    requests: int = 0
    """Requests sent in this cycle, counting one that went unanswered."""
    answered: int = 0
    """Requests in this cycle that got a valid answer."""
    slowest: float = 0.0
    """Longest wait for a valid answer in this cycle, in seconds."""
    slowest_registers: str = ""
    """The request that took that long to answer."""
    aborted: str = ""
    """The request that went unanswered, when the cycle was cut short."""
    failure: str = ""
    """How it went unanswered: silent, partial, mismatch, or session (the
    encryption session was gone, so nothing could be asked)."""
    received: int = 0
    """Bytes the station sent about the request it did not answer."""
    sample: str = ""
    """The start of those bytes, hex, to tell a late answer from noise."""
    attempts: int = 0
    """Times the aborted request was sent before it was given up on."""
    retried: int = 0
    """Reads in this cycle that were sent a second time."""
    recovered: int = 0
    """Reads in this cycle that only answered on the second attempt."""
    drained: int = 0
    """Waits for a stray second answer after a retried read (see
    _drain_stray_answer), whether or not one arrived."""
    discarded: int = 0
    """Stray second answers the drain actually caught and threw away. A second
    answer identical to the first is dropped earlier by the duplicate filter in
    _notification_handler and is not counted here."""
    abort_streak: int = 0
    """Cycles in a row that had an abort, not counting the one in progress."""


class DeviceReaderConfig:
    def __init__(self, timeout: int = 60, use_encryption: bool = False):
        self.timeout = timeout
        self.use_encryption = use_encryption


class DeviceReader:
    def __init__(
        self,
        mac: str,
        bluetti_device: BluettiDevice,
        future_builder_method: Callable[[], asyncio.Future[Any]],
        config: DeviceReaderConfig = DeviceReaderConfig(),
        lock: asyncio.Lock | None = None,
        ble_client: BleakClient | None = None,
    ):
        self.mac = mac
        self.bluetti_device = bluetti_device
        self.create_future = future_builder_method
        self.config = config
        self.polling_lock = lock or asyncio.Lock()

        self.ble_client = ble_client
        """Used for unittests"""

        self.logger = logging.getLogger(
            f"{__name__}.{mac_loggable(mac).replace(':', '_')}"
        )

        self.device = None
        self.client = None

        self.has_notifier = False
        self._notified_client = None
        """Client whose notify subscription is confirmed live. Only set
        after start_notify() succeeds, so a failed setup is retried."""
        self.current_registers = None
        self.notify_response = bytearray()
        self.notify_future: asyncio.Future[Any] | None = None
        self.encryption = BluettiEncryption()
        self.encrypted_buffer = bytearray()
        self.last_notification: bytes | None = None
        """Raw bytes of the previous notification, used to drop repeats."""
        self.handshake_failed = False
        """Set when the key exchange cannot complete; only a reconnect fixes it."""
        self._startup_values: dict[str, Any] = {}
        """Answers for the infrequent fields (bluetti_device.STARTUP_FIELDS).
        They are asked for once per connection and reported from here for the
        rest of it, so a consumer still sees the same keys every cycle."""
        self._connected = False
        self._resyncs = 0
        """Resyncs since the kept link last answered (see MAX_RESYNCS_ON_ONE_LINK)."""
        self._resynced_at = 0.0
        """When the last resync gave up on a request (time.monotonic())."""
        self._answered = False
        """Whether this connection has answered a request yet. A station that
        is still waking tends to miss the first one, so that miss is logged
        quietly; after the link has proven itself a missing answer is news."""
        self.link_stats = LinkStats()
        """What the last read cycle did on the wire; see LinkStats."""
        self._rx_bytes = 0
        self._rx_frames = 0
        self._rx_unexpected = bytearray()
        self._cycle_aborted = False
        """Set when a read cycle ended in an abort, to carry the streak over."""
        self._retry_pending = False
        """Set while a read is being asked for a second time, so the answer can
        be counted as a recovery rather than a plain answer."""
        # MODBUS bytes and notification frames seen about the request in
        # flight, so a timeout can say whether the station stayed quiet or
        # answered something that was not usable. _rx_unexpected keeps the start
        # of a frame that was discarded, which is otherwise thrown away before
        # the timeout can describe it.

    async def connect(self) -> bool:
        """Establish a persistent connection to the device.
        
        Note: This method should be called while holding the polling_lock
        to avoid race conditions. The read() method handles this automatically.
        """
        if self._connected and self.client and self.client.is_connected:
            return True

        new_link = False
        try:
            if self.ble_client:
                self.device = None
                self.client = self.ble_client
            elif (
                self.client is not None
                and self.client.is_connected
                and self._resyncs < MAX_RESYNCS_ON_ONE_LINK
                and (
                    self.encryption.is_ready_for_commands
                    or not self.config.use_encryption
                )
            ):
                # _resync_link() kept this link, and its encryption session,
                # up on purpose. Resume it: a station that does not advertise
                # while connected is not found by a new scan, and a second
                # establish_connection() would stack a link on the live one.
                # A lost session is not resumed: only a new link makes the
                # peer issue a fresh challenge.
                self.logger.debug("Resuming the kept connection")
            else:
                if self.client is not None and self.client.is_connected:
                    self.logger.debug(
                        "Not resuming the kept connection (%d resyncs, session ready: %s), dropping it",
                        self._resyncs,
                        self.encryption.is_ready_for_commands,
                    )
                    await self._mark_disconnected()

                self.device = await BleakScanner.find_device_by_address(
                    self.mac, timeout=5
                )

                if self.device is None:
                    self.logger.error("Device not found")
                    return False

                self.logger.debug("Connecting to device")

                self.client = await establish_connection(
                    BleakClientWithServiceCache,
                    self.device,
                    self.device.name or "Unknown Device",
                    max_attempts=10,
                )
                # A new link means a new key exchange: a session, buffer or
                # duplicate filter left by the previous link must not make the
                # handshake look finished or garble the peer's first frames.
                self.encryption.reset()
                self.encrypted_buffer.clear()
                self.last_notification = None
                self._resyncs = 0
                # The startup fields belong to the link that answered them.
                self._startup_values.clear()
                new_link = True

            # _notified_client tracks the client whose subscription is
            # confirmed live. has_notifier records that a subscription
            # exists (or may exist) on the current client so disconnect()
            # can stop it even when start_notify() raised after already
            # registering the callback. Because _notified_client is only
            # set on success, a failed setup is retried by the next
            # connect() instead of being skipped as already subscribed.
            if self.client is not self._notified_client:
                self.has_notifier = True
                await self.client.start_notify(
                    NOTIFY_UUID, self._notification_handler
                )
                self._notified_client = self.client

            self.logger.debug("Connected to device")

            if self.config.use_encryption:
                self.handshake_failed = False
                if not await self._await_handshake():
                    await self._abandon_handshake()
                    return False

            if not new_link:
                # The request a resync gave up on may still be answered on
                # this link, and an answer of the same size would pass for
                # the next request's. Let it land, with nothing pending,
                # before anything is sent again.
                quiet = (
                    self._resynced_at
                    + RESPONSE_TIMEOUT_SECONDS
                    + STRAY_ANSWER_SECONDS
                    - time.monotonic()
                )
                if quiet > 0:
                    self.logger.debug(
                        "Waiting %.1fs for a late answer before sending", quiet
                    )
                    await asyncio.sleep(quiet)

            self._connected = True
            self._answered = False
            return True

        except BleakError as err:
            self.logger.warning("Bleak error during connect: %s", err)
            await self._cleanup_failed_connect()
            return False
        except asyncio.CancelledError:
            # Home Assistant cancels tasks on reload/unload. Tear down
            # whatever we started, then let the cancellation propagate
            # instead of reporting it as an ordinary connection failure.
            await self._cleanup_failed_connect()
            raise
        except BaseException as err:
            self.logger.warning("Unknown error during connect: %s", err)
            await self._cleanup_failed_connect()
            return False

    async def _mark_disconnected(self) -> None:
        """Forget a connection that the device or the stack dropped.

        The notify subscription and the encryption session are only valid
        for the lifetime of a single connection, so they are cleared here
        as well. Without this, the next connect() would see the stale
        has_notifier flag, skip start_notify() on the replacement client
        and then never receive a response again. The stale client is
        disconnected too (when we own it) so a fresh connect cannot
        stack a second connection on top of a half-dead one.
        """
        self._connected = False
        # The link may still be up (a BleakError on a live link, a supplied
        # client): unsubscribe, or the next connect() subscribes a second time,
        # which some backends (bleak-esphome) refuse for good.
        if self.has_notifier and self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(NOTIFY_UUID)
            except Exception:
                pass
        self.has_notifier = False
        self._notified_client = None
        self.encryption.reset()
        self.encrypted_buffer.clear()
        self.last_notification = None
        # The infrequent fields belong to the connection that answered them. A
        # station that drops and comes back may have been renamed or reflashed
        # in the meantime, so ask for them again.
        self._startup_values.clear()

        if self.ble_client is None and self.client and self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    async def _cleanup_failed_connect(self) -> None:
        """Tear down a half-established connection.

        If connect() fails after the client was created (for example the
        encryption handshake never completed), leaving that client
        connected would leak it: the next connect() would open a second
        connection to the device instead of starting from a clean state.

        A caller-supplied ble_client is not ours to tear down; the
        caller cleans it up through disconnect().
        """
        if self.ble_client is not None:
            return

        self._connected = False

        if self.has_notifier and self.client:
            try:
                await self.client.stop_notify(NOTIFY_UUID)
            except Exception:
                pass
            self.has_notifier = False
            self._notified_client = None

        if self.client and self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception:
                pass

        self.encryption.reset()
        self.encrypted_buffer.clear()
        self.last_notification = None

    async def _await_handshake(self) -> bool:
        """Wait for the key exchange of a fresh connection to finish.

        A failed exchange never recovers over a live link: the peer only sends
        a new challenge on a new connection. Waiting longer than the deadline
        would just burn the caller's timeout and leave a half-open connection
        behind, which the next connect() would stack a second link on top of.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HANDSHAKE_TIMEOUT_SECONDS

        while not self.encryption.is_ready_for_commands:
            if self.handshake_failed:
                self.logger.warning(
                    "Key exchange failed, reconnecting on the next read"
                )
                return False

            if loop.time() >= deadline:
                self.logger.warning(
                    "Key exchange did not complete within %ss, reconnecting",
                    HANDSHAKE_TIMEOUT_SECONDS,
                )
                return False

            await asyncio.sleep(1)
            self.logger.debug("Waiting for encryption handshake...")

        return True

    async def _abandon_handshake(self) -> None:
        """Tear down a link whose key exchange cannot complete.

        Unlike _mark_disconnected() this also closes a caller-supplied client:
        a live link without a session can never carry a command, and dropping
        it is the only way to get the peer to restart the exchange. The owner
        of a supplied client rebuilds the connection from its disconnect
        callback.
        """
        self.handshake_failed = False
        self._connected = False
        self.current_registers = None
        self.notify_response = bytearray()
        self.notify_future = None

        if self.has_notifier and self.client:
            try:
                await self.client.stop_notify(NOTIFY_UUID)
            except Exception:
                pass
            self.has_notifier = False
            self._notified_client = None

        if self.client and self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception:
                pass

        self.encryption.reset()
        self.encrypted_buffer.clear()
        self.last_notification = None
        # Like _mark_disconnected(): the infrequent fields belong to the
        # connection that answered them, and this one is gone.
        self._startup_values.clear()

    async def _recover_from_timeout(self) -> None:
        """Get the link ready for the next cycle after a request went unanswered.

        Normally that is a resync. A lost encryption session cannot be resynced
        though: only a new connection makes the peer issue a fresh challenge.
        """
        if self._session_lost():
            await self._abandon_handshake()
        else:
            await self._resync_link()

    def _session_lost(self) -> bool:
        """Whether the link has no session left to carry a command."""
        return self.config.use_encryption and not self.encryption.is_ready_for_commands

    async def _resync_link(self) -> None:
        """Reset the notification pipeline without dropping the connection.

        A request that got no valid answer leaves the link desynced: an answer
        may still be on its way and would otherwise be handed to the next
        request. Unsubscribing discards whatever is in flight, and clearing the
        subscription flags makes the next connect() subscribe again.

        Unlike _mark_disconnected() this keeps the encryption session: the
        device only restarts a handshake when the connection itself drops, so
        resetting the session over a live link would wedge the device. The
        connection is not ours to drop either when a client was supplied.

        A retried read that does get an answer never comes here, although the
        second answer to it may still be on its way; _drain_stray_answer()
        catches that one instead. See its docstring for how the two relate.
        """
        self.logger.debug("Resyncing the notification pipeline")

        if self._connected:
            # Once per failure: the pack selector restore and read() can both
            # resync after the same one.
            self._resyncs += 1
        # Before anything that awaits: a second cancel there must still leave
        # the next read() to reconnect rather than send over this link as is.
        self._connected = False
        self._resynced_at = time.monotonic()
        if self.client is not None and not self.client.is_connected:
            # The link is already gone, and its session with it. A client its
            # owner brings back up needs a new key exchange, and the startup
            # fields are asked for again on it.
            self.encryption.reset()
            self._startup_values.clear()

        self.current_registers = None
        self.notify_response = bytearray()
        self.notify_future = None
        self.encrypted_buffer.clear()
        self.last_notification = None

        if self.has_notifier and self.client:
            try:
                await self.client.stop_notify(NOTIFY_UUID)
            except Exception:
                pass
            self.has_notifier = False
            self._notified_client = None

    async def disconnect(self):
        """Clean up the reader's notification and connection state."""
        async with self.polling_lock:
            if self.has_notifier and self.client:
                try:
                    await self.client.stop_notify(NOTIFY_UUID)
                    self.logger.debug("Stopped notifier")
                except Exception:
                    pass
                self.has_notifier = False
                self._notified_client = None

            # Only disconnect clients created and managed by this reader.
            # A supplied ble_client belongs to the caller.
            if (
                self.ble_client is None
                and self.client
                and self.client.is_connected
            ):
                await self.client.disconnect()
                self.logger.debug("Disconnected from device")

            self.encryption.reset()
            self.encrypted_buffer.clear()
            self.last_notification = None
            self._connected = False
            self._startup_values.clear()

    async def _read_uint16(self, address: int) -> int | None:
        """Read one uint16 register, or None when the answer is too short."""
        registers = ReadableRegisters(address, 1)
        body = registers.parse_response(await self._async_send_command(registers))
        if len(body) < 2:
            return None
        return int.from_bytes(body[:2], "big")

    async def _restore_pack_selector(
        self, selector_register: int | None, saved_selector: int | None
    ) -> None:
        """Put the pack bank selector back where the station had it.

        Writing a slot number into the selector makes a real station report that
        slot as the number of packs installed, and the inflated count outlives
        the cycle, so every later cycle reads back a number the walk itself
        caused. A walk that runs to the end leaves the selector on the last slot;
        a walk cut short leaves it on whichever slot was being probed. Both need
        putting back.
        """
        if selector_register is None or saved_selector is None:
            return
        if not 1 <= saved_selector <= self.bluetti_device.max_packs:
            # Nothing sensible to put back, so do not write a slot number the
            # station never claimed.
            self.logger.debug(
                "Pack selector register %s read as %s, leaving it alone",
                selector_register,
                saved_selector,
            )
            return
        try:
            await self._async_send_command(
                self.bluetti_device.get_pack_selector(saved_selector)
            )
            self.logger.debug(
                "Restored pack selector register %s to %s",
                selector_register,
                saved_selector,
            )
        except Exception as err:
            # The walk's own failure is the one worth reporting, so do not let a
            # failed repair replace it. Do drop the desynced pipeline though:
            # the answer to this write may still turn up. Or the session is
            # what failed, and only a new link brings one back.
            self.logger.debug(
                "Could not restore pack selector register %s: %s",
                selector_register,
                err,
            )
            await self._recover_from_timeout()

    async def _read_pack(
        self, pack: int, pack_registers: List[ReadableRegisters], raw: bool
    ) -> dict:
        """Read the pack registers of the slot the selector points at."""
        pack_data: dict = {}

        for register in pack_registers:
            body = register.parse_response(await self._async_send_command(register))

            self.logger.debug("Raw data: %s", body)

            if raw:
                pack_data[register.starting_address] = body
                continue

            parsed = self.bluetti_device.parse(
                register.starting_address,
                body,
                pack_num=pack,
            )

            self.logger.debug("Parsed data: %s", parsed)
            pack_data.update(parsed)

        return pack_data

    @staticmethod
    def _pack_has_data(pack_data: dict) -> bool:
        """Whether a pack slot is actually occupied.

        An empty slot answers every pack register with zero. A real pack
        always reports a voltage, even when it is fully drained, so an
        all-zero answer means nothing is sitting in that slot.
        """
        for value in pack_data.values():
            if isinstance(value, bytes):
                if value.strip(b"\x00"):
                    return True
            elif isinstance(value, (int, Decimal)):
                if value:
                    return True
            elif value:
                return True

        return False

    async def read(
        self,
        only_registers: List[ReadableRegisters] | None = None,
        raw: bool = False,
        include_packs: bool = True,
    ) -> dict | None:

        registers = self.bluetti_device.get_polling_registers()
        pack_registers = self.bluetti_device.get_pack_polling_registers()

        if only_registers is not None:
            registers = only_registers
            pack_registers = []
        elif not include_packs:
            # The pack walk is a selector write and a settle wait per slot,
            # which is most of a cycle on a tower station. A consumer that
            # polls often can walk the slots now and then instead. No
            # pack_slots key is reported, so the consumer can tell that this
            # cycle said nothing about the slots rather than finding them empty.
            pack_registers = []

        parsed_data: dict = {}

        self.logger.debug("Reading device registers")

        async with self.polling_lock:
            try:
                async with async_timeout.timeout(self.config.timeout):
                    if not self._connected:
                        if not await self.connect():
                            return None

                    self.logger.debug("Connected to device")

                    if self.config.use_encryption and not (
                        self.encryption.is_ready_for_commands
                    ):
                        # connect() only reports success with a finished
                        # exchange, so the session broke since the link came
                        # up. Only a new connection makes the peer issue a
                        # fresh challenge.
                        self.logger.warning(
                            "Encryption session is not ready, reconnecting"
                        )
                        await self._abandon_handshake()
                        return None

                    if only_registers is None:
                        # The infrequent fields are only asked for until this
                        # connection has answered them. A field that never
                        # parses stays in the cycle, so a broken read is
                        # retried rather than remembered as missing. Decided
                        # once the link is up: whoever held the lock, or
                        # connect() itself, may have dropped the connection,
                        # and the cache with it.
                        pending = [
                            f
                            for f in self.bluetti_device.startup_fields
                            if f.name not in self._startup_values
                        ]
                        if pending:
                            registers = (
                                registers
                                + self.bluetti_device.coalesce_fields(pending)
                            )

                    self._begin_cycle()

                    for register in registers:
                        body = register.parse_response(
                            await self._async_send_command(register)
                        )

                        self.logger.debug("Raw data: %s", body)

                        if raw:
                            d = {}
                            d[register.starting_address] = body
                            parsed_data.update(d)
                            continue

                        parsed = self.bluetti_device.parse(
                            register.starting_address, body
                        )

                        self.logger.debug("Parsed data: %s", parsed)

                        parsed_data.update(parsed)

                        # Keep the answers to the infrequent fields as soon as
                        # they arrive: a cycle that fails further down should
                        # not lose them and ask for all of them again.
                        for name, value in parsed.items():
                            if name in STARTUP_FIELDS:
                                self._startup_values[name] = value

                    # Register 96 counts the packs that are installed, but it
                    # does not say which slots they occupy: an AC300 holding
                    # packs in slots 2, 3 and 4 still reports 3. So the walk
                    # covers every slot the station can hold, and stops early
                    # once the reported number of packs have answered.
                    installed = parsed_data.get(FieldName.PACK_NUM.value)
                    if not isinstance(installed, int) or installed < 0:
                        # Profile without a pack_num field, or a bad reading.
                        installed = None
                    if raw:
                        # Raw scans report every slot, empty ones included.
                        installed = None

                    # Probing the slots writes the bank selector, and real
                    # stations react to that: the reported pack count climbs to
                    # the highest slot written and stays there. Remember the
                    # selector's own value so the walk can put it back.
                    selector_register = self.bluetti_device.get_pack_selector_register()
                    saved_selector: int | None = None
                    if pack_registers and not raw and selector_register is not None:
                        saved_selector = await self._read_uint16(selector_register)

                    slot_limit = self.bluetti_device.max_packs if pack_registers else 0
                    slots: list[int] = []

                    try:
                        for pack in range(1, slot_limit + 1):
                            if installed is not None and len(slots) >= installed:
                                break

                            # The selector carries no answer worth parsing; the
                            # slot's registers are read next.
                            await self._async_send_command(
                                self.bluetti_device.get_pack_selector(pack),
                            )

                            # We need to wait for the powerstation to populate all
                            # registers, but a loaded slot is usually ready before
                            # that wait is over: look early, and only wait out the
                            # remainder for a slot that came back empty.
                            early_look = min(
                                PACK_SELECTOR_EARLY_LOOK_SECONDS,
                                PACK_SELECTOR_SETTLE_SECONDS,
                            )
                            await asyncio.sleep(early_look)

                            pack_data = await self._read_pack(
                                pack, pack_registers, raw
                            )

                            if not raw and not self._pack_has_data(pack_data):
                                await asyncio.sleep(
                                    PACK_SELECTOR_SETTLE_SECONDS - early_look
                                )
                                pack_data = await self._read_pack(
                                    pack, pack_registers, raw
                                )

                            if raw or self._pack_has_data(pack_data):
                                parsed_data.update(pack_data)
                                slots.append(pack)
                            else:
                                self.logger.debug(
                                    "Pack slot %s answered with nothing, treating it as empty",
                                    pack,
                                )

                        # Tell the consumer which slots hold a pack. Key presence
                        # on its own cannot say whether a slot is empty or a pack
                        # that just failed to answer, and a consumer that hides
                        # entities needs that difference to stay stable.
                        if pack_registers and not raw:
                            parsed_data[FieldName.PACK_SLOTS.value] = slots

                        if installed is not None and len(slots) != installed:
                            self.logger.debug(
                                "Station reported %s packs, %s answered in slots 1-%s",
                                installed,
                                len(slots),
                                slot_limit,
                            )
                    finally:
                        # A walk cut short still leaves the selector on the last
                        # slot it probed, so put it back either way.
                        await self._restore_pack_selector(
                            selector_register, saved_selector
                        )

            except ResponseTimeout as err:
                # Stop here rather than sending more commands: the answer to
                # this request may still arrive and would be read as the answer
                # to the next one, which is how a register ends up holding the
                # value of a different register.
                self._log_timeout("Read aborted: %s%s", err, self._cycle_note())
                await self._recover_from_timeout()
                return None
            except TimeoutError:
                self.logger.warning("Timeout")
                # Cut short mid-request like a cancelled read: the answer may
                # still arrive, so the link is resynced the same way.
                await self._resync_link()
                return None
            except BleakError as err:
                self.logger.warning("Bleak error: %s", err)
                # Connection may be lost: drop all per-connection state so
                # the next read() reconnects with a fresh client, notifier
                # and encryption handshake.
                await self._mark_disconnected()
                return None
            except asyncio.CancelledError:
                # External cancellation (e.g. HA reload/unload, or a caller's
                # wait_for). A request may be half answered, so resync: that
                # unsubscribes, which discards the late answer, and keeps a
                # live link and its session usable. If the link is gone the
                # next connect() finds out. The reader's own timeout arrives
                # as TimeoutError instead and is resynced above.
                await self._resync_link()
                raise
            except BaseException as err:
                self.logger.warning("Unknown error %s", err)
                return None

            # Report the infrequent fields from the cache. A cycle only asks
            # for the ones that are still unanswered, but a consumer should
            # keep seeing every key it has already been given. A raw scan
            # reports register ranges rather than fields, so it is left alone.
            if not raw:
                for name, value in self._startup_values.items():
                    parsed_data.setdefault(name, value)

            # Check if dict is empty
            if not parsed_data:
                return None

            return parsed_data

    async def read_locked(
        self, only_registers: List[ReadableRegisters], raw: bool = False
    ) -> dict | None:
        """Read selected registers while the caller holds polling_lock."""
        parsed_data: dict = {}

        try:
            async with async_timeout.timeout(self.config.timeout):
                if not self._connected:
                    if not await self.connect():
                        return None

                if self.config.use_encryption and not (
                    self.encryption.is_ready_for_commands
                ):
                    # Same recovery as read(): the next call reconnects.
                    self.logger.warning(
                        "Encryption session is not ready, reconnecting"
                    )
                    await self._abandon_handshake()
                    return None

                self._begin_cycle()

                for register in only_registers:
                    body = register.parse_response(
                        await self._async_send_command(register)
                    )

                    if raw:
                        parsed_data[register.starting_address] = body
                    else:
                        parsed_data.update(
                            self.bluetti_device.parse(
                                register.starting_address, body
                            )
                        )

        except ResponseTimeout as err:
            # Same reasoning as read(): a write verification read-back that
            # gets no valid answer must not be followed by another command on
            # a link that may still be carrying a late answer.
            self._log_timeout("Read aborted: %s%s", err, self._cycle_note())
            await self._recover_from_timeout()
            return None
        except TimeoutError:
            self.logger.warning("Timeout reading selected registers")
            await self._resync_link()
            return None
        except BleakError as err:
            self.logger.warning("Bleak error reading selected registers: %s", err)
            # Same recovery path as read(): the next read/read_locked()
            # reconnects from scratch.
            await self._mark_disconnected()
            return None
        except asyncio.CancelledError:
            # Same as read(): resync rather than forget a link that may be up.
            await self._resync_link()
            raise
        except BaseException as err:
            self.logger.warning("Error reading selected registers: %s", err)
            return None

        return parsed_data or None

    def _log_timeout(self, message: str, *args: Any) -> None:
        """Report a request that went unanswered.

        The first request on a fresh connection is the one a waking station
        tends to miss, which is normal and not worth a warning. Once the
        connection has been answering, a missing answer is news.
        """
        self.logger.log(
            logging.WARNING if self._answered else logging.DEBUG, message, *args
        )

    def _begin_cycle(self) -> None:
        """Start counting a new read cycle.

        The abort streak carries over: a station that fails the same way cycle
        after cycle is a different fault from one that drops an occasional
        request, and a single log line cannot tell them apart.
        """
        stats = self.link_stats
        stats.abort_streak = stats.abort_streak + 1 if self._cycle_aborted else 0
        self._cycle_aborted = False
        stats.requests = 0
        stats.answered = 0
        stats.slowest = 0.0
        stats.slowest_registers = ""
        stats.aborted = ""
        stats.failure = ""
        stats.received = 0
        stats.sample = ""
        stats.attempts = 0
        stats.retried = 0
        stats.recovered = 0
        stats.drained = 0
        stats.discarded = 0
        self._retry_pending = False

    def _record_answer(self, registers: DeviceRegister, elapsed: float) -> None:
        """Note a request that was answered, and how long it took."""
        stats = self.link_stats
        stats.answered += 1

        if self._retry_pending:
            # The station answered, just not to the first ask.
            stats.recovered += 1
            self._retry_pending = False

        if elapsed > stats.slowest:
            stats.slowest = elapsed
            stats.slowest_registers = str(registers)

        if elapsed >= SLOW_REQUEST_SECONDS:
            # Kept for when a later request fails: a station that is about to
            # go quiet usually answers late first.
            self.logger.debug("Slow answer to %s: %.1fs", registers, elapsed)

    def _rx_sample(self) -> str:
        """The start of whatever arrived about the request in flight, hex.

        A frame that was recognised as an answer to something else is thrown
        away as soon as it is spotted, so fall back to the copy kept for it.
        """
        pending = bytes(self.notify_response[:RX_SAMPLE_BYTES])
        if pending:
            return pending.hex()
        return bytes(self._rx_unexpected[:RX_SAMPLE_BYTES]).hex()

    def _rx_state(self, registers: DeviceRegister) -> tuple[str, str]:
        """Sort out what the station sent about a request it did not answer.

        Returns the kind of fault and a phrase for the log. "No valid response"
        on its own covers three different faults that point at different
        causes: nothing at all is a station that is busy or has quietly dropped
        the link, too few bytes is an answer lost on the way up, and a full
        frame that fits no pending request is a late answer to a request that
        was already given up on.
        """
        expected = registers.response_size()
        sample = self._rx_sample()

        if not self._rx_bytes:
            return "silent", "the station sent nothing"

        if self._rx_bytes < expected:
            return (
                "partial",
                f"the station sent {self._rx_bytes} of {expected} bytes in "
                f"{self._rx_frames} frame(s) then stopped ({sample})",
            )

        return (
            "mismatch",
            f"the station sent {self._rx_bytes} bytes that are not an answer "
            f"to this request ({sample})",
        )

    def _cycle_note(self) -> str:
        """Say where in the cycle a read was cut short, for the log line."""
        stats = self.link_stats
        if not stats.requests:
            return ""

        parts = [
            f"request #{stats.requests} of this cycle",
            f"{stats.answered} answered before it",
        ]
        if stats.attempts > 1:
            parts.append(f"{stats.attempts} attempts for it")
        if stats.slowest >= SLOW_REQUEST_SECONDS:
            parts.append(
                f"slowest answer {stats.slowest:.1f}s ({stats.slowest_registers})"
            )
        if stats.abort_streak:
            parts.append(f"{stats.abort_streak + 1} cycles in a row with an abort")

        return " [" + "; ".join(parts) + "]"

    async def _async_send_command(self, registers: DeviceRegister) -> bytes:
        """Send a command and return its validated response.

        A read that goes unanswered is asked again before the cycle is given up
        on. That is what a station like the AC300 needs: it drops a request now
        and then, then answers the next cycle as though nothing had happened, so
        the only thing the first attempt cost was a whole cycle of data. Only a
        read is asked twice. A late answer to the first attempt is still an
        answer to the same request, so it settles the read correctly; the
        answer to the other attempt may still be on its way when the read
        returns, and _drain_stray_answer() keeps it away from the next request.

        Raises ResponseTimeout when no valid answer arrives in time; the caller
        must then stop using this connection. A MODBUS exception answer (for
        example a register range the device does not support) returns an empty
        response.
        """
        attempts = READ_ATTEMPTS if isinstance(registers, ReadableRegisters) else 1

        attempt = 0
        while True:
            attempt += 1
            if self._session_lost():
                # The session was lost mid-cycle (an InvalidSignature resets
                # it). Nothing sent over this link can be answered before a new
                # key exchange, so this is a missing answer, not an empty one,
                # and not worth asking twice.
                stats = self.link_stats
                stats.aborted = str(registers)
                stats.failure = "session"
                self._cycle_aborted = True
                raise ResponseTimeout(registers, "encryption session lost")

            try:
                response = await self._send_once(
                    registers, attempt, attempt == attempts
                )
            except ResponseTimeout:
                if attempt == attempts:
                    raise

                if not self._session_lost():
                    # A lost session gives up on the next pass instead.
                    self.link_stats.retried += 1
                    self._retry_pending = True
                    await asyncio.sleep(RETRY_PAUSE_SECONDS)
                continue

            if attempt > 1:
                await self._drain_stray_answer(registers)
            return response

    async def _drain_stray_answer(self, registers: DeviceRegister) -> None:
        """Let the second answer to a retried read land while nothing is pending.

        With no request in flight the notification handler only collects what
        arrives, so the stray answer cannot settle a later request. Stops as
        soon as an answer of this size is in, or after STRAY_ANSWER_SECONDS when
        the station never sends one.

        A second answer that is byte for byte the first one (the register did
        not change between the two asks) never reaches the buffer: the
        duplicate filter in _notification_handler drops it, because
        last_notification still holds the first answer. In that case the wait
        is what keeps it away from the next request, which would accept it
        since _send_once() clears last_notification. Only an answer that
        differs is collected here, and that ends the wait early.

        How this relates to _resync_link(): both deal with an answer that is
        still on its way to a request nobody is waiting for. _resync_link()
        runs when a read is given up on (read() and read_locked()); it drops
        the receive state and the notification subscription so the next
        connect() starts clean, without waiting for anything. This drain
        covers the case the retry opened, where the read did succeed: the
        link stays as it is and the stray answer is waited for and thrown
        away. Whether stop_notify/start_notify alone would stop a delayed
        answer from arriving afterwards is not established, so the drain does
        not rely on it. Keep the two in step when either one changes.
        """
        self.link_stats.drained += 1
        self.notify_response = bytearray()
        size = registers.response_size()
        started = time.monotonic()
        deadline = started + STRAY_ANSWER_SECONDS

        while len(self.notify_response) < size and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        if self.notify_response:
            self.link_stats.discarded += 1
            self.logger.debug(
                "Discarded a second answer to %s after %.2fs: %s",
                registers,
                time.monotonic() - started,
                bytes(self.notify_response[:RX_SAMPLE_BYTES]).hex(),
            )
        else:
            self.logger.debug(
                "No differing second answer to %s within %ss",
                registers,
                STRAY_ANSWER_SECONDS,
            )
        self.notify_response = bytearray()

    async def _send_once(
        self, registers: DeviceRegister, attempt: int, last_attempt: bool
    ) -> bytes:
        """Send a command once and wait for the answer to it.

        ``last_attempt`` decides how a missing answer is reported: a read that
        still has a retry coming is a debug line, not a warning, because the
        retry is what would otherwise have thrown away the whole cycle.
        """
        self.current_registers = registers
        self.notify_response = bytearray()
        self.notify_future = self.create_future()
        self.encrypted_buffer.clear()
        # Two identical answers to two different requests are legitimate.
        self.last_notification = None
        self._rx_bytes = 0
        self._rx_frames = 0
        self._rx_unexpected = bytearray()
        if attempt == 1:
            self.link_stats.requests += 1
        self.link_stats.attempts = attempt
        sent_at = time.monotonic()

        command_bytes = bytes(registers)

        # Encrypt command
        if self.config.use_encryption is True:
            command_bytes = self.encryption.aes_encrypt(
                command_bytes, self.encryption.secure_aes_key, None
            )

        try:
            # Make request
            await self.client.write_gatt_char(WRITE_UUID, command_bytes)

            self.logger.debug("Request sent (%s)", registers)

            # Wait for response
            res = await asyncio.wait_for(
                self.notify_future, timeout=RESPONSE_TIMEOUT_SECONDS
            )

            self.logger.debug("Got response (%d bytes)", len(res))

            self._answered = True
            self._resyncs = 0
            self._record_answer(registers, time.monotonic() - sent_at)

            return cast(bytes, res)
        except BleakError:
            # A failed GATT write/notify almost always means the
            # connection is gone. Propagate it so the caller's
            # BleakError handler can drop the dead connection and
            # reconnect on the next poll; swallowing it here would
            # leave _connected stuck at True forever.
            raise
        except asyncio.TimeoutError:
            # No valid answer to this request. The GATT link can still look
            # healthy while the device skipped a response, but the answer may
            # also still be on its way: keep going would hand that answer to
            # the next request, which is how a register ends up holding the
            # value of a different one.
            kind, detail = self._rx_state(registers)

            if not last_attempt:
                self.logger.debug(
                    "No answer to %s within %ss, asking again: %s",
                    registers,
                    RESPONSE_TIMEOUT_SECONDS,
                    detail,
                )
                raise ResponseTimeout(registers, detail) from None

            stats = self.link_stats
            stats.aborted = str(registers)
            stats.failure = kind
            stats.received = self._rx_bytes
            stats.sample = self._rx_sample()
            self._cycle_aborted = True

            self._log_timeout(
                "No valid response to %s within %ss (client connected: %s): %s",
                registers,
                RESPONSE_TIMEOUT_SECONDS,
                self.client.is_connected if self.client else False,
                detail,
            )
            raise ResponseTimeout(registers, detail) from None
        finally:
            # Answers that arrive from now on are not for a pending request.
            self.notify_future = None

    def _calculate_expected_encrypted_length(self, buffer: bytearray) -> int | None:
        """Calculate expected total length of an encrypted message."""
        if len(buffer) < 2:
            return None

        data_len = (buffer[0] << 8) + buffer[1]

        key, iv = self.encryption.getKeyIv()
        if iv is None:
            header_size = 6
        else:
            header_size = 2

        padded_len = (
            (data_len + AES_BLOCK_SIZE - 1) // AES_BLOCK_SIZE
        ) * AES_BLOCK_SIZE

        return header_size + padded_len

    async def _write_handshake(self, payload: bytes, what: str) -> bool:
        """Send a key exchange reply.

        A BleakError raised inside a notification callback is swallowed by
        bleak's dispatch, which leaves the read waiting its full timeout with
        nothing in the log naming the cause. Catching it here and flagging the
        handshake lets the read give the connection up instead.
        """
        try:
            await self.client.write_gatt_char(WRITE_UUID, payload)
            return True
        except BleakError as err:
            self.logger.warning("Failed to send the %s: %s", what, err)
            self.handshake_failed = True
            return False

    async def _notification_handler(self, _: int, data: bytearray):
        """Handle bt data."""
        self.logger.debug("Got new data (%d bytes)", len(data))

        # Some stations (the Apex 300 among them) deliver every frame two or
        # three times, and retry the key exchange until they see our answer.
        # Replying to a repeat makes a second local keypair, so the peer ends
        # up sharing a secret with a key we threw away: the handshake is
        # confirmed on both sides and the device then ignores every command.
        # A half-assembled answer is exempt, because its next chunk has to be
        # kept even when it looks exactly like the chunk before it.
        notification = bytes(data)
        if notification == self.last_notification and not self.notify_response:
            self.logger.debug("Duplicate notification ignored")
            return
        self.last_notification = notification

        if self.config.use_encryption is True:
            message = Message(data)

            if message.is_pre_key_exchange:
                message.verify_checksum()

                try:
                    message_type = message.type
                except ValueError:
                    # Unknown frame. bleak runs this callback as a detached
                    # task, so raising here is silent and the read hangs.
                    self.logger.warning("Unknown key exchange message type")
                    return

                if message_type == MessageType.CHALLENGE:
                    challenge_response = self.encryption.msg_challenge(message)
                    if challenge_response is not None:
                        await self._write_handshake(
                            challenge_response, "challenge response"
                        )
                    return

                if message_type == MessageType.CHALLENGE_ACCEPTED:
                    self.logger.debug("Challenge accepted")
                    return

                return

            if self.encryption.unsecure_aes_key is None:
                self.logger.error(
                    "Received encrypted message before key initialization"
                )
                return

            self.encrypted_buffer.extend(data)

            expected_len = self._calculate_expected_encrypted_length(
                self.encrypted_buffer
            )

            if expected_len is None:
                return

            if len(self.encrypted_buffer) < expected_len:
                self.logger.debug(
                    "Buffering fragment: %d/%d bytes",
                    len(self.encrypted_buffer),
                    expected_len,
                )
                return

            complete_message = bytes(self.encrypted_buffer[:expected_len])

            if len(self.encrypted_buffer) > expected_len:
                self.encrypted_buffer = self.encrypted_buffer[expected_len:]
            else:
                self.encrypted_buffer.clear()

            key, iv = self.encryption.getKeyIv()

            try:
                decrypted = Message(
                    self.encryption.aes_decrypt(complete_message, key, iv)
                )
            except ValueError as e:
                self.logger.error("Decryption failed: %s", e)
                self.encrypted_buffer.clear()
                return

            if decrypted.is_pre_key_exchange:
                decrypted.verify_checksum()

                try:
                    decrypted_type = decrypted.type
                except ValueError:
                    self.logger.warning("Unknown key exchange message type")
                    return

                if decrypted_type == MessageType.CHALLENGE:
                    # A device can restart the exchange inside the encrypted
                    # channel. This is what refreshes the IV that the peer
                    # pubkey is later verified against, so skipping it makes
                    # every following pubkey signature look invalid.
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
                        peer_pubkey_response = self.encryption.msg_peer_pubkey(
                            decrypted
                        )
                    except InvalidSignature:
                        # Almost always a stale IV: drop the session and let
                        # the next connection restart the exchange rather than
                        # leaving the link wedged.
                        self.logger.warning(
                            "Peer pubkey signature rejected, reconnecting"
                        )
                        self.encryption.reset()
                        self.encrypted_buffer.clear()
                        self.handshake_failed = True
                        return
                    await self._write_handshake(
                        peer_pubkey_response, "peer pubkey reply"
                    )
                    return

                if decrypted_type == MessageType.PUBKEY_ACCEPTED:
                    try:
                        self.encryption.msg_key_accepted(decrypted)
                    except (ValueError, AttributeError) as err:
                        self.logger.warning("Key exchange rejected: %s", err)
                        self.handshake_failed = True
                    return

                # A key exchange frame is never MODBUS data.
                return

            # Handle as message
            data = decrypted.buffer

        # Save data
        self.notify_response.extend(data)

        # Keep track of what the station sends while a request is in flight, so
        # a request that goes unanswered can say what did arrive about it.
        if self.notify_future is not None and not self.notify_future.done():
            self._rx_bytes += len(data)
            self._rx_frames += 1

        # The connection is persistent, so notifications can arrive outside of
        # a pending command (leftovers, write acknowledgements, ...). Only the
        # future of the running command may be resolved here.
        if self.notify_future is None or self.notify_future.done():
            self.logger.debug(
                "Notification (%d bytes) arrived with no pending command",
                len(data),
            )
            return

        registers = self.current_registers
        if registers is None:
            return

        response = bytes(self.notify_response)

        if registers.is_valid_exception(response):
            # A register range this device does not answer. Replying with
            # nothing keeps those fields out of the result instead of failing
            # the whole poll.
            self.logger.debug(
                "Exception response to %s: %s", registers, response.hex()
            )
            self.notify_future.set_result(bytes())
            return

        size = registers.response_size()
        if len(response) < size:
            self.logger.debug(
                "Partial answer (%d/%d bytes), waiting for the rest",
                len(response),
                size,
            )
            return

        if len(response) == size and registers.is_valid_response(response):
            self.notify_future.set_result(response)
            return

        # Not the answer to this request: a late answer to an earlier one, a
        # write acknowledgement, or a corrupted frame. A function 03 answer
        # carries no register address, so length plus byte count is the only
        # thing to go on. Discard it and keep waiting; if no valid answer
        # follows, the request times out and the read is aborted.
        self.logger.debug(
            "Discarding response that does not match %s: %s",
            registers,
            response.hex(),
        )
        self._rx_unexpected = bytearray(response[:RX_SAMPLE_BYTES])
        self.notify_response = bytearray()
