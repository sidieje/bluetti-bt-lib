from ..base_devices import BaseDeviceV2
from ..fields import FieldName, UIntField, DecimalField, BoolFieldNonZero, SwitchFieldNonZero


class AC2P(BaseDeviceV2):
    """Bluetti AC2P device.

    Note: AC2P uses register 2011 for AC output state (not 1509 like AC2A).
    The AC output register returns non-standard boolean values:
    - 1 = ON
    - 3 = OFF (device returns 3, not 0)

    For reading AC output state, we use BoolFieldNonZero which treats only
    value 1 as True. Any other value (including 3) is treated as False.

    For the control switches, we use SwitchFieldNonZero: it writes standard
    boolean values (0/1), which the device accepts, and reads 3 back as off.
    """

    def __init__(self):
        super().__init__(
            [
                # Power readings
                UIntField(FieldName.DC_OUTPUT_POWER, 140),
                UIntField(FieldName.AC_OUTPUT_POWER, 142),
                UIntField(FieldName.DC_INPUT_POWER, 144),
                UIntField(FieldName.AC_INPUT_POWER, 146),
                DecimalField(FieldName.POWER_GENERATION, 154),
                # Status sensors - AC2P uses register 2011 for AC output (not 1509)
                BoolFieldNonZero(FieldName.AC_OUTPUT_ON, 2011),
                BoolFieldNonZero(FieldName.DC_OUTPUT_ON, 2012),
                BoolFieldNonZero(FieldName.POWER_LIFTING_ON, 2021),
                # Control switches (writable)
                SwitchFieldNonZero(FieldName.CTRL_AC, 2011),
                SwitchFieldNonZero(FieldName.CTRL_DC, 2012),
                SwitchFieldNonZero(FieldName.CTRL_POWER_LIFTING, 2021),
            ],
        )
