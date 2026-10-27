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


# Register 15600 contains both the system state and silent charging state.
# Each entry is: (system_on, silent_charging).
CHARGER2_CONTROL_STATES = {
    0x5529: (True, False),
    0xAA2A: (False, False),
    0xAA26: (False, True),
    0x5525: (True, True),
}


class Charger2ControlField(SwitchField):
    """Decode one boolean control from Charger 2 register 15600."""

    def __init__(self, name: FieldName, address: int, state_index: int):
        super().__init__(name, address)
        self.state_index = state_index

    def parse(self, data: bytes) -> bool:
        value = struct.unpack("!H", data)[0]

        try:
            state = CHARGER2_CONTROL_STATES[value]
        except KeyError:
            raise ValueError(
                f"Unexpected Charger 2 control value: 0x{value:04X}"
            ) from None

        return state[self.state_index]

    def is_writeable(self) -> bool:
        return True

    def allowed_write_type(self, value) -> bool:
        return isinstance(value, bool)


class Charger2SystemSwitchField(Charger2ControlField):
    """Charger 2 system on/off control."""

    def __init__(self, name: FieldName, address: int):
        super().__init__(name, address, state_index=0)


class Charger2SilentChargingField(Charger2ControlField):
    """Charger 2 silent charging control."""

    def __init__(self, name: FieldName, address: int):
        super().__init__(name, address, state_index=1)


class CHARGER2(BaseDeviceV2):
    def __init__(self):
        super().__init__(
            [
                DecimalField(FieldName.DC_INPUT_VOLTAGE, 15531, 1),
                DecimalField(FieldName.DC_INPUT_CURRENT, 15532, 2),
                UIntField(FieldName.DC_INPUT_POWER, 15534),

                DecimalField(FieldName.DC_2_OUTPUT_VOLTAGE, 15535, 1),
                SignedDecimalField(
                    FieldName.DC_2_OUTPUT_CURRENT, 15536, 2, multiplier=-1
                ),
                SignedDecimalField(
                    FieldName.DC_2_OUTPUT_POWER_TOTAL, 15538, 0, multiplier=-1
                ),

                DecimalField(FieldName.B_VOLTAGE, 15543, 1),
                SignedDecimalField(
                    FieldName.B_IO_POWER, 15546, 0, multiplier=-1
                ),
                UIntField(FieldName.BATTERY_SOC, 15584),

                DecimalField(FieldName.B_ALT_VOLTAGE, 15539, 1),
                # Starter battery side: positive while the vehicle (alternator)
                # charges through the Charger 2, negative while reverse
                # charging feeds the starter battery.
                SignedDecimalField(FieldName.B_ALT_CURRENT, 15540, 2),
                SignedDecimalField(FieldName.B_ALT_IO_POWER, 15542, 0),

                Charger2SystemSwitchField(
                    FieldName.CTRL_SYSTEM_ON_OFF,
                    15600,
                ),
                Charger2SilentChargingField(
                    FieldName.CTRL_SILENT_CHARGING,
                    15600,
                ),

                SwapStringField(FieldName.DEVICE_TYPE, 15500, 6),
                SerialNumberField(FieldName.DEVICE_SN, 15506),
            ],
            include_common_fields=False,
        )

    def build_write_command(self, name: str, value):
        # These controls share register 15600. Their writes must be
        # constructed by DeviceWriter using a fresh read-modify-write.
        # Do not construct a standalone command for either field here.
        if name in (
            FieldName.CTRL_SYSTEM_ON_OFF.value,
            FieldName.CTRL_SILENT_CHARGING.value,
        ):
            raise ValueError(
                "Charger 2 controls require a read-modify-write operation"
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
