import struct
from typing import Any, Mapping

from . import FieldName, SwitchField


class SharedRegisterControlField(SwitchField):
    """One boolean control packed into a register shared with other controls.

    A Charger 2 keeps its system on/off and its silent charging in one register,
    15600, and not as two bits: the station answers whole state words, of which
    only a handful are valid. Writing one control therefore means reading the
    register, changing this control, and writing back a word that also says what
    the other control is doing - a bare 0 or 1 there is a word the station has
    never reported.

    A field cannot do that on its own, because it needs a fresh read of the
    register, so DeviceWriter performs the read-modify-write. The field carries
    what the writer needs in order to stay out of the product's business:
    ``state_table`` maps each word the station reports to the tuple of control
    values it stands for, and ``state_index`` is which element of that tuple
    this field is.

    A field sharing a register as plain 0/1 flags, rather than as a state word,
    is not this: it can still be written on its own and stays a SwitchField.
    """

    def __init__(
        self,
        name: FieldName,
        address: int,
        state_table: Mapping[int, tuple[Any, ...]],
        state_index: int,
        verification_delay: float | None = None,
    ):
        super().__init__(name, address, verification_delay)
        self.state_table = state_table
        self.state_index = state_index

    def parse(self, data: bytes) -> bool:
        value = struct.unpack("!H", data)[0]

        try:
            state = self.state_table[value]
        except KeyError:
            raise ValueError(
                f"Register {self.address} reads 0x{value:04X}, which is not a"
                " control word this integration knows"
            ) from None

        return state[self.state_index]
