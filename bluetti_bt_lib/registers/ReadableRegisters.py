import struct

from . import DeviceRegister, RegisterAction


class ReadableRegisters(DeviceRegister):
    def __init__(self, starting_address: int, quantity: int):
        super().__init__(
            RegisterAction.READ, struct.pack("!HH", starting_address, quantity)
        )
        self.starting_address = starting_address
        self.quantity = quantity

    def response_size(self):
        # 3 byte header
        # each returned field is actually 2 bytes (16-bit word)
        # 2 byte crc
        return 2 * self.quantity + 5

    def is_partial_response(self, response: bytes) -> bool:
        """The start of an answer to this request, before body and CRC land.

        The byte count is what says this answer covers the quantity that was
        asked for, which is as much as a function 03 answer can be identified
        from before it is complete.
        """
        return (
            len(response) >= 3
            and response[2] == 2 * self.quantity
            and super().is_partial_response(response)
        )

    def is_valid_response(self, response: bytes) -> bool:
        """A complete, uncorrupted answer to exactly this request: length, slave address,
        function code, byte count and CRC. A function 03 answer does not say which registers
        it holds, so the byte count is the only check that it belongs to this request."""
        return (
            len(response) == self.response_size()
            and response[0] == self.cmd[0]
            and response[1] == self.register_action.value
            and response[2] == 2 * self.quantity
            and super().is_valid_response(response)
        )

    def parse_response(self, response: bytes):
        return bytes(response[3:-2])

    def __repr__(self):
        return f"ReadableRegisters(starting_address={self.starting_address}, quantity={self.quantity})"
