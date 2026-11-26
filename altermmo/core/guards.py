"""Command guards: where and when a command may run.

Legacy commands started with hand-written checks such as ``if BUSY == 0:`` and
silently did nothing otherwise.  During a 10-minute event the bot therefore
ignored nearly everything, which players could not tell apart from a crash.

Guards are ordinary ``commands.check`` decorators::

    @commands.command(name="polowanie")
    @guards.boss_channel_only()
    @guards.unless_busy()
    async def hunting(self, ctx): ...

    @commands.command(name="ranking")
    @guards.unless_busy(read_only=True)
    async def ranking(self, ctx): ...

What happens when the bot is busy is decided in one place by
``Settings.busy_policy`` (see :mod:`altermmo.settings`):

``"notify"``          everything is blocked while busy, with a short "event in
                      progress" notice                              (default)
``"read_only_free"``  display-only commands always work; the others answer with
                      the notice
``"silent"``          everything is blocked while busy, silently    (legacy)

Where commands work (owner's decision): gameplay commands only on the boss
channel (``boss_channel_only``), display-only commands on any channel of the
server, nothing at all in private messages (global check in ``AlterBot``).

Owner's rule: nothing may post into a running fight or make it stutter.  While
a typed fight is on a channel (``challenge.fight_on``) the busy notice is not
posted there (:func:`notify_busy`), and every new typed command carries
:func:`not_during_fight` - it says nothing at all then.
"""

from __future__ import annotations

import time

from discord.ext import commands

from . import challenge, io

BUSY_NOTICE = ("Trwa teraz inne wydarzenie – spróbuj ponownie za chwilę "
               "<:Bedge:970576892874854400>")

#: Do not repeat the busy notice on a channel more often than this (seconds).
_NOTICE_COOLDOWN = 20.0
_last_notice: dict[int, float] = {}


class SilentCheckFailure(commands.CheckFailure):
    """A guard said no and nothing should be sent to the user."""


def boss_channel_only():
    """Run only on the boss channel (production or test); silent elsewhere."""

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.bot.is_boss_channel(ctx.channel):
            return True
        raise SilentCheckFailure("not the boss channel")

    return commands.check(predicate)


def busy_notice_due(channel_id: int, *, note: bool = True) -> bool:
    """True when the busy notice may be posted on the channel now.

    One notice per :data:`_NOTICE_COOLDOWN` seconds per channel - shared by the
    typed commands (:func:`notify_busy`) and the button clicks
    (:func:`altermmo.core.ui.reply`), so players hammering both cannot fill
    the channel in the middle of an event.  ``note=True`` also records that
    the notice is being posted now; with ``note=False`` call
    :func:`note_busy_notice` once it really was.
    """
    if time.monotonic() - _last_notice.get(channel_id, 0.0) < _NOTICE_COOLDOWN:
        return False
    if note:
        note_busy_notice(channel_id)
    return True


def note_busy_notice(channel_id: int) -> None:
    """Record that the busy notice has just been posted on the channel."""
    _last_notice[channel_id] = time.monotonic()


async def notify_busy(ctx: commands.Context) -> None:
    """Tell the user the bot is busy (rate limited per channel, never raises).

    Silent while a typed fight is on the channel (``challenge.fight_on``): a
    notice between the fight's prompts is exactly the stutter the owner does
    not want (and it would use up Discord's budget of messages on the channel).
    The rate limit is not charged then.
    """
    if challenge.fight_on(getattr(ctx, "channel", None)):
        return
    if not busy_notice_due(ctx.channel.id):
        return
    try:
        await io.send(ctx.channel, BUSY_NOTICE, attempts=1)
    except Exception:  # noqa: BLE001 - a notice is never worth an error
        pass


def unless_busy(*, read_only: bool = False, stable: bool = False):
    """Block the command while an exclusive activity (event, hunt, tower...) runs.

    ``read_only=True`` marks commands that only display something (ranking, pet,
    stable...); under the default policy they are not blocked at all.
    ``stable=True`` marks the stable's commands: they also work while the
    busy holder keeps the stable open (``GameState.stable_open`` - the sign-up
    of the "Trening towarzyszy" event).
    """

    async def predicate(ctx: commands.Context) -> bool:
        bot = ctx.bot
        if not bot.state.busy:
            return True
        if stable and bot.state.stable_open:
            return True
        policy = bot.settings.busy_policy
        if read_only and policy == "read_only_free":
            return True
        if policy != "silent":
            await notify_busy(ctx)
        raise SilentCheckFailure(f"busy with {bot.state.busy_owner}")

    return commands.check(predicate)


def not_during_fight():
    """Say nothing and do nothing while a typed fight is on the command's channel (``challenge.fight_on``).

    For every new typed command (owner's rule: nothing may post into a running
    fight).  A boss fight holds no busy flag, so :func:`unless_busy` alone lets
    such a command answer between the fight's prompts.
    """

    async def predicate(ctx: commands.Context) -> bool:
        if challenge.fight_on(ctx.channel):
            raise SilentCheckFailure("a typed fight is on the channel")
        return True

    return commands.check(predicate)


def owner_or_admin():
    """Administrator permission (the legacy ``has_permissions(administrator=True)``)."""
    return commands.has_permissions(administrator=True)
