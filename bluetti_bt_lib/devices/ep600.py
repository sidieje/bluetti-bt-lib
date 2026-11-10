from ..base_devices import BaseDeviceV2
from ..fields import (
    FieldName,
    UIntField,
    NumberField,
    DecimalField,
    SwapStringField,
    SwitchField,
    BoolField,
)

# Only the disabled pack fields at the end of this file use these. Uncomment
# together with those fields, not before.
# from ..fields import SerialNumberField, VersionField


class EP600(BaseDeviceV2):
    def __init__(self):
        super().__init__(
            [
                DecimalField(FieldName.POWER_GENERATION, 1202, 1),
                UIntField(FieldName.PV_S1_POWER, 1212),
                DecimalField(FieldName.PV_S1_VOLTAGE, 1213, 1),
                DecimalField(FieldName.PV_S1_CURRENT, 1214, 1),
                UIntField(FieldName.PV_S2_POWER, 1220),
                DecimalField(FieldName.PV_S2_VOLTAGE, 1221, 1),
                DecimalField(FieldName.PV_S2_CURRENT, 1222, 1),
                UIntField(FieldName.SM_P1_POWER, 1228),
                DecimalField(FieldName.SM_P1_VOLTAGE, 1229, 1),
                DecimalField(FieldName.SM_P1_CURRENT, 1230, 1),
                UIntField(FieldName.SM_P2_POWER, 1236),
                DecimalField(FieldName.SM_P2_VOLTAGE, 1237, 1),
                DecimalField(FieldName.SM_P2_CURRENT, 1238, 1),
                UIntField(FieldName.SM_P3_POWER, 1244),
                DecimalField(FieldName.SM_P3_VOLTAGE, 1245, 1),
                DecimalField(FieldName.SM_P3_CURRENT, 1246, 1),
                DecimalField(FieldName.GRID_FREQUENCY, 1300, 1),
                UIntField(FieldName.GRID_P1_POWER, 1313),
                DecimalField(FieldName.GRID_P1_VOLTAGE, 1314, 1),
                DecimalField(FieldName.GRID_P1_CURRENT, 1315, 1),
                UIntField(FieldName.GRID_P2_POWER, 1319),
                DecimalField(FieldName.GRID_P2_VOLTAGE, 1320, 1),
                DecimalField(FieldName.GRID_P2_CURRENT, 1321, 1),
                UIntField(FieldName.GRID_P3_POWER, 1325),
                DecimalField(FieldName.GRID_P3_VOLTAGE, 1326, 1),
                DecimalField(FieldName.GRID_P3_CURRENT, 1327, 1),
                DecimalField(FieldName.AC_OUTPUT_FREQUENCY, 1500, 1),
                UIntField(FieldName.AC_P1_POWER, 1510),
                DecimalField(FieldName.AC_P1_VOLTAGE, 1511, 1),
                DecimalField(FieldName.AC_P1_CURRENT, 1512, 1),
                UIntField(FieldName.AC_P2_POWER, 1517),
                DecimalField(FieldName.AC_P2_VOLTAGE, 1518, 1),
                DecimalField(FieldName.AC_P2_CURRENT, 1519, 1),
                UIntField(FieldName.AC_P3_POWER, 1524),
                DecimalField(FieldName.AC_P3_VOLTAGE, 1525, 1),
                DecimalField(FieldName.AC_P3_CURRENT, 1526, 1),
                SwitchField(FieldName.CTRL_AC, 2011),
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
                BoolField(FieldName.CTRL_GENERATOR, 2246),
                DecimalField(FieldName.GRID_VOLT_MIN_VAL, 2435, 1),
                DecimalField(FieldName.GRID_VOLT_MAX_VAL, 2436, 1),
                DecimalField(FieldName.GRID_FREQ_MIN_VALUE, 2437, 2),
                DecimalField(FieldName.GRID_FREQ_MAX_VALUE, 2438, 2),
                SwapStringField(FieldName.WIFI_NAME, 12002, 16),
            ],
            # Battery pack fields: disabled, needs work, untested. Kept
            # commented out rather than deleted so the register map survives
            # for whoever picks the work up. This is deliberate, not leftover
            # from a refactor -- see commit 6bca489 "added ep600 test, disabled
            # v2 pack polling", which turned V2 pack polling off as a unit.
            #
            # Why the fields cannot be read today: walking the pack slots
            # writes a bank selector first, and BaseDeviceV2 has no
            # get_pack_selector() (its override is commented out for the same
            # reason, with the address guessed at 3001 and later 7000 and never
            # confirmed on hardware). Without a selector, max_packs has to stay
            # 0, and BluettiDevice.__init__ then leaves
            # pack_polling_registers empty, so the reader never walks the slots.
            #
            # To resume: confirm the selector register against a real EP600,
            # implement BaseDeviceV2.get_pack_selector() and
            # get_pack_selector_register(), restore the two imports at the top
            # of this file, and uncomment the list below together with
            # max_packs. Then check the values against a station with a pack
            # fitted. None of these addresses has ever been read back from a
            # live EP600, so do not enable this on their strength alone.
            # [
            #     SwapStringField(FieldName.PACK_TYPE, 6101, 6),
            #     SerialNumberField(FieldName.PACK_SN, 6107),
            #     VersionField(FieldName.PACK_VER_BCU, 6175),
            #     VersionField(FieldName.PACK_VER_BMU, 6178),
            #     VersionField(FieldName.PACK_VER_SAFETY_MOD, 6181),
            #     VersionField(FieldName.PACK_VER_HV_MOD, 6184),
            # ],
            # max_packs=2,
        )
