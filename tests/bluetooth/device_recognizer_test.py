import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bluetti_bt_lib.bluetooth import device_recognizer
from bluetti_bt_lib.bluetooth.device_reader import DeviceReader
from bluetti_bt_lib.devices import DEVICES
from bluetti_bt_lib.utils.bleak_client_mock import (
    ClientMockNoEncryption,
    r_sn,
    r_sstr,
)

SN = 2446000191559


def plain_readers():
    """Reader factory for a station that answers in the clear.

    The encrypted probe reports nothing, which is what a station that never
    offers a key exchange leaves behind, and the plain probe runs against the
    mock client for real.
    """

    def reader(mac, device, future, config, **kwargs):
        if not config.use_encryption:
            return DeviceReader(
                mac, device, future, config, ble_client=kwargs["ble_client"]
            )

        mock = Mock(
            config=config,
            read=AsyncMock(return_value=None),
            disconnect=AsyncMock(),
        )
        mock.encryption.unsecure_aes_key = None
        return mock

    return reader


class TestRecognizeDevice(unittest.IsolatedAsyncioTestCase):
    async def recognize(
        self,
        type_data: str,
        register: int = 110,
        sn_register: int = 116,
        swapped: bool = True,
    ):
        """Recognise an unencrypted station reporting type_data in register."""
        client = ClientMockNoEncryption()

        if swapped:
            client.add_r_sstr(register, type_data, 12)
        else:
            client.add_r_str(register, type_data, 12)

        client.add_r_sn(sn_register, SN)

        connect = AsyncMock(return_value=client)

        with (
            patch.object(device_recognizer, "DeviceReader", plain_readers()),
            patch.object(device_recognizer, "_open_connection", connect),
        ):
            result = await device_recognizer.recognize_device(
                client.address, asyncio.get_running_loop().create_future
            )

        self.assertFalse(client.is_connected, "the connection was left open")
        return result, connect

    async def test_every_registered_name_is_recognised(self):
        for name in [*DEVICES, "CHARGER 2"]:
            with self.subTest(name=name):
                result, _ = await self.recognize(name)
                self.assertEqual(result and result.name, name)
                self.assertEqual(result.sn, SN)
                self.assertFalse(result.encrypted)

    async def test_junk_type_data_is_not_a_device(self):
        # Register 110 can hold junk (raw 80 48 decodes to "H"). The sentinel
        # serial the type is checked with must not complete it into a name.
        for type_data in ("H", "H1", "H2", "Handsfree "):
            with self.subTest(type_data=type_data):
                result, _ = await self.recognize(type_data)
                self.assertIsNone(result)

    async def test_type_at_the_charger2_register(self):
        # The Charger 2 speaks V2 but reports its type at 15500, so one scan
        # finds it without a profile of its own. Nothing answers at 110.
        result, _ = await self.recognize(
            "CHARGER 2", register=15500, sn_register=15506
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.name, "CHARGER 2")
        self.assertEqual(result.iot_version, 2)
        self.assertEqual(result.sn, SN)

    async def test_type_at_the_v1_register(self):
        # V1 units report an unswapped name at 10 with the serial at 17.
        result, _ = await self.recognize(
            "AC60", register=10, sn_register=17, swapped=False
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.name, "AC60")
        self.assertEqual(result.iot_version, 1)
        self.assertEqual(result.sn, SN)

    async def test_one_connection_is_enough(self):
        # The probes share the connection the recognizer opened, which is what
        # the speedup rests on: one scan and one connect for the whole job.
        _, connect = await self.recognize("AC300")

        self.assertEqual(connect.await_count, 1)

    async def test_no_connection_is_not_a_device(self):
        connect = AsyncMock(return_value=None)

        with (
            patch.object(device_recognizer, "_open_connection", connect),
            patch.object(device_recognizer, "DeviceReader") as readers,
        ):
            self.assertIsNone(
                await device_recognizer.recognize_device(
                    "00:11:00:11:00:11",
                    asyncio.get_running_loop().create_future,
                )
            )

        readers.assert_not_called()
        self.assertEqual(connect.await_count, 1)


class TestEncryptedRecognition(unittest.IsolatedAsyncioTestCase):
    async def recognize(self, reads, challenged: bool):
        """Recognise a station whose probe answers over an encrypted reader."""
        client = ClientMockNoEncryption()
        connect = AsyncMock(return_value=client)
        encrypted = Mock(
            config=Mock(use_encryption=True),
            read=AsyncMock(side_effect=list(reads)),
            disconnect=AsyncMock(),
        )
        encrypted.encryption.unsecure_aes_key = b"\x01" * 16 if challenged else None

        def reader(mac, device, future, config, **kwargs):
            if config.use_encryption:
                return encrypted
            return Mock(
                config=config,
                read=AsyncMock(return_value=None),
                disconnect=AsyncMock(),
            )

        with (
            patch.object(device_recognizer, "DeviceReader", reader),
            patch.object(device_recognizer, "_open_connection", connect),
        ):
            result = await device_recognizer.recognize_device(
                client.address, asyncio.get_running_loop().create_future
            )

        return result, encrypted

    async def test_encrypted_station_is_reported_encrypted(self):
        result, _ = await self.recognize(
            [{110: r_sstr("AP300", 12)}, {116: r_sn(SN)}], challenged=True
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.name, "AP300")
        self.assertTrue(result.encrypted)
        self.assertEqual(result.sn, SN)

    async def test_key_exchange_still_in_flight_gets_another_cycle(self):
        # A station that offered a key exchange is encrypted even when the
        # exchange had not finished by the time the probe ran out of time, so
        # the same link is asked again rather than taken for an unencrypted one.
        result, encrypted = await self.recognize(
            [None, {110: r_sstr("AP300", 12)}, {116: r_sn(SN)}], challenged=True
        )

        self.assertIsNotNone(result)
        self.assertTrue(result.encrypted)
        self.assertEqual(encrypted.read.await_count, 3)

    async def test_a_station_that_offered_nothing_is_not_encrypted(self):
        # No key exchange offered, and nothing in the clear either: the plain
        # probe follows and nothing is reported.
        result, encrypted = await self.recognize([None], challenged=False)

        self.assertIsNone(result)
        self.assertEqual(encrypted.read.await_count, 1)


if __name__ == "__main__":
    unittest.main()
