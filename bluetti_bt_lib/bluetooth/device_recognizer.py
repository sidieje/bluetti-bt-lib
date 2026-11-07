import asyncio
import logging
from typing import Any, Callable

from bleak import BleakClient, BleakScanner
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from ..base_devices import BaseDeviceV2
from ..bluetooth import DeviceReader, DeviceReaderConfig
from ..devices import DEVICE_NAME_RE
from ..fields import FieldName, SerialNumberField, StringField, SwapStringField
from ..registers import ReadableRegisters

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


# Where a station reports its type, with the iot version and the serial number
# range that belongs to it. V2 units answer at 110, V1 units at 10, and the
# Charger 2 at 15500: it speaks the V2 protocol but moved the common fields.
#
# All three are read on one connection, so a single probe resolves the type of
# every supported station. This used to be two connections per candidate
# profile - six in all - and a station that answered none of them paid for each
# one before reporting "Device type not supported".
_TYPE_REGISTERS = (
    (110, 2, 116),
    (10, 1, 17),
    (15500, 2, 15506),
)

_TYPE_SCAN = [ReadableRegisters(address, 6) for address, _, _ in _TYPE_REGISTERS]

# A type register holds a string, but not always in the same encoding: V2 and
# the Charger 2 swap the byte pairs, V1 does not. Both are tried, which also
# covers the V2 units that populate the V1 range with an unswapped name.
_TYPE_FIELDS = (
    SwapStringField(FieldName.DEVICE_TYPE, 0, 6),
    StringField(FieldName.DEVICE_TYPE, 0, 6),
)

# A probe starts on a connection that is already up, so these only have to
# cover the key exchange and the reads, not a scan and a connect as well.
#
# ENCRYPTED_PROBE stays under the reader's HANDSHAKE_TIMEOUT_SECONDS on purpose:
# the probe then ends on its own timeout, which resyncs and keeps the link for
# the plain probe, rather than on the key exchange giving up, which closes a
# supplied client.
#
# A station that is not encrypted never answers the key exchange, so it waits
# this whole timeout out before the plain probe runs. Bench timings over a live
# link: notifications were up about 0.8s after connect, the exchange confirmed
# within the next second, and the type read took another 0.4s, so an AP300
# recognised in 2.05s and a Charger 2 in 1.37s. Five seconds is still more than
# twice the worst of those.
ENCRYPTED_PROBE_TIMEOUT_SECONDS = 5
PLAIN_PROBE_TIMEOUT_SECONDS = 8


async def recognize_device(
    mac: str,
    future_builder_method: Callable[[], asyncio.Future[Any]],
) -> DeviceRecognizerResult | None:
    """Report what is at mac, over one connection shared by both probes.

    Returns None when nothing answered with a supported device type.
    """
    client = await _open_connection(mac)

    try:
        # Encryption first, so a station that accepts a key exchange is
        # reported encrypted even when it also answers in the clear.
        for use_encryption in (True, False):
            if client is not None and not client.is_connected:
                # A failed key exchange closes the link, and the plain probe
                # needs a live one to ask the same question of.
                client = await _open_connection(mac)

            # A station that stopped advertising is not worth a second scan.
            if client is None:
                return None

            result = await _probe(
                client, mac, future_builder_method, use_encryption
            )

            if result is not None:
                return result

        return None
    finally:
        await _close_connection(client)


async def _probe(
    client: BleakClient,
    mac: str,
    future_builder_method: Callable[[], asyncio.Future[Any]],
    use_encryption: bool,
) -> DeviceRecognizerResult | None:
    """Ask one connection for the type, encrypted or in the clear."""
    reader = DeviceReader(
        mac,
        # The base device is only a carrier: every register below is read raw
        # and decoded here rather than through a profile.
        BaseDeviceV2(),
        future_builder_method,
        DeviceReaderConfig(
            timeout=(
                ENCRYPTED_PROBE_TIMEOUT_SECONDS
                if use_encryption
                else PLAIN_PROBE_TIMEOUT_SECONDS
            ),
            use_encryption=use_encryption,
        ),
        ble_client=client,
    )

    try:
        _LOGGER.debug("Recognition probe: encryption=%s", use_encryption)

        data = await reader.read(_TYPE_SCAN, raw=True)

        if (
            data is None
            and use_encryption
            and reader.encryption.unsecure_aes_key is not None
        ):
            # The station offered a key exchange, so it is encrypted and the
            # exchange was still in flight when the probe ran out of time. The
            # link keeps its session, so another cycle can finish it.
            _LOGGER.debug("Key exchange in progress, asking again")
            data = await reader.read(_TYPE_SCAN, raw=True)

        _LOGGER.debug("Recognition probe result: %r", data)

        if data is None:
            return None

        for address, iot_version, sn_address in _TYPE_REGISTERS:
            type_data = _match_type(data.get(address))

            if type_data is None:
                continue

            sn = await _read_serial(reader, sn_address)

            _LOGGER.debug(
                "Recognised %s from register %s (encrypted=%s)",
                type_data,
                address,
                use_encryption,
            )

            return DeviceRecognizerResult(
                type_data, iot_version, use_encryption, sn
            )

        return None
    finally:
        try:
            await reader.disconnect()
        except Exception:
            _LOGGER.debug(
                "Error cleaning up device reader",
                exc_info=True,
            )


def _match_type(body: bytes | None) -> str | None:
    """Return the device name a type register holds, or None for anything else.

    All of it has to be the name, not completed by the sentinel serial
    ("Handsfree " + "12345678"): register 110 can hold junk, and raw 80 48
    decodes to "H", which is a prefix of nothing we support.
    """
    if not body:
        return None

    for field in _TYPE_FIELDS:
        type_data = field.parse(body)

        if not type_data:
            continue

        match = DEVICE_NAME_RE.match(type_data + "12345678")

        if match is not None and match[1] == type_data:
            return type_data

    return None


async def _read_serial(reader: DeviceReader, address: int):
    """Read a serial number, or the sentinel the probe reported for none."""
    data = await reader.read([ReadableRegisters(address, 4)], raw=True)
    body = data.get(address) if data else None

    if body:
        sn = SerialNumberField(FieldName.DEVICE_SN, address).parse(body)

        if isinstance(sn, int):
            return sn

    return "000000000000"


async def _open_connection(mac: str) -> BleakClient | None:
    """Bring up a connection for the probes to share.

    The recognizer owns it rather than the readers: DeviceReader never closes a
    client it was handed, and connecting once keeps the scan and the retries out
    of the probe timeouts, which then only have to cover the work they measure.
    """
    device = await BleakScanner.find_device_by_address(mac, timeout=5)

    if device is None:
        _LOGGER.error("Device not found")
        return None

    try:
        return await establish_connection(
            BleakClientWithServiceCache,
            device,
            device.name or "Unknown Device",
            max_attempts=10,
        )
    except Exception as err:
        # A station that has stopped advertising mid-discovery is a normal end
        # to recognition, not a fault to raise over.
        _LOGGER.debug("Unable to connect: %s", err)
        return None


async def _close_connection(client: BleakClient | None) -> None:
    if client is None:
        return

    try:
        if client.is_connected:
            await client.disconnect()
    except Exception:
        _LOGGER.debug("Error closing the recognition connection", exc_info=True)
