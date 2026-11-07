from typing import List

from . import BluettiDevice
from ..fields import DeviceField
from ..fields import FieldName, SwapStringField, UIntField, SerialNumberField
from ..registers import ReadableRegisters


class BaseDeviceV2(BluettiDevice):
    def __init__(
        self,
        additional_fields: List[DeviceField] = [],
        pack_fields: List[DeviceField] = [],
        max_packs: int = 0,
        include_common_fields: bool = True,
    ):
        common_fields = (
            [
                SwapStringField(FieldName.DEVICE_TYPE, 110, 6),
                SerialNumberField(FieldName.DEVICE_SN, 116),
                UIntField(FieldName.BATTERY_SOC, 102, min=0, max=100),
            ]
            if include_common_fields
            else []
        )

        super().__init__(
            common_fields + additional_fields,
            pack_fields,
            max_packs,
        )

    def get_full_registers_range(self) -> List[ReadableRegisters]:
        return [ReadableRegisters(i, 10) for i in range(0, 20000, 10)]

    def get_iot_version(self) -> int:
        return 2

    # def get_pack_selector(self, pack: int) -> WriteableRegister:
    #     return WriteableRegister(7000, pack)  # TODO test
