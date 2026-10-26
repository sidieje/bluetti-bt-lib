from decimal import Decimal

from ..enums import EcoMode, DisplayMode, ChargingMode, WorkingModeV2alt
from ..fields import (
    FieldName,
    UIntField,
    NumberField,
    SelectField,
    SwitchField,
    DecimalField,
    VersionField,
    SwapStringField,
)
from ..base_devices import BaseDeviceV2


class EL300(BaseDeviceV2):
    def __init__(self):
        super().__init__(
            [
                DecimalField(FieldName.TIME_REMAINING, 104, 0, 1 / Decimal(60)),
                UIntField(FieldName.DC_OUTPUT_POWER, 140),
                UIntField(FieldName.AC_OUTPUT_POWER, 142),
                UIntField(FieldName.DC_INPUT_POWER, 144),
                UIntField(FieldName.AC_INPUT_POWER, 146),
                DecimalField(FieldName.AC_INPUT_VOLTAGE, 1314, 1),
                DecimalField(FieldName.AC_INPUT_CURRENT, 1315, 1),
                # 1511 mirrors the grid input on this family, the real AC
                # output is 1431.
                DecimalField(FieldName.AC_OUTPUT_VOLTAGE, 1431, 1),
                # UPS working mode: V2 portables keep it at 2005 and use
                # different values than the V1 UpsMode register (3001). The
                # Elite 300 numbers them differently again (Customized UPS
                # reads 3), see WorkingModeV2alt.
                SelectField(FieldName.CTRL_UPS_MODE, 2005, WorkingModeV2alt),
                # UPS grid-charge enable; the AC300 exposes the same function
                # as ctrl_grid_charge_on (register 3011 there).
                SwitchField(FieldName.CTRL_GRID_CHARGE_ON, 2008),
                SwitchField(FieldName.CTRL_AC, 2011),
                SwitchField(FieldName.CTRL_DC, 2012),
                SwitchField(FieldName.CTRL_ECO_DC, 2014),
                SelectField(FieldName.CTRL_ECO_TIME_MODE_DC, 2015, EcoMode),
                # Owners report the same eco ranges as the EL100 V2: 5-20 W DC.
                NumberField(FieldName.CTRL_ECO_MIN_POWER_DC, 2016, 1, 5, 20, 1),
                SwitchField(FieldName.CTRL_ECO_AC, 2017),
                SelectField(FieldName.CTRL_ECO_TIME_MODE_AC, 2018, EcoMode),
                # ... and 10-40 W AC.
                NumberField(FieldName.CTRL_ECO_MIN_POWER_AC, 2019, 1, 10, 40, 1),
                SelectField(FieldName.CTRL_CHARGING_MODE, 2020, ChargingMode),
                SwitchField(FieldName.CTRL_POWER_LIFTING, 2021),
                NumberField(
                    FieldName.BATTERY_SOC_RANGE_START,
                    2022,
                    1,
                    5,
                    100,
                    1,
                ),
                NumberField(
                    FieldName.BATTERY_SOC_RANGE_END,
                    2023,
                    1,
                    5,
                    100,
                    1,
                ),
                SelectField(FieldName.CTRL_DISPLAY_TIMEOUT, 2067, DisplayMode),
                VersionField(FieldName.VER_BMS, 6175),
                SwapStringField(FieldName.WIFI_NAME, 12002, 16),
            ],
        )
