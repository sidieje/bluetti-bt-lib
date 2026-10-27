
"""Bluetti write command."""

import argparse
import asyncio
import logging
from typing import Any

from bleak import BleakClient

from ..bluetooth import DeviceWriter, DeviceWriterConfig
from ..bluetooth.device_reader import DeviceReader, DeviceReaderConfig
from ..devices.charger2 import CHARGER2
from ..utils.device_builder import build_device
from bleak.exc import BleakError


async def async_write(
    address: str, type: str, encryption: bool, field: str, value: Any
):
    client = BleakClient(address)
    built = build_device(type + "12345678")

    if built is None:
        print("Unsupported powerstation type")
        return

    print("Client created")

    control_fields = {
        "ctrl_system_on_off",
        "ctrl_silent_charging",
    }
    field_obj = next((f for f in built.fields if f.name == field), None)
    # Shared-register controls (15600, and 15626 via compose) are written as
    # a read-modify-write and need a reader on the same connection.
    needs_reader = isinstance(built, CHARGER2) and (
        field in control_fields or callable(getattr(field_obj, "compose", None))
    )

    if needs_reader:
        # The reader and writer must share one BLE connection and lock.
        polling_lock = asyncio.Lock()

        reader = DeviceReader(
            address,
            built,
            asyncio.Future,
            DeviceReaderConfig(use_encryption=encryption),
            lock=polling_lock,
            ble_client=client,
        )

        print("Reader created")

        # Connect the shared BLE client before the reader sets up notifications.
        try:
            async with polling_lock:
                if not client.is_connected:
                    await client.connect()

                if not await reader.connect():
                    print("Unable to connect to device")
                    await client.disconnect()
                    return
        except BleakError as err:
            print(f"Unable to connect to device: {err}")
            return

        writer = DeviceWriter(
            client,
            built,
            DeviceWriterConfig(use_encryption=encryption),
            lock=polling_lock,
            reader=reader,
        )

        # Reuse the reader's notification handler and encryption session.
        writer.attach(reader.encryption)

    else:
        # Preserve the existing path for other devices and fields.
        writer = DeviceWriter(
            client,
            built,
            DeviceWriterConfig(use_encryption=encryption),
        )

    print("Writer created")

    await writer.write(field, value)


def start():
    """Entrypoint."""
    parser = argparse.ArgumentParser(description="Write to bluetti device")
    parser.add_argument("-m", "--mac", type=str, help="Mac-address of the powerstation")
    parser.add_argument(
        "-t", "--type", type=str, help="Type of the powerstation (AC70 f.ex.)"
    )
    parser.add_argument("--on", type=bool, help="Value to write")
    parser.add_argument("--off", type=bool, help="Value to write")
    parser.add_argument("-v", "--value", type=int, help="Value to write")
    parser.add_argument(
        "-s", "--select", type=str, help="Value to write to a Select/Enum field"
    )
    parser.add_argument(
        "-e", "--encryption", type=bool, help="Add this if encryption is needed"
    )
    parser.add_argument("field", type=str, help="Field name (ctrl_dc f.ex.)")
    args = parser.parse_args()

    if args.mac is None or args.type is None or args.field is None:
        parser.print_help()
        return

    # No value given
    if (
        args.on is None
        and args.off is None
        and args.value is None
        and args.select is None
    ):
        parser.print_help()
        return

    value = args.on is not None
    if args.off is not None:
        value = False

    if args.value is not None:
        value = args.value

    if args.select:
        value = args.select

    logging.basicConfig(level=logging.WARNING)

    asyncio.run(
        async_write(
            args.mac,
            args.type,
            args.encryption,
            args.field,
            value,
        )
    )


if __name__ == "__main__":
    start()
