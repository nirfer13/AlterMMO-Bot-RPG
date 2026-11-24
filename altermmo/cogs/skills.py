"""Skill gem commands: ``!pokazskill``, ``!klejnoty``, ``!wybierzklejnot``, ``!uzyjskilla``, admin helpers.

``!pokazskill`` and ``!klejnoty`` only display something and work on every
channel of the server; ``!wybierzklejnot`` and ``!uzyjskilla`` are gameplay and
work on the boss channel only.  The menu under the ``!klejnoty`` list does what
``!wybierzklejnot`` does, under the same rules (it answers a choice made on
another channel privately).  The game itself lives in
:mod:`altermmo.features.skills`.
"""

from __future__ import annotations

import logging

from discord.ext import commands

from ..core import guards, io
from ..core.scene import Scene
from ..features import modifiers, skills

log = logging.getLogger(__name__)


class SkillsCog(commands.Cog, name="Skills"):
    def __init__(self, bot) -> None:
        self.bot = bot

    # -- display only (any channel) -----------------------------------------------

    @commands.command(name="pokazskill",
                      aliases=['pokazumiejetnosc', 'pokażumiejętność', 'pu', 'pokazskilla', 'ps'],
                      brief="Show user's skill.")
    @guards.unless_busy(read_only=True)
    async def show_skill(self, ctx: commands.Context) -> None:
        await skills.show_skill(Scene.from_ctx(ctx))

    # ("klejnoty", not "skille": one letter away from ``!skill``, which USES the gem.)
    @commands.command(name="klejnoty",
                      aliases=["sakwa", "skille", "umiejetnosci", "umiejętności"],
                      brief="Show all skill gems of the user.")
    @guards.unless_busy(read_only=True)
    async def show_skills(self, ctx: commands.Context) -> None:
        await skills.show_skills(Scene.from_ctx(ctx))

    # -- gameplay (boss channel only) ---------------------------------------------

    @commands.command(name="wybierzklejnot",
                      aliases=["wybierzskill", "wybierzumiejetnosc", "wybierzumiejętność", "ws"],
                      brief="Make another skill gem of the user the active one.")
    @guards.boss_channel_only()
    @guards.unless_busy()
    async def choose_skill(self, ctx: commands.Context, number: str) -> None:
        await skills.choose_skill(Scene.from_ctx(ctx), number)

    @choose_skill.error
    async def choose_skill_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            try:
                await io.send(ctx.channel, "Po spacji podaj numer klejnotu z listy **!klejnoty**, "
                                           "np. **!wybierzklejnot 2**.")
            except Exception:  # noqa: BLE001 - a hint is never worth an error
                log.debug("Could not send the !wybierzklejnot hint.", exc_info=True)
        else:
            await ctx.bot.on_command_error(ctx, error, force=True)

    @commands.command(name="uzyjskilla",
                      aliases=['użyjumiejetnosc', 'użyjumiejętność', 'us',
                               'uzyj', 'uzyjskill', 'uu', 'skill'],
                      brief="Use user's skill.")
    @guards.boss_channel_only()
    @guards.unless_busy()
    async def use_skill(self, ctx: commands.Context) -> None:
        state = ctx.bot.state
        if state.busy:  # somebody took the channel between the guard and here
            await guards.notify_busy(ctx)
            return
        scene = Scene.from_ctx(ctx)
        with state.hold_busy("skill"):
            skill_type = await skills.use_skill(scene, ctx.author)
        if skill_type == skills.SUMMON:
            # Outside the busy block: the summoned boss takes over the channel itself.
            await skills.skill_summon_boss(scene)

    # -- administrators -----------------------------------------------------------

    # command to debug
    @commands.command(name="ModifiersLoad", brief="Load modifiers and prints values.")
    @guards.owner_or_admin()
    async def load_modifiers(self, ctx: commands.Context) -> None:
        await modifiers.load_modifiers(self.bot)
        await io.send(ctx.channel, "Modifiers file loaded.")

    # command to debug
    @commands.command(name="GenerateSkill", brief="Generates skill and print its data.")
    @guards.owner_or_admin()
    async def generate_skill(self, ctx: commands.Context) -> None:
        skill_id = await skills.generate_skill_gem(Scene.from_ctx(ctx))
        await io.send(ctx.channel, f"Wygenerowano klejnot umiejętności (SKILL_ID {skill_id}).")

    @commands.command(name="DajKlejnot", brief="Give a member a skill gem (admin); optional kind 1-6.")
    @guards.owner_or_admin()
    async def give_gem(self, ctx: commands.Context, player_id: int, variant: int | None = None) -> None:
        if variant is not None and variant not in skills.GEM_NAMES:
            await io.send(ctx.channel, "Rodzaj klejnotu: 1-6 (" + ", ".join(
                f"{kind} = {name}" for kind, name in skills.GEM_NAMES.items()) + ").")
            return
        if await skills.assign_skill(Scene.from_ctx(ctx), player_id, variant):
            await io.send(ctx.channel, f"<@{player_id}> dostaje nowy klejnot umiejętności (**!klejnoty**).")
        else:
            await io.send(ctx.channel, f"<@{player_id}> ma już taki klejnot (albo wszystkie, które mogą wypaść).")

    @commands.command(name="createSkillsDatabase",
                      brief="Create table SKILLS table with one default record.")
    @guards.owner_or_admin()
    async def create_skills_table(self, ctx: commands.Context) -> None:
        await skills.create_skills_table(self.bot)
        await self.bot.db.execute(skills.SCHEMA[1], idempotent=True)
        await io.send(ctx.channel, "Baza danych SKILLS utworzona.")

    @commands.command(name="skill_rest", brief="Show rest image")
    @guards.owner_or_admin()
    @guards.unless_busy()
    async def rest_image(self, ctx: commands.Context) -> None:
        state = ctx.bot.state
        if state.busy:  # somebody took the channel between the guard and here
            await guards.notify_busy(ctx)
            return
        with state.hold_busy("skill"):
            await skills.skill_rest(Scene.from_ctx(ctx))


async def setup(bot) -> None:
    await bot.add_cog(SkillsCog(bot))
