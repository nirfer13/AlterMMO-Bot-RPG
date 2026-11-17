"""Small shared helpers (port of the legacy ``functions/functions_general.py``).

The legacy ``fClear`` (wipe the boss channel) now lives in
:func:`altermmo.core.io.clear_channel`.
"""

from __future__ import annotations


def victory_bar(a: float, b: float, length: int = 20) -> str:
    """Advantage bar between two values, e.g. the HP of two duelists.

    ``a`` is drawn in red squares, ``b`` in blue ones; ``length`` is the total
    number of squares.
    """
    total = a + b
    if total == 0:
        return "⚖️ " + "▬" * length + " ⚖️"
    a_len = round((a / total) * length)
    a_len = max(0, min(length, a_len))
    return "🟥" * a_len + "🟦" * (length - a_len)
