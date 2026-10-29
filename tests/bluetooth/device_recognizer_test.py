import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bluetti_bt_lib.bluetooth import device_recognizer
from bluetti_bt_lib.bluetooth.device_reader import DeviceReader
from bluetti_bt_lib.devices import DEVICES
from bluetti_bt_lib.utils.bleak_client_mock import ClientMockNoEncryption


class TestRecognizeDevice(unittest.IsolatedAsyncioTestCase):
    async def recognize(self, type_data: str):
        """Recognize an unencrypted V2 unit reporting type_data in register 110."""
        client = ClientMockNoEncryption()
        client.add_r_sstr(110, type_data, 12)
        client.add_r_sn(116, 2446000191559)

        def reader(mac, device, future, config):
            if config.use_encryption:
                # The encrypted probes find nothing.
                return Mock(
                    config=config,
                    read=AsyncMock(return_value=None),
                    disconnect=AsyncMock(),
                )
            return DeviceReader(mac, device, future, config, ble_client=client)

        with patch.object(device_recognizer, "DeviceReader", reader):
            return await device_recognizer.recognize_device(
                client.address, asyncio.get_running_loop().create_future
            )

    async def test_every_registered_name_is_recognised(self):
        for name in [*DEVICES, "CHARGER 2"]:
            with self.subTest(name=name):
                result = await self.recognize(name)
                self.assertEqual(result and result.name, name)

    async def test_junk_type_data_is_not_a_device(self):
        # Register 110 can hold junk (raw 80 48 decodes to "H"). The sentinel
        # serial the type is checked with must not complete it into a name.
        for type_data in ("H", "H1", "H2", "Handsfree "):
            with self.subTest(type_data=type_data):
                self.assertIsNone(await self.recognize(type_data))


if __name__ == "__main__":
    unittest.main()
