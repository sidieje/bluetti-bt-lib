"""Plain wording for the Bleak exceptions that reach the log.

Bleak renders a GATT protocol error as the ``(code, message)`` tuple it was
built from, so a station that simply refused a request appears in the Home
Assistant log as::

    Bleak error: (<BleakGATTProtocolErrorCode.UNLIKELY_ERROR: 14>,
                  'GATT Protocol Error: Unlikely Error')

which reads like a crash. The number is the useful part, so keep that and say
what it means.
"""

from __future__ import annotations

#: GATT protocol errors worth translating. Anything else falls back to naming
#: the code, which is still more readable than the exception's own rendering.
_MEANINGS = {
    0x01: "the station was asked for something it does not expose (invalid handle)",
    0x0D: "the station rejected the size of the value it was given",
    0x0E: "the station refused the request and gave no reason",
    0x11: "the station has no resources left for another request",
    0x12: "the Bluetooth stack on this machine lost track of the station",
    0xFE: "the station already has a request in progress",
    0xFF: "the request asked for a range the station does not have",
}


def describe_bleak_error(err: BaseException) -> str:
    """Describe a Bleak failure in one phrase that does not look like a crash."""
    code = getattr(err, "code", None)
    if code is not None:
        try:
            number = int(code)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return str(err) or err.__class__.__name__

        meaning = _MEANINGS.get(number, "the station refused the request")
        return f"{meaning} (GATT protocol error {number})"

    text = str(err).strip()
    return text or err.__class__.__name__
