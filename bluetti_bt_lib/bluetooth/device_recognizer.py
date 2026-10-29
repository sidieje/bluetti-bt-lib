import asyncio
import logging
from typing import Any, Callable, List

from ..base_devices import BluettiDevice, BaseDeviceV1, BaseDeviceV2
from ..bluetooth import DeviceReader, DeviceReaderConfig
from ..devices import DEVICE_NAME_RE
from ..devices.charger2 import CHARGER2
from ..fields import FieldName

_LOGGER = logging.getLogger(__name__)


class DeviceRecognizerResult:
    def __init__(
        self, name: str, iot_version: int, encrypted: bool, sn: int | None = None
    ):
        self.name = name
        self.iot_version = iot_version
        self.encrypted = encrypted
        self.sn = sn
        self.full_name = name + str(sn)


async def recognize_device(
    mac: str,
    future_builder_method: Callable[[], asyncio.Future[Any]],
) -> DeviceRecognizerResult | None:
    # Since we don't know the type we use the base device
    bluetti_devices: List[BluettiDevice] = [
        BaseDeviceV2(),
        BaseDeviceV1(),
        CHARGER2(),
    ]

    for bluetti_device in bluetti_devices:
        device_readers = [
            DeviceReader(
                mac,
                bluetti_device,
                future_builder_method,
                DeviceReaderConfig(
                    timeout=15,
                    use_encryption=True,
                ),
            ),
            DeviceReader(
                mac,
                bluetti_device,
                future_builder_method,
                DeviceReaderConfig(timeout=8),
            ),
        ]

        for device_reader in device_readers:
            try:
                registers = bluetti_device.get_device_type_registers()

                _LOGGER.debug(
                    "Recognition probe: profile=%s, encryption=%s, registers=%s",
                    type(bluetti_device).__name__,
                    device_reader.config.use_encryption,
                    registers,
                )

                # Read the device type first.
                data = await device_reader.read(registers)

                _LOGGER.debug(
                    "Recognition probe result: profile=%s, encryption=%s, "
                    "data=%r",
                    type(bluetti_device).__name__,
                    device_reader.config.use_encryption,
                    data,
                )

                if data is None:
                    continue

                type_data = data.get(FieldName.DEVICE_TYPE.value)

                if type_data is None:
                    _LOGGER.error("No data in device type type_data")
                    continue

                if not isinstance(type_data, str):
                    _LOGGER.error("Invalid data in device type type_data")
                    continue

                if type_data == "":
                    continue

                # All of type_data must be the name, not completed by the
                # sentinel serial ("Handsfree " + "12345678").
                match = DEVICE_NAME_RE.match(type_data + "12345678")
                if match is None or match[1] != type_data:
                    # Some V2 Devices populate the V1 register for type, so we need to check here
                    _LOGGER.debug(
                        "Invalid device type: profile=%s, encryption=%s, "
                        "type_data=%r",
                        type(bluetti_device).__name__,
                        device_reader.config.use_encryption,
                        type_data,
                    )
                    continue

                # Reuse the same reader for the serial-number read.
                data = await device_reader.read(
                    bluetti_device.get_device_sn_registers(),
                )

                if data is None:
                    return DeviceRecognizerResult(
                        type_data,
                        bluetti_device.get_iot_version(),
                        device_reader.config.use_encryption,
                        "000000000000",
                    )

                sn_data = data.get(FieldName.DEVICE_SN.value)

                if not isinstance(sn_data, int):
                    return DeviceRecognizerResult(
                        type_data,
                        bluetti_device.get_iot_version(),
                        device_reader.config.use_encryption,
                        "000000000000",
                    )

                return DeviceRecognizerResult(
                    type_data,
                    bluetti_device.get_iot_version(),
                    device_reader.config.use_encryption,
                    sn_data,
                )
            finally:
                try:
                    await device_reader.disconnect()
                except Exception:
                    _LOGGER.debug(
                        "Error cleaning up device reader",
                        exc_info=True,
                    )

    return None
