import struct
from decimal import Decimal

from . import DeviceField, FieldName


class SignedDecimalField(DeviceField):
    def __init__(
        self,
        name: FieldName,
        address: int,
        scale: int = 1,
        multiplier: float = 1,
        min: Decimal | None = None,
        max: Decimal | None = None,
    ):
        super().__init__(name, address, 1)
        self.scale = scale
        self.multiplier = multiplier
        self.min = min
        self.max = max

    def parse(self, data: bytes) -> Decimal:
        val = Decimal(struct.unpack("!h", data)[0])
        result = (val / 10 ** self.scale) * Decimal(str(self.multiplier))
        # A zero reading times a negative multiplier is Decimal("-0").
        return result.copy_abs() if result.is_zero() else result

    def in_range(self, value: Decimal) -> bool:
        if self.min is not None and self.min > value:
            return False
        if self.max is not None and self.max < value:
            return False
        return True
