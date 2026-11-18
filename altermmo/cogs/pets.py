"""Pet (companion) commands: ``$towarzysz``, ``$odrodzenie``, ``$stajnia`` ...

Display-only commands (``$towarzysz``, ``$stajnia``, ``$rankingtowarzyszy``)
work on every channel of the server; everything that changes a pet works on
the boss channel only.  The game itself lives in :mod:`altermmo.features.pets`,
including the buttons under a shown pet and the stable (their clicks are
answered by the views themselves, nothing to register here).

``$nazwij`` takes the whole rest of the message as the name
(``$nazwij Sir Burek``); quotes around it are still accepted and dropped.
"""

from __future__ import annotations

import asyncio
import logging

from discord.ext import commands

from ..core import guards, io, ui
from ..core.scene import Scene
from ..features import bossdetect, pets

log = logging.getLogger(__name__)


MSG_NAME_MISSING = "Po spacji podaj imię towarzysza np. **$nazwij Burek**."

#: Opening -> closing quote pairs accepted around a name given to ``$nazwij``.
_QUOTES = {'"': '"', "„": "”", "“": "”", "”": "”", "«": "»", "‘": "’", "'": "'"}


def _unquote(text: str) -> str:
    """``"Sir Burek"`` -> ``Sir Burek`` (only one pair of quotes around the whole text)."""
    text = text.strip()
    if len(text) >= 2 and _QUOTES.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return text


async def _say(ctx: commands.Context, text: str) -> None:
    """Answer from an error handler; a failure here is only logged."""
    try:
        await io.send(ctx.channel, text)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.debug("Could not send an error reply.", exc_info=True)


class PetsCog(commands.Cog, name="Pets"):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Buttons under "unlock your private messages" hints posted before a restart.
        bossdetect.register(self.bot)

    # -- display only (any channel) -----------------------------------------------

    @commands.command(name="wyczuwanie", aliases=["powiadomienia", "powiadomieniaboss"],
                      brief="Switch the boss warnings of the pet off / on.")
    @guards.unless_busy(read_only=True)
    async def toggle_boss_warnings(self, ctx: commands.Context) -> None:
        await bossdetect.toggle(Scene.from_ctx(ctx))

    @commands.command(name="towarzysz", aliases=['t'], brief="Shows author's pet.")
    @guards.unless_busy(read_only=True)
    async def show_pet(self, ctx: commands.Context) -> None:
        await pets.show_pet(Scene.from_ctx(ctx), ctx.author)

    @commands.command(name="stajnia", aliases=["s"], brief="Show player's stable.")
    @guards.unless_busy(read_only=True)
    async def check_stable(self, ctx: commands.Context) -> None:
        await pets.check_stable(Scene.from_ctx(ctx))

    @commands.command(name="rankingtowarzyszy", aliases=['towarzysze', 'rt'],
                      brief="Shows pets ranking.")
    @guards.unless_busy(read_only=True)
    async def pet_ranking(self, ctx: commands.Context) -> None:
        await pets.pet_ranking(Scene.from_ctx(ctx))

    # -- gameplay (boss channel only) ---------------------------------------------

    @commands.command(name="porzucam", brief="Discards author's pet.")
    @guards.boss_channel_only()
    @guards.unless_busy()
    async def discard_pet(self, ctx: commands.Context) -> None:
        await pets.discard_pet(Scene.from_ctx(ctx))

    @commands.command(name="odrodzenie", aliases=['odr', 'o'], brief="Reroll the pet.")
    @guards.boss_channel_only()
    @guards.unless_busy()
    async def reroll_pet(self, ctx: commands.Context) -> None:
        await pets.reroll_pet(Scene.from_ctx(ctx), ctx.author)

    @commands.command(name="mocneodrodzenie", aliases=["odrodzenie2", "odr2", "mocneodr", "mo"],
                      brief="Advanced reroll the pet.")
    @guards.boss_channel_only()
    @guards.unless_busy()
    async def adv_reroll_pet(self, ctx: commands.Context) -> None:
        state = ctx.bot.state
        if state.busy:  # somebody took the channel between the guard and here
            await guards.notify_busy(ctx)
            return
        with state.hold_busy("adv-reroll"):
            await pets.adv_reroll_pet(Scene.from_ctx(ctx))

    @commands.command(name="oswiecenie", aliases=['oświecenie'], brief="Enlight the pet.")
    @guards.boss_channel_only()
    @guards.unless_busy()
    async def enlight_pet(self, ctx: commands.Context) -> None:
        await pets.enlight_pet(Scene.from_ctx(ctx), ctx.author)

    @commands.command(name="transformacja", aliases=['transform'], brief="Transform the pet.")
    @guards.boss_channel_only()
    @guards.unless_busy()
    async def transform_pet(self, ctx: commands.Context) -> None:
        await pets.transform_pet(Scene.from_ctx(ctx), ctx.author)

    @commands.command(name="schowajtowarzysza",
                      aliases=["schowaj", "st"], brief="Stores author's pet.")
    @guards.boss_channel_only()
    @guards.unless_busy()
    async def store_pet(self, ctx: commands.Context, slot: str) -> None:
        await pets.store_pet(Scene.from_ctx(ctx), slot)

    @store_pet.error
    async def storepet_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await _say(ctx, "Po spacji podaj numer miejsca w stajni, do którego chcesz schować " +
                            "towarzysza (1-4) np. **$schowajtowarzysza 1**.")
        else:
            await ctx.bot.on_command_error(ctx, error, force=True)

    @commands.command(name="wyciagnijtowarzysza",
                      aliases=['wyciągnijtowarzysza', "wyciągnij", "wyciagnij", "wt"],
                      brief="Takes a pet out of the stable.")
    @guards.boss_channel_only()
    @guards.unless_busy()
    async def unstore_pet(self, ctx: commands.Context, slot: str) -> None:
        await pets.unstore_pet(Scene.from_ctx(ctx), slot)

    @unstore_pet.error
    async def unstorepet_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await _say(ctx, "Po spacji podaj numer miejsca w stajni, z którego chcesz wyjąć " +
                            "towarzysza (1-4) np. **$wyciagnijtowarzysza 1**.")
        else:
            await ctx.bot.on_command_error(ctx, error, force=True)

    @commands.command(name="nazwij", aliases=['n'], brief="Set the name of author's pet.")
    @commands.cooldown(1, 60*60*23, commands.BucketType.user)
    @guards.boss_channel_only()
    @guards.unless_busy()
    async def name_pet(self, ctx: commands.Context, *, name: str) -> None:
        # The whole rest of the message is the name ("$nazwij Sir Burek"), one
        # pair of quotes around it (the old way to give a long name) is dropped.
        name = _unquote(name)
        if not name:  # only quotes were given
            ctx.command.reset_cooldown(ctx)
            await _say(ctx, MSG_NAME_MISSING)
            return
        if not await pets.name_pet(Scene.from_ctx(ctx), name):
            ctx.command.reset_cooldown(ctx)  # nothing was named: the 23 hours do not start

    @name_pet.error
    async def name_pet_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            # The same text as the "Zmień imię" button of the pet panel (shares this cooldown).
            await _say(ctx, pets.MSG_RENAME_COOLDOWN.format(when=ui.when(error.retry_after)))
            return
        if not isinstance(error, commands.CheckFailure):
            # The cooldown was taken just before the arguments were read / the
            # command ran; it failed (no name given, database down), so give it back.
            # A failed check never touched the cooldown and must not clear it.
            ctx.command.reset_cooldown(ctx)
        await ctx.bot.on_command_error(ctx, error, force=True)

    # -- administrators -----------------------------------------------------------

    @commands.command(name="PetLvlUp", brief="Level up pet of the player id.")
    @guards.owner_or_admin()
    async def level_up_pet(self, ctx: commands.Context, player_id: int) -> None:
        done = await pets.level_up_pet(Scene.from_ctx(ctx), player_id)
        await io.safe_react(ctx.message, "✅" if done else "❌")

    @commands.command(name="AddScroll")
    @guards.owner_or_admin()
    async def add_scroll(self, ctx: commands.Context, number: int, player_id: int) -> None:
        await pets.assign_scroll(Scene.from_ctx(ctx), number, player_id)
        await io.safe_react(ctx.message, "✅")

    @commands.command(name="AddShard")
    @guards.owner_or_admin()
    async def add_shard(self, ctx: commands.Context, number: int, player_id: int) -> None:
        await pets.assign_shard(Scene.from_ctx(ctx), number, player_id)
        await io.safe_react(ctx.message, "✅")

    @commands.command(name="GenerateEgg", brief="Generates egg and print its data.")
    @guards.owner_or_admin()
    async def generate_egg(self, ctx: commands.Context) -> None:
        pet_id = await pets.generate_pet_egg(Scene.from_ctx(ctx))
        await io.send(ctx.channel, f"Wygenerowano jajko (PET_ID {pet_id}).")

    @commands.command(name="createPetOwnersDatabase",
                      brief="Create table PETOWNER table with one default record.")
    @guards.owner_or_admin()
    async def create_pet_owners_table(self, ctx: commands.Context) -> None:
        await pets.create_pet_owners_table(self.bot)
        await io.send(ctx.channel, "Baza danych PETOWNERS utworzona.")

    @commands.command(name="createPetsDatabase",
                      brief="Create table PETS table with one default record.")
    @guards.owner_or_admin()
    async def create_pets_table(self, ctx: commands.Context) -> None:
        await pets.create_pets_table(self.bot)
        await io.send(ctx.channel, "Baza danych PETS utworzona.")

    @commands.command(name="ReassignPet",
                      brief="Assign pet_id to player_id.")
    @guards.owner_or_admin()
    async def reassign_pet(self, ctx: commands.Context, pet_id: int, player_id: int) -> None:
        if await pets.reassign_pet(self.bot, pet_id, player_id):
            await io.send(ctx.channel, f"Pet {pet_id} przypisany do gracza <@{player_id}>.")
        else:
            await io.send(ctx.channel, f"Pet {pet_id} nie został przypisany: gracz <@{player_id}> ma już "
                                       "towarzysza albo taki pet nie istnieje.")


async def setup(bot) -> None:
    await bot.add_cog(PetsCog(bot))
