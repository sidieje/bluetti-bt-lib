"""Find out why a station stops answering, and which requests it does it on.

Run with the pipx venv python:
  ~/.local/share/pipx/venvs/bluetti-bt-lib-sidieje/bin/python tools/link_health_probe.py \
      -m <MAC> -t AC300 -n 20
Add --enc for the encrypted stations (Elite V2 models, AP300, EL10, Charger 2).

Two modes:

  default      run full poll cycles, exactly as Home Assistant does, and report
               for every cycle that was cut short which request was the last
               one sent, how many answered before it, how slow the slowest
               answer of the cycle was, and what the station sent about the
               request it left unanswered.

  --blocks     ask for each poll block on its own, --per-block times each. A
               block that answers fine alone but fails in the middle of a cycle
               is a queueing or timing problem; a block that fails in both is
               the register itself.

The failure kinds the reader now reports:

  silent     the station sent nothing at all about that request
  partial    it started answering and the rest of the frame never arrived
  mismatch   a full frame arrived that answers a different request
"""

import argparse
import asyncio
import logging
import statistics
import time
from collections import Counter

from bluetti_bt_lib.bluetooth.device_reader import (
    SLOW_REQUEST_SECONDS,
    DeviceReader,
    DeviceReaderConfig,
)
from bluetti_bt_lib.utils.device_builder import build_device


def _stats_line(reader: DeviceReader) -> str:
    """What the reader knows about the request that just went unanswered."""
    stats = reader.link_stats
    parts = [f"request #{stats.requests}", f"{stats.answered} answered before it"]
    if stats.attempts > 1:
        parts.append(f"{stats.attempts} attempts for it")
    if stats.slowest >= SLOW_REQUEST_SECONDS:
        parts.append(f"slowest answer {stats.slowest:.2f}s ({stats.slowest_registers})")
    if stats.abort_streak:
        parts.append(f"{stats.abort_streak + 1} cycles in a row with an abort")
    detail = f"  <- {stats.aborted}: {stats.failure}"
    if stats.sample:
        detail += f" ({stats.received} bytes, {stats.sample})"
    return detail + "\n      " + "; ".join(parts)


async def run_rounds(reader: DeviceReader, args) -> None:
    """Poll the way Home Assistant does and report every cut-short cycle."""
    ok = 0
    streak = 0
    worst_streak = 0
    failures: Counter[str] = Counter()
    times: list[float] = []
    retried = 0
    recovered = 0
    drained = 0
    discarded = 0

    for round_number in range(1, args.rounds + 1):
        walk_packs = args.packs_every >= 1 and (
            args.packs_every == 1 or round_number % args.packs_every == 0
        )

        started = time.monotonic()
        data = await reader.read(include_packs=walk_packs)
        elapsed = time.monotonic() - started
        times.append(elapsed)

        stats = reader.link_stats
        retried += stats.retried
        recovered += stats.recovered
        drained += stats.drained
        discarded += stats.discarded
        if data is None:
            ok_or_not = "ABORT "
            failures[f"{stats.aborted} [{stats.failure}]"] += 1
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            ok += 1
            streak = 0
            ok_or_not = "ok    "

        slow = f"  slowest {stats.slowest:.2f}s" if stats.slowest else ""
        asks = f"  {stats.retried} asked again" if stats.retried else ""
        if stats.drained:
            asks += f", {stats.drained} drained ({stats.discarded} discarded)"
        print(
            f"[{round_number:>3}] {elapsed:5.2f}s  {ok_or_not}"
            f"  {stats.answered}/{stats.requests} requests"
            f"{'' if walk_packs else '  no pack walk'}{slow}{asks}"
            + (_stats_line(reader) if data is None else "")
        )

        await asyncio.sleep(args.gap)

    print(f"\n{ok}/{args.rounds} cycles answered, median {statistics.median(times):.2f}s")
    if retried:
        # How much of the dropped-request problem the second ask is covering for.
        print(f"{retried} requests asked for again, {recovered} answered the second time")
    if drained:
        # How often the reader waited for a second answer after a retry, and
        # how often one that differed from the first actually turned up.
        print(f"{drained} drains after a retry, {discarded} stray answers discarded")
    if worst_streak:
        print(f"longest run of cut-short cycles: {worst_streak}")
    if failures:
        print("requests that went unanswered:")
        for label, count in failures.most_common():
            print(f"  {count:>3}x  {label}")
    else:
        print("every cycle answered in full")


async def run_blocks(reader: DeviceReader, args) -> None:
    """Ask for each poll block on its own, so a bad register stands out."""
    blocks = reader.bluetti_device.get_polling_registers()

    for block in blocks:
        answered = 0
        latencies: list[float] = []
        kinds: Counter[str] = Counter()

        for _ in range(args.per_block):
            started = time.monotonic()
            data = await reader.read(only_registers=[block])
            elapsed = time.monotonic() - started

            if data is None:
                kinds[reader.link_stats.failure or "unknown"] += 1
            else:
                answered += 1
                latencies.append(elapsed)

            await asyncio.sleep(args.gap)

        timing = (
            f"median {statistics.median(latencies):.2f}s"
            f", slowest {max(latencies):.2f}s"
            if latencies
            else "never answered"
        )
        kinds_text = ", ".join(f"{count}x {kind}" for kind, count in kinds.items())
        print(
            f"{str(block):<58} {answered}/{args.per_block} answered"
            f"  {timing}" + (f"  [{kinds_text}]" if kinds_text else "")
        )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--mac", required=True)
    parser.add_argument("-t", "--type", required=True)
    parser.add_argument("--enc", action="store_true", help="station needs encryption")
    parser.add_argument("-n", "--rounds", type=int, default=20)
    parser.add_argument(
        "--gap", type=float, default=2.0, help="seconds between rounds or probes"
    )
    parser.add_argument(
        "--packs-every",
        type=int,
        default=0,
        help="walk the pack slots every Nth round (0 = never, 1 = every round)",
    )
    parser.add_argument(
        "--blocks",
        action="store_true",
        help="probe each poll block on its own instead of running full cycles",
    )
    parser.add_argument(
        "--per-block", type=int, default=5, help="probes per block in --blocks mode"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show the reader's debug detail"
    )
    args = parser.parse_args()

    device = build_device(args.type + "12345678")
    if device is None:
        print("unsupported type")
        return

    reader = DeviceReader(
        args.mac,
        device,
        asyncio.Future,
        DeviceReaderConfig(use_encryption=args.enc),
    )
    reader.logger.setLevel(logging.DEBUG if args.verbose else logging.WARNING)

    blocks = device.get_polling_registers()
    print(
        f"{args.type} {args.mac}: {len(blocks)} poll blocks, "
        f"{len(device.pack_polling_registers)} pack blocks, "
        f"{device.max_packs} pack slots"
    )
    print("poll order: " + ", ".join(f"{b.starting_address}+{b.quantity}" for b in blocks))

    if args.blocks:
        await run_blocks(reader, args)
    else:
        await run_rounds(reader, args)


asyncio.run(main())
