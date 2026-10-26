from enum import Enum, unique


@unique
class WorkingModeV2alt(Enum):
    """Alternative numbering of the V2 working mode (register 2005).

    Confirmed on the Elite 300 only: an owner's unit in Customized UPS mode,
    as shown by the BLUETTI app and the official integration, reads 3 here, a
    value that WorkingModeV2 does not know. The other three modes (PV
    Priority, Standard, Time Control) have not been seen yet, so they stay
    unmapped rather than guessed. Do not attach this enum to another model
    until its values have been seen on that model.
    """

    CUSTOMIZED = 3
