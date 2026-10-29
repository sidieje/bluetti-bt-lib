from decimal import Decimal

from ..base_devices import BaseDeviceV2
from ..enums import ChargingMode, EcoMode, WorkingModeV2
from ..fields import (
    FieldName,
    UIntField,
    SignedDecimalField,
    DecimalField,
    SwitchField,
    SelectField,
    SwapStringField,
    VersionField,
)


class FP(BaseDeviceV2):
    """FridgePower"""

    def __init__(self):
        super().__init__(
            [
                DecimalField(FieldName.TIME_REMAINING, 104, 0, 1 / Decimal(60)),
                UIntField(FieldName.DC_OUTPUT_POWER, 140),
                UIntField(FieldName.AC_OUTPUT_POWER, 142),
                UIntField(FieldName.DC_INPUT_POWER, 144),
                # Signed, and negative while the unit draws from the grid, so
                # the sign is flipped to report the power flowing in.
                SignedDecimalField(FieldName.AC_INPUT_POWER, 146, 0, multiplier=-1),
                VersionField(FieldName.VER_ARM, 1114),
                VersionField(FieldName.VER_DSP, 1117),
                DecimalField(FieldName.DC_INPUT_VOLTAGE, 1213, 1),
                DecimalField(FieldName.DC_INPUT_CURRENT, 1214, 1),
                DecimalField(FieldName.AC_INPUT_FREQUENCY, 1300, 1),
                DecimalField(FieldName.AC_INPUT_VOLTAGE, 1314, 1),
                # Signed like the input power; the 0.1 A step stays in the scale.
                SignedDecimalField(FieldName.AC_INPUT_CURRENT, 1315, 1, multiplier=-1),
                DecimalField(FieldName.AC_OUTPUT_FREQUENCY, 1500, 1),
                # Unlike the Elite V2 portables, 1511 is the AC output on this
                # unit rather than a mirror of the grid input.
                DecimalField(FieldName.AC_OUTPUT_VOLTAGE, 1511, 1),
                # Register 2005 takes the same values as the other V2
                # portables, not the V1 UpsMode spelling used at 3001.
                SelectField(FieldName.CTRL_UPS_MODE, 2005, WorkingModeV2),
                SwitchField(FieldName.CTRL_AC, 2011),
                SwitchField(FieldName.CTRL_DC, 2012),
                SwitchField(FieldName.CTRL_ECO_DC, 2014),
                SelectField(FieldName.CTRL_ECO_TIME_MODE_DC, 2015, EcoMode),
                # Readable, but the app's accepted ranges are unconfirmed on
                # this unit, so they stay out of the writable set.
                UIntField(FieldName.CTRL_ECO_MIN_POWER_DC, 2016),
                SwitchField(FieldName.CTRL_ECO_AC, 2017),
                SelectField(FieldName.CTRL_ECO_TIME_MODE_AC, 2018, EcoMode),
                UIntField(FieldName.CTRL_ECO_MIN_POWER_AC, 2019),
                SelectField(FieldName.CTRL_CHARGING_MODE, 2020, ChargingMode),
                SwitchField(FieldName.CTRL_POWER_LIFTING, 2021),
                UIntField(FieldName.BATTERY_SOC_RANGE_START, 2022),
                UIntField(FieldName.BATTERY_SOC_RANGE_END, 2023),
                # The app's "time of use" switch; the AC300 and AC500 expose the
                # same function at 3013.
                SwitchField(FieldName.CTRL_TIME_CONTROL_ON, 2029),
                # Called "Grid Self-Adaption" in the app.
                SwitchField(FieldName.CTRL_GRID_STABILIZING, 2225),
                VersionField(FieldName.VER_BMS, 6175),
                SwapStringField(FieldName.WIFI_NAME, 12002, 16),
            ],
        )
