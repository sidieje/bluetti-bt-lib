import asyncio
import logging
import struct
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from bleak.exc import BleakGATTProtocolError

from bluetti_bt_lib import DeviceReader
from bluetti_bt_lib.base_devices import BaseDeviceV1
from bluetti_bt_lib.bluetooth import device_reader
from bluetti_bt_lib.registers.WriteableRegister import WriteableRegister
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption

# A request is given RESPONSE_TIMEOUT_SECONDS to be answered; a test should not
# spend five seconds waiting for one that is never coming.
FAST_TIMEOUT_SECONDS = 0.05


class DeafMock(ClientMockNoEncryption):
    """Answers every request except the ones numbered in deaf_at."""

    def __init__(self, deaf_at: set[int]) -> None:
        super().__init__()
        self.deaf_at = deaf_at
        self.requests = 0

    async def write_gatt_char(self, char_specifier, data, response=None) -> None:
        self.requests += 1
        if self.requests in self.deaf_at:
            return

        await super().write_gatt_char(char_specifier, data, response)


class FlakyMock(ClientMockNoEncryption):
    """Answers every request, but the ones numbered in late_at arrive slowly.

    A station that is about to go quiet usually answers late first, and the
    reader is meant to notice that.
    """

    def __init__(self, late_at: set[int], delay: float) -> None:
        super().__init__()
        self.late_at = late_at
        self.delay = delay
        self.requests = 0

    async def write_gatt_char(self, char_specifier, data, response=None) -> None:
        self.requests += 1
        if self.requests in self.late_at:
            await asyncio.sleep(self.delay)

        await super().write_gatt_char(char_specifier, data, response)


class TruncatedMock(ClientMockNoEncryption):
    """Answers the requests numbered in truncated_at with a frame that stops
    short of its CRC."""

    #: which requests to mangle, set by subclasses that answer differently
    truncate = True

    def __init__(self, at: set[int]) -> None:
        super().__init__()
        self.at = at
        self.requests = 0

    async def write_gatt_char(self, char_specifier, data, response=None) -> None:
        self.requests += 1
        if self.requests not in self.at or self._callback is None:
            await super().write_gatt_char(char_specifier, data, response)
            return

        request = bytes(data)
        address, quantity = struct.unpack_from("!HH", request, 2)

        if self.truncate:
            # The station started answering and the rest never arrived.
            frame = self._frame(request[0], address, quantity)
            await self._callback(char_specifier, frame[: len(frame) - 3])
            return

        # A valid frame, just not one that answers this request: it covers more
        # registers than were asked for, so the reader discards it.
        await self._callback(
            char_specifier, self._frame(request[0], address, quantity + 4)
        )

    def _frame(self, slave: int, address: int, quantity: int) -> bytearray:
        return self._read_frame(slave, self._get_register(address, quantity))


class WrongAnswerMock(TruncatedMock):
    """Answers one request with a complete frame for a different register range."""

    truncate = False


class StrayByteMock(ClientMockNoEncryption):
    """Answers the requests numbered in at with junk bytes in front of the frame.

    Seen on an AC300: a byte turns up in the notify stream that belongs to no
    request, and the answer that follows it is shifted along by that much.
    """

    def __init__(self, at: set[int], junk: bytes = b"\x41") -> None:
        super().__init__()
        self.at = at
        self.junk = junk
        self.requests = 0

    async def write_gatt_char(self, char_specifier, data, response=None) -> None:
        self.requests += 1
        if self.requests not in self.at or self._callback is None:
            await super().write_gatt_char(char_specifier, data, response)
            return

        # The junk arrives as a notification of its own, which is how it shows up
        # on a station: the reader only sees the two together in its buffer.
        await self._callback(char_specifier, bytearray(self.junk))
        await super().write_gatt_char(char_specifier, data, response)


class SplitStrayMock(StrayByteMock):
    """Junk in front of an answer whose last notification lands after the buffer
    is already as long as the answer.

    This is the AC300 frame from the 30 Oct log: 21 bytes for a 21-byte answer,
    head ``41 01 03 10``. The stray byte pushes the answer along, so the buffer
    reaches the expected length one byte before the answer is complete, and the
    reader used to throw the whole thing away at that moment.
    """

    #: whether the last byte of the answer is sent at all
    send_tail = True

    async def write_gatt_char(self, char_specifier, data, response=None) -> None:
        self.requests += 1
        if self.requests not in self.at or self._callback is None:
            # Skip StrayByteMock, which would count this request a second time.
            await ClientMockNoEncryption.write_gatt_char(
                self, char_specifier, data, response
            )
            return

        request = bytes(data)
        address, quantity = struct.unpack_from("!HH", request, 2)
        frame = self._read_frame(request[0], self._get_register(address, quantity))

        # Cut so that the junk plus the first chunk is exactly one answer long.
        cut = len(frame) - len(self.junk)
        await self._callback(char_specifier, bytearray(self.junk))
        await self._callback(char_specifier, frame[:cut])
        if self.send_tail:
            await self._callback(char_specifier, frame[cut:])


class StuckShiftMock(SplitStrayMock):
    """A shifted answer whose tail never arrives.

    The reader should keep the bytes it has rather than throw them away, and if
    the tail really is lost, say so in terms of a frame that arrived out of
    place instead of a frame that is not an answer at all.
    """

    send_tail = False


class RefusingMock(ClientMockNoEncryption):
    """Fails every write with a GATT protocol error.

    This is what a station that refuses a request looks like to the reader, and
    Bleak's own rendering of it is the (code, message) tuple it was built from.
    """

    def __init__(self, code: int = 0x0E) -> None:
        super().__init__()
        self.code = code

    async def write_gatt_char(self, char_specifier, data, response=None) -> None:
        raise BleakGATTProtocolError(self.code)


class TestTimeoutLogging(unittest.IsolatedAsyncioTestCase):
    def make_reader(self, ble_mock: ClientMockNoEncryption) -> DeviceReader:
        return DeviceReader(
            "00:11:00:11:00:11",
            BaseDeviceV1(),
            asyncio.Future,
            ble_client=ble_mock,
            lock=asyncio.Lock(),
        )

    @contextmanager
    def fast_timing(self):
        """Shorten the waits a request that goes unanswered sits through.

        A read is asked for twice, so a station that never answers would cost a
        test that timeout twice over, plus the pause between the two asks.
        """
        with patch.object(
            device_reader, "RESPONSE_TIMEOUT_SECONDS", FAST_TIMEOUT_SECONDS
        ), patch.object(device_reader, "RETRY_PAUSE_SECONDS", 0.0):
            yield

    def levels(self, logs, text: str) -> list[int]:
        """Levels of the records mentioning text."""
        return [r.levelno for r in logs.records if text in r.getMessage()]

    def joined(self, logs) -> str:
        return "\n".join(r.getMessage() for r in logs.records)

    def aborted(self, logs) -> list[str]:
        """The lines that report a cycle that was cut short."""
        return [r.getMessage() for r in logs.records if "Read aborted" in r.getMessage()]

    async def test_a_dropped_read_is_asked_for_again(self):
        # The AC300 skips a request now and then, then answers the next cycle as
        # though nothing had happened. Giving up on the first attempt threw away
        # a whole cycle of data over one answer that a second ask gets.
        reader = self.make_reader(DeafMock({2}))

        with self.fast_timing():
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                data = await reader.read()

        self.assertIsNotNone(data)
        self.assertEqual(reader.link_stats.retried, 1)
        self.assertEqual(reader.link_stats.recovered, 1)
        self.assertIn("asking again", self.joined(logs))
        # One missed answer is not worth a warning when the retry covered it.
        self.assertEqual(self.levels(logs, "No valid response"), [])
        self.assertEqual(self.aborted(logs), [])

    async def test_a_write_is_never_sent_twice(self):
        # Retrying a read is safe because a late answer still answers the same
        # request. A write has no such excuse: asking twice does it twice.
        ble_mock = DeafMock({1})
        reader = self.make_reader(ble_mock)
        await reader.connect()

        with self.fast_timing():
            with self.assertRaises(device_reader.ResponseTimeout):
                await reader._async_send_command(WriteableRegister(3006, 2))

        self.assertEqual(ble_mock.requests, 1)

    async def test_a_link_that_has_not_answered_yet_logs_quietly(self):
        # A station that is still waking misses the first request, which is
        # normal enough that it should not fill the HA log at startup.
        ble_mock = DeafMock({1, 2})
        reader = self.make_reader(ble_mock)

        with self.fast_timing():
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                data = await reader.read()

        self.assertIsNone(data)
        self.assertEqual(self.levels(logs, "No valid response"), [logging.DEBUG])
        self.assertEqual(self.aborted(logs).__len__(), 1)
        self.assertEqual(self.levels(logs, "Read aborted"), [logging.DEBUG])

    async def test_a_link_that_stopped_answering_still_warns(self):
        # Once the station has proven it is answering, a missing answer is news.
        ble_mock = DeafMock({2, 3})
        reader = self.make_reader(ble_mock)

        with self.fast_timing():
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                data = await reader.read()

        self.assertIsNone(data)
        self.assertEqual(self.levels(logs, "Read aborted"), [logging.WARNING])
        # One line per abort, not one for the request and one for the cycle: the
        # warning carries the timeout, the link state and what the station sent.
        line = self.aborted(logs)[0]
        self.assertIn("within", line)
        self.assertIn("client connected: True", line)
        self.assertIn("the station sent nothing", line)
        self.assertEqual(self.levels(logs, "No valid response"), [logging.DEBUG])

    async def test_a_station_that_sends_nothing_says_so(self):
        # "No valid response" used to be the whole story, which left a station
        # that went quiet indistinguishable from one that answered garbage.
        reader = self.make_reader(DeafMock({2, 3}))

        with self.fast_timing():
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                await reader.read()

        self.assertIn("the station sent nothing", self.joined(logs))
        self.assertEqual(reader.link_stats.failure, "silent")
        self.assertEqual(reader.link_stats.received, 0)
        self.assertIn("starting_address=10", reader.link_stats.aborted)

    async def test_an_answer_that_stops_short_is_reported_as_partial(self):
        reader = self.make_reader(TruncatedMock({2, 3}))

        with self.fast_timing():
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                await reader.read()

        message = self.joined(logs)
        self.assertEqual(reader.link_stats.failure, "partial")
        # The second request of the cycle is the 10+6 startup block, which is
        # answered in 17 bytes; the mock drops the last three of them.
        self.assertEqual(reader.link_stats.received, 14)
        self.assertIn("the station sent 14 of 17 bytes", message)
        self.assertIn("then stopped", message)
        self.assertTrue(reader.link_stats.sample)

    async def test_an_answer_to_another_request_is_reported_as_mismatch(self):
        reader = self.make_reader(WrongAnswerMock({2, 3}))

        with self.fast_timing():
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                await reader.read()

        self.assertEqual(reader.link_stats.failure, "mismatch")
        self.assertIn("are not an answer to this request", self.joined(logs))
        # The bytes that did arrive are quoted, which is how a late answer to a
        # request that was already given up on is told apart from noise.
        self.assertTrue(reader.link_stats.sample)

    async def test_the_abort_says_where_in_the_cycle_it_happened(self):
        # Whether the fault follows the same position in the poll order is the
        # difference between a register that will not answer and a link that
        # gives up partway through a cycle.
        reader = self.make_reader(DeafMock({2, 3}))

        with self.fast_timing():
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                await reader.read()

        self.assertEqual(len(self.aborted(logs)), 1)
        line = self.aborted(logs)[0]
        self.assertIn("request #2 of this cycle", line)
        self.assertIn("1 answered before it", line)
        self.assertIn("2 attempts for it", line)
        self.assertNotIn("cycles in a row", line)

    async def test_repeated_aborts_are_counted_together(self):
        # The same fault cycle after cycle is a different problem from an
        # occasional dropped request, and one log line cannot tell them apart.
        reader = self.make_reader(DeafMock({2, 3, 5, 6}))

        with self.fast_timing(), patch.object(device_reader, "STRAY_ANSWER_SECONDS", 0):
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                await reader.read()
                await reader.read()

        self.assertEqual(len(self.aborted(logs)), 2)
        self.assertNotIn("cycles in a row", self.aborted(logs)[0])
        self.assertIn("2 cycles in a row with an abort", self.aborted(logs)[1])
        self.assertEqual(reader.link_stats.abort_streak, 1)

    async def test_a_repeated_fault_of_a_kind_warns_once(self):
        # A tower that does this every forty minutes is a quirk of that station,
        # not forty problems. The first one of a kind warns, the rest wait.
        reader = self.make_reader(DeafMock({2, 3, 5, 6}))

        with self.fast_timing():
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                await reader.read()
                await reader.read()

        self.assertEqual(
            self.levels(logs, "Read aborted"), [logging.WARNING, logging.DEBUG]
        )

    async def test_the_next_warning_says_what_was_held_back(self):
        # Nothing is hidden for good: once the window passes, the next loud one
        # carries a tally of the quiet ones from inside it.
        reader = self.make_reader(DeafMock({2, 3, 5, 6, 8, 9}))

        with self.fast_timing():
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                await reader.read()  # the first one of a kind speaks up
                await reader.read()  # the same fault again is held back
                with patch.object(device_reader, "ABORT_REPORT_WINDOW_SECONDS", 0.0):
                    await reader.read()  # out of the window, and it says why

        self.assertEqual(
            self.levels(logs, "Read aborted"),
            [logging.WARNING, logging.DEBUG, logging.WARNING],
        )
        warnings = [
            record.getMessage()
            for record in logs.records
            if record.levelno == logging.WARNING
            and "Read aborted" in record.getMessage()
        ]
        self.assertNotIn("since the last warning", warnings[0])
        self.assertIn("1 more the same way since the last warning", warnings[1])

    async def test_an_answer_shifted_by_a_stray_byte_is_recovered(self):
        # The AC300 slips a byte into the stream. The answer behind it is whole
        # and CRC-correct, only one byte away from where the reader looks for it,
        # and used to cost a cycle of data and two warnings.
        reader = self.make_reader(StrayByteMock({2}))

        with self.fast_timing():
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                data = await reader.read()

        self.assertIsNotNone(data)
        self.assertEqual(reader.link_stats.shifted, 1)
        self.assertIn("Recovered the answer", self.joined(logs))
        self.assertEqual(self.aborted(logs), [])

    async def test_junk_that_leaves_no_clean_answer_is_still_discarded(self):
        # Re-anchoring may only land on a run of bytes that is exactly one answer
        # long. More junk than that is still reported as a bad frame.
        reader = self.make_reader(StrayByteMock({2, 3}, junk=b"\x41\x42\x43\x44\x45"))

        with self.fast_timing():
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                data = await reader.read()

        self.assertIsNone(data)
        self.assertEqual(reader.link_stats.failure, "mismatch")
        self.assertEqual(reader.link_stats.shifted, 0)
        self.assertIn("Discarding response that does not match", self.joined(logs))

    async def test_an_answer_split_across_notifications_after_junk_is_recovered(self):
        # The 30 Oct AC300 log: "the station sent 21 bytes that are not an answer
        # to this request (410103100017000000000000)" for a 21-byte answer. The
        # 0x41 is the stray byte, so the answer behind it was one byte short of
        # landing when the buffer hit the expected length - and the reader threw
        # away a frame that was about to become valid.
        reader = self.make_reader(SplitStrayMock({2}))

        with self.fast_timing():
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                data = await reader.read()

        self.assertIsNotNone(data)
        self.assertEqual(reader.link_stats.shifted, 1)
        self.assertEqual(reader.link_stats.retried, 0)
        self.assertEqual(self.aborted(logs), [])
        self.assertNotIn("Discarding response that does not match", self.joined(logs))

    async def test_a_shifted_answer_that_never_finishes_says_so(self):
        # Keeping the bytes is only half the job: if the tail really is lost, the
        # abort has to say the answer arrived out of place, which is a different
        # fault from a frame that answers nothing.
        reader = self.make_reader(StuckShiftMock({2, 3}))

        with self.fast_timing():
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                await reader.read()

        self.assertEqual(reader.link_stats.failure, "misaligned")
        self.assertIn("out of place", self.joined(logs))
        self.assertNotIn("are not an answer to this request", self.joined(logs))

    async def test_a_refused_request_is_said_plainly_once(self):
        # The 30 Oct AC300 log repeated "Bleak error: (<BleakGATTProtocolError
        # Code.UNLIKELY_ERROR: 14>, 'GATT Protocol Error: Unlikely Error')"
        # every poll for one fault: unreadable, alarming, and not news after
        # the first time.
        reader = self.make_reader(RefusingMock())

        with self.fast_timing():
            with self.assertLogs(reader.logger, level="DEBUG") as logs:
                first = await reader.read()
                second = await reader.read()
                with patch.object(device_reader, "ABORT_REPORT_WINDOW_SECONDS", 0.0):
                    third = await reader.read()

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertIsNone(third)
        lines = [r for r in logs.records if "BLE read failed" in r.getMessage()]
        self.assertEqual(
            [r.levelname for r in lines], ["WARNING", "DEBUG", "WARNING"]
        )
        message = lines[0].getMessage()
        self.assertIn("GATT protocol error 14", message)
        self.assertIn("gave no reason", message)
        self.assertIn("link will be rebuilt", message)
        self.assertNotIn("BleakGATTProtocolErrorCode", message)
        self.assertNotIn("since the last warning", message)
        self.assertNotIn("since the last warning", lines[1].getMessage())
        self.assertIn(
            "1 more the same way since the last warning", lines[2].getMessage()
        )

    async def test_a_slow_answer_is_noticed(self):
        # A station that is about to stop answering usually answers late first,
        # and that is only worth knowing if the reader writes it down.
        reader = self.make_reader(FlakyMock({1}, FAST_TIMEOUT_SECONDS / 2))

        with self.fast_timing():
            with patch.object(device_reader, "SLOW_REQUEST_SECONDS", 0.01):
                with self.assertLogs(reader.logger, level="DEBUG") as logs:
                    await reader.read()

        self.assertIn("Slow answer", self.joined(logs))
        self.assertGreaterEqual(reader.link_stats.slowest, FAST_TIMEOUT_SECONDS / 2)
        self.assertIn("starting_address=36", reader.link_stats.slowest_registers)


if __name__ == "__main__":
    unittest.main()
