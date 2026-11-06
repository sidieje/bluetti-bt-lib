import json
import argparse
import asyncio
import logging

from ..base_devices import BaseDeviceV1, BaseDeviceV2, BluettiDevice
from ..fields import FieldName, get_unit
from ..utils.device_builder import build_device
from .types import ReadallData

REGISTER_BLOCK_SIZE = 20
FULL_REGISTER_MAP_SIZE = 40000


def build_register_map(registers: dict[str, str]) -> bytearray:
    """Rebuild a full register map out of the blocks in a readall dump.

    readall keys each hex block by the register it starts at, so every block
    goes back where it came from. Placing a block by its own key rather than by
    its turn in the dictionary means a dump that lists blocks out of order, or
    is missing one, still decodes the blocks it does hold.
    """
    registers_map = bytearray(FULL_REGISTER_MAP_SIZE)

    for address, block_hex in registers.items():
        offset = int(address) * 2
        if offset < 0 or offset + REGISTER_BLOCK_SIZE > FULL_REGISTER_MAP_SIZE:
            # A key outside the range readall reads is not a block that can be
            # placed, and writing past the end would grow the map out from
            # under parse(), which reads its size back out of the data.
            continue

        block = bytes.fromhex(block_hex) if len(block_hex) > 0 else b""
        # Slice assignment resizes a bytearray when the replacement is not
        # exactly the width of the slice, so every block is trimmed or padded
        # to a whole block first. A block of any other width would slide the
        # registers it overlaps.
        block = block[:REGISTER_BLOCK_SIZE].ljust(REGISTER_BLOCK_SIZE, b"\x00")
        registers_map[offset : offset + REGISTER_BLOCK_SIZE] = block

    return registers_map


def parse_readall(data: ReadallData) -> tuple[dict, str | None]:
    """Decode a readall dump.

    The base device is only used to read the type register, then the matching
    device profile decodes the whole map. Returns the decoded fields and, when
    no profile could be used, the reason why.
    """
    base_device: BluettiDevice
    if data.iotVersion == 1:
        base_device = BaseDeviceV1()
    else:
        base_device = BaseDeviceV2()

    registers_map = build_register_map(data.registers)

    parsed = base_device.parse(0, registers_map)

    device_type = parsed.get(FieldName.DEVICE_TYPE.value)

    if device_type is None:
        return parsed, "Unknown device type"

    device = build_device(device_type + "12345678")

    if device is None:
        return parsed, f"Unsupported powerstation type {device_type!r}"

    return device.parse(0, registers_map), None


async def async_parse_file(filename: str):
    print(f"Reading file {filename}")

    with open(filename, "r") as json_data:
        dict_data = json.load(json_data)
        data = ReadallData(**dict_data)

    parsed, error = parse_readall(data)

    if error is not None:
        print(error)
        print("Parsed data:", parsed)
        return

    print()
    for key, value in parsed.items():
        key = FieldName(key)
        unit = get_unit(key)
        print(f"{key}: {value}" + ("" if unit is None else unit))


def start():
    """Entrypoint."""
    parser = argparse.ArgumentParser(description="Parse readall output files")
    parser.add_argument(
        "file", type=str, help="JSON file of the powerstation readall output"
    )
    args = parser.parse_args()

    if args.file is None:
        parser.print_help()
        return

    logging.basicConfig(level=logging.WARNING)

    asyncio.run(async_parse_file(args.file))
