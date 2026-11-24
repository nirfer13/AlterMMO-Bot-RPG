"""Experience the owner still has to hand out (port of ``functions_expsum.py``).

Kept in ``data/expSummary.json`` as ``{"<discord id>": <exp>}``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..core import io

if TYPE_CHECKING:  # pragma: no cover
    from ..bot import AlterBot
    from ..core.scene import Scene

log = logging.getLogger(__name__)

EMPTY_SUMMARY = "Brak doświadczenia do rozdania."


def _load(bot: "AlterBot") -> dict[str, int]:
    data = bot.store.exp_summary.load()
    return data if isinstance(data, dict) else {}


def init_file_exp(bot: "AlterBot") -> None:
    """Reset the summary (after the owner handed the experience out)."""
    bot.store.exp_summary.save({})


def add_file_exp(bot: "AlterBot", player_id: int | str, experience: int) -> bool:
    """Add ``experience`` to the player's pending total.  Never raises.

    True when it was written; False when the file could not be saved (logged).
    Most callers do not look: a fight goes on whatever the bookkeeping does.
    The mine (features/dungeon.py) does - its reward is paid once, and an
    experience that was not recorded is reported to the owner.
    """
    try:
        players = _load(bot)
        key = str(player_id)
        players[key] = int(players.get(key, 0)) + int(experience)
        bot.store.exp_summary.save(players)
        return True
    except Exception:  # noqa: BLE001 - bookkeeping must not break a fight
        log.exception("Could not record %s exp for player %s.", experience, player_id)
        return False


def add_file_exp_many(bot: "AlterBot", changes: dict[int | str, int]) -> None:
    """Several players at once, in ONE save of the file.  Never raises.

    The Horus bot collects the file every few seconds; two separate saves
    could be split by it (one half collected now, the other later).
    """
    try:
        players = _load(bot)
        for player_id, experience in changes.items():
            key = str(player_id)
            players[key] = int(players.get(key, 0)) + int(experience)
        bot.store.exp_summary.save(players)
    except Exception:  # noqa: BLE001 - bookkeeping must not break a fight
        log.exception("Could not record exp for players %s.", dict(changes))


async def show_file_exp(scene: "Scene") -> None:
    """Post the summary as ``<@id>: exp`` lines (split to fit Discord's limit)."""
    players = _load(scene.bot)
    lines = [f"<@{key}>: {value}" for key, value in players.items()]
    if not lines:
        await scene.send(EMPTY_SUMMARY)
        return
    chunk: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > 1900 and chunk:
            await io.send(scene.channel, "\n".join(chunk))
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        await io.send(scene.channel, "\n".join(chunk))
