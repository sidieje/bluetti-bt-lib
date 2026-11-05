from decimal import Decimal

from ..enums import EcoMode, DisplayMode, ChargingMode, WorkingModeV2
from ..fields import (
    FieldName,
    UIntField,
    NumberField,
    SelectField,
    SwitchField,
    PulseSwitchField,
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
                # Bench-confirmed on an Apex 300 on 5 Nov. Bit 13 is the standby
                # flag: the word reads 0x0800 awake and 0x2800 in standby, and
                # it went back again when standby was switched off. The same
                # word read 0x0802 with the AC inverter on and 0x0806 with AC
                # and DC on, which puts the inverters on bits 1 and 2; the rest
                # of the word is reported as it reads rather than decoded.
                UIntField(FieldName.STANDBY_FLAGS, 124),
                UIntField(FieldName.DC_OUTPUT_POWER, 140),
                UIntField(FieldName.AC_OUTPUT_POWER, 142),
                UIntField(FieldName.DC_INPUT_POWER, 144),
                UIntField(FieldName.AC_INPUT_POWER, 146),
                # The standby switch, on while the station is in standby. The
                # state is read from 174: an Apex 300 answers 0 while it is
                # awake and 8 once it is in standby, and it came back the same
                # way when standby was switched off, in both directions of the
                # changeover sweep on 5 Nov. Bit 3 is the only bit that moved,
                # so it is the only bit read.
                #
                # The write goes to a different register. 2013 is the word the
                # official integration writes as SetCtrlPowerOn when its sleep
                # switch is toggled - 4 to enter sleep and 3 to leave it, after
                # arming two remote-control settings whose registers it never
                # names - which an Elite 200 V2 owner traced in the integration's
                # debug log. The Elite 100 V2 has no sleep switch in its app at
                # all, so the Apex 300 was asked instead: it answers 0 at 2013
                # while awake and 2 in standby, but writing 2 both wakes and
                # sleeps it and the register reads 0 again either way, so 2013 is
                # a pulse (a power button) rather than a mirror of the state.
                # Nothing has ever answered a write to 174.
                #
                # So either way the switch is thrown it sends the same 2, and
                # which way the station moves depends on where it already was.
                # 2013 sits inside the 2005+16 block the profile already reads,
                # so the pulse register costs no read of its own, and 174 costs
                # only the read the standby word used to need on its own.
                #
                # The verification window is wider than the 3.5s default because
                # nothing has measured how fast 174 follows the pulse; a shorter
                # window would report a write that had landed as unconfirmed.
                PulseSwitchField(
                    FieldName.CTRL_SLEEP_MODE,
                    174,
                    pulse_address=2013,
                    pulse_value=2,
                    on_bit=8,
                    verification_delay=10.0,
                ),
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
                # 2013 is the register the standby switch above pulses. It is
                # still read, inside the 2005+16 block the profile asks for
                # anyway, but no field reports it: it answers 0 whichever state
                # the station is in, so there is nothing there to show.
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
