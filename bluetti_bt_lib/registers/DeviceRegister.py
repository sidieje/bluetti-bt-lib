import struct
import crcmod.predefined
from enum import Enum

modbus_crc = crcmod.predefined.mkCrcFun("modbus")


class RegisterAction(Enum):
    READ = 3
    WRITE = 6


class DeviceRegister:
    def __init__(self, register_action: RegisterAction, data: bytes):
        self.register_action = register_action

        self.cmd = bytearray(len(data) + 4)
        self.cmd[0] = 1
        self.cmd[1] = register_action.value
        self.cmd[2:-2] = data
        struct.pack_into("<H", self.cmd, -2, modbus_crc(self.cmd[:-2]))

    def response_size(self) -> int:
        """Returns the expected response size in bytes"""
        raise NotImplementedError()

    def __iter__(self):
        """Provide an iter implemention so that bytes(cmd) works"""
        return iter(self.cmd)

    def is_exception_response(self, response: bytes):
        """Checks the response code to see if it's a MODBUS exception"""
        if len(response) < 2:
            return False
        else:
            return response[1] == self.register_action.value + 0x80

    def is_partial_response(self, response: bytes) -> bool:
        """Whether this is the start of an answer to this request.

        Too short to carry a CRC, so only the bytes every answer to this command
        shares get checked: the slave address and the function code.
        """
        return (
            3 <= len(response) < self.response_size()
            and response[0] == self.cmd[0]
            and response[1] == self.register_action.value
        )

    def is_valid_response(self, response: bytes):
        """Validates that the reponse is complete and uncorrupted"""
        if len(response) < 3:
            return False

        crc = modbus_crc(response[0:-2])
        crc_bytes = crc.to_bytes(2, byteorder="little")
        return response[-2:] == crc_bytes

    def is_valid_exception(self, response: bytes) -> bool:
        """A complete MODBUS exception answer to this request (5 bytes: slave address,
        function code + 0x80, exception code, CRC)"""
        return (
            len(response) == 5
            and response[0] == self.cmd[0]
            and self.is_exception_response(response)
            and DeviceRegister.is_valid_response(self, response)
        )

    def parse_response(self, response: bytes):
        """Returns the raw body of the response"""
        raise NotImplementedError()
