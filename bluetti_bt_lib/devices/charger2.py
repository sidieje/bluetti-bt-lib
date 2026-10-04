from ..base_devices import BaseDeviceV2
from ..fields import FieldName, UIntField, DecimalField, SwapStringField, SerialNumberField, SignedDecimalField, AbsoluteDecimalField


class CHARGER2(BaseDeviceV2):
    def __init__(self):
        super().__init__(
            [
                DecimalField(FieldName.DC_INPUT_VOLTAGE, 15531, 1),
                DecimalField(FieldName.DC_INPUT_CURRENT, 15532, 2),
                UIntField(FieldName.DC_INPUT_POWER, 15534),

                DecimalField(FieldName.DC_2_OUTPUT_VOLTAGE, 15535, 1),
                AbsoluteDecimalField(FieldName.DC_2_OUTPUT_CURRENT, 15536, 2),
                AbsoluteDecimalField(FieldName.DC_2_OUTPUT_POWER_TOTAL, 15538, 0),

                DecimalField(FieldName.B_VOLTAGE, 15543, 1),
                SignedDecimalField(FieldName.B_IO_POWER, 15546, 0),
                UIntField(FieldName.BATTERY_SOC, 15584),

                DecimalField(FieldName.B_ALT_VOLTAGE, 15539, 1),
                DecimalField(FieldName.B_ALT_CURRENT, 15540, 2),
                DecimalField(FieldName.B_ALT_IO_POWER, 15542, 0),

                SwapStringField(FieldName.DEVICE_TYPE, 15500, 6),
                SerialNumberField(FieldName.DEVICE_SN, 15506),
            ],
        )
