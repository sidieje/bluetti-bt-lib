import struct

from . import SwitchField


class SwitchFieldNonZero(SwitchField):
    """Switch that writes 0/1 but reads like BoolFieldNonZero: only 1 is on.

    For registers that report a value other than 0 when off, such as 3 on the
    AC2P's 2011. A plain SwitchField reads those as None, so the switch shows
    unknown and a written False never verifies.
    """

    def parse(self, data: bytes) -> bool:
        return struct.unpack("!H", data)[0] == 1
