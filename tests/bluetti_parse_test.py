"""Regression tests for the readall dump parser.

The readall file format changed when the read blocks went from 50 registers
starting at register 1 to 10 registers starting at register 0. The parser kept
its old assumptions, which shifted every decoded field by one register, and
nothing caught it because no test built a dump. These tests build dumps exactly
the way ``bluetti-readall`` writes them.
"""

import unittest
from decimal import Decimal
from unittest.mock import patch

from bluetti_bt_lib.base_devices import BaseDeviceV1
from bluetti_bt_lib.fields import FieldName
from bluetti_bt_lib.scripts.bluetti_parse import (
    FULL_REGISTER_MAP_SIZE,
    build_register_map,
    parse_readall,
)
from bluetti_bt_lib.scripts.types import ReadallData
from bluetti_bt_lib.utils.bleak_client_mock import r_int, r_sstr, r_str

# The register a dump starts at, and the width of one read block, both of which
# come from BaseDeviceV1/BaseDeviceV2.get_full_registers_range().
FIRST_REGISTER = 0
BLOCK_REGISTERS = 10

V1_TYPE_REGISTER = 10
V2_TYPE_REGISTER = 110
V1_SOC_REGISTER = 43
V2_SOC_REGISTER = 102

# AC300 per-pack readings, selected one pack at a time by a write to 3006.
PACK_VOLTAGE_REGISTER = 98
PACK_SOC_REGISTER = 99


def _blank_map() -> bytearray:
    return bytearray(FULL_REGISTER_MAP_SIZE)


def _put_int(registers_map: bytearray, register: int, value: int) -> None:
    offset = register * 2
    registers_map[offset : offset + 2] = r_int(value)


def _put_string(
    registers_map: bytearray, register: int, size: int, value: str, swap: bool
) -> None:
    """Write a string field over all of its registers.

    ``size`` counts registers, so the field holds twice that many bytes.
    """
    raw = r_sstr(value, size * 2) if swap else r_str(value, size * 2)
    offset = register * 2
    registers_map[offset : offset + size * 2] = raw


def _as_dump(registers_map: bytearray, iot_version: int) -> dict[str, str]:
    """Split a register map into hex blocks, keyed like readall keys them."""
    end = 8000 if iot_version == 1 else 20000
    return {
        str(start): registers_map[start * 2 : start * 2 + BLOCK_REGISTERS * 2].hex()
        for start in range(FIRST_REGISTER, end, BLOCK_REGISTERS)
    }


def _v1_dump(device_type: str = "EB3A", soc: int = 55) -> ReadallData:
    registers_map = _blank_map()
    _put_string(registers_map, V1_TYPE_REGISTER, 6, device_type, swap=False)
    _put_int(registers_map, V1_SOC_REGISTER, soc)
    return ReadallData(
        mac="00:11:00:11:00:11",
        iotVersion=1,
        encryption=False,
        registers=_as_dump(registers_map, 1),
    )


def _v2_dump(device_type: str = "AC70", soc: int = 55) -> ReadallData:
    registers_map = _blank_map()
    _put_string(registers_map, V2_TYPE_REGISTER, 6, device_type, swap=True)
    _put_int(registers_map, V2_SOC_REGISTER, soc)
    return ReadallData(
        mac="00:11:00:11:00:11",
        iotVersion=2,
        encryption=False,
        registers=_as_dump(registers_map, 2),
    )


def _ac300_dump() -> ReadallData:
    registers_map = _blank_map()
    _put_string(registers_map, V1_TYPE_REGISTER, 6, "AC300", swap=False)
    _put_int(registers_map, V1_SOC_REGISTER, 55)
    _put_int(registers_map, PACK_VOLTAGE_REGISTER, 12345)
    _put_int(registers_map, PACK_SOC_REGISTER, 77)
    return ReadallData(
        mac="00:11:00:11:00:11",
        iotVersion=1,
        encryption=False,
        registers=_as_dump(registers_map, 1),
    )


class TestBuildRegisterMap(unittest.TestCase):
    def test_blocks_are_placed_at_their_own_register_offsets(self):
        registers_map = build_register_map({"0": "0102", "10": "0304"})
        self.assertEqual(registers_map[0:2], b"\x01\x02")
        self.assertEqual(registers_map[20:22], b"\x03\x04")

    def test_blocks_are_placed_by_key_and_not_by_order(self):
        """The key says where a block belongs, so the order says nothing.

        json.load happens to keep the order a file lists its keys in, so a
        dump written by bluetti-readall has always arrived in order here. A
        hand-edited or re-serialised dump need not, and placing blocks by
        their turn through the dictionary would quietly misplace them.
        """
        registers_map = build_register_map({"20": "0304", "0": "0102", "10": "0506"})
        self.assertEqual(registers_map[0:2], b"\x01\x02")
        self.assertEqual(registers_map[20:22], b"\x05\x06")
        self.assertEqual(registers_map[40:42], b"\x03\x04")

    def test_absent_block_leaves_a_gap_rather_than_shifting_the_rest(self):
        """A block the dump never got an answer for is a gap, not a shift.

        Dropping a key used to pull every block after it down by a block,
        which is the same fault as reading the map from the wrong register.
        """
        registers_map = build_register_map({"0": "0102", "20": "0304"})
        self.assertEqual(len(registers_map), FULL_REGISTER_MAP_SIZE)
        self.assertEqual(registers_map[20:40], b"\x00" * 20)
        self.assertEqual(registers_map[40:42], b"\x03\x04")

    def test_empty_block_is_still_a_whole_block(self):
        """An empty block is a whole block.

        Padding it with less than a block used to shrink the map and slide
        every later block down, which is why the map is checked for length as
        well as for the offset after the gap.
        """
        registers_map = build_register_map({"0": "0102", "10": "", "20": "0304"})
        self.assertEqual(len(registers_map), FULL_REGISTER_MAP_SIZE)
        self.assertEqual(registers_map[20:40], b"\x00" * 20)
        self.assertEqual(registers_map[40:42], b"\x03\x04")

    def test_block_outside_the_map_is_dropped(self):
        """A key past the readall range cannot be placed.

        Writing it would grow the map, and parse() takes the range of fields
        it will decode from the map's length.
        """
        registers_map = build_register_map({"0": "0102", "20000": "0304", "-1": "0506"})
        self.assertEqual(len(registers_map), FULL_REGISTER_MAP_SIZE)
        self.assertEqual(registers_map[0:2], b"\x01\x02")
        self.assertEqual(registers_map[-20:], b"\x00" * 20)


class TestParseReadall(unittest.TestCase):
    def test_v1_device_identification(self):
        parsed, error = parse_readall(_v1_dump())
        self.assertIsNone(error)
        self.assertEqual(parsed[FieldName.DEVICE_TYPE.value], "EB3A")

    def test_v2_device_identification(self):
        parsed, error = parse_readall(_v2_dump())
        self.assertIsNone(error)
        self.assertEqual(parsed[FieldName.DEVICE_TYPE.value], "AC70")

    def test_ordinary_register_is_decoded(self):
        """A field decodes to the value written at its own register.

        Reading the map from register 1 instead of 0 shifted every field down
        by one register, so the SOC read the word before the one that was set.
        """
        for dump in (_v1_dump(), _v2_dump()):
            with self.subTest(iotVersion=dump.iotVersion):
                parsed, error = parse_readall(dump)
                self.assertIsNone(error)
                self.assertEqual(parsed[FieldName.BATTERY_SOC.value], 55)

    def test_pack_fields_are_keyed_by_field_name(self):
        """Per-pack fields come back under their plain field name.

        Passing a pack number in makes parse() prefix the keys with it, and the
        dump only ever holds one pack's readings, so the prefix is not something
        the caller can turn back into a FieldName.
        """
        parsed, error = parse_readall(_ac300_dump())
        self.assertIsNone(error)
        self.assertEqual(parsed[FieldName.PACK_VOLTAGE.value], Decimal("123.45"))
        self.assertEqual(parsed[FieldName.PACK_BATTERY_SOC.value], 77)
        for key in parsed:
            FieldName(key)

    def test_missing_intermediate_block_does_not_misalign_the_map(self):
        """A block the dump is missing still costs a whole block.

        readall writes an empty string for a read that answered with nothing,
        and a key can be absent from the file altogether. Either way the
        registers after it belong at their own addresses, and pulling them
        down a block is the same class of fault as reading the map from the
        wrong starting register.
        """
        for missing, label in (("", "empty"), (None, "absent")):
            with self.subTest(block=label):
                dump = _v1_dump()
                if missing is None:
                    del dump.registers["30"]
                else:
                    dump.registers["30"] = missing

                registers_map = build_register_map(dump.registers)
                self.assertEqual(len(registers_map), FULL_REGISTER_MAP_SIZE)

                parsed, error = parse_readall(dump)
                self.assertIsNone(error)
                self.assertEqual(parsed[FieldName.DEVICE_TYPE.value], "EB3A")
                self.assertEqual(parsed[FieldName.BATTERY_SOC.value], 55)

    def test_dump_with_blocks_out_of_order_still_decodes(self):
        """The order the keys sit in is not part of the format."""
        dump = _v1_dump()
        dump.registers = dict(reversed(list(dump.registers.items())))

        parsed, error = parse_readall(dump)
        self.assertIsNone(error)
        self.assertEqual(parsed[FieldName.DEVICE_TYPE.value], "EB3A")
        self.assertEqual(parsed[FieldName.BATTERY_SOC.value], 55)

    def test_unknown_device_type_reports_a_diagnostic(self):
        """A type this library has no profile for is reported, not raised.

        What comes back is the base device decode, since there is no profile to
        decode it with.
        """
        parsed, error = parse_readall(_v1_dump(device_type="AC1000"))
        self.assertEqual(parsed[FieldName.DEVICE_TYPE.value], "AC1000")
        self.assertEqual(error, "Unsupported powerstation type 'AC1000'")

    def test_empty_device_type_reports_a_diagnostic(self):
        parsed, error = parse_readall(_v1_dump(device_type=""))
        self.assertEqual(parsed[FieldName.DEVICE_TYPE.value], "")
        self.assertEqual(error, "Unsupported powerstation type ''")

    def test_missing_device_type_is_still_reported_as_unknown(self):
        """The guard for a type register that yields nothing is still in place."""
        with patch.object(BaseDeviceV1, "parse", return_value={}):
            parsed, error = parse_readall(_v1_dump())
        self.assertEqual(parsed, {})
        self.assertEqual(error, "Unknown device type")


if __name__ == "__main__":
    unittest.main()
