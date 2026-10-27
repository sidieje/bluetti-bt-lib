# bluetti-bt-lib
Unofficial Library for basic communication to Bluetti powerstations.
Core functions based on https://github.com/warhammerkid/bluetti_mqtt

The current [Roadmap for this project and repositories using this library can be found here](https://github.com/users/Patrick762/projects/4)

Home Assistant Integration [can be found here](https://github.com/sidieje/hassio-bluetti-bt)

## Disclaimer
This library is provided without any warranty or support by Bluetti. I do not take responsibility for any problems it may cause in all cases. Use it at your own risk.

## Projects using this library

- [Home Assistant Integration](https://github.com/Patrick762/hassio-bluetti-bt)
- MQTT Server (planned)
- [UPS Server (NUT compatible)](https://github.com/Patrick762/nut-server-bluetti)

## Supported Powerstations and data

Validated

|Device Name|total_battery_percent|dc_input_power|ac_input_power|dc_output_power|ac_output_power|
|-----------|---------------------|--------------|--------------|---------------|---------------|
|AC70       |✅                   |✅            |✅            |✅             |✅             |
|AC180      |✅                   |✅            |✅            |✅             |✅             |
|EB3A       |✅                   |✅            |✅            |✅             |✅             |
|EP600      |✅                   |PV            |Grid          |❌             |AC Phases      |
|Handsfree 1|✅                   |✅            |✅            |✅             |✅             |

Added and mostly validated by contributors (some are moved here from the HA Integration https://github.com/Patrick762/hassio-bluetti-bt):


|Device Name|Contributor                                                                            |total_battery_percent|dc_input_power |ac_input_power |dc_output_power|ac_output_power|
|-----------|---------------------------------------------------------------------------------------|---------------------|---------------|---------------|---------------|---------------|
|AC2A       |[@ruanmed](https://github.com/ruanmed)                                                 |✅                   |✅             |✅             |✅             |✅             |
|AC50B      |[@goetzc](https://github.com/goetzc), [@Patrick762](https://github.com/Patrick762)     |✅                   |✅             |✅             |✅             |✅             |
|AC60       |[@mzpwr](https://github.com/mzpwr)                                                     |✅                   |✅             |✅             |✅             |✅             |
|AC60P      |[@mzpwr](https://github.com/mzpwr)                                                     |✅                   |✅             |✅             |✅             |✅             |
|AC70P      |[@matthewpucc](https://github.com/matthewpucc)                                         |✅                   |✅             |✅             |✅             |✅             |
|AC180P     |@Patrick762                                                                            |✅                   |✅             |✅             |✅             |✅             |
|AC200L     |bluetti-mqtt                                                                           |✅                   |✅             |✅             |✅             |✅             |
|AC200M     |bluetti-mqtt                                                                           |✅                   |✅             |✅             |✅             |✅             |
|AC200PL    |[@0x4E4448](https://github.com/0x4E4448)                                               |✅                   |✅             |✅             |✅             |✅             |
|AC300      |bluetti-mqtt                                                                           |✅                   |✅             |✅             |✅             |✅             |
|AC500      |bluetti-mqtt                                                                           |✅                   |✅             |✅             |✅             |✅             |
|AP300      |[@seaburger](https://github.com/seaburger), [@sidieje](https://github.com/sidieje)     |✅                   |✅             |✅             |✅             |✅             |
|EL10       |[sidieje](https://github.com/sidieje)                                                  |✅                   |✅             |✅             |✅             |✅             |
|EL30V2     |[@dgudim](https://github.com/dgudim)                                                   |✅                   |✅             |✅             |✅             |✅             |
|EL100mini  |[@MadPB](https://github.com/MadPB)														|✅                   |✅             |✅             |✅             |✅             |
|EL100V2    |[@seaburger](https://github.com/seaburger), [lesolas20](https://github.com/lesolas20)  |✅                   |✅             |✅             |✅             |✅             |
|EL300      |[@suiciety](https://github.com/suiciety)                                               |✅                   |✅             |✅             |✅             |✅             |
|Elite 200 V2|[@nlimper](https://github.com/nlimper)                                                |✅                   |✅             |✅             |✅             |✅             |
|EP500      |bluetti-mqtt                                                                           |✅                   |✅             |✅             |✅             |✅             |
|EP500P     |bluetti-mqtt                                                                           |✅                   |✅             |✅             |✅             |✅             |
|EP760      |[@Apfuntimes](https://github.com/Apfuntimes)                                           |✅                   |PV             |Grid           |❌             |AC Phases      |
|EP800      |[@jhagenk](https://github.com/jhagenk)                                                 |✅                   |❌             |❌             |❌             |❌             |
|Handsfree 2|[@pablogiaccaglia](https://github.com/pablogiaccaglia)                                 |✅                   |✅             |✅             |✅             |✅             |
|PR30V2     |@gentoo90                                                                              |✅                   |✅             |✅             |✅             |✅             |
|PR100V2    |shares PR30V2 register layout (pending validation)                                     |✅                   |✅             |✅             |✅             |✅             |


## Controls

**Validated**

- AC300, AP300, EL100V2, EL10 and Charger 2 — [@sidieje](https://github.com/sidieje)
- AC200L — [@seaburger](https://github.com/seaburger)
- EL30V2 — [@x3ccd4828](https://github.com/x3ccd4828)
- EL300 — [@suiciety](https://github.com/suiciety)
- Handsfree 2 — [@pablogiaccaglia](https://github.com/pablogiaccaglia)

|Device Name|ctrl_ac|ctrl_dc|ctrl_ups_mode|soc_range_start|soc_range_end|ctrl_eco_min_power|
|-----------|-------|-------|-------------|---------------|---------------|------------------|
|AC200L     |✅     |✅     |✅           |✅             |✅             |❌                |
|AC200M     |✅     |✅     |❌           |❌             |❌             |❌                |
|AC200PL    |✅     |✅     |❌           |❌             |❌             |❌                |
|AC300      |✅     |✅     |✅           |✅             |✅             |❌                |
|AC500      |✅     |✅     |✅           |✅             |✅             |❌                |
|AP300      |✅     |✅     |✅           |✅             |✅             |✅                |
|EB3A       |✅     |✅     |❌           |❌             |❌             |❌                |
|EL10       |✅     |✅     |❌           |❌             |❌             |✅                |
|EL100mini  |✅     |✅     |✅           |✅             |✅             |✅                |
|EL100V2    |✅     |✅     |✅           |✅             |✅             |✅                |
|Elite 200 V2|✅    |✅     |✅           |✅             |✅             |✅                |
|EL30V2     |✅     |✅     |✅           |✅             |✅             |✅                |
|EL300      |✅     |✅     |✅           |✅             |✅             |✅                |
|EP500      |✅     |✅     |✅           |✅             |✅             |❌                |
|EP500P     |✅     |✅     |✅           |✅             |✅             |❌                |
|EP600      |✅     |❌     |❌           |✅             |✅             |❌                |
|Handsfree 1|✅     |✅     |❌           |❌             |❌             |❌                |
|Handsfree 2|✅     |✅     |❌           |❌             |❌             |❌                |

The AC200M, AC200PL, EP500 and EP500P rows come from their register maps, but their controls have not been tested through this library yet.

The eco auto-off thresholds (registers 2016 and 2019) are readable on most
Elite portables, but they are only writable where an owner has confirmed the
Bluetti app offers them, and the ranges differ per model: 5-20 W on DC and
10-40 W on AC for the AP300, EL100 mini, EL100 V2, EL30 V2, EL200 V2 and
EL300, and 5-10 W / 15-30 W on the EL10.

The Charger 2 carries a different set of switches, so it gets its own table:

|Device Name|ctrl_system_on_off|ctrl_silent_charging|
|-----------|------------------|---------------------|
|Charger 2  |✅                |✅                   |

## Installation

```bash
pip install bluetti-bt-lib
```

## Commands for testing

Commands included in this library should only be used for testing.

### Scan for supported devices

```bash
usage: bluetti-scan [-h] [-r REGEX] [-s SCAN_TIME]

Detect bluetti devices by bluetooth name

options:
  -h, --help            show this help message and exit
  -r REGEX, --regex REGEX
                        Custom regex to match device name
  -s SCAN_TIME, --scan-time SCAN_TIME
                        How long to scan for devices (seconds)
```

Example output: `['EB3A', '00:00:00:00:00:00']`

### Detect device type by mac address

```bash
usage: bluetti-detect [-h] mac

Detect bluetti devices

positional arguments:
  mac         Mac-address of the powerstation

options:
  -h, --help  show this help message and exit
```

Example:

```bash
bluetti-detect 00:00:00:00:00:00
```

Example output: `Device type is 'EB3A' with iot version 1 and serial 0000000000000. Full name: EB3A0000000000000`

### Read device data for supported devices

```bash
usage: bluetti-read [-h] [-m MAC] [-t TYPE] [-e ENCRYPTION]

Detect bluetti devices

options:
  -h, --help            show this help message and exit
  -m MAC, --mac MAC     Mac-address of the powerstation
  -t TYPE, --type TYPE  Type of the powerstation (AC70 f.ex.)
  -e ENCRYPTION, --encryption ENCRYPTION
                        Add this if encryption is needed
```

Example:

```bash
bluetti-read -m 00:00:00:00:00:00 -t EB3A
```

Example output:
```bash
FieldName.DEVICE_TYPE: EB3A
FieldName.DEVICE_SN: 0000000000000
FieldName.BATTERY_SOC: 92%
FieldName.DC_INPUT_POWER: 0W
FieldName.AC_INPUT_POWER: 0W
FieldName.AC_OUTPUT_POWER: 0W
FieldName.DC_OUTPUT_POWER: 0W
FieldName.CTRL_AC: False
FieldName.CTRL_DC: True
FieldName.CTRL_LED_MODE: LedMode.OFF
FieldName.CTRL_POWER_OFF: False
FieldName.CTRL_ECO: False
FieldName.CTRL_ECO_TIME_MODE: EcoMode.HOURS1
FieldName.CTRL_CHARGING_MODE: ChargingMode.STANDARD
FieldName.CTRL_POWER_LIFTING: False
```

### Write to supported device

INFO: Devices with encryption are currently not supported!

```bash
usage: bluetti-write [-h] [-m MAC] [-t TYPE] [--on ON] [--off OFF] [-v VALUE] [-e ENCRYPTION] field

Write to bluetti device

positional arguments:
  field                 Field name (ctrl_dc f.ex.)

options:
  -h, --help            show this help message and exit
  -m MAC, --mac MAC     Mac-address of the powerstation
  -t TYPE, --type TYPE  Type of the powerstation (AC70 f.ex.)
  --on ON               Value to write
  --off OFF             Value to write
  -v VALUE, --value VALUE
                        Value to write (integer, see enum for value)
  -e ENCRYPTION, --encryption ENCRYPTION
                        Add this if encryption is needed
```

Example:

```bash
bluetti-write -m 00:00:00:00:00:00 -t EB3A --on on ctrl_ac
```

### Probe a station that keeps dropping reads

`tools/link_health_probe.py` is for a station that answers some polls and not
others. It runs the same read cycles Home Assistant runs, and reports for every
cycle that was cut short which request went unanswered, how many answered before
it, and what the station sent about the request it skipped:

```bash
usage: tools/link_health_probe.py [-h] -m MAC -t TYPE [--enc] [-n ROUNDS]
                                  [--gap GAP] [--packs-every PACKS_EVERY]
                                  [--blocks] [--per-block PER_BLOCK] [-v]
```

The reader sorts a missing answer into one of three kinds, which is the part that
tells the causes apart:

| kind | what happened | usually means |
| --- | --- | --- |
| `silent` | nothing at all came back | the station is busy, or has quietly dropped the link |
| `partial` | it started answering and the rest never arrived | a frame lost on the way up |
| `mismatch` | a whole frame came back that answers a different request | a late answer to a request that was already given up on |

`--blocks` asks for each poll block on its own instead: a block that answers fine
alone but fails in the middle of a cycle is a timing problem, a block that fails
both ways is that register. `--packs-every N` walks the pack slots every Nth
round, which is how a device with battery packs is tested with and without them.

A read that goes unanswered is asked for once more before the cycle is given up
on. Writes are never sent twice. A station that was only slow then answers both
asks: the first answer settles the read, and the second is still on its way. A
function 03 answer does not say which register it is for, so the reader waits up
to `STRAY_ANSWER_SECONDS` for that second answer and throws it away before it
asks anything else; otherwise the next read of the same size would take it for
its own. A cycle the retry saved is not logged as a warning at all, and the
probe prints how many requests had to be asked twice, how many of those answered
the second time, how often the reader waited for a second answer (`drained`) and
how often one that differed from the first arrived and was thrown away
(`discarded`).

## Adding fields

To add new fields, you can use the `bluetti-detect` command to first find out which version of iot protocol is used and if it uses encryption.

After you got this information, you can use the `bluetti-readall` command to read every registry and save the data to a json file and a hexdump (10 bytes per line). You should also note all values you see in the app to later compare the data.

Here's how to use the `bluetti-readall` command:

```bash
usage: bluetti-readall [-h] [-m MAC] [-v VERSION] [-e ENCRYPTION]

Detect bluetti devices

options:
  -h, --help            show this help message and exit
  -m MAC, --mac MAC     Mac-address of the powerstation
  -v VERSION, --version VERSION
                        IoT protocol version
  -e ENCRYPTION, --encryption ENCRYPTION
                        Add this if encryption is needed
```

With the separate tool at [bluetti-bt-raw-reader](https://github.com/Patrick762/bluetti-bt-raw-reader) you can view those values in a more understandable way.

You can also share the output with me using [this form](https://forms.gle/ewp7DYigtaN3ZLc68)


To test added fields with the created json file, use `bluetti-parse`:

```bash
usage: bluetti-parse [-h] file

Parse readall output files

positional arguments:
  file        JSON file of the powerstation readall output

options:
  -h, --help  show this help message and exit
```
