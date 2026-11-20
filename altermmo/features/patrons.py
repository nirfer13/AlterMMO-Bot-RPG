"""Supporters: patrons, crafters, Twitch subscribers and YouTube supporters.

Port of the legacy ``functions/functions_patrons.py``, which knew only the
patron tiers and the "Crafter" role.  Owner's decision of 2026-11-20:
everything that was reserved for those two groups is open to Twitch
subscribers and to the supporters ("members") of both YouTube channels as
well.  One set of roles (:data:`SUPPORTER_ROLES`) decides it for the whole
game - premium eggs, the mirror transformation, the prayer at the shrine,
posing as the boss, the party event and the collector's achievement roles all
ask :func:`check_if_patron` / :func:`get_patron` and nothing else.

The function names still say "patron" (legacy name, many callers); read it as
"supporter".

None of these roles is handed out by this bot (``features/twitch.py`` grants
only the viewer, chat, VIP and moderator roles, none of which is listed here):
the bot only reads them, so whoever manages the roles on the server - e.g.
Discord's Twitch / YouTube integrations - decides who is a supporter.

The shared Polish wording and the "how to support" links live here too, so
every text of the game names the groups the same way.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot

#: "Crafter" role.
CRAFTER_ROLE = 687185998550925312
#: Patron (Patronite) tiers 1-4.
PATRON_ROLES = (1113402734280970331, 1113402836705890355, 1113403087734980608, 1113403223508779068)
#: Twitch subscribers of the channel.
TWITCH_SUB_ROLE = 1199797849660063834
#: YouTube channel members (supporters of AlterMMO / Surowe AlterMMO).
YOUTUBE_ROLES = (891314182459371520, 1546795477176025120)
#: Every role that makes its holder a supporter.  The order is the order in which
#: the roles are listed for players (Patronite first: the way the owner recommends).
SUPPORTER_ROLES = (*PATRON_ROLES, CRAFTER_ROLE, TWITCH_SUB_ROLE, *YOUTUBE_ROLES)

# -- shared wording (Polish, nominative; other grammatical cases are written out in the texts) --

#: The collective word for all the groups.
SUPPORTERS_SHORT = "wspierający"
#: The groups spelled out - used once wherever a text has room for it.
SUPPORTERS_LONG = "Patroni, Crafterzy, subskrybenci Twitch i wspierający na YouTube"

# -- how to become a supporter --------------------------------------------------------

#: The way the owner recommends.
PATRONITE_URL = "https://patronite.pl/altermmo"
YOUTUBE_JOIN_URL = "http://youtube.com/@AlterMMO/join"
YOUTUBE_RAW_JOIN_URL = "http://youtube.com/@Surowe_AlterMMO/join"
TWITCH_SUB_URL = "https://www.twitch.tv/products/altermmo_pl"
#: ``(label, url)`` in the order they are shown; the first one is the recommended one.
SUPPORT_LINKS: tuple[tuple[str, str], ...] = (
    ("Patronite", PATRONITE_URL),
    ("YouTube AlterMMO", YOUTUBE_JOIN_URL),
    ("YouTube Surowe AlterMMO", YOUTUBE_RAW_JOIN_URL),
    ("Twitch", TWITCH_SUB_URL),
)


def supporter_mentions() -> str:
    """All supporter roles as role mentions: ``<@&1>, <@&2> ... i <@&8>``.

    Meant for embeds, where a role mention shows the role's name as the server
    calls it and notifies nobody.
    """
    mentions = [f"<@&{role_id}>" for role_id in SUPPORTER_ROLES]
    return ", ".join(mentions[:-1]) + " i " + mentions[-1]


def check_if_patron(player: Any) -> bool:
    """True when ``player`` is a supporter: holds any of :data:`SUPPORTER_ROLES`.

    That is a patron of any tier, a crafter, a Twitch subscriber or a YouTube
    supporter of either channel (before 2026-11-20: patrons and crafters only).
    """
    roles = getattr(player, "roles", None) or []
    return any(role.id in SUPPORTER_ROLES for role in roles)


def get_patron(bot: "AlterBot") -> Any:
    """A random member holding a supporter role, or None when there is none.

    Drawn from all of :data:`SUPPORTER_ROLES` (Twitch and YouTube roles
    included since 2026-11-20).  The legacy weighting is kept on purpose: the
    member lists of the roles are simply concatenated, so a member holding
    several of the roles (say a patron who also subscribes on Twitch) is
    proportionally more likely to be picked.
    """
    members: list[Any] = []
    for role_id in SUPPORTER_ROLES:
        role = bot.role(role_id)
        if role is not None:
            members.extend(role.members)
    return random.choice(members) if members else None
