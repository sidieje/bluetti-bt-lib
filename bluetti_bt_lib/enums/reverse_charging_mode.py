from enum import Enum, unique


@unique
class ReverseChargingMode(Enum):
    """Charger 2 reverse charging mode (high byte of register 15626)."""

    TRICKLE = 1
    PULSE = 2
    TOP_UP = 4
