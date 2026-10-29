import asyncio
import struct
import unittest
from decimal import Decimal
from unittest.mock import patch

from bluetti_bt_lib import DeviceReader
from bluetti_bt_lib.base_devices import BaseDeviceV1
from bluetti_bt_lib.bluetooth import device_reader
from bluetti_bt_lib.devices import AC200M, AC300, AC500, AC60
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption

# The V1 tower stations all report their installed pack count at register 96
# and keep the per-pack data behind the pack selector at 3006.
PACK_STATIONS = (AC200M, AC300, AC500)

PACK_VOLTAGE_REGISTER = 98
PACK_SOC_REGISTER = 99
PACK_SELECTOR_REGISTER = 3006


class SlotAwareMock(ClientMockNoEncryption):
    """Answers the pack registers per slot, with empty slots reading as zero."""

    def __init__(self, packs: dict[int, tuple[int, int]]) -> None:
        super().__init__()
        self.packs = packs
        self.selected: list[int] = []
        self._slot = 0

    async def write_gatt_char(self, char, data, response=None) -> None:
        frame = struct.unpack_from("!HHHH", data)
        # Only a write selects a slot; a read of the same register must not be
        # mistaken for one.
        if (frame[0] & 0xFF) == 6 and frame[1] == PACK_SELECTOR_REGISTER:
            self._slot = frame[2]
            self.selected.append(self._slot)
        await super().write_gatt_char(char, data, response)

    def _get_register(self, starting_address: int, size: int) -> bytes:
        if starting_address in (PACK_VOLTAGE_REGISTER, PACK_SOC_REGISTER):
            voltage, soc = self.packs.get(self._slot, (0, 0))
            self.add_r_int(PACK_VOLTAGE_REGISTER, voltage)
            self.add_r_int(PACK_SOC_REGISTER, soc)
        return super()._get_register(starting_address, size)


class CountingSlotMock(SlotAwareMock):
    """Records one entry per pass over the pack registers of a slot."""

    def __init__(self, packs: dict[int, tuple[int, int]]) -> None:
        super().__init__(packs)
        self.pack_reads: list[int] = []

    def _get_register(self, starting_address: int, size: int) -> bytes:
        if starting_address == PACK_VOLTAGE_REGISTER:
            self.pack_reads.append(self._slot)
        return super()._get_register(starting_address, size)


class SelectorSideEffectMock(SlotAwareMock):
    """A station whose reported pack count follows the slot it was last given.

    Observed on an AC300: writing slot 4 into the selector leaves register 96
    claiming four packs, so a walk that ends on the top slot changes what the
    next cycle reads back.
    """

    async def write_gatt_char(self, char, data, response=None) -> None:
        frame = struct.unpack_from("!HHHH", data)
        if (frame[0] & 0xFF) == 6 and frame[1] == PACK_SELECTOR_REGISTER:
            self.add_r_int(96, frame[2])
        await super().write_gatt_char(char, data, response)


async def _read(device, ble_mock, raw: bool = False):
    reader = DeviceReader(
        "00:11:00:11:00:11",
        device,
        asyncio.Future,
        ble_client=ble_mock,
    )
    # The reader waits for the station to switch packs; no need to wait in tests.
    with patch.object(device_reader, "PACK_SELECTOR_SETTLE_SECONDS", 0):
        return await reader.read(raw=raw)


class TestPackLayout(unittest.TestCase):
    def test_pack_stations_read_pack_num_at_96(self):
        for device_cls in PACK_STATIONS:
            with self.subTest(device=device_cls.__name__):
                device = device_cls()
                field = next(f for f in device.fields if f.name == "pack_num")
                self.assertEqual(field.address, 96)
                self.assertEqual(
                    [f.name for f in device.pack_fields], ["voltage", "battery_soc"]
                )
                self.assertGreater(device.max_packs, 0)

    def test_max_packs_is_the_slots_a_station_holds(self):
        # The reader walks every slot, so this is how far an empty station goes.
        self.assertEqual(AC200M().max_packs, 3)
        self.assertEqual(AC300().max_packs, 4)
        self.assertEqual(AC500().max_packs, 6)


class TestPackSlots(unittest.IsolatedAsyncioTestCase):
    async def _read(self, device: BaseDeviceV1, packs: dict, pack_num: int):
        ble_mock = SlotAwareMock(packs)
        ble_mock.add_r_int(96, pack_num)
        # A station at rest keeps the selector pointing at its last pack, which
        # is what the reader reads back before a walk.
        ble_mock.add_r_int(PACK_SELECTOR_REGISTER, pack_num)
        return await _read(device, ble_mock), ble_mock

    async def test_packs_do_not_have_to_start_at_slot_one(self):
        # The reported case: an AC300 with the bottom slot empty.
        data, ble_mock = await self._read(
            AC300(), {2: (5120, 80), 3: (5110, 70), 4: (5100, 60)}, pack_num=3
        )

        self.assertEqual(data.get("pack_num"), 3)
        self.assertEqual(data.get("pack_slots"), [2, 3, 4])
        self.assertNotIn("pack_1_voltage", data)
        self.assertNotIn("pack_1_battery_soc", data)
        self.assertEqual(data.get("pack_2_voltage"), Decimal("51.2"))
        self.assertEqual(data.get("pack_3_voltage"), Decimal("51.1"))
        self.assertEqual(data.get("pack_4_voltage"), Decimal("51.0"))
        self.assertEqual(data.get("pack_4_battery_soc"), 60)
        # Every slot was probed, then the selector went back where it was.
        self.assertEqual(ble_mock.selected, [1, 2, 3, 4, 3])

    async def test_empty_slots_between_packs_are_skipped(self):
        data, ble_mock = await self._read(
            AC500(), {2: (5120, 80), 4: (5100, 60)}, pack_num=2
        )

        # The gaps have to be walked, so the walk reaches slot 4 and stops.
        self.assertEqual(ble_mock.selected, [1, 2, 3, 4, 2])
        self.assertEqual(data.get("pack_slots"), [2, 4])
        self.assertEqual(data.get("pack_2_voltage"), Decimal("51.2"))
        self.assertEqual(data.get("pack_4_voltage"), Decimal("51.0"))
        for slot in (1, 3, 5, 6):
            self.assertNotIn(f"pack_{slot}_voltage", data)
            self.assertNotIn(f"pack_{slot}_battery_soc", data)

    async def test_walk_stops_once_the_reported_packs_have_answered(self):
        data, ble_mock = await self._read(
            AC500(), {1: (5120, 80), 2: (5110, 70)}, pack_num=2
        )

        self.assertEqual(ble_mock.selected, [1, 2, 2])
        self.assertEqual(data.get("pack_slots"), [1, 2])
        self.assertEqual(data.get("pack_2_voltage"), Decimal("51.1"))
        self.assertNotIn("pack_3_voltage", data)
        self.assertNotIn("pack_6_battery_soc", data)

    async def test_no_packs_reported_skips_the_walk(self):
        data, ble_mock = await self._read(AC300(), {}, pack_num=0)

        self.assertEqual(ble_mock.selected, [])
        self.assertEqual(data.get("pack_num"), 0)
        self.assertEqual(data.get("pack_slots"), [])
        self.assertNotIn("pack_1_voltage", data)

    async def test_missing_pack_num_walks_every_slot(self):
        # A profile without a pack_num field leaves nothing to stop the walk
        # short of the slots the device says it holds.
        device = AC500()
        device.fields = [f for f in device.fields if f.name != "pack_num"]

        ble_mock = SlotAwareMock({5: (5100, 60)})
        ble_mock.add_r_int(PACK_SELECTOR_REGISTER, 5)
        data = await _read(device, ble_mock)

        self.assertEqual(ble_mock.selected, [1, 2, 3, 4, 5, 6, 5])
        self.assertEqual(data.get("pack_slots"), [5])
        self.assertEqual(data.get("pack_5_voltage"), Decimal("51.0"))
        self.assertNotIn("pack_6_voltage", data)

    async def test_raw_scans_report_every_slot(self):
        # Raw is for register scanning, so it keeps the empty slots and leaves
        # out the synthesized slot list.
        ble_mock = SlotAwareMock({2: (5120, 80)})
        ble_mock.add_r_int(96, 1)

        data = await _read(AC300(), ble_mock, raw=True)

        self.assertEqual(ble_mock.selected, [1, 2, 3, 4])
        self.assertNotIn("pack_slots", data)

    async def test_stations_without_packs_have_no_slot_list(self):
        data = await _read(AC60(), ClientMockNoEncryption())

        self.assertIsNotNone(data)
        self.assertNotIn("pack_slots", data)


class TestSelectorRestore(unittest.IsolatedAsyncioTestCase):
    async def test_pack_count_survives_a_walk(self):
        # The count the station reports has to be the same in the cycle after a
        # walk as it was in the cycle that walked. Without the restore, three
        # packs in slots 2, 3 and 4 come back as four from then on.
        ble_mock = SelectorSideEffectMock(
            {2: (5120, 80), 3: (5110, 70), 4: (5100, 60)}
        )
        ble_mock.add_r_int(96, 3)
        ble_mock.add_r_int(PACK_SELECTOR_REGISTER, 3)

        reader = DeviceReader(
            "00:11:00:11:00:11", AC300(), asyncio.Future, ble_client=ble_mock
        )

        with patch.object(device_reader, "PACK_SELECTOR_SETTLE_SECONDS", 0):
            first = await reader.read()
            second = await reader.read()

        self.assertEqual(ble_mock.selected, [1, 2, 3, 4, 3, 1, 2, 3, 4, 3])
        self.assertEqual(first.get("pack_num"), 3)
        self.assertEqual(second.get("pack_num"), 3)
        self.assertEqual(second.get("pack_slots"), [2, 3, 4])

    async def test_a_walk_cut_short_still_puts_the_selector_back(self):
        # A walk that dies part way through leaves the selector on the slot it
        # was probing, which is enough to corrupt the count on its own.
        ble_mock = SelectorSideEffectMock({2: (5120, 80), 3: (5110, 70)})
        ble_mock.add_r_int(96, 3)
        ble_mock.add_r_int(PACK_SELECTOR_REGISTER, 3)

        reader = DeviceReader(
            "00:11:00:11:00:11", AC300(), asyncio.Future, ble_client=ble_mock
        )
        real_read_pack = reader._read_pack

        async def quiet_slot_3(pack, pack_registers, raw):
            if pack == 3:
                raise device_reader.ResponseTimeout("slot 3 stopped answering")
            return await real_read_pack(pack, pack_registers, raw)

        with patch.object(device_reader, "PACK_SELECTOR_SETTLE_SECONDS", 0), patch.object(
            reader, "_read_pack", quiet_slot_3
        ):
            self.assertIsNone(await reader.read())

        # Slot 3 was probed, then the selector went back to 3 anyway.
        self.assertEqual(ble_mock.selected, [1, 2, 3, 3])

        # The next cycle still reads three packs, not the number the aborted
        # walk would have left behind.
        with patch.object(device_reader, "PACK_SELECTOR_SETTLE_SECONDS", 0), patch.object(
            device_reader, "RESPONSE_TIMEOUT_SECONDS", 0.5
        ), patch.object(device_reader, "STRAY_ANSWER_SECONDS", 0):
            second = await reader.read()
        self.assertEqual(second.get("pack_num"), 3)
        self.assertEqual(second.get("pack_slots"), [2, 3])

    async def test_a_selector_that_reads_as_no_slot_is_left_alone(self):
        # Nothing sensible was saved, so the walk must not invent a write.
        ble_mock = SlotAwareMock({1: (5120, 80)})
        ble_mock.add_r_int(96, 1)

        await _read(AC300(), ble_mock)

        self.assertEqual(ble_mock.selected, [1])


class TestPackPolling(unittest.IsolatedAsyncioTestCase):
    async def test_installed_packs_are_read_from_the_shared_map(self):
        ble_mock = ClientMockNoEncryption()
        ble_mock.add_r_str(10, "AC500", 6)
        ble_mock.add_r_sn(17, 2551110023321)
        ble_mock.add_r_int(43, 80)
        ble_mock.add_r_int(96, 2)
        ble_mock.add_r_int(PACK_VOLTAGE_REGISTER, 5120)
        ble_mock.add_r_int(PACK_SOC_REGISTER, 77)

        data = await _read(AC500(), ble_mock)

        self.assertIsNotNone(data)
        self.assertEqual(data.get("pack_num"), 2)
        self.assertEqual(data.get("pack_slots"), [1, 2])
        self.assertEqual(data.get("pack_1_battery_soc"), 77)
        self.assertEqual(data.get("pack_2_battery_soc"), 77)
        # Register 98 is centivolts, so 5120 reads as 51.2 V per pack.
        self.assertEqual(data.get("pack_1_voltage"), Decimal("51.2"))
        self.assertEqual(data.get("pack_2_voltage"), Decimal("51.2"))
        # Register 96 says two packs, so slots 3 to 6 are never selected.
        self.assertNotIn("pack_3_battery_soc", data)
        self.assertNotIn("pack_6_battery_soc", data)
        self.assertNotIn("pack_3_voltage", data)
        self.assertNotIn("pack_6_voltage", data)


    async def test_pack_walk_can_be_skipped_for_a_cycle(self):
        # A consumer that polls often can walk the slots now and then. A
        # skipped cycle sends no selector writes and says nothing about the
        # slots, which is not the same as reporting them empty.
        ble_mock = SlotAwareMock({1: (5120, 80), 2: (5110, 70)})
        ble_mock.add_r_int(96, 2)

        reader = DeviceReader(
            "00:11:00:11:00:11", AC300(), asyncio.Future, ble_client=ble_mock
        )
        data = await reader.read(include_packs=False)

        self.assertIsNotNone(data)
        self.assertEqual(ble_mock.selected, [])
        self.assertEqual(data.get("pack_num"), 2)
        self.assertNotIn("pack_slots", data)
        self.assertNotIn("pack_1_voltage", data)
        self.assertNotIn("pack_2_battery_soc", data)


class TestPackSettle(unittest.IsolatedAsyncioTestCase):
    """The settle wait is paid in full only by slots that answer with nothing."""

    async def _read(self, device: BaseDeviceV1, packs: dict, pack_num: int):
        ble_mock = CountingSlotMock(packs)
        ble_mock.add_r_int(96, pack_num)

        reader = DeviceReader(
            "00:11:00:11:00:11", device, asyncio.Future, ble_client=ble_mock
        )

        slept: list[float] = []

        async def record_sleep(seconds):
            slept.append(seconds)

        with patch("asyncio.sleep", record_sleep):
            data = await reader.read()

        return data, ble_mock, slept

    async def test_loaded_slot_only_pays_the_early_look(self):
        _, ble_mock, slept = await self._read(
            AC300(), {1: (5120, 80), 2: (5110, 70)}, pack_num=2
        )

        # One short wait per slot, and the pack registers are read once each.
        self.assertEqual(slept, [1, 1])
        self.assertEqual(ble_mock.pack_reads, [1, 2])

    async def test_empty_slot_waits_out_the_settle(self):
        data, ble_mock, slept = await self._read(
            AC300(), {2: (5120, 80)}, pack_num=1
        )

        # Slot 1 answers nothing, so it waits the rest of the settle and reads
        # again. Slot 2 is loaded and only pays the early look.
        self.assertEqual(slept, [1, 2, 1])
        self.assertEqual(ble_mock.pack_reads, [1, 1, 2])
        self.assertEqual(data.get("pack_slots"), [2])


if __name__ == "__main__":
    unittest.main()
