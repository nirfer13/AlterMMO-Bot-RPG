"""Types shared by fights and the boss life-cycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FightResult:
    """How a boss encounter ended.

    ``losers_to_ban`` - members who must receive the "dead hunter" role.  The
    role is handed out by the boss life-cycle *after* the post-fight reset
    (which first removes the role from everybody), exactly like the legacy
    order ``resetEnd()`` -> ``setDeadHunters()``.
    """

    won: bool
    losers_to_ban: list[Any] = field(default_factory=list)


class FightInterrupted(RuntimeError):
    """A fight cannot go on for technical reasons (repeated connection problems).

    Raise it from a fight to hand control back to the boss life-cycle, which
    then applies the "interrupted fight" policy (by default the boss stays and
    may be attacked again).  Never raise it for a player's mistake.
    """
