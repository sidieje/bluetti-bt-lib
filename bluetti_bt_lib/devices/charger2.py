import struct

from ..base_devices import BaseDeviceV2
from ..bluetooth import ReadableRegisters
from ..fields import (
    SwitchField,
    FieldName,
    UIntField,
    DecimalField,
    SwapStringField,
    SerialNumberField,
    SignedDecimalField,
)
from ..registers import WriteableRegister


class Charger2SystemSwitchField(SwitchField):
    """Charger 2 system on/off control.

    Register 15600 uses complete 16-bit control words:
        0x5529 = ON
        0xAA2A = OFF
    """

    def __init__(self, name: FieldName, address: int):
        super().__init__(name, address)

    def parse(self, data: bytes) -> bool:
        value = struct.unpack("!H", data)[0]

        if value == 0x5529:
            return True

        if value == 0xAA2A:
            return False

        raise ValueError(
            f"Unexpected Charger 2 system switch value: 0x{value:04X}"
        )

    def is_writeable(self) -> bool:
        return True

    def allowed_write_type(self, value) -> bool:
        return isinstance(value, bool)


class CHARGER2(BaseDeviceV2):
    def __init__(self):
        super().__init__(
            [
                DecimalField(FieldName.DC_INPUT_VOLTAGE, 15531, 1),
                DecimalField(FieldName.DC_INPUT_CURRENT, 15532, 2),
                UIntField(FieldName.DC_INPUT_POWER, 15534),

                DecimalField(FieldName.DC_2_OUTPUT_VOLTAGE, 15535, 1),
                SignedDecimalField(FieldName.DC_2_OUTPUT_CURRENT, 15536, 2, multiplier=-1),
                SignedDecimalField(FieldName.DC_2_OUTPUT_POWER_TOTAL, 15538, 0, multiplier=-1),

                DecimalField(FieldName.B_VOLTAGE, 15543, 1),
                SignedDecimalField(FieldName.B_IO_POWER, 15546, 0, multiplier=-1),
                UIntField(FieldName.BATTERY_SOC, 15584),

                DecimalField(FieldName.B_ALT_VOLTAGE, 15539, 1),
                DecimalField(FieldName.B_ALT_CURRENT, 15540, 2),
                DecimalField(FieldName.B_ALT_IO_POWER, 15542, 0),

                Charger2SystemSwitchField(
                    FieldName.CTRL_SYSTEM_ON_OFF,
                    15600,
                ),

                SwapStringField(FieldName.DEVICE_TYPE, 15500, 6),
                SerialNumberField(FieldName.DEVICE_SN, 15506),
            ],
        )

    def build_write_command(self, name: str, value):
        if name == FieldName.CTRL_SYSTEM_ON_OFF.value:
            return WriteableRegister(
                15600,
                0x5529 if value else 0xAA2A,
            )

        return super().build_write_command(name, value)

    def get_device_type_registers(self):
        return [
            ReadableRegisters(15500, 6),
        ]

    def get_device_sn_registers(self):
        return [
            ReadableRegisters(15506, 4),
        ]
