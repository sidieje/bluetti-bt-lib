from ..base_devices import BaseDeviceV1
from ..fields import (
    FieldName,
    EnumField,
    DecimalField,
    UIntField,
    NumberField,
    SelectField,
    BoolField,
    SwitchField,
    VersionField,
)
from ..enums import OutputMode, UpsMode, SplitPhaseMode


class AC300(BaseDeviceV1):
    # This station is the noisiest link of the ones we have logs for, and every
    # round trip is a chance to slip a byte, so it is worth merging harder than
    # the default: seven requests per cycle become four (36+8, 70+11, 86+11 and
    # 3001+16).
    #
    # It also changes the shape of the answers. Three of the seven blocks the
    # default limits produce ask for eight registers, which is the one size
    # whose answer is 21 bytes long, and both of the aborts we have logs of were
    # 21-byte answers that arrived one byte out of frame. The new layout leaves
    # only one block that size. That is an observed correlation rather than a
    # proven mechanism, so treat it as something to check against the link
    # counters, not a fix.
    #
    # The wider blocks also read twenty registers the old layout left out:
    # 72-73, 75-76, 78-79, 89-95, 3002-3003, 3006, 3009-3010, 3012 and 3014.
    # They are the gaps between registers this station already answers, and
    # 3006 is the pack bank selector the walk already writes, but a station
    # that refuses an unmapped register answers the whole block with an
    # exception, which would cost every field in it.
    #
    # Bench-checked on an AC300: all four blocks answered eight times out of
    # eight, slowest 0.25s, and 40 poll cycles ran clean at a median 0.80s
    # against 1.40s for the seven-request layout. The retry rate was the same
    # in both runs, 3 in 40, so this is a cheaper cycle rather than a
    # better-behaved link.
    max_poll_block = 16
    max_poll_gap = 8

    def __init__(self):
        super().__init__(
            [
                VersionField(FieldName.VER_ARM, 23),
                VersionField(FieldName.VER_DSP, 25),
                DecimalField(FieldName.POWER_GENERATION, 41, 1),
                EnumField(FieldName.AC_OUTPUT_MODE, 70, OutputMode),
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
                # SelectField(FieldName.CTRL_DISPLAY_TIMEOUT, 3061, DisplayMode),
            ],
            [
                # Per-pack readings, each selected by the write to 3006
                # (BaseDeviceV1.get_pack_selector). The read-only PACK_SELECTED
                # and the 16 per-pack cell voltages stay off the profile: the
                # selector write is independent of these fields, and sixteen
                # entities per pack is more noise than it is worth.
                DecimalField(FieldName.PACK_VOLTAGE, 98, 2),
                UIntField(FieldName.PACK_BATTERY_SOC, 99),
            ],
            # Every slot the tower can hold. The reader walks all of them and
            # drops the ones that answer with nothing, because register 96
            # counts packs without saying which slots they sit in.
            max_packs=4,
        )
