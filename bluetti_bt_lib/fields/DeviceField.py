from typing import Any

from ..fields import FieldName


class DeviceField:
    def __init__(
        self,
        name: FieldName,
        address: int,
        size: int,
        verification_delay: float | None = None,
    ):
        self.name = name.value
        self.address = address
        self.size = size
        # Optional per-field override of the delay DeviceWriter waits
        # before reading a write back, for slow-to-apply settings.
        self.verification_delay = verification_delay

    def parse(self, data: bytes) -> Any:
        raise NotImplementedError

    @property
    def write_address(self) -> int:
        """The register a write to this field goes to.

        For every field but a pulse control this is the register the field is
        read from; see PulseSwitchField, where the two are different words.
        """
        return self.address

    def is_writeable(self) -> bool:
        return False

    def allowed_write_type(self, value: Any) -> bool:
        return False

    def in_range(self, value: Any) -> bool:
        return True
