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
"""

import argparse
import asyncio
import logging
import struct
import time
from collections import Counter

from bluetti_bt_lib.base_devices.bluetti_device import BluettiDevice
from bluetti_bt_lib.bluetooth import DeviceWriter, DeviceWriterConfig
from bluetti_bt_lib.bluetooth.device_reader import DeviceReader, DeviceReaderConfig
from bluetti_bt_lib.registers.ReadableRegisters import ReadableRegisters
from bluetti_bt_lib.utils.device_builder import build_device

# Bump when the script changes: raw.githubusercontent.com can hand back a cached
# copy, and the header line is the only way to tell which copy actually ran.
PROBE_REVISION = "2026-11-05.1"

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

    for sample in range(1, args.samples + 1):
        stamp = "[" + format(sample, ">3") + "] " + time.strftime("%H:%M:%S")

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
            if sample < args.samples:
                await asyncio.sleep(args.gap)
            continue

        if args.pulse_at == sample:
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
            previous = current
        if sample < args.samples:
            await asyncio.sleep(args.gap)

    print("")
    print(str(trusted_count) + "/" + str(args.samples) + " samples compared")
    if empty:
        print(str(empty) + " samples got nothing at all")
    if cut:
        print(str(cut) + " samples were cut short mid-cycle and left out")

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
    parser.add_argument("-m", "--mac", required=True)
    parser.add_argument("-t", "--type", required=True)
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
    parser.add_argument("-n", "--samples", type=int, default=20)
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
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

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
