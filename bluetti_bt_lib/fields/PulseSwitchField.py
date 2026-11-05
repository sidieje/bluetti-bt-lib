import struct

from . import FieldName, SwitchField


class PulseSwitchField(SwitchField):
    """A switch whose state sits in one register and is driven by a fixed pulse
    written to another.

    The Apex 300's standby is why this exists. Register 174 reports whether the
    station is asleep, but nothing has ever answered a write to it, while the
    only write known to move the state is 2 to 2013 - and that write toggles the
    station whichever way it is asked, so the value it carries says nothing
    about the state being asked for. A plain SwitchField cannot describe that
    pair: the register it reads is not the register it writes.

    ``on_bit`` is the bit of the read word that says the switch is on, because
    these words carry other state as well: an awake Apex 300 reads 0 at 174 and
    8 once it is in standby, and the same word is expected to hold other flags.
    Any word with that bit clear reads as off, so a word that grows new bits
    still reports the standby state rather than going unknown.
    """

    def __init__(
        self,
        name: FieldName,
        address: int,
        pulse_address: int,
        pulse_value: int,
        on_bit: int = 1,
        verification_delay: float | None = None,
    ):
        super().__init__(name, address, verification_delay)
        self.pulse_address = pulse_address
        self.pulse_value = pulse_value
        self.on_bit = on_bit

    @property
    def write_address(self) -> int:
        return self.pulse_address

    def parse(self, data: bytes) -> bool:
        return bool(struct.unpack("!H", data)[0] & self.on_bit)
