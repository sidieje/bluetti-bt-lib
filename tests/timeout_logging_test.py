import asyncio
import logging
import struct
import unittest
from contextlib import contextmanager
from unittest.mock import patch

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
        self.assertEqual(self.levels(logs, "No valid response"), [logging.WARNING])
        self.assertEqual(len(self.aborted(logs)), 1)
        self.assertEqual(self.levels(logs, "Read aborted"), [logging.WARNING])

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
