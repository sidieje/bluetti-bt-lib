from ..base_devices import BaseDeviceV1
from ..fields import (
    FieldName,
    EnumField,
    DecimalField,
    SwitchField,
    SelectField,
    UIntField,
    VersionField,
)
from ..enums import OutputMode, DisplayMode


class AC200M(BaseDeviceV1):
    def __init__(self):
        super().__init__(
            [
                VersionField(FieldName.VER_ARM, 23),
                VersionField(FieldName.VER_DSP, 25),
                DecimalField(FieldName.POWER_GENERATION, 41, 1),
                EnumField(FieldName.AC_OUTPUT_MODE, 70, OutputMode),
                DecimalField(FieldName.INTERNAL_AC_VOLTAGE, 71, 1, 10),
                DecimalField(FieldName.INTERNAL_AC_FREQUENCY, 74, 2, 10),
                DecimalField(FieldName.INTERNAL_DC_INPUT_VOLTAGE, 86, 1),
                DecimalField(FieldName.INTERNAL_DC_INPUT_POWER, 87, 1, 10),
                DecimalField(FieldName.INTERNAL_DC_INPUT_CURRENT, 88, 2),
                # How many packs are installed. The reader polls it before the
                # pack walk and uses it to stop early, but it does not say
                # which slots are occupied, so it cannot pick them out. A cycle
                # that is not walking the slots leaves it out of the cycle
                # altogether; see bluetti_device.PACK_COUNT_FIELDS.
                UIntField(FieldName.PACK_NUM, 96),
                SwitchField(FieldName.CTRL_AC, 3007),
                SwitchField(FieldName.CTRL_DC, 3008),
                SwitchField(FieldName.CTRL_POWER_OFF, 3060),
                SelectField(FieldName.CTRL_DISPLAY_TIMEOUT, 3061, DisplayMode),
            ],
            [
                # Same pack registers as the AC300: pack voltage at 98 and
                # per-pack SOC at 99, read through the write to 3006.
                DecimalField(FieldName.PACK_VOLTAGE, 98, 2),
                UIntField(FieldName.PACK_BATTERY_SOC, 99),
            ],
            # Every slot the station can hold: an internal pack plus two
            # expansion packs. The reader walks all three and drops the empty
            # ones.
            max_packs=3,
        )
