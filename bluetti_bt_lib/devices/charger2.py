import struct

from ..base_devices import BaseDeviceV2
from ..bluetooth import ReadableRegisters
from ..enums import ReverseChargingMode
from ..fields import (
    BoolField,
    EnumField,
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
                f"Charger 2 reads 0x{value:04X} at register {self.address},"
                " which is not a control word this integration knows"
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


# Register 15626 holds the reverse charging (starter battery) settings. The low
# two bits are the switch, the same 01 = on / 10 = off pairs as register 15600.
# The high byte is the selected mode, which the station keeps while reverse
# charging is off. Bits 2-7 read 0x68 in every dump taken so far and are left
# alone.
#
# Dumps from a Charger 2 on 26 Oct 2026: 0x0169 trickle on, 0x0269 pulse on,
# 0x0469 top-up on, 0x016A trickle selected but off, 0x046A top-up selected but
# off. Pulse with the switch off has never been dumped, so this rule predicts
# 0x026A for it. Two mode bits have never been seen set together.
#
# A live read through this library on 31 Oct 2026 answered 15626 cleanly on a
# station that was idle: top-up selected with the switch off, which is the
# layout above, and the mode byte survived the switch being off.
REVERSE_CHARGING_ON = 0b01
REVERSE_CHARGING_OFF = 0b10


def _reverse_charging_state(value: int) -> int | None:
    """The on/off bits of 15626, or None if the word is not a known layout.

    The high byte has to hold a known mode as well: a late answer meant for
    another register (0x5529 from 15600 showed up at 15626 in a test read) ends
    in 0b01, so the low bits alone would read as "on".
    """
    state = value & 0b11
    mode = value >> 8

    if mode not in {m.value for m in ReverseChargingMode}:
        return None
    if state not in (REVERSE_CHARGING_ON, REVERSE_CHARGING_OFF):
        return None

    return state


class Charger2ReverseChargingField(BoolField):
    """Reverse charging on/off, the low two bits of register 15626.

    Read only for now: the register is shared with the mode, so a write has to
    be a read-modify-write, and it starts pushing current into a car's starter
    battery, which a test bench may not be able to absorb. The Bluetti app
    already sets it. BoolField rather than SwitchField so Home Assistant shows
    a read-only state instead of a switch that would always be refused.

    compose() is the write side's bit handling, kept here so that making this a
    control later is a base class change rather than a rewrite. Nothing calls it
    until the field is writeable, and the ctrl_ name is already the one it will
    keep; see the note where these two fields are registered.
    """

    def parse(self, data: bytes) -> bool:
        value = struct.unpack("!H", data)[0]
        state = _reverse_charging_state(value)

        if state == REVERSE_CHARGING_ON:
            return True
        if state == REVERSE_CHARGING_OFF:
            return False

        raise ValueError(
            f"Charger 2 reads 0x{value:04X} at register {self.address},"
            " which is not a reverse charging layout this integration knows"
        )

    def compose(self, current: int, value: bool) -> int | None:
        """Register word that sets this switch and keeps the rest of it."""
        if not isinstance(value, bool):
            return None
        if _reverse_charging_state(current) is None:
            return None

        state = REVERSE_CHARGING_ON if value else REVERSE_CHARGING_OFF
        return (current & ~0b11 & 0xFFFF) | state


class Charger2ReverseChargingModeField(EnumField):
    """Which reverse charging mode is selected, the high byte of register 15626.

    Read only, for the same reason as the switch, and an EnumField rather than a
    SelectField so it is reported as state rather than offered as a select.
    parse() checks the high byte only: a word whose switch bits are unknown is
    still a mode the station has selected, and the switch field reports that
    half of it.
    """

    def __init__(self, name: FieldName, address: int):
        super().__init__(name, address, ReverseChargingMode)

    def parse(self, data: bytes) -> ReverseChargingMode:
        value = struct.unpack("!H", data)[0]

        try:
            return ReverseChargingMode(value >> 8)
        except ValueError:
            raise ValueError(
                f"Charger 2 reads 0x{value:04X} at register {self.address},"
                " whose high byte is not a reverse charging mode"
            ) from None

    def compose(self, current: int, value) -> int | None:
        """Register word that selects this mode and keeps the switch bits."""
        option = self.to_option(value)
        if option is None or _reverse_charging_state(current) is None:
            return None

        return (option.value << 8) | (current & 0x00FF)


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
                # Starter battery side. The raw registers read positive while the
                # vehicle (alternator) charges through the Charger 2 (seen:
                # 0x1760 = 59.84 A, 0x030A = 778 W) and negative while reverse
                # charging feeds the starter battery (seen: 0xFCE0 = -8.00 A,
                # 0xFF9C = -100 W) -- the opposite raw polarity to the other
                # signed Charger 2 registers. Flipped with multiplier=-1 so a
                # given sign means the same direction of flow on both sides of
                # the device, which is what the Home Assistant power flow cards
                # the B_IO_POWER flip (15546) was chosen for rely on.
                SignedDecimalField(
                    FieldName.B_ALT_CURRENT, 15540, 2, multiplier=-1
                ),
                SignedDecimalField(
                    FieldName.B_ALT_IO_POWER, 15542, 0, multiplier=-1
                ),

                Charger2SystemSwitchField(
                    FieldName.CTRL_SYSTEM_ON_OFF,
                    15600,
                ),
                Charger2SilentChargingField(
                    FieldName.CTRL_SILENT_CHARGING,
                    15600,
                ),
                # Reverse charging (starter battery) state. Reported, not
                # written: these two share register 15626, so a write has to be
                # a read-modify-write, and it moves current towards a car's
                # starter battery. The Bluetti app already sets both.
                #
                # They keep the ctrl_ prefix anyway. Reading 15626 is safe and
                # the layout is proven; what is unproven is writing it, so only
                # the writeability is on hold. The prefix is the name these
                # fields will carry once the write side is tested against real
                # hardware, and a consumer that has wired up the entity today
                # would break on a rename then, so the naming convention is
                # settled now rather than revisited later.
                Charger2ReverseChargingField(
                    FieldName.CTRL_REVERSE_CHARGING,
                    15626,
                ),
                Charger2ReverseChargingModeField(
                    FieldName.CTRL_REVERSE_CHARGING_MODE,
                    15626,
                ),
                # A setting, not a measurement: the power of the top-up mode
                # in W (app: Top-up -> Power). Changing it in the app from
                # 100 to 150 W changed 15631 from 0x0064 to 0x0096, so the word
                # is the watt count itself, big-endian, with no scaling. The app
                # allows 100-800 W (0x0064 to 0x0320), which is why the whole
                # 16 bit word is read: above 255 W the high byte is in use.
                # Settings seen on real units so far: 100, 150 and 200 W. Read
                # only until the step the app accepts has been recorded.
                UIntField(FieldName.REVERSE_CHARGING_TOP_UP_POWER, 15631),

                SwapStringField(FieldName.DEVICE_TYPE, 15500, 6),
                SerialNumberField(FieldName.DEVICE_SN, 15506),
            ],
            include_common_fields=False,
        )

    def build_write_command(self, name: str, value):
        # These controls share register 15600. Their writes must be
        # constructed by DeviceWriter using a fresh read-modify-write.
        # Do not construct a standalone command for either field here.
        #
        # The reverse charging fields on 15626 share a register the same way,
        # but they are not writeable, so build_write_command already returns
        # None for them. If they ever become writeable they have to join this
        # tuple (or go through their compose()): a standalone word there would
        # zero the mode byte, or the switch, along with it.
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
