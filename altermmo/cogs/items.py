"""The player items ("Przedmioty"): ``!przedmioty`` and the owner's ``!DajPrzedmiot``.

The items themselves live in :mod:`altermmo.features.items`.  ``!przedmioty``
(skróty ``!eq``, ``!ekw``, ``!plecak``, ``!ekwipunek``) shows what the player
carries - like the hub's "🎒 Przedmioty" button: a display command (any
channel of the server, the busy policy of the display commands), silent while
a typed fight is on the channel (``guards.not_during_fight``), also for a
player without a pet.

``!wskrzes @gracz`` (skróty ``!wskrzesz``, ``!wskrzeszenie``; owner,
2026-11-26) reads a Zwój wskrzeszenia over a fallen hunter: the dead-hunter
role goes (``items.revive``).  A gameplay command (the boss channel only).

For the owner's support (not in the help): ``!DajPrzedmiot @gracz <klucz|nazwa>
[n]`` gives ``n`` items (1 when left out); a negative ``n`` takes them back,
never below 0.  Every use is logged and told to the owner's log channel.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ..core import guards, io
from ..core.scene import Scene
from ..features import items

log = logging.getLogger(__name__)

MSG_GIVEN = "Gracz <@{player_id}> ma teraz {emoji} **{label}**: {count}."
MSG_UNKNOWN = ("Nie znam przedmiotu **{given}**. Podaj klucz albo nazwę: {known}, np. "
               "**!DajPrzedmiot @gracz klucz do wieży 2** (minus zabiera, np. **-1**).")
MSG_NO_STORE = ("{emoji} **{label}**: chwilowo niedostępne - w bazie danych brakuje miejsca na nie ({where}). "
                "Uruchom bota ponownie: brakujące tabele i kolumny dodaje sam przy starcie.")
#: For the owner's log channel: who changed what.
MSG_LOGGED = ("🎒 !DajPrzedmiot: {admin} - **{name}** {delta:+d} dla gracza {player} (<@{player_id}>), "
              "ma teraz: {count}.")


def parse_request(text: str) -> tuple[str, int]:
    """``"klucz do wieży 2"`` -> ``("klucz do wieży", 2)``: the item and the number (1 when left out)."""
    words = str(text or "").split()
    if words:
        try:
            return " ".join(words[:-1]), int(words[-1])
        except ValueError:
            pass
    return " ".join(words), 1


class ItemsCog(commands.Cog, name="Przedmioty"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(name="przedmioty", aliases=["ekwipunek", "eq", "plecak", "ekw"],
                      brief="Show the player's items.")
    @guards.not_during_fight()
    @guards.unless_busy(read_only=True)
    async def przedmioty(self, ctx: commands.Context) -> None:
        await items.show_items(Scene.from_ctx(ctx))

    @commands.command(name="wskrzes", aliases=["wskrzesz", "wskrzeszenie"],
                      brief="Revive a fallen hunter with a Zwój wskrzeszenia.")
    @guards.in_order(guards.boss_channel_only(), guards.not_during_fight(), guards.unless_busy())
    async def wskrzes(self, ctx: commands.Context, gracz: discord.Member | None = None) -> None:
        """``!wskrzes @gracz``: the dead-hunter role goes, for one Zwój wskrzeszenia (``items.revive``).

        A gameplay command: the boss channel only, silent during a typed fight,
        the busy notice while the channel is busy - one ordered check
        (``guards.in_order``).  Without a (known) player the command says how to use it.
        """
        await items.revive(Scene.from_ctx(ctx), gracz)

    @commands.command(name="DajPrzedmiot", brief="Give (or take back) a player's item (support).")
    @guards.owner_or_admin()
    async def give_item(self, ctx: commands.Context, member: discord.Member, *, what: str = "") -> None:
        name, number = parse_request(what)
        item = items.find(name)
        quiet = discord.AllowedMentions.none()
        if item is None:
            known = ", ".join(f"**{entry.key}** ({entry.name})" for entry in items.ITEMS)
            await io.send(ctx.channel, MSG_UNKNOWN.format(given=discord.utils.escape_markdown(name) or "?",
                                                          known=known), allowed_mentions=quiet)
            return
        if not await items.store_ready(self.bot, item):
            where = "kolumna SUMMON_SCROLLS tabeli PETOWNER" if item.in_petowner else f"tabela {items.TABLE}"
            await io.send(ctx.channel, MSG_NO_STORE.format(emoji=item.emoji, label=item.label, where=where),
                          allowed_mentions=quiet)
            return
        count = await items.adjust(self.bot, member, item, number)
        log.info("!DajPrzedmiot by %s (%s): %s %+d for %s (%s), now %s.", ctx.author,
                 getattr(ctx.author, "id", "?"), item.key, number, member, member.id, count)
        await io.send(ctx.channel, MSG_GIVEN.format(player_id=member.id, emoji=item.emoji, label=item.label,
                                                    count=count), allowed_mentions=quiet)
        await self.bot.log_to_owner(MSG_LOGGED.format(admin=ctx.author, name=item.name, delta=number,
                                                      player=member, player_id=member.id, count=count))


async def setup(bot) -> None:
    await bot.add_cog(ItemsCog(bot))
