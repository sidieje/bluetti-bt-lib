from ..base_devices import BaseDeviceV1
from ..fields import (
    FieldName,
    EnumField,
    DecimalField,
    NumberField,
    SelectField,
    BoolField,
    SwitchField,
    UIntField,
    VersionField,
)
from ..enums import OutputMode, DisplayMode, UpsMode, SplitPhaseMode


class AC500(BaseDeviceV1):
    # The polling fields are the same shape as the AC300's, so the eight
    # default blocks merge to the same four wide ones (36+8, 70+11, 86+11 and
    # 3001+16) plus the orphan 3061 that the AC300 does not have. See the note
    # on AC300 for why; the bench run was on an AC300, so a probe against a
    # real AC500 is still worth having.
    max_poll_block = 16
    max_poll_gap = 8

    def __init__(self):
        super().__init__(
            [
                VersionField(FieldName.VER_ARM, 23),
                VersionField(FieldName.VER_DSP, 25),
                DecimalField(FieldName.POWER_GENERATION, 41, 1),
                EnumField(FieldName.AC_OUTPUT_MODE, 70, OutputMode),
                # Same scaling as the AC300: the tower reports millivolts and
                # centihertz, so no multiplier.
                DecimalField(FieldName.INTERNAL_AC_VOLTAGE, 71, 1),
                DecimalField(FieldName.INTERNAL_AC_FREQUENCY, 74, 2),
                DecimalField(FieldName.AC_INPUT_VOLTAGE, 77, 1),
                DecimalField(FieldName.AC_INPUT_FREQUENCY, 80, 2),
                DecimalField(FieldName.PV_S1_VOLTAGE, 86, 1),
                DecimalField(FieldName.PV_S1_POWER, 87, 1, 10),
                DecimalField(FieldName.PV_S1_CURRENT, 88, 2, 10),
                # How many packs are installed. The reader polls it before the
                # pack walk and uses it to stop early, but it does not say
                # which slots are occupied, so it cannot pick them out. A cycle
                # that is not walking the slots leaves it out of the cycle
                # altogether; see bluetti_device.PACK_COUNT_FIELDS.
                UIntField(FieldName.PACK_NUM, 96),
                SelectField(FieldName.CTRL_UPS_MODE, 3001, UpsMode),
                BoolField(FieldName.CTRL_SPLIT_PHASE, 3004),
                EnumField(FieldName.CTRL_SPLIT_PHASE_MODE, 3005, SplitPhaseMode),
                SwitchField(FieldName.CTRL_AC, 3007),
                SwitchField(FieldName.CTRL_DC, 3008),
                SwitchField(FieldName.CTRL_GRID_CHARGE_ON, 3011),
                SwitchField(FieldName.CTRL_TIME_CONTROL_ON, 3013),
                NumberField(
                    FieldName.BATTERY_SOC_RANGE_START,
                    3015,
                    1,
                    5,
                    100,
                    1,
                ),
                NumberField(
                    FieldName.BATTERY_SOC_RANGE_END,
                    3016,
                    1,
                    5,
                    100,
                    1,
                ),
                SelectField(FieldName.CTRL_DISPLAY_TIMEOUT, 3061, DisplayMode),
            ],
            [
                # Same pack registers as the AC300: pack voltage at 98 and
                # per-pack SOC at 99, read through the write to 3006.
                DecimalField(FieldName.PACK_VOLTAGE, 98, 2),
                UIntField(FieldName.PACK_BATTERY_SOC, 99),
            ],
            # Every slot the tower can hold. The reader walks all of them and
            # drops the ones that answer with nothing, because register 96
            # counts packs without saying which slots they sit in.
            max_packs=6,
        )
