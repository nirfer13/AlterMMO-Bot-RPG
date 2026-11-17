"""Skill gem commands: ``$pokazskill``, ``$uzyjskilla`` and the admin helpers.

``$pokazskill`` only displays something and works on every channel of the
server; ``$uzyjskilla`` is gameplay and works on the boss channel only.  The
game itself lives in :mod:`altermmo.features.skills`.
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

    # -- gameplay (boss channel only) ---------------------------------------------

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

    @commands.command(name="createSkillsDatabase",
                      brief="Create table SKILLS table with one default record.")
    @guards.owner_or_admin()
    async def create_skills_table(self, ctx: commands.Context) -> None:
        await skills.create_skills_table(self.bot)
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
