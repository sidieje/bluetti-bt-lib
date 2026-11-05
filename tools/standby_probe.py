"""Find the register that says whether a station is awake or in standby.

Register 2013 answers 0 both after a wake pulse and after a sleep pulse, so it
looks like a command register that clears itself (a power button) rather than a
mirror of the state. A standby switch still needs somewhere to read the state
from, so this probe watches registers and reports only the words that move. At
the end it lists the registers that held more than one value, which are the
candidates for a real state register.

Run it with the pipx venv python:
  ~/.local/share/pipx/venvs/bluetti-bt-lib-sidieje/bin/python standby_probe.py \
      -m <MAC> -t AP300 -e -n 20 --gap 60

By default it watches the blocks the profile already polls, and it closes the
link between samples (--link duty). That matters: on 5 Nov an AP300 held 2013
at 0 for a whole run of a probe that kept one connection open and sent 24 reads
per sample. A station read that continuously is being kept awake by the poll,
so it never cycles into standby and there is no state change to find.

Widen the sweep with one --range per block:
  --range 2000-2100 --range 100-199 --range 1300-1450

Keep any sweep small. A block read costs this station about 2.5s, so 24 reads
per sample is a minute of continuous traffic, and a station asked that much
starts dropping requests. A dropped request is also how a register comes to
hold the value of a different register: a function 03 answer carries no
address, so the byte count is the only thing tying an answer to the request
that asked for it, and a late answer of the same size is indistinguishable from
the answer that was wanted. The 5 Nov run showed a whole block of values slide
by exactly 16 registers between two samples. Two defences are built in here:
consecutive reads get different quantities, and a sample whose cycle was
aborted is reported but never diffed or recorded.

Send the pulse from the same connection instead of a second terminal:
  --pulse-at 10 --pulse-value 2
That writes ctrl_sleep_mode before sample 10, so the write and the words that
follow it sit on one timeline. A station only answers one BLE connection at a
time, so a bluetti-write from another terminal would have to wait for this run
to stop. In --link duty mode the pulse rides the sample's own connection.

A run only shows something if the station changed state inside it. If it stays
awake the whole time, every sample reads the same and the run says "nothing
moved", which is a null result rather than a broken probe. So change the state
yourself and let the probe straddle it:
  --switch
That takes two samples, stops, tells you to switch standby on in the app, waits
for enter, takes two more, and then prints the registers whose values never
overlap between the two halves. Those are the ones that track the state. Use -n
for a different number of samples per side, --switch-text if you are switching
standby off instead, and --dump FILE to keep the samples for a later
--compare FILE_A FILE_B or --group FILE.
"""

import argparse
import asyncio
import json
import logging
import struct
import time
from collections import Counter

from bluetti_bt_lib.base_devices.bluetti_device import BluettiDevice
from bluetti_bt_lib.bluetooth import DeviceWriter, DeviceWriterConfig
from bluetti_bt_lib.bluetooth.device_reader import DeviceReader, DeviceReaderConfig
from bluetti_bt_lib.enums.eco_mode import EcoMode
from bluetti_bt_lib.registers.ReadableRegisters import ReadableRegisters
from bluetti_bt_lib.utils.device_builder import build_device

# Bump when the script changes: raw.githubusercontent.com can hand back a cached
# copy, and the header line is the only way to tell which copy actually ran.
PROBE_REVISION = "2026-11-05.3"

# The narrowest read worth issuing when walking the chunk widths down.
MIN_CHUNK = 8


def parse_range(text: str) -> tuple[int, int]:
    """Accept "2000-2100", "2000:2100" or a single register such as "2013"."""
    parts = [part for part in text.replace(":", "-").split("-") if part]
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        raise argparse.ArgumentTypeError("bad register range: " + repr(text))
    if not 1 <= len(numbers) <= 2:
        raise argparse.ArgumentTypeError("bad register range: " + repr(text))
    start, end = numbers[0], numbers[-1]
    if start > end:
        start, end = end, start
    if start < 0 or end > 65535:
        raise argparse.ArgumentTypeError(
            "registers must be inside 0-65535: " + repr(text)
        )
    return start, end


def parse_watch(text: str) -> list[int]:
    return [int(part) for part in text.split(",") if part]


def chunk_requests(ranges: list[tuple[int, int]], size: int) -> list[ReadableRegisters]:
    """Cut the watched ranges into reads no wider than the station answers.

    The widths walk down from the maximum instead of repeating it, because two
    reads of the same width are interchangeable as far as the station's answers
    are concerned: a function 03 answer identifies itself only by its byte count
    (ReadableRegisters.is_valid_response), so an answer that arrives late for one
    read settles the next read of the same width. Making the widths differ
    between neighbours is what keeps a late answer from being filed under a
    register nobody asked about.
    """
    requests: list[ReadableRegisters] = []
    width = size
    for start, end in ranges:
        address = start
        while address <= end:
            quantity = min(width, end - address + 1)
            requests.append(ReadableRegisters(address, quantity))
            address += quantity
            width = size if width <= MIN_CHUNK else width - 1
    return requests


def block_words(body: bytes) -> list[int]:
    """Split one block answer into its 16-bit words."""
    count = len(body) // 2
    return list(struct.unpack("!" + str(count) + "H", body))


def show(value: int) -> str:
    return str(value) + " (0x" + format(value, "04X") + ")"


def write_dump(path: str, meta: dict, samples: list[dict]) -> None:
    """Save the run so it can be compared against another run later.

    Rewritten after every sample, so a run that is stopped early still leaves
    whatever it had collected.
    """
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"meta": meta, "samples": samples}, handle, indent=1, sort_keys=True)


def read_dump(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def seen_values(dump: dict) -> dict[int, set[int]]:
    """Every value each register answered across a saved run."""
    seen: dict[int, set[int]] = {}
    for sample in dump.get("samples", []):
        for address, value in sample.get("values", {}).items():
            seen.setdefault(int(address), set()).add(int(value))
    return seen


def label(dump: dict) -> str:
    meta = dump.get("meta", {})
    parts = [str(meta.get("note", "")) or "unlabelled", str(meta.get("device", "?"))]
    if meta.get("link"):
        parts.append("link=" + str(meta["link"]))
    if meta.get("started"):
        parts.append(str(meta["started"]))
    return " ".join(parts)


def compare_dumps(path_a: str, path_b: str) -> None:
    """Print the registers that answered differently in two saved runs."""
    first = read_dump(path_a)
    second = read_dump(path_b)
    values_a = seen_values(first)
    values_b = seen_values(second)

    for path, dump, values in ((path_a, first, values_a), (path_b, second, values_b)):
        print(
            path
            + ": "
            + label(dump)
            + ", "
            + str(len(dump.get("samples", [])))
            + " samples, "
            + str(len(values))
            + " registers"
        )

    shared = sorted(set(values_a) & set(values_b))
    if not shared:
        print("the two runs watched nothing in common, so there is nothing to compare")
        return

    differs = [address for address in shared if values_a[address] != values_b[address]]
    print(str(len(shared)) + " registers were watched by both runs")
    if not differs:
        print(
            "none of them answered differently: the two runs looked the same to "
            "this station"
        )
    for address in differs:
        print(
            "  "
            + format(address, ">5")
            + "  "
            + (str(first.get("meta", {}).get("note", "")) or "A")
            + " "
            + ", ".join(str(value) for value in sorted(values_a[address]))
            + "   "
            + (str(second.get("meta", {}).get("note", "")) or "B")
            + " "
            + ", ".join(str(value) for value in sorted(values_b[address]))
        )

    one_sided = sorted(set(values_a) ^ set(values_b))
    if one_sided:
        print(
            str(len(one_sided))
            + " register(s) only answered in one run (a block that went silent): "
            + ", ".join(str(address) for address in one_sided)
        )


def dump_meta(args, requests: list[ReadableRegisters]) -> dict:
    return {
        "probe": PROBE_REVISION,
        "device": args.type,
        "mac": args.mac,
        "link": args.link,
        "note": args.note,
        "gap": args.gap,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "blocks": [
            [request.starting_address, request.quantity] for request in requests
        ],
    }


# The AP300 keeps its output eco setup in the block it already polls. It is
# printed as context only: eco is the load threshold that switches an output off,
# and nothing here proves it is what puts the station into standby.
SLEEP_MODE = 2013
ECO_SWITCH = (("AC", 2017), ("DC", 2014))
ECO_TIME = {"AC": 2018, "DC": 2015}
ECO_MIN_POWER = {"AC": 2019, "DC": 2016}


def eco_name(value: int) -> str:
    if value in [member.value for member in EcoMode]:
        return str(value) + "h"
    return "no timer"


def standby_report(current: dict[int, int], args) -> None:
    """Print the sleep and eco words, so the setup is visible without a guess."""
    if SLEEP_MODE in current:
        print("  ctrl_sleep_mode (2013) reads " + show(current[SLEEP_MODE]))
    for port, switch_address in ECO_SWITCH:
        switch = current.get(switch_address)
        if switch is None:
            continue
        timer = current.get(ECO_TIME[port]) or 0
        power = current.get(ECO_MIN_POWER[port])
        print(
            "  "
            + port
            + " eco "
            + ("on" if switch else "off")
            + ": "
            + eco_name(timer)
            + " below "
            + str(power)
            + "W"
        )
    print(
        "  eco is the output load threshold, not a proven standby timer: the only"
        " standby change known to work on this station is writing 2013"
    )
    if args.pulse_at:
        print(
            "  a ctrl_sleep_mode="
            + str(args.pulse_value)
            + " pulse goes out before sample "
            + str(args.pulse_at)
            + ", which forces that change"
        )
    else:
        print(
            "  nothing is forced in this run, so it only shows something if the "
            "station changes state on its own; use --pulse-at, or --ask to label "
            "each sample with what you can see"
        )


async def ask_state() -> str:
    """Ask the operator what the station is doing right now."""
    answer = (
        (
            await asyncio.to_thread(
                input,
                "  what does the station look like? [a]wake/[s]tandby/enter=skip: ",
            )
        )
        .strip()
        .lower()
    )
    if answer.startswith("s"):
        return "standby"
    if answer.startswith("a"):
        return "awake"
    return ""


def group_summary(dump: dict) -> None:
    """Find the registers that track the label the operator gave each sample.

    This is the question the whole probe exists for: given samples labelled by
    what the station looked like, which registers say the same thing?
    """
    labels: dict[str, dict[int, set[int]]] = {}
    for sample in dump.get("samples", []):
        name = str(sample.get("observed", "")) or "unlabelled"
        per = labels.setdefault(name, {})
        for address, value in sample.get("values", {}).items():
            per.setdefault(int(address), set()).add(int(value))

    print(label(dump) + ": " + str(len(dump.get("samples", []))) + " samples")
    named = sorted(labels)
    print(
        "  "
        + ", ".join(
            name + ": " + str(len(labels[name])) + " registers" for name in named
        )
    )
    if len(named) < 2:
        print("  label samples with --ask to split them by what you saw")
        return

    common = set.intersection(*[set(per) for per in labels.values()])
    perfect: list[int] = []
    partly: list[int] = []
    for address in sorted(common):
        sets = [labels[name][address] for name in named]
        pairs = [
            (sets[i], sets[j])
            for i in range(len(sets))
            for j in range(i + 1, len(sets))
        ]
        if all(not (first & second) for first, second in pairs):
            perfect.append(address)
        elif any(first != second for first, second in pairs):
            partly.append(address)

    if perfect:
        print(
            "  "
            + str(len(perfect))
            + " register(s) never overlap between the states, so they track it:"
        )
        for address in perfect:
            print(
                "    "
                + format(address, ">5")
                + "  "
                + "  ".join(
                    name
                    + "="
                    + "/".join(str(value) for value in sorted(labels[name][address]))
                    for name in named
                )
            )
    else:
        print(
            "  no register separated the labelled states: the state is not in "
            "these words, or the labels do not match what the station did"
        )
    if partly:
        print(
            "  "
            + str(len(partly))
            + " register(s) differed but overlapped (counters, or a lag): "
            + ", ".join(str(address) for address in partly)
        )


def group_report(path: str) -> None:
    group_summary(read_dump(path))


async def switch_prompt(args) -> None:
    """Hold the run still while the operator changes the station by hand."""
    print("")
    print("  >>> " + args.switch_text)
    await asyncio.to_thread(input, "  press enter once it has settled: ")
    if args.settle:
        await asyncio.sleep(args.settle)


async def read_once(
    reader: DeviceReader, requests: list[ReadableRegisters]
) -> tuple[dict[int, int], int, str]:
    """Read every watched block as one cycle: words found, blocks silent, abort.

    One read() call means one cycle. The reader stops the cycle at the first
    request that goes unanswered, which is what keeps a late answer away from
    the next request; asking for each block in its own read() would start a fresh
    cycle every time and let the previous cycle's late answer settle it.
    """
    data = await reader.read(only_registers=requests, raw=True)
    found: dict[int, int] = {}
    if data:
        for base, body in data.items():
            for offset, value in enumerate(block_words(body)):
                found[base + offset] = value
    silent = len(requests) - len(data or {})
    return found, max(silent, 0), reader.link_stats.aborted


async def pulse(reader: DeviceReader, device: BluettiDevice, value: int) -> None:
    """Write ctrl_sleep_mode over the connection the reader already holds."""
    if reader.client is None or not reader.client.is_connected:
        # The link belongs to the reader, and a station that slept may have
        # dropped it. Writing over a dead client would only raise inside bleak.
        print("  pulse: skipped, no live connection to write over")
        return

    writer = DeviceWriter(
        reader.client,
        device,
        DeviceWriterConfig(use_encryption=reader.config.use_encryption),
        lock=reader.polling_lock,
        reader=reader,
    )
    # The reader owns the notifier and the encryption session, so the writer has
    # to borrow them rather than start a second pair on the same characteristic.
    writer.attach(reader.encryption)
    written = await writer.write("ctrl_sleep_mode", value)
    print(
        "  pulse: ctrl_sleep_mode="
        + str(value)
        + " -> "
        + ("accepted" if written else "REJECTED")
    )


async def run(
    reader: DeviceReader,
    device: BluettiDevice,
    requests: list[ReadableRegisters],
    args,
) -> None:
    previous: dict[int, int] = {}
    values: dict[int, set[int]] = {}
    change_count: Counter[int] = Counter()
    trusted_count = 0
    empty = 0
    cut = 0
    reported = False
    snapshots: list[dict] = []
    meta = dump_meta(args, requests)

    # --switch runs the same number of samples on each side of a manual change,
    # so the two halves can be compared against each other at the end.
    phases = ["before", "after"] if args.switch else ["before"]
    total = args.samples * len(phases)

    for index in range(1, total + 1):
        phase = phases[(index - 1) // args.samples]
        if args.switch and index > 1 and (index - 1) % args.samples == 0:
            await switch_prompt(args)

        stamp = (
            "["
            + format(index, ">3")
            + "] "
            + time.strftime("%H:%M:%S")
            + ("  " + phase if args.switch else "")
        )

        # Ask before touching the link: connecting can itself wake a station, so
        # the label has to describe what the operator saw beforehand.
        if args.switch:
            observed = phase
        elif args.ask:
            observed = await ask_state()
        else:
            observed = ""

        connected = True
        if args.link == "duty":
            # Open the link, look, close it again. The station spends the gap
            # between samples undisturbed, which is the only way it gets a
            # chance to fall into standby on its own.
            async with reader.polling_lock:
                connected = await reader.connect()
        if not connected:
            empty += 1
            print(stamp + "  could not connect")
            if index < total:
                await asyncio.sleep(args.gap)
            continue

        if args.pulse_at == index:
            await pulse(reader, device, args.pulse_value)

        started = time.monotonic()
        current, silent, aborted = await read_once(reader, requests)
        took = time.monotonic() - started

        if args.link == "duty":
            await reader.disconnect()

        # A cycle that was cut short may have filed a late answer under the
        # wrong address, so its words are not comparable with anything.
        trusted = bool(current) and not aborted
        if not current:
            empty += 1
        elif trusted:
            trusted_count += 1
        else:
            cut += 1

        moved: list[int] = []
        if trusted:
            for address, value in sorted(current.items()):
                values.setdefault(address, set()).add(value)
                if address in previous and previous[address] != value:
                    change_count[address] += 1
                    moved.append(address)

        watch = "  ".join(
            str(address) + "=" + str(current[address])
            for address in args.watch
            if address in current
        )
        print(
            stamp
            + "  "
            + format(took, "5.2f")
            + "s"
            + "  "
            + format(len(current), ">4")
            + " words"
            + ("  " + str(silent) + " blocks silent" if silent else "")
            + ("  seen: " + observed if observed else "")
            + ("  watch: " + watch if watch else "")
        )
        if aborted:
            print(
                "        cycle aborted at " + aborted + ": this sample is not compared"
            )
        for address in moved:
            print(
                "        "
                + str(address)
                + " "
                + show(previous[address])
                + " -> "
                + show(current[address])
            )
        if not current:
            print("        nothing answered this sample")

        if trusted:
            if not reported:
                reported = True
                standby_report(current, args)
            snapshots.append(
                {
                    "index": index,
                    "time": time.strftime("%H:%M:%S"),
                    "took": round(took, 2),
                    "observed": observed,
                    "values": {
                        str(address): value
                        for address, value in sorted(current.items())
                    },
                }
            )
            if args.dump:
                write_dump(args.dump, meta, snapshots)
            previous = current
        if index < total:
            await asyncio.sleep(args.gap)

    print("")
    print(str(trusted_count) + "/" + str(total) + " samples compared")
    if empty:
        print(str(empty) + " samples got nothing at all")
    if cut:
        print(str(cut) + " samples were cut short mid-cycle and left out")
    if args.dump:
        print(
            "saved "
            + str(len(snapshots))
            + " samples to "
            + args.dump
            + " (compare with --compare)"
        )
    if args.switch:
        print("")
        group_summary({"meta": meta, "samples": snapshots})
        print("")

    candidates = sorted(a for a, seen in values.items() if len(seen) > 1)
    if not candidates:
        print("nothing moved: every watched register answered the same value")
        return

    print(str(len(candidates)) + " register(s) changed during the run:")
    for address in candidates:
        seen = sorted(values[address])
        print(
            "  "
            + format(address, ">5")
            + "  values "
            + ", ".join(str(value) for value in seen)
            + "  changed "
            + str(change_count[address])
            + "x"
        )
    print(
        str(len(values) - len(candidates))
        + " register(s) answered the same value every sample"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="watch which registers move when a ctrl_sleep_mode pulse goes out"
        " [probe " + PROBE_REVISION + "]",
    )
    parser.add_argument("-m", "--mac")
    parser.add_argument("-t", "--type")
    parser.add_argument(
        "-e", "--enc", action="store_true", help="station needs encryption"
    )
    parser.add_argument(
        "--range",
        dest="ranges",
        type=parse_range,
        action="append",
        help="block to watch, start-end (repeatable; default: the blocks this profile polls)",
    )
    parser.add_argument(
        "--watch",
        type=parse_watch,
        default=[],
        help="registers to print on every sample, comma separated",
    )
    parser.add_argument(
        "-n",
        "--samples",
        type=int,
        default=None,
        help="samples to take (default 20, or 2 per side with --switch)",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=60.0,
        help="seconds of quiet between samples (default 60, so the station can sleep)",
    )
    parser.add_argument(
        "--link",
        choices=("duty", "hold"),
        default="duty",
        help="duty: connect, read and disconnect each sample (default). "
        "hold: keep one connection open the whole run, which tends to keep "
        "the station awake.",
    )
    parser.add_argument(
        "--pulse-at",
        type=int,
        default=0,
        help="write ctrl_sleep_mode before this sample number (0 = never)",
    )
    parser.add_argument(
        "--pulse-value", type=int, default=2, help="value for --pulse-at"
    )
    parser.add_argument(
        "--chunk",
        type=int,
        default=0,
        help="widest read to issue for --range (default: what the profile asks for)",
    )
    parser.add_argument(
        "--dump",
        metavar="FILE",
        help="save every compared sample to FILE, so two runs can be compared later",
    )
    parser.add_argument(
        "--note",
        default="",
        help="label saved with --dump, for example awake or asleep",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("FILE_A", "FILE_B"),
        help="compare two --dump files and exit",
    )
    parser.add_argument(
        "--group",
        metavar="FILE",
        help="report which registers track the labels inside one --dump file, and exit",
    )
    parser.add_argument(
        "--ask",
        action="store_true",
        help="ask what the station looks like before each sample, and save that label",
    )
    parser.add_argument(
        "--switch",
        action="store_true",
        help="take the samples twice, pausing in between to change standby by hand,"
        " then report which registers separate the two halves",
    )
    parser.add_argument(
        "--switch-text",
        default="switch standby mode ON in the app now",
        help="what to tell the operator to do at the pause",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=10.0,
        help="seconds to wait after the pause before sampling again (default 10)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.samples is None:
        args.samples = 2 if args.switch else 20

    if args.compare:
        compare_dumps(args.compare[0], args.compare[1])
        return
    if args.group:
        group_report(args.group)
        return
    if not args.mac or not args.type:
        parser.error("-m/--mac and -t/--type are required (unless using --compare)")

    device = build_device(args.type + "12345678")
    if device is None:
        print("unsupported type")
        return

    if args.ranges:
        size = args.chunk or device.max_poll_block
        requests = chunk_requests(args.ranges, size)
        watched = ", ".join(str(start) + "-" + str(end) for start, end in args.ranges)
        widths = str(len(set(request.quantity for request in requests))) + " widths"
    else:
        # The profile's own layout: the fewest reads that still cover the
        # control block, in the sizes this station is used to answering.
        requests = list(device.get_polling_registers())
        watched = "the " + str(len(requests)) + " blocks this profile polls"
        widths = ", ".join(
            str(request.starting_address) + "+" + str(request.quantity)
            for request in requests
        )

    print(
        args.type
        + " "
        + args.mac
        + ": watching "
        + watched
        + " ("
        + str(sum(r.quantity for r in requests))
        + " words in "
        + str(len(requests))
        + " reads, "
        + widths
        + ")"
        + "  link="
        + args.link
        + "  "
        + str(args.samples * (2 if args.switch else 1))
        + " samples"
        + ("  switch" if args.switch else "")
        + " [probe "
        + PROBE_REVISION
        + "]"
    )
    if args.pulse_at and not any(f.name == "ctrl_sleep_mode" for f in device.fields):
        print(
            "this profile has no ctrl_sleep_mode field, so --pulse-at cannot write it"
        )
        return

    # Let the reader own the link: it scans for the address, retries the
    # connection, and rebuilds it when a sleeping station drops it. Handing the
    # reader a BleakClient of our own would leave it unconnected, because the
    # reader only subscribes on a client its caller has already connected.
    reader = DeviceReader(
        args.mac,
        device,
        asyncio.Future,
        DeviceReaderConfig(use_encryption=args.enc),
    )
    reader.logger.setLevel(logging.DEBUG if args.verbose else logging.WARNING)

    if args.link == "hold":
        # connect() expects the polling lock, the same way read() takes it.
        async with reader.polling_lock:
            connected = await reader.connect()
        if not connected:
            print("unable to connect")
            print(
                "wake the station first (bluetti-read -m "
                + args.mac
                + " -t "
                + args.type
                + " -e 1), then re-run; if the header above does"
                " not say [probe "
                + PROBE_REVISION
                + "] this is a cached copy of the script"
            )
            return

    try:
        await run(reader, device, requests, args)
    finally:
        await reader.disconnect()


asyncio.run(main())
