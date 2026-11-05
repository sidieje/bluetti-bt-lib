"""Find the register that says whether a station is awake or in standby.

Register 2013 answers 0 both after a wake pulse and after a sleep pulse, so it
looks like a command register that clears itself (a power button) rather than a
mirror of the state. A standby switch still needs somewhere to read the state
from, so this probe watches a block of registers and reports only the words
that move. At the end it lists the registers that held more than one value,
which are the candidates for a real state register.

Run it with the pipx venv python:
  ~/.local/share/pipx/venvs/bluetti-bt-lib-sidieje/bin/python tools/standby_probe.py \
      -m <MAC> -t AP300 -e -n 40 --gap 5

The control block is watched by default. Widen the sweep with one --range per
block:
  --range 2000-2100 --range 100-199 --range 1300-1450

Send the pulse from the same connection instead of a second terminal:
  --pulse-at 10 --pulse-value 2
That writes ctrl_sleep_mode before sample 10, so the write and the words that
follow it sit on one timeline. A station only answers one BLE connection at a
time, so a bluetti-write from another terminal would have to wait for this run
to stop.

A fresh connection is what may wake the station in the first place, so the
probe connects once and keeps the link open for the whole run. If the link
drops because the station slept, that shows up as samples with no answer,
which is itself a candidate signal.
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

DEFAULT_RANGE = "2000-2100"
# Bump when the script changes: raw.githubusercontent.com can hand back a cached
# copy, and the header line is the only way to tell which copy actually ran.
PROBE_REVISION = "2026-07-29.2"


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
        raise argparse.ArgumentTypeError("registers must be inside 0-65535: " + repr(text))
    return start, end


def parse_watch(text: str) -> list[int]:
    return [int(part) for part in text.split(",") if part]


def chunk_requests(ranges: list[tuple[int, int]], size: int) -> list[ReadableRegisters]:
    """Cut the watched ranges into reads no wider than the station answers."""
    requests: list[ReadableRegisters] = []
    for start, end in ranges:
        address = start
        while address <= end:
            quantity = min(size, end - address + 1)
            requests.append(ReadableRegisters(address, quantity))
            address += quantity
    return requests


def block_words(body: bytes) -> list[int]:
    """Split one block answer into its 16-bit words."""
    count = len(body) // 2
    return list(struct.unpack("!" + str(count) + "H", body))


def show(value: int) -> str:
    return str(value) + " (0x" + format(value, "04X") + ")"


async def read_once(
    reader: DeviceReader, requests: list[ReadableRegisters]
) -> tuple[dict[int, int], int]:
    """Read every watched block once, returning the words and the silent blocks."""
    found: dict[int, int] = {}
    silent = 0
    for request in requests:
        data = await reader.read(only_registers=[request], raw=True)
        if not data:
            silent += 1
            continue
        for base, body in data.items():
            for offset, value in enumerate(block_words(body)):
                found[base + offset] = value
    return found, silent


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
    print("  pulse: ctrl_sleep_mode=" + str(value) + " -> "
          + ("accepted" if written else "REJECTED"))


async def run(
    reader: DeviceReader,
    device: BluettiDevice,
    requests: list[ReadableRegisters],
    args,
) -> None:
    previous: dict[int, int] = {}
    values: dict[int, set[int]] = {}
    change_count: Counter[int] = Counter()
    answered = 0
    empty = 0

    for sample in range(1, args.samples + 1):
        if args.pulse_at == sample:
            await pulse(reader, device, args.pulse_value)

        started = time.monotonic()
        current, silent = await read_once(reader, requests)
        took = time.monotonic() - started

        if current:
            answered += 1
        else:
            empty += 1

        moved: list[int] = []
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
            "[" + format(sample, ">3") + "] " + time.strftime("%H:%M:%S")
            + "  " + format(took, "5.2f") + "s"
            + "  " + format(len(current), ">4") + " words"
            + ("  " + str(silent) + " blocks silent" if silent else "")
            + ("  watch: " + watch if watch else "")
        )
        for address in moved:
            print("        " + str(address) + " " + show(previous[address])
                  + " -> " + show(current[address]))
        if not current:
            print("        nothing answered this sample")

        previous = current
        if sample < args.samples:
            await asyncio.sleep(args.gap)

    print("")
    print(str(answered) + "/" + str(args.samples) + " samples answered")
    if empty:
        print(str(empty) + " samples got nothing at all")

    candidates = sorted(a for a, seen in values.items() if len(seen) > 1)
    if not candidates:
        print("nothing moved: every watched register answered the same value")
        return

    print(str(len(candidates)) + " register(s) changed during the run:")
    for address in candidates:
        seen = sorted(values[address])
        print("  " + format(address, ">5") + "  values "
              + ", ".join(str(value) for value in seen)
              + "  changed " + str(change_count[address]) + "x")
    print(str(len(values) - len(candidates))
          + " register(s) answered the same value every sample")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="watch which registers move when a ctrl_sleep_mode pulse goes out"
        " [probe " + PROBE_REVISION + "]",
    )
    parser.add_argument("-m", "--mac", required=True)
    parser.add_argument("-t", "--type", required=True)
    parser.add_argument("-e", "--enc", action="store_true", help="station needs encryption")
    parser.add_argument(
        "--range",
        dest="ranges",
        type=parse_range,
        action="append",
        help="block to watch, start-end (repeatable, default " + DEFAULT_RANGE + ")",
    )
    parser.add_argument(
        "--watch",
        type=parse_watch,
        default=[],
        help="registers to print on every sample, comma separated",
    )
    parser.add_argument("-n", "--samples", type=int, default=30)
    parser.add_argument("--gap", type=float, default=5.0, help="seconds between samples")
    parser.add_argument(
        "--pulse-at",
        type=int,
        default=0,
        help="write ctrl_sleep_mode before this sample number (0 = never)",
    )
    parser.add_argument("--pulse-value", type=int, default=2, help="value for --pulse-at")
    parser.add_argument(
        "--chunk",
        type=int,
        default=0,
        help="registers per read (default: what the profile already asks for)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    device = build_device(args.type + "12345678")
    if device is None:
        print("unsupported type")
        return

    ranges = args.ranges or [parse_range(DEFAULT_RANGE)]
    size = args.chunk or device.max_poll_block
    requests = chunk_requests(ranges, size)
    total = sum(end - start + 1 for start, end in ranges)

    print(
        args.type + " " + args.mac + ": watching "
        + ", ".join(str(start) + "-" + str(end) for start, end in ranges)
        + " (" + str(total) + " words in " + str(len(requests))
        + " reads of at most " + str(size) + ")"
        + " [probe " + PROBE_REVISION + "]"
    )
    if args.pulse_at and not any(f.name == "ctrl_sleep_mode" for f in device.fields):
        print("this profile has no ctrl_sleep_mode field, so --pulse-at cannot write it")
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

    # connect() expects the polling lock, the same way read() takes it.
    async with reader.polling_lock:
        connected = await reader.connect()
    if not connected:
        print("unable to connect")
        print(
            "wake the station first (bluetti-read -m " + args.mac
            + " -t " + args.type + " -e 1), then re-run; if the header above does"
            " not say [probe " + PROBE_REVISION + "] this is a cached copy of the script"
        )
        return

    try:
        await run(reader, device, requests, args)
    finally:
        await reader.disconnect()


asyncio.run(main())
