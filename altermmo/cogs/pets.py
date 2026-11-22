"""Pet (companion) commands: ``$towarzysz``, ``$odrodzenie``, ``$stajnia`` ...

Display-only commands (``$towarzysz``, ``$stajnia``, ``$rankingtowarzyszy``)
work on every channel of the server; everything that changes a pet works on
the boss channel only.  The game itself lives in :mod:`altermmo.features.pets`,
including the buttons under a shown pet and the stable (their clicks are
answered by the views themselves, nothing to register here).

``$nazwij`` takes the whole rest of the message as the name
(``$nazwij Sir Burek``); quotes around it are still accepted and dropped.

``$DajPieczec <player_id> <0-3> [tak/nie/częściowa]`` (administrators) sets the
seal and the foil of a member's active pet (``tak`` full Holo, ``częściowa``
partial Holo, ``nie`` none, left out: unchanged), ``$DajRamke <player_id> <id|baza>``
its frame result (``S1``, ``R9-A`` ... of the pet's own class; ``baza`` = the
base frame of the class): the owner's way to look at the cards and to put a
mistake right - players get their cosmetics only from the rolls of a new egg.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from ..core import guards, io, ui
from ..core.scene import Scene
from ..features import bossdetect, patrons, petcards, pets

log = logging.getLogger(__name__)


MSG_NAME_MISSING = "Po spacji podaj imię towarzysza np. **$nazwij Burek**."

# -- $DajPieczec (administrators) -------------------------------------------------------
MSG_SEAL_SET = ("Towarzysz gracza <@{player_id}> (PET_ID {pet_id}) ma teraz: pieczęć - **{seal}**, "
                "folia Holo - **{holo}**. Widać to na karcie towarzysza (**$towarzysz**).")
MSG_SEAL_RANGE = "Pieczęć: 0-3 (0 = brak, {names})."
MSG_SEAL_HOLO = ("Folia Holo: **tak** (pełna), **częściowa** albo **nie** (pominięta - bez zmian), np. "
                 "**$DajPieczec {player_id} 3 tak**.")
MSG_SEAL_NO_PET = "<@{player_id}> nie ma przy sobie towarzysza - nie ma komu dać pieczęci."
MSG_SEAL_NO_COLUMNS = ("W tabeli PETS nie ma kolumn SEAL i HOLO (nie udało się ich dodać przy starcie bota), "
                       "więc kosmetyka towarzyszy jest wyłączona. Uruchom bota ponownie - kolumny dodaje "
                       "sam przy starcie.")
#: Added to the answer when the player is not a supporter right now.
MSG_SEAL_NOT_SHOWN = (" Uwaga: ten gracz nie ma teraz rangi wspierającego, więc pieczęci i folii nie widać - "
                      "pojawią się, gdy ją zdobędzie.")
SEAL_NONE = "brak"
#: How ``MSG_SEAL_SET`` names the foil (``petcards.FOIL_*``).
FOIL_ANSWERS = {petcards.FOIL_NONE: "nie", petcards.FOIL_PARTIAL: "częściowa", petcards.FOIL_FULL: "tak"}

# -- $DajRamke (administrators) ---------------------------------------------------------
MSG_FRAME_SET = ("Towarzysz gracza <@{player_id}> (PET_ID {pet_id}) ma teraz ramkę **{frame}**. "
                 "Widać to na karcie towarzysza (**$towarzysz**).")
#: How the answer names the base frame of a class.
FRAME_BASE = "{name} (podstawowa)"
MSG_FRAME_BAD = ("Ramki **{given}** nie ma w puli tego towarzysza (klasa: {tier}). Możliwe: {ids} - albo "
                 "**baza** (podstawowa ramka klasy), np. **$DajRamke {player_id} {example}**.")
MSG_FRAME_NO_TIER = ("Towarzysz gracza <@{player_id}> (PET_ID {pet_id}) jest zwykły - ramki mają tylko "
                     "towarzysze Shiny, Ultra Shiny i mityczni.")
MSG_FRAME_NO_PET = "<@{player_id}> nie ma przy sobie towarzysza - nie ma komu dać ramki."
MSG_FRAME_NO_COLUMNS = ("W tabeli PETS nie ma kolumn FRAME i HOLO_PARTIAL (nie udało się ich dodać przy starcie "
                        "bota), więc ramek z puli i częściowej folii Holo nie da się ustawić. Pieczęcie i pełna "
                        "folia Holo działają. Uruchom bota ponownie - kolumny dodaje sam przy starcie.")
MSG_FRAME_NOT_SHOWN = (" Uwaga: ten gracz nie ma teraz rangi wspierającego, więc tej ramki nie widać (karta ma "
                       "podstawową ramkę klasy) - pojawi się, gdy ją zdobędzie.")
#: Class names in the answers of ``$DajRamke``: labels (as in the owner's pule.json), shown after
#: "klasa:" so that no word has to agree with the feminine "klasa".
TIER_NAMES = {"S": "Shiny", "U": "Ultra Shiny", "M": "Mityczny"}

#: What ``$DajPieczec`` takes for the foil (True / False: full Holo / none, as always).
_YES_NO = {"tak": True, "t": True, "yes": True, "y": True, "true": True, "1": True, "holo": True,
           "pełna": True, "pelna": True, "pełne": True, "pelne": True, "full": True,
           "nie": False, "n": False, "no": False, "false": False, "0": False, "brak": False,
           "częściowa": petcards.FOIL_PARTIAL, "czesciowa": petcards.FOIL_PARTIAL,
           "częściowe": petcards.FOIL_PARTIAL, "czesciowe": petcards.FOIL_PARTIAL,
           "partial": petcards.FOIL_PARTIAL}

#: Opening -> closing quote pairs accepted around a name given to ``$nazwij``.
_QUOTES = {'"': '"', "„": "”", "“": "”", "”": "”", "«": "»", "‘": "’", "'": "'"}


def _unquote(text: str) -> str:
    """``"Sir Burek"`` -> ``Sir Burek`` (only one pair of quotes around the whole text)."""
    text = text.strip()
    if len(text) >= 2 and _QUOTES.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return text


def _frame_name(frame: petcards.Frame) -> str:
    """``Gwiazda Szronu (animowana)`` / ``Srebrne okucia (podstawowa)``."""
    return FRAME_BASE.format(name=frame.name) if frame.base else frame.label


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
        # Buttons under "unlock your private messages" hints and under the private
        # "a boss is coming" warnings posted before a restart.
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

    @commands.command(name="DajPieczec",
                      brief="Set the seal (0-3) and the foil (tak/nie/częściowa) of a member's active pet "
                            "(admin).")
    @guards.owner_or_admin()
    async def give_seal(self, ctx: commands.Context, player_id: int, seal: int,
                        holo: str | None = None) -> None:
        # The foil left out: only the seal changes.
        wanted = None if holo is None else _YES_NO.get(holo.strip().lower())
        if holo is not None and wanted is None:
            await io.send(ctx.channel, MSG_SEAL_HOLO.format(player_id=player_id))
            return
        status, now = await pets.set_cosmetics(self.bot, player_id, seal, wanted)
        if status == "bad_seal":
            await io.send(ctx.channel, MSG_SEAL_RANGE.format(names=", ".join(
                f"{tier} = {name}" for tier, name in sorted(petcards.SEAL_NAMES.items()))))
        elif status == "no_columns":
            await io.send(ctx.channel, MSG_SEAL_NO_COLUMNS)
        elif status == "no_frame_columns":
            await io.send(ctx.channel, MSG_FRAME_NO_COLUMNS)
        elif now is None:
            await io.send(ctx.channel, MSG_SEAL_NO_PET.format(player_id=player_id))
        else:
            text = MSG_SEAL_SET.format(
                player_id=player_id, pet_id=now["pet_id"],
                seal=petcards.SEAL_NAMES.get(now["seal"], SEAL_NONE), holo=FOIL_ANSWERS[now["foil"]])
            # Cosmetics are shown only while the pet's owner supports: say so, or the owner
            # of the server looks for a seal that is stored but hidden.
            if (now["seal"] or now["foil"]) and not patrons.check_if_patron(await self.bot.member(player_id)):
                text += MSG_SEAL_NOT_SHOWN
            await io.send(ctx.channel, text)

    @commands.command(name="DajRamke", aliases=["DajRamkę"],
                      brief="Set the frame (an id of the pet's class or 'baza') of a member's active pet (admin).")
    @guards.owner_or_admin()
    async def give_frame(self, ctx: commands.Context, player_id: int, frame: str) -> None:
        status, now = await pets.set_frame(self.bot, player_id, frame)
        if status == "no_frame_columns":
            await io.send(ctx.channel, MSG_FRAME_NO_COLUMNS)
        elif status == "alone":
            await io.send(ctx.channel, MSG_FRAME_NO_PET.format(player_id=player_id))
        elif status == "no_tier":
            await io.send(ctx.channel, MSG_FRAME_NO_TIER.format(player_id=player_id, pet_id=now["pet_id"]))
        elif status == "bad_frame":
            pool = petcards.POOLS[now["tier"]]
            await io.send(ctx.channel, MSG_FRAME_BAD.format(
                given=discord.utils.escape_markdown(frame[:32]), tier=TIER_NAMES.get(now["tier"], now["tier"]),
                ids=", ".join(f"**{item.id}** ({_frame_name(item)})" for item in pool),
                player_id=player_id, example=pool[-1].id))
        else:
            chosen = petcards.FRAMES[now["frame"] or petcards.BASE_FRAME[now["tier"]]]
            text = MSG_FRAME_SET.format(player_id=player_id, pet_id=now["pet_id"], frame=_frame_name(chosen))
            # As with the seal: a rarer frame is shown only while the pet's owner supports.
            if now["frame"] and not patrons.check_if_patron(await self.bot.member(player_id)):
                text += MSG_FRAME_NOT_SHOWN
            await io.send(ctx.channel, text)

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
