"""Bleak Mock Client for unittests."""

from decimal import Decimal
import struct
import sys
from typing import Awaitable, Callable, List, Union
import uuid
from bleak.backends.characteristic import BleakGATTCharacteristic
import crcmod

if sys.version_info < (3, 12):
    from typing_extensions import Buffer
else:
    from collections.abc import Buffer

modbus_crc = crcmod.predefined.mkCrcFun("modbus")


def swap_bytes(data: bytes):
    """Swaps the place of every other byte, returning a new byte array"""
    arr = bytearray(data)
    for i in range(0, len(arr) - 1, 2):
        arr[i], arr[i + 1] = arr[i + 1], arr[i]
    return arr


def r_int(value: int):
    return struct.pack("!H", value)


def r_str(value: str, max_size: int):
    return struct.pack(f"!{max_size}s", value.encode("ascii"))


def r_sstr(value: str, max_size: int):
    return swap_bytes(r_str(value, max_size))


def r_sn(value: int):
    part4 = value & 0xFFFF
    part3 = (value >> 16) & 0xFFFF
    part2 = (value >> 32) & 0xFFFF
    part1 = (value >> 48) & 0xFFFF
    return struct.pack("!4H", part4, part3, part2, part1)


class BleakClientMock:
    """Mock a BLE Client."""

    def __init__(self, packs_max: int = 0):
        self.address = "00:11:00:11:00:11"
        self._bytemap: bytearray = bytearray(40000)
        self.packs: List[bytearray] = [bytearray() for _ in range(packs_max)]
        self._connected = True
        self._callback = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def add_r_int(self, register: int, value: int):
        real = register * 2
        self._bytemap[real : real + 2] = r_int(value)

    def add_r_str(self, register: int, value: str, max_size: int):
        real = register * 2
        self._bytemap[real : real + max_size * 2] = r_str(value, max_size)

    def add_r_sstr(self, register: int, value: str, max_size: int):
        real = register * 2
        self._bytemap[real : real + max_size * 2] = r_sstr(value, max_size)

    def add_r_sn(self, register: int, value: int):
        real = register * 2
        self._bytemap[real : real + 8] = r_sn(value)

    def add_pack(self, n: int, b: bytearray):
        self.packs[n] = b

    async def start_notify(
        self,
        char_specifier: Union[BleakGATTCharacteristic, int, str, uuid.UUID],
        callback: Callable[
            [BleakGATTCharacteristic, bytearray], Union[None, Awaitable[None]]
        ],
        **kwargs,
    ) -> None:
        self._callback = callback

    async def stop_notify(
        self,
        char_specifier: Union[BleakGATTCharacteristic, int, str, uuid.UUID],
    ) -> None:
        self._callback = None

    async def write_gatt_char(
        self,
        char_specifier: Union[BleakGATTCharacteristic, int, str, uuid.UUID],
        data: Buffer,
        response: bool | None = None,
    ) -> None:
        if self._callback is None:
            # Nobody is listening on the notify characteristic
            return

        request = bytes(data)
        slave, function = request[0], request[1]

        if function == 3:
            address, quantity = struct.unpack_from("!HH", request, 2)
            body = self._get_register(address, quantity)
            content = self._read_frame(slave, body)
        elif function == 6:
            # A function 06 slave answers by echoing the request back.
            content = bytearray(request)
        else:
            return

        await self._callback(char_specifier, content)

    def _read_frame(self, slave: int, body: bytes) -> bytearray:
        """Wrap register contents in a function 03 answer (header, byte count, CRC)."""
        frame = bytearray(len(body) + 5)
        frame[0] = slave
        frame[1] = 3
        frame[2] = len(body)
        frame[3:-2] = body
        struct.pack_into("<H", frame, -2, modbus_crc(frame[:-2]))
        return frame

    def _get_register(self, addr: int, size: int) -> bytes:
        """The register contents a read of size words at addr returns."""
        real = addr * 2
        return bytes(self._bytemap[real : real + size * 2])


class ClientMockNoEncryption(BleakClientMock):
    """Mock for unencrypted devices"""
