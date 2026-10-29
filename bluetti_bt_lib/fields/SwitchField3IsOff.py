import struct

from . import SwitchField


class SwitchField3IsOff(SwitchField):
    """Switch that writes 0/1 and also reads 3 as off.

    The AC2P reports 3 on 2011 when its AC output is off. A plain SwitchField
    reads that as None, so the switch shows unknown and a written False never
    verifies. Any other value is still unknown, so it raises ValueError and
    the field is skipped for that read rather than reported as off.
    """

    def parse(self, data: bytes) -> bool:
        value = struct.unpack("!H", data)[0]
        if value not in (0, 1, 3):
            raise ValueError(f"Unexpected switch value: 0x{value:04X}")
        return value == 1
