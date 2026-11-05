from decimal import Decimal

from ..enums import EcoMode, DisplayMode, ChargingMode, WorkingModeV2
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


class AP300(BaseDeviceV2):
    # Bench-confirmed on a real AP300. The eight default blocks become seven,
    # because 2005+1 and 2011+10 merge into one 16-register read. The station
    # answered that read 8/8 at a 0.13s median, no slower than the two narrow
    # reads it replaced, and 40/40 cycles answered in full with no retries;
    # the cycle median went 1.28s to 1.10s. A later run over 283 requests saw no
    # repeated frames at all: this station repeats its key exchange frames, not
    # its poll answers.
    max_poll_block = 16
    max_poll_gap = 8

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
                # different values than the V1 UpsMode register (3001).
                SelectField(FieldName.CTRL_UPS_MODE, 2005, WorkingModeV2),
                # Register 2008 is the grid-charge enable on the Elite V2
                # portables. On an Apex 300 it reads 0 while the app shows grid
                # charging enabled, and a write is not reflected in the
                # read-back, so the switch is left off this profile.
                SwitchField(FieldName.CTRL_AC, 2011),
                SwitchField(FieldName.CTRL_DC, 2012),
                # 2013 is the word the official integration writes as
                # SetCtrlPowerOn when its sleep switch is toggled: 4 to enter
                # sleep, 3 to leave it, after arming remoteSet (1) and
                # remoteSetSoc (20). An Elite 200 V2 owner reported that write
                # in the official integration's debug log and traced it to this
                # register. The Elite 100 V2 has no sleep switch in its app, so
                # the Apex 300 was asked instead. It answers 0 while awake and 2
                # in standby, but writing 2 both wakes and sleeps it, and the
                # register reads 0 again either way, so 2013 behaves like a pulse
                # (a power button) rather than a mirror of the state. The word is
                # reported as it reads because neither value is the 0/1 a switch
                # decodes, and the write range covers the two words the station
                # has shown plus the 3 and 4 the official integration uses, so
                # those can be tried from the bench. Which register holds the
                # awake/standby state is still open; tools/standby_probe.py
                # watches the neighbouring registers for it.
                NumberField(FieldName.CTRL_SLEEP_MODE, 2013, 1, 0, 4, 1),
                SwitchField(FieldName.CTRL_ECO_DC, 2014),
                SelectField(FieldName.CTRL_ECO_TIME_MODE_DC, 2015, EcoMode),
                # The app lets the owner set the DC eco auto-off threshold.
                NumberField(FieldName.CTRL_ECO_MIN_POWER_DC, 2016, 1, 5, 20, 1),
                SwitchField(FieldName.CTRL_ECO_AC, 2017),
                SelectField(FieldName.CTRL_ECO_TIME_MODE_AC, 2018, EcoMode),
                # ... and the AC one, which the app ranges at 10-40 W.
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
