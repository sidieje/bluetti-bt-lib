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

    # V2 pack polling is disabled, so the two pack methods below are commented
    # out rather than deleted. This is deliberate -- commit 6bca489 "added
    # ep600 test, disabled v2 pack polling" turned them off as a unit -- and it
    # is the reason EP600, the only V2 profile with pack fields, keeps
    # max_packs at 0. See the note there for how to resume.
    #
    # Both are untested. The selector address was guessed twice and confirmed
    # neither time: 3001 when the code was first written (805040f) and 7000 when
    # it was switched off. get_pack_selector_register() is inherited from
    # BluettiDevice, which reports None for "no bank selector", so the reader
    # would not save and restore the bank either. Uncommenting only one of the
    # two is not enough to enable the walk.
    #
    # Re-add WriteableRegister to the ..registers import above as well.
    #
    # def get_pack_selector(self, pack: int) -> WriteableRegister:
    #     return WriteableRegister(7000, pack)
    #
    # def get_pack_selector_register(self) -> int:
    #     return 7000
