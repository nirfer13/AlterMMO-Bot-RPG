"""Patrons and crafters (port of the legacy ``functions/functions_patrons.py``)."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot

#: "Crafter" role.
CRAFTER_ROLE = 687185998550925312
#: Patron tiers 1-4.
PATRON_ROLES = (1113402734280970331, 1113402836705890355, 1113403087734980608, 1113403223508779068)
SUPPORTER_ROLES = (CRAFTER_ROLE, *PATRON_ROLES)


def check_if_patron(player: Any) -> bool:
    """True when ``player`` is a patron (any tier) or a crafter."""
    roles = getattr(player, "roles", None) or []
    return any(role.id in SUPPORTER_ROLES for role in roles)


def get_patron(bot: "AlterBot") -> Any:
    """A random member holding a patron/crafter role, or None when there is none.

    A member with several of the roles is proportionally more likely to be
    picked (legacy behaviour: the role member lists were simply concatenated).
    """
    members: list[Any] = []
    for role_id in SUPPORTER_ROLES:
        role = bot.role(role_id)
        if role is not None:
            members.extend(role.members)
    return random.choice(members) if members else None
