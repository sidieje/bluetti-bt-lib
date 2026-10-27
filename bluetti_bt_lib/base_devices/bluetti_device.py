import logging
from typing import Any, List

from ..registers import ReadableRegisters, WriteableRegister
from ..fields import DeviceField, BoolField, BoolFieldNonZero, SwitchField, SelectField, NumberField, FieldName

_LOGGER = logging.getLogger(__name__)

# Asking for one field at a time costs a radio round trip each, and a station
# with 28 of them spends most of a poll cycle waiting for answers. Fields that
# sit near each other are read as one block instead: the answer carries the
# whole range and parse() picks the fields out by offset.
#
# The block limit is the 10 register chunk that the full range scan
# (bluetti_readall) already sends, which is a request size stations are known
# to answer. The gap limit says how many unread registers it is worth carrying
# along to save a round trip. Battery pack fields are not merged; see
# BluettiDevice.__init__.
MAX_POLL_BLOCK = 10
MAX_POLL_GAP = 4

# Other fields describe the station rather than what it is doing: its identity,
# its firmware, and the Wi-Fi name it was last told to use. Those only change
# when someone intervenes, so asking for them every cycle is traffic that
# answers a question nobody asked. The reader asks for each one once per
# connection and reports the cached value afterwards; see DeviceReader.
#
# Keyed by field name, like the field categories below, so every device that
# declares one of these gets the saving for free. DeviceField.name holds the
# string, not the enum member.
#
# Write verification is not affected: the writer reads back the field it wrote
# through an explicit register list, so a field that is not polled is still
# checked after a write. Battery pack firmware (PACK_VER_*) is deliberately
# absent: those registers sit in the pack bank, which is walked separately, and
# a pack can be swapped while the station stays connected.
STARTUP_FIELDS = {
    FieldName.WIFI_NAME.value,
    FieldName.VER_BMS.value,
    FieldName.VER_ARM.value,
    FieldName.VER_DSP.value,
    FieldName.DEVICE_TYPE.value,
    FieldName.DEVICE_SN.value,
}


def _polling_blocks(fields: List[DeviceField]) -> List[ReadableRegisters]:
    """Cover the fields with as few read requests as those two limits allow."""
    blocks: List[ReadableRegisters] = []

    for field in fields:
        end = field.address + field.size - 1

        if blocks:
            start = blocks[-1].starting_address
            covered_end = start + blocks[-1].quantity - 1
            block_end = max(end, covered_end)

            if (
                field.address - covered_end <= MAX_POLL_GAP
                and block_end - start + 1 <= MAX_POLL_BLOCK
            ):
                blocks[-1] = ReadableRegisters(start, block_end - start + 1)
                continue

        blocks.append(ReadableRegisters(field.address, field.size))

    return blocks


class BluettiDevice:
    def __init__(
        self,
        fields: List[DeviceField],
        pack_fields: List[DeviceField] = [],
        max_packs: int = 0,
    ):
        self.fields = fields
        self.pack_fields = pack_fields
        self.max_packs = max_packs

        # Per field that parse() had to leave out: the reason last reported,
        # and how many reads of that same value were skipped quietly since.
        self._skipped_fields: dict[str, tuple[str, int]] = {}

        self.fields.sort(key=lambda f: f.address)
        self.pack_fields.sort(key=lambda f: f.address)

        # Split the fields into what is worth asking for every cycle and what
        # only has to be answered once. parse() still walks the whole field
        # list, so a startup block that happens to cover a live register
        # reports that register too.
        self.startup_fields: List[DeviceField] = [
            f for f in self.fields if f.name in STARTUP_FIELDS
        ]
        self.polling_fields: List[DeviceField] = [
            f for f in self.fields if f.name not in STARTUP_FIELDS
        ]

        self.polling_registers: List[ReadableRegisters] = _polling_blocks(
            self.polling_fields
        )
        self.startup_registers: List[ReadableRegisters] = _polling_blocks(
            self.startup_fields
        )
        self.pack_polling_registers: List[ReadableRegisters] = []

        # Check if we even have battery pack fields defined
        if len(self.pack_fields) == 0 or max_packs == 0:
            return

        # Pack fields are not merged. Their bank is switched by a selector write
        # followed by a settle wait, and an AC300 was seen ignoring a merged
        # 98+2 read that the same station answers as two separate reads. The
        # walk only saves one request per slot, which is not worth that risk.
        for f in self.pack_fields:
            self.pack_polling_registers.append(ReadableRegisters(f.address, f.size))

    def get_polling_registers(self) -> List[ReadableRegisters]:
        """Returns all registers required to poll device fields"""
        return self.polling_registers

    def get_startup_registers(self) -> List[ReadableRegisters]:
        """Returns the registers for the fields that only answer once"""
        return self.startup_registers

    def coalesce_fields(self, fields: List[DeviceField]) -> List[ReadableRegisters]:
        """Cover the given fields with as few read requests as the limits allow.

        A block covers a range, so re-asking for the few startup fields that
        are still unanswered is cheaper than asking for the startup set again.
        """
        return _polling_blocks(fields)

    def get_pack_polling_registers(self) -> List[ReadableRegisters]:
        """Returns all registers required to poll device battery pack fields"""
        return self.pack_polling_registers

    def get_full_registers_range(self) -> List[ReadableRegisters]:
        """Returns all registers which are tested with the readall command"""
        raise NotImplementedError

    def get_device_type_registers(self) -> List[ReadableRegisters]:
        """Returns the register storing the type of the device"""
        raise NotImplementedError

    def get_device_sn_registers(self) -> List[ReadableRegisters]:
        """Returns the register storing the serial number of the device"""
        raise NotImplementedError

    def get_iot_version(self) -> int:
        """Get the IoT protocol version of the device"""
        raise NotImplementedError

    def get_pack_selector(self, pack: int) -> WriteableRegister:
        """Returns the register to request a specific battery pack"""
        raise NotImplementedError

    def get_pack_selector_register(self) -> int | None:
        """Address of the register that picks which pack bank is readable.

        None when the profile has no bank selector. The reader uses it to put
        the value back after probing the slots: writing slot numbers into this
        register changes the number of packs the station reports.
        """
        return None

    def parse(
        self, starting_address: int, data: bytes, pack_num: int | None = None
    ) -> dict:
        """Parse data"""

        # Offsets and size are counted in 2 byte chunks, so for the range we
        # need to divide the byte size by 2
        data_size = int(len(data) / 2)

        # Filter out fields not in range
        r = range(starting_address, starting_address + data_size)
        fields = [
            f
            for f in (self.fields + self.pack_fields)
            if f.address in r and f.address + f.size - 1 in r
        ]

        # Parse fields
        parsed = {}
        for f in fields:
            data_start = 2 * (f.address - starting_address)
            field_data = data[data_start : data_start + 2 * f.size]
            if pack_num is not None and f in self.pack_fields:
                key = f"pack_{str(pack_num)}_{f.name}"
            else:
                key = f.name
            try:
                value = f.parse(field_data)
            except ValueError as err:
                # One field with a value it does not know (a Charger 2 that
                # briefly reports an unknown control word right after a
                # reconnect, for one) must not cost the whole cycle: leave
                # that field out and keep the others.
                #
                # struct.error is deliberately not caught. A truncated answer
                # never gets here: the reader only accepts a frame whose
                # length, byte count and CRC match the request, and the filter
                # above only hands a field to parse() when all of its
                # registers are inside the data, so field_data is always
                # 2 * f.size bytes long. A struct.error would mean a field
                # whose parse() disagrees with its own size, which is a bug in
                # that field and should fail loudly rather than be skipped.
                self._report_skipped_field(key, err)
                continue
            self._clear_skipped_field(key)
            if not f.in_range(value):
                continue
            parsed[key] = value

        return parsed

    def _report_skipped_field(self, key: str, err: ValueError) -> None:
        """Log a field that was left out, once per field and value.

        A station can sit in such a state for a while (a Charger 2 reads
        0x001E at 15600 for about a minute after a hard disconnect), and a
        warning on every poll says nothing new after the first one. The first
        skip of a value warns; the same value again is DEBUG and counted; a
        different value warns again and says how many were held back. Once the
        field parses again it starts over, so a later recurrence warns again.
        """
        reason = str(err)
        last, held = self._skipped_fields.get(key, ("", 0))

        if reason == last:
            self._skipped_fields[key] = (reason, held + 1)
            _LOGGER.debug("Skipping %s again: %s", key, reason)
            return

        self._skipped_fields[key] = (reason, 0)
        extra = f" [{held} more skipped reads of the previous value]" if held else ""
        _LOGGER.warning("Skipping %s: %s%s", key, reason, extra)

    def _clear_skipped_field(self, key: str) -> None:
        """Note that a field that was being skipped parses again."""
        last = self._skipped_fields.pop(key, None)
        if last is not None:
            _LOGGER.debug(
                "%s parses again after %d skipped read(s)", key, last[1] + 1
            )

    def build_write_command(self, name: str, value: Any) -> WriteableRegister | None:
        """Build a command to write values to the device"""

        matches = [f for f in self.fields if f.name == name]
        fields = [f for f in matches if f.is_writeable()]

        if len(fields) == 0:
            return None

        field = next(iter(fields))

        # Convert value to an integer if its not already
        if isinstance(field, SelectField):
            option = field.to_option(value)
            if option is None:
                # Not an option this field can hold.
                return None
            value = option.value
        elif isinstance(field, SwitchField):
            value = 1 if value else 0
        elif isinstance(field, NumberField):
            if not field.allowed_write_type(value) or not field.in_range(value):
                return None
            value = int(value)

        return WriteableRegister(field.address, value)

    def get_bool_fields(self):
        """Returns all bool fields for this device"""
        return [
            f
            for f in self.fields
            if (isinstance(f, BoolField) or isinstance(f, BoolFieldNonZero))
            and not isinstance(f, SwitchField)
        ]

    def get_switch_fields(self):
        """Returns all switch fields for this device"""
        return [f for f in self.fields if isinstance(f, SwitchField)]

    def get_select_fields(self):
        """Returns all select fields for this device"""
        return [f for f in self.fields if isinstance(f, SelectField)]

    def get_sensor_fields(self):
        """Returns all sensor fields for this device"""
        return [
            f
            for f in self.fields
            if not isinstance(f, BoolField)
            and not isinstance(f, BoolFieldNonZero)
            and not isinstance(f, SwitchField)
            and not isinstance(f, SelectField)
        ]
