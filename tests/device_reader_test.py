import asyncio
import struct
import unittest

from unittest.mock import patch

from bleak.exc import BleakError

from bluetti_bt_lib.base_devices import BaseDeviceV1
from bluetti_bt_lib import DeviceReader
from bluetti_bt_lib.bluetooth import DeviceReaderConfig
from bluetti_bt_lib.fields import DecimalField, FieldName, UIntField
from bluetti_bt_lib.registers import ReadableRegisters
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption


class RequestRecordingMock(ClientMockNoEncryption):
    """Mock that also records which registers each request asked for."""

    def __init__(self):
        super().__init__()
        self.reads: list[tuple[int, int]] = []

    async def write_gatt_char(self, char_specifier, data, response=None):
        command = bytes(data)
        if len(command) >= 4 and command[1] == 3:
            self.reads.append(struct.unpack_from("!HH", command, 2))

        await super().write_gatt_char(char_specifier, data, response)


class PackStation(BaseDeviceV1):
    """Small V1 profile with pack slots, so a test can exercise the walk."""

    def __init__(self):
        super().__init__(
            additional_fields=[UIntField(FieldName.PACK_NUM, 96)],
            pack_fields=[
                DecimalField(FieldName.PACK_VOLTAGE, 98, 2),
                UIntField(FieldName.PACK_BATTERY_SOC, 99),
            ],
            max_packs=4,
        )


class SelectorSideEffectMock(ClientMockNoEncryption):
    """Mock that applies writes to its register map.

    A tower station treats a write to the pack bank selector as a claim about
    how many packs it holds, so the installed pack count follows whatever slot
    was last selected. That is the side effect the walk has to undo.
    """

    SELECTOR_REGISTER = 3006
    PACK_COUNT_REGISTER = 96
    EMPTY_SLOT = 1

    def __init__(self):
        super().__init__()
        self.selector_writes: list[int] = []

    def set_register(self, register: int, value: int) -> None:
        struct.pack_into("!H", self._bytemap, register * 2, value)

    def register_value(self, register: int) -> int:
        return struct.unpack_from("!H", self._bytemap, register * 2)[0]

    async def write_gatt_char(self, char_specifier, data, response=None):
        command = bytes(data)
        if len(command) >= 6 and command[1] == 6:
            address, value = struct.unpack_from("!HH", command, 2)
            self.set_register(address, value)
            if address == self.SELECTOR_REGISTER:
                self.selector_writes.append(value)
                self.set_register(self.PACK_COUNT_REGISTER, value)

        await super().write_gatt_char(char_specifier, data, response)

    def _get_register(self, addr: int, size: int) -> bytes:
        # Only the selected slot answers: slot 1 holds nothing in this test.
        if addr >= 98 and self.register_value(self.SELECTOR_REGISTER) == 1:
            return bytes(size * 2)
        return super()._get_register(addr, size)


class TestDeviceReader(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ble_mock = RequestRecordingMock()

        # Device type
        self.ble_mock.add_r_str(10, "AC300", 6)
        # Serial
        self.ble_mock.add_r_sn(17, 2300000000000)
        # DC input power
        self.ble_mock.add_r_int(36, 10)
        # AC input power
        self.ble_mock.add_r_int(37, 8)
        # AC output power
        self.ble_mock.add_r_int(38, 9)
        # AC output power
        self.ble_mock.add_r_int(39, 7)
        # SOC
        self.ble_mock.add_r_int(43, 78)

    async def test_read_all_correct(self):
        device = BaseDeviceV1()
        reader = DeviceReader(
            "00:11:00:11:00:11",
            device,
            asyncio.Future,
            ble_client=self.ble_mock,
            lock=asyncio.Lock(),
        )

        data = await reader.read()

        self.assertEqual(data.get(FieldName.DEVICE_TYPE.value), "AC300")
        self.assertEqual(data.get(FieldName.DEVICE_SN.value), 2300000000000)
        self.assertEqual(data.get(FieldName.DC_INPUT_POWER.value), 10)
        self.assertEqual(data.get(FieldName.AC_INPUT_POWER.value), 8)
        self.assertEqual(data.get(FieldName.AC_OUTPUT_POWER.value), 9)
        self.assertEqual(data.get(FieldName.DC_OUTPUT_POWER.value), 7)
        self.assertEqual(data.get(FieldName.BATTERY_SOC.value), 78)

    async def test_read_soc_wrong(self):
        # SOC
        self.ble_mock.add_r_int(43, 1234)

        device = BaseDeviceV1()
        reader = DeviceReader(
            "00:11:00:11:00:11",
            device,
            asyncio.Future,
            ble_client=self.ble_mock,
            lock=asyncio.Lock(),
        )

        data = await reader.read()

        self.assertIsNone(data.get(FieldName.BATTERY_SOC.value))

    async def test_failed_notify_setup_is_cleaned_up(self):
        device = BaseDeviceV1()
        reader = DeviceReader(
            "00:11:00:11:00:11",
            device,
            asyncio.Future,
            ble_client=self.ble_mock,
            lock=asyncio.Lock(),
        )

        original_start_notify = self.ble_mock.start_notify

        async def start_notify_then_fail(char_specifier, callback, **kwargs):
            await original_start_notify(char_specifier, callback, **kwargs)
            raise RuntimeError("Simulated notification setup failure")

        self.ble_mock.start_notify = start_notify_then_fail

        self.assertFalse(await reader.connect())
        self.assertIsNotNone(self.ble_mock._callback)

        await reader.disconnect()

        self.assertIsNone(self.ble_mock._callback)
        self.assertTrue(self.ble_mock.is_connected)

    async def test_connection_drop_clears_per_connection_state(self):
        device = BaseDeviceV1()
        reader = DeviceReader(
            "00:11:00:11:00:11",
            device,
            asyncio.Future,
            ble_client=self.ble_mock,
            lock=asyncio.Lock(),
        )

        self.assertTrue(await reader.connect())
        self.assertTrue(reader.has_notifier)

        original_write = self.ble_mock.write_gatt_char

        async def write_drops_connection(*args, **kwargs):
            raise BleakError("connection dropped")

        self.ble_mock.write_gatt_char = write_drops_connection

        data = await reader.read([ReadableRegisters(36, 1)])

        self.ble_mock.write_gatt_char = original_write

        self.assertIsNone(data)
        self.assertFalse(reader._connected)
        # The notifier flag must be cleared so the next connect()
        # re-subscribes on the replacement client instead of silently
        # waiting for notifications that never arrive.
        self.assertFalse(reader.has_notifier)

    async def test_reconnect_after_drop_resubscribes_notify(self):
        device = BaseDeviceV1()
        reader = DeviceReader(
            "00:11:00:11:00:11",
            device,
            asyncio.Future,
            ble_client=self.ble_mock,
            lock=asyncio.Lock(),
        )

        self.assertTrue(await reader.connect())

        original_write = self.ble_mock.write_gatt_char

        async def write_drops_connection(*args, **kwargs):
            raise BleakError("connection dropped")

        self.ble_mock.write_gatt_char = write_drops_connection
        self.assertIsNone(await reader.read([ReadableRegisters(36, 1)]))

        # Simulate recovery: once the connection works again the reader
        # must re-subscribe the notifier and read normally.
        self.ble_mock.write_gatt_char = original_write

        data = await reader.read([ReadableRegisters(36, 1)])
        self.assertEqual(data.get(FieldName.DC_INPUT_POWER.value), 10)

    async def test_failed_notify_setup_is_retried_on_next_connect(self):
        device = BaseDeviceV1()
        reader = DeviceReader(
            "00:11:00:11:00:11",
            device,
            asyncio.Future,
            ble_client=self.ble_mock,
            lock=asyncio.Lock(),
        )

        original_start_notify = self.ble_mock.start_notify

        async def start_notify_fails(char_specifier, callback, **kwargs):
            raise RuntimeError("Simulated subscription failure")

        self.ble_mock.start_notify = start_notify_fails
        self.assertFalse(await reader.connect())

        # The failed subscription must not be remembered as active:
        # the next connect() has to retry start_notify() and then read.
        self.ble_mock.start_notify = original_start_notify

        self.assertTrue(await reader.connect())
        self.assertIsNotNone(self.ble_mock._callback)

        data = await reader.read([ReadableRegisters(36, 1)])
        self.assertEqual(data.get(FieldName.DC_INPUT_POWER.value), 10)

    async def test_connect_cancellation_propagates(self):
        device = BaseDeviceV1()
        reader = DeviceReader(
            "00:11:00:11:00:11",
            device,
            asyncio.Future,
            DeviceReaderConfig(timeout=15, use_encryption=True),
            ble_client=self.ble_mock,
            lock=asyncio.Lock(),
        )

        # The encrypted handshake never completes with this mock, so the
        # task sits in the handshake wait loop until it is cancelled.
        task = asyncio.create_task(reader.connect())
        await asyncio.sleep(0.1)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertFalse(reader._connected)

    def build_reader(self, device=None) -> DeviceReader:
        return DeviceReader(
            "00:11:00:11:00:11",
            device or BaseDeviceV1(),
            asyncio.Future,
            ble_client=self.ble_mock,
            lock=asyncio.Lock(),
        )

    async def test_infrequent_fields_are_asked_for_once(self):
        # The identity registers describe the station rather than what it is
        # doing, so a cycle stops asking for them once this connection has
        # answered, while the values keep being reported.
        reader = self.build_reader()

        first = await reader.read()
        self.assertEqual(first.get(FieldName.DEVICE_TYPE.value), "AC300")
        self.assertIn((10, 6), self.ble_mock.reads)
        self.assertIn((17, 4), self.ble_mock.reads)

        self.ble_mock.reads.clear()

        second = await reader.read()
        self.assertEqual(second.get(FieldName.DEVICE_TYPE.value), "AC300")
        self.assertEqual(second.get(FieldName.DEVICE_SN.value), 2300000000000)
        self.assertEqual(second.get(FieldName.BATTERY_SOC.value), 78)
        self.assertNotIn((10, 6), self.ble_mock.reads)
        self.assertNotIn((17, 4), self.ble_mock.reads)

    async def test_infrequent_fields_are_asked_again_after_disconnect(self):
        # The cached answers belong to the connection that produced them. A
        # station that was renamed or reflashed while we were away has to be
        # asked again.
        reader = self.build_reader()

        await reader.read()
        self.ble_mock.reads.clear()

        await reader.disconnect()
        await reader.read()

        self.assertIn((10, 6), self.ble_mock.reads)
        self.assertIn((17, 4), self.ble_mock.reads)

    async def test_live_fields_are_still_asked_for_every_cycle(self):
        # The saving must not reach past the infrequent fields.
        reader = self.build_reader()

        await reader.read()
        self.ble_mock.reads.clear()

        await reader.read()

        self.assertTrue(self.ble_mock.reads)
        self.assertIn((36, 8), self.ble_mock.reads)

    async def test_pack_count_is_asked_for_once_when_slots_are_not_walked(self):
        # The installed pack count only changes when someone swaps a pack, and a
        # cycle that is not walking the slots has no other use for it, so it is
        # asked for once per connection instead of costing a request of its own
        # every cycle. The value keeps being reported from the cache.
        self.ble_mock.add_r_int(96, 2)
        reader = self.build_reader(PackStation())

        first = await reader.read(include_packs=False)
        self.assertIn((96, 1), self.ble_mock.reads)
        self.assertEqual(first.get(FieldName.PACK_NUM.value), 2)

        self.ble_mock.reads.clear()

        second = await reader.read(include_packs=False)
        self.assertNotIn((96, 1), self.ble_mock.reads)
        self.assertIn((36, 8), self.ble_mock.reads)
        self.assertEqual(second.get(FieldName.PACK_NUM.value), 2)

    @patch(
        "bluetti_bt_lib.bluetooth.device_reader.PACK_SELECTOR_SETTLE_SECONDS",
        0.01,
    )
    @patch(
        "bluetti_bt_lib.bluetooth.device_reader.PACK_SELECTOR_EARLY_LOOK_SECONDS",
        0.01,
    )
    async def test_pack_count_is_polled_again_on_a_walk_cycle(self):
        # The cached count is only good enough to skip a walk with. Once a cycle
        # does walk the slots the count has to be fresh, so a pack swapped while
        # the station stayed connected is still noticed.
        self.ble_mock.add_r_int(96, 1)
        self.ble_mock.add_r_int(98, 5220)  # pack voltage
        self.ble_mock.add_r_int(99, 45)  # pack state of charge
        reader = self.build_reader(PackStation())

        await reader.read(include_packs=False)
        self.ble_mock.reads.clear()

        await reader.read(include_packs=True)
        self.assertIn((96, 1), self.ble_mock.reads)

    @patch(
        "bluetti_bt_lib.bluetooth.device_reader.PACK_SELECTOR_SETTLE_SECONDS",
        0.01,
    )
    @patch(
        "bluetti_bt_lib.bluetooth.device_reader.PACK_SELECTOR_EARLY_LOOK_SECONDS",
        0.01,
    )
    async def test_pack_walk_restores_the_bank_selector(self):
        # Probing the slots writes the pack bank selector, and a tower station
        # reacts by reporting that many packs: walking slot 4 makes an AC300
        # claim four. The walk has to put the selector back afterwards, or the
        # next cycle reports the inflated count.
        self.ble_mock = SelectorSideEffectMock()
        self.ble_mock.set_register(96, 3)  # three packs installed
        self.ble_mock.set_register(3006, 3)  # selector at rest matches the count
        self.ble_mock.set_register(98, 5220)  # pack voltage
        self.ble_mock.set_register(99, 45)  # pack state of charge

        data = await self.build_reader(PackStation()).read()

        # Slot 1 is empty, so the walk covers every slot and then restores.
        self.assertEqual(self.ble_mock.selector_writes, [1, 2, 3, 4, 3])
        self.assertEqual(self.ble_mock.register_value(96), 3)
        self.assertEqual(data.get(FieldName.PACK_SLOTS.value), [2, 3, 4])
        self.assertEqual(data.get(FieldName.PACK_NUM.value), 3)
