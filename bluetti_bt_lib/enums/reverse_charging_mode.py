from enum import Enum, unique


@unique
class ReverseChargingMode(Enum):
    """Charger 2 reverse charging mode (high byte of register 15626).

    The values are the bits the station uses, not a counter: one bit per mode.
    Only one has ever been seen set at a time, which is what the Bluetti app
    offers, but the firmware has never been asked to hold two.
    """

    TRICKLE = 1
    PULSE = 2
    TOP_UP = 4
