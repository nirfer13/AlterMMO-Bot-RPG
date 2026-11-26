"""features/events.py: the random events of the boss channel, played through fake Discord."""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import discord
import pytest

from altermmo.core import challenge, timing, ui
from altermmo.core.db import DatabaseUnavailable
from altermmo.core.state import EventType
from altermmo.features import dova, events, items, patrons


# -- helpers -------------------------------------------------------------------------

def clicks_on(game, fragment: str, *clicks) -> list[asyncio.Task]:
    """When the bot posts a panel containing ``fragment``: ``(member, key)`` click it, in this order.

    Returns the list the click task is appended to; awaiting it gives the interactions.
    """
    tasks: list[asyncio.Task] = []

    def hook(message) -> None:
        if fragment not in message.text or message.view is None:
            return

        async def run():
            return [await game.click(message, member, key) for member, key in clicks]

        task = asyncio.ensure_future(run())
        game._tasks.append(task)
        tasks.append(task)

    game.channel.send_hooks.append(hook)
    return tasks


def join_on(game, fragment: str, *members) -> list[asyncio.Task]:
    """``members`` click "Dołącz" (or the panel's only button) as soon as the panel appears."""
    return clicks_on(game, fragment, *[(member, "join") for member in members])


def no_listeners(game) -> bool:
    return not game.bot.extra_events.get("on_reaction_add") and not game.bot.extra_events.get("on_message")


def no_live_buttons(game) -> bool:
    """Every flow is over: no message listens to its buttons and none carries them."""
    return all(not ui.is_live(message.id) and (message.view is None or message.view.is_finished())
               for message in game.channel.sent)


def files(game) -> list[str]:
    return [name for message in game.channel.sent for name in message.files]


def panel(game, fragment: str):
    return next(message for message in game.channel.sent if fragment in message.text)


@pytest.fixture
def quick(monkeypatch):
    """Waiting times 100x shorter (600 s -> 6 s, debug 5 s -> 0.05 s, 15 s -> 0.15 s)."""
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.01)


@pytest.fixture
def loot(monkeypatch):
    """Record ``daily.rand_loot`` calls instead of granting loot."""
    calls: list[tuple] = []

    async def rand_loot(scene, rarity, hunter, boost_percent):
        calls.append((rarity, hunter, boost_percent))
        return ""

    monkeypatch.setattr(events.daily, "rand_loot", rand_loot)
    return calls


@pytest.fixture
def loot_sources(loot, monkeypatch):
    """Where each recorded ``daily.rand_loot`` call draws from (``items.LOOT_SOURCE``; None: the world)."""
    sources: list = []
    recorded = events.daily.rand_loot

    async def rand_loot(scene, rarity, hunter, boost_percent):
        sources.append(items.LOOT_SOURCE.get())
        return await recorded(scene, rarity, hunter, boost_percent)

    monkeypatch.setattr(events.daily, "rand_loot", rand_loot)
    return sources


def low_rolls(monkeypatch) -> None:
    monkeypatch.setattr(random, "randint", lambda low, high: low)


def high_rolls(monkeypatch) -> None:
    monkeypatch.setattr(random, "randint", lambda low, high: high)


def first_choice(monkeypatch) -> None:
    monkeypatch.setattr(random, "choice", lambda pool: list(pool)[0])


async def give_scrolls(game, member, scrolls) -> None:
    await game.bot.db.execute(
        "INSERT INTO PETOWNER (PLAYER_ID, PET_ID, PET_OWNED, REROLL_SCROLL) VALUES ($1, 0, FALSE, $2)",
        member.id, scrolls)


async def scrolls_of(game, member):
    value = await game.bot.db.fetchval("SELECT REROLL_SCROLL FROM PETOWNER WHERE PLAYER_ID = $1", member.id)
    return None if value is None else int(value)


def not_found() -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")


# -- chest ---------------------------------------------------------------------------

async def test_spawn_chest_posts_picture_and_embed(game, monkeypatch):
    high_rolls(monkeypatch)
    chest = await events.spawn_chest(game.scene())
    assert files(game) == ["4.png"]
    assert game.channel.contains("Skrzynia ze skarbami!")
    assert game.channel.contains("**!skrzynia**")
    assert chest is game.channel.messages[-1] and chest.view is None   # the button comes from the cog


@pytest.mark.parametrize("roll, rarity", [(89, 1), (90, 2)])
async def test_open_chest_gives_loot_to_the_author(game, loot, monkeypatch, roll, rarity):
    rolls = iter([roll, 17])
    monkeypatch.setattr(random, "randint", lambda low, high: next(rolls))
    player = game.add_member("Ala")
    await events.open_chest(game.scene(player, "!skrzynia"))
    assert loot == [(rarity, player, 17)]


async def test_open_chest_with_real_loot(game_db, monkeypatch):
    monkeypatch.setattr(random, "random", lambda: 1.0)
    player = game_db.add_member("O'Brien")
    await events.open_chest(game_db.scene(player, "!skrzynia"))
    assert game_db.channel.contains("Drop - O'Brien")


# -- invasion ------------------------------------------------------------------------

async def test_invasion_repelled_everybody_gets_loot(game, loot, quick, monkeypatch):
    low_rolls(monkeypatch)  # picture 0, chance 0, rarity roll 0 -> table 1
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    stranger = SimpleNamespace(id=5, bot=False, name="Obcy", roles=[])   # not on the server
    clicked = clicks_on(game, "Inwazja na wioskę!", (ala, "join"), (game.bot.user, "join"), (ola, "join"),
                        (ala, "join"), (stranger, "join"))
    await events.spawn_invasion(game.scene())
    assert loot == [(1, ala, 20), (1, ola, 20)]
    assert not game.channel.contains("nie udało się obronić")
    second_click = (await clicked[0])[3]
    assert second_click.public == [f"<@{ala.id}> {events.MSG_ALREADY_SIGNED}"]   # on the channel, to Ala
    assert game.channel.messages.index(panel(game, "Inwazja na wioskę!")) < game.channel.messages.index(
        second_click.replies[0].message)
    assert no_listeners(game) and no_live_buttons(game)


async def test_invasion_lost(game, loot, quick, monkeypatch):
    high_rolls(monkeypatch)  # chance 100 > 1 * 10 + 10
    ala = game.add_member("Ala")
    join_on(game, "Inwazja na wioskę!", ala)
    await events.spawn_invasion(game.scene())
    assert loot == []
    assert game.channel.contains("Niestety nie udało się obronić wioski")
    assert files(game)[-1] == "5.png"


async def test_invasion_without_defenders_is_always_lost(game, loot, quick, monkeypatch):
    low_rolls(monkeypatch)  # chance 0: the legacy bot "won" silently with nobody
    await events.spawn_invasion(game.scene())
    assert loot == []
    assert game.channel.contains("Niestety nie udało się obronić wioski")


async def test_invasion_message_deleted_ends_quietly(game, loot, quick, monkeypatch):
    low_rolls(monkeypatch)
    ala = game.add_member("Ala")
    join_on(game, "Inwazja na wioskę!", ala)

    async def fetch_message(message_id):
        raise not_found()

    monkeypatch.setattr(game.channel, "fetch_message", fetch_message)
    await events.spawn_invasion(game.scene())
    assert loot == []
    assert not game.channel.contains("nie udało się obronić")
    assert no_listeners(game) and no_live_buttons(game)


async def test_invasion_survives_a_failed_fetch(game, loot, quick, monkeypatch):
    low_rolls(monkeypatch)
    ala = game.add_member("Ala")
    join_on(game, "Inwazja na wioskę!", ala)

    async def fetch_message(message_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(game.channel, "fetch_message", fetch_message)
    await events.spawn_invasion(game.scene())
    assert [call[1] for call in loot] == [ala]


# -- party ---------------------------------------------------------------------------

def toast_answers(game, *answers) -> None:
    """Every time the bot posts a toast, ``(member, text, delay)`` answers are written."""
    def hook(message) -> None:
        if message.content.startswith('"**'):
            for member, text, delay in answers:
                game.say_later(member, text, delay)
    game.channel.send_hooks.append(hook)


TOAST = events.TOASTS[0]


async def test_party_without_patrons_is_skipped(game, loot):
    await events.spawn_party(game.scene())
    assert game.channel.sent == []


async def test_party_with_too_few_guests(game, loot, quick, monkeypatch):
    first_choice(monkeypatch)
    patron = game.add_member("Mecenas", roles=[patrons.CRAFTER_ROLE])
    ala = game.add_member("Ala")
    clicked = clicks_on(game, "Zdrowie Mecenas!", (ala, "join"), (game.bot.user, "join"), (ala, "join"))
    await events.spawn_party(game.scene())
    assert (await clicked[0])[2].answers == [events.MSG_ALREADY_SIGNED]   # a second click counts once
    assert game.channel.contains("Pozostało napić się w samotności. Zdrowie Mecenas!")
    assert game.channel.sent[0].content == patron.display_avatar.url
    assert files(game) == ["0.png"]
    assert loot == []


@pytest.mark.parametrize("role_id", [patrons.TWITCH_SUB_ROLE, *patrons.YOUTUBE_ROLES])
async def test_party_honours_a_twitch_subscriber_or_a_youtube_supporter(game, loot, quick, monkeypatch, role_id):
    """Since 2026-11-20 the party is for every supporter; its invitation says "wspierający"."""
    first_choice(monkeypatch)
    supporter = game.add_member("Widz", roles=[role_id])
    game.add_member("VIP", roles=[964845150213906442])                  # a Twitch VIP is no supporter
    await events.spawn_party(game.scene())
    assert game.channel.sent[0].content == supporter.display_avatar.url
    invitation = panel(game, "Zdrowie Widz!")
    assert "za zdrowie jednego z naszych wspierających?" in invitation.embeds[0].description
    assert game.channel.contains("Pozostało napić się w samotności. Zdrowie Widz!")


def test_chest_and_invasion_promise_cosmetics_to_supporters():
    hint = "\n\n*Wspierający mogą wydropić dodatkowe, kosmetyczne przedmioty!*"
    assert events.CHEST_DESCR.endswith(hint) and events.INVASION_DESCR.endswith(hint)


async def test_party_full_flow(game, loot, quick, monkeypatch):
    first_choice(monkeypatch)
    high_rolls(monkeypatch)
    game.add_member("Mecenas", roles=[patrons.CRAFTER_ROLE])
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    join_on(game, "Zdrowie Mecenas!", ala, ola)
    # Ola is wrong first, somebody chats on another channel, then Ala gets it right (case-insensitive).
    toast_answers(game, (ola, "na zdrowie", 0), (ala, TOAST.upper(), 0.01))
    await events.spawn_party(game.scene())
    assert game.channel.count("Brawo Ala!") == events.PARTY_ROUNDS
    assert game.channel.contains("**KOLEJKA 8!**")
    assert game.channel.contains('"**' + " ".join(TOAST.upper()) + '**"')
    assert game.channel.contains("Ranking pijaków!")
    assert game.channel.contains(f"Graulasje <@{ala.id}>")
    assert game.channel.contains(f"1. <@{ala.id}>: 8")
    assert loot == [(2, ala, 50)]
    assert no_listeners(game) and no_live_buttons(game)


async def test_party_nobody_types_a_toast(game, loot, quick, monkeypatch):
    first_choice(monkeypatch)
    game.add_member("Mecenas", roles=[patrons.CRAFTER_ROLE])
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    join_on(game, "Zdrowie Mecenas!", ala, ola)
    await events.spawn_party(game.scene())  # legacy: max() of an empty ranking crashed here
    assert game.channel.count("Pfff... Miernoty.") == events.PARTY_ROUNDS
    assert not game.channel.contains("Ranking pijaków!")
    assert loot == []


async def test_party_ignores_other_channels_and_the_bot(game, loot, quick, monkeypatch):
    first_choice(monkeypatch)
    game.add_member("Mecenas", roles=[patrons.CRAFTER_ROLE])
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    join_on(game, "Zdrowie Mecenas!", ala, ola)

    def hook(message) -> None:
        if message.content.startswith('"**'):
            game.say(ala, TOAST, channel=game.notice_channel)
            game.say(game.bot.user, TOAST)

    game.channel.send_hooks.append(hook)
    await events.spawn_party(game.scene())
    assert game.channel.count("Pfff... Miernoty.") == events.PARTY_ROUNDS and not game.channel.contains("Brawo")


async def test_party_winner_left_the_server(game, loot, quick, monkeypatch):
    first_choice(monkeypatch)
    game.add_member("Mecenas", roles=[patrons.CRAFTER_ROLE])
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    join_on(game, "Zdrowie Mecenas!", ala, ola)
    toast_answers(game, (ala, TOAST, 0))

    def leave(message) -> None:
        if "Ranking pijaków!" in message.text and ala in game.guild.members:
            game.guild.members.remove(ala)

    game.channel.send_hooks.append(leave)
    await events.spawn_party(game.scene())
    assert game.channel.contains("Ranking pijaków!")
    assert loot == []


async def test_party_disrupted_round_is_repeated(game, loot, quick, monkeypatch):
    first_choice(monkeypatch)
    game.add_member("Mecenas", roles=[patrons.CRAFTER_ROLE])
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    join_on(game, "Zdrowie Mecenas!", ala, ola)
    prompts = []

    def hook(message) -> None:
        if message.content.startswith('"**'):
            prompts.append(message)
            if len(prompts) == 1:  # the connection drops during the first toast
                game.bot.net.mark_disconnected()
                game.bot.net.mark_connected()
            else:
                game.say_later(ala, TOAST, 0)

    game.channel.send_hooks.append(hook)
    await events.spawn_party(game.scene())
    assert game.channel.count(challenge.MSG_DISRUPTED) == 1
    assert len(prompts) == events.PARTY_ROUNDS + 1
    assert game.channel.count("Brawo Ala!") == events.PARTY_ROUNDS
    assert not game.channel.contains("Pfff... Miernoty.")


# -- memory --------------------------------------------------------------------------

CHOSEN = events.MEMORY_EMOTES[:4]


def rune_keys(*emotes) -> list[str]:
    return [events.rune_key(events.MEMORY_EMOTES.index(emote)) for emote in emotes]


async def test_memory_solved(game, loot, monkeypatch):
    first_choice(monkeypatch)
    high_rolls(monkeypatch)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    clicks_on(game, "Mędrzec potrzebuje pomocy!", (game.bot.user, "join"), (ala, "join"), (ola, "join"))
    answers = clicks_on(game, "Zaczynam przygotowywać zaklęcie", (ola, rune_keys(events.MEMORY_EMOTES[9])[0]),
                        *[(ala, key) for key in rune_keys(*CHOSEN)])
    assert await events.spawn_memory(game.scene()) is True
    shown = next(m for m in game.channel.sent if "Zapamiętaj je!" in m.content)
    assert all(emote in shown.content for emote in CHOSEN) and shown.deleted and shown.reactions == []
    asked = panel(game, "Zaczynam przygotowywać")
    assert f"<@{ala.id}>" in asked.content and asked.reactions == []
    refused = (await answers[0])[0]
    assert refused.answers == [ui.MSG_NOT_YOURS_NAMED.format(player_id=ala.id)]
    assert refused.replies[0].message in game.channel.messages        # a public line; the runes still worked
    assert game.channel.count("*Dobrze!* - Sanczo dopinguje.") == 4
    assert game.channel.contains("Zaklęcie gotowe!")
    assert files(game) == ["0.png", "2.png"]
    assert loot == [(2, ala, 25)]
    assert f"{ala.mention} pomaga Sanczo." in panel(game, "Mędrzec potrzebuje pomocy!").text
    assert no_listeners(game) and no_live_buttons(game)


async def test_memory_counts_down_in_one_line_and_then_hides_the_runes(game, loot, monkeypatch):
    """Owner: every countdown is one line - "... 20... 10... 5..." in one edited message, not three."""
    first_choice(monkeypatch)
    ala = game.add_member("Ala")
    join_on(game, "Mędrzec potrzebuje pomocy!", ala)
    clicks_on(game, "Zaczynam przygotowywać zaklęcie", *[(ala, key) for key in rune_keys(*CHOSEN)])
    seen: list[tuple[float, list[str], bool]] = []

    async def pause(seconds):
        shown = next(m for m in game.channel.sent if "Zapamiętaj je!" in m.content)
        count = [m.content for m in game.channel.sent if m.content.startswith("...")]
        seen.append((seconds, count, shown.deleted))

    monkeypatch.setattr(challenge, "_pause", pause)
    assert await events.spawn_memory(game.scene()) is True
    assert seen == [(10, ["... 20..."], False),
                    (5, ["... 20... 10..."], False),
                    (5, ["... 20... 10... 5..."], False)]          # the runes stay up for the whole count
    count = [m for m in game.channel.sent if m.content.startswith("...")]
    assert [m.content for m in count] == ["... 20... 10... 5..."] and len(count[0].edits) == 2
    shown = next(m for m in game.channel.sent if "Zapamiętaj je!" in m.content)
    assert shown.deleted and all(emote in shown.content for emote in CHOSEN)
    assert game.channel.contains("Zaklęcie gotowe!") and [call[1] for call in loot] == [ala]


async def test_memory_wrong_emote(game, loot, monkeypatch):
    first_choice(monkeypatch)
    ala = game.add_member("Ala")
    join_on(game, "Mędrzec potrzebuje pomocy!", ala)
    clicks_on(game, "Zaczynam przygotowywać zaklęcie", *[(ala, key) for key in rune_keys(CHOSEN[0], CHOSEN[2])])
    assert await events.spawn_memory(game.scene()) is False
    assert game.channel.count("*Dobrze!*") == 1
    assert game.channel.contains("Sanczo nie zdążył dokończyć.")
    assert files(game) == ["0.png", "1.png"]
    assert loot == []
    assert no_live_buttons(game)


async def test_memory_a_pressed_rune_cannot_count_twice(game, loot, monkeypatch):
    first_choice(monkeypatch)
    ala = game.add_member("Ala")
    join_on(game, "Mędrzec potrzebuje pomocy!", ala)
    seen = []

    def hook(message) -> None:
        if "*Dobrze!*" in message.text:
            asked = panel(game, "Zaczynam przygotowywać")
            seen.append(asked.button(rune_keys(CHOSEN[0])[0]).disabled)
    game.channel.send_hooks.append(hook)
    clicks_on(game, "Zaczynam przygotowywać zaklęcie", *[(ala, key) for key in rune_keys(*CHOSEN)])
    assert await events.spawn_memory(game.scene()) is True
    assert seen[0] is True


async def test_memory_nobody_volunteers(game, loot, short_timeouts):
    assert await events.spawn_memory(game.scene()) is False
    assert game.channel.contains("*Ech, poradzę sobie sam...*")
    assert files(game) == ["0.png", "1.png"]
    assert no_listeners(game) and no_live_buttons(game)


async def test_memory_too_slow(game, loot, quick, monkeypatch):
    first_choice(monkeypatch)
    ala = game.add_member("Ala")
    join_on(game, "Mędrzec potrzebuje pomocy!", ala)
    clicks_on(game, "Zaczynam przygotowywać zaklęcie", (ala, rune_keys(CHOSEN[0])[0]))
    assert await events.spawn_memory(game.scene()) is False
    assert game.channel.contains("*Ech, za wolno, poradzę sobie sam...*")
    assert loot == []
    assert no_live_buttons(game)


# -- hazard --------------------------------------------------------------------------

WINNING, LOSING = events.cup_key(0), events.cup_key(1)


async def test_hazard_won(game_db, loot, monkeypatch):
    game = game_db
    first_choice(monkeypatch)
    high_rolls(monkeypatch)
    ala = game.add_member("Ala")
    await give_scrolls(game, ala, 10)
    clicks_on(game, "Zaryzykuj!", (game.bot.user, WINNING), (ala, WINNING))
    assert await events.spawn_hazard(game.scene()) is True
    assert await scrolls_of(game, ala) == 15
    assert game.channel.contains("*Ech, gratuluje* - Dova rzekł zawiedziony.")
    assert loot == [(2, ala, 55)]
    assert f"Kubek wybiera {ala.mention}." in panel(game, "Zaryzykuj!").text
    assert no_listeners(game) and no_live_buttons(game)


async def test_hazard_lost(game_db, loot, monkeypatch):
    game = game_db
    first_choice(monkeypatch)
    ala = game.add_member("Ala")
    await give_scrolls(game, ala, 5)
    clicks_on(game, "Zaryzykuj!", (ala, LOSING))
    assert await events.spawn_hazard(game.scene()) is False
    assert await scrolls_of(game, ala) == 0
    assert game.channel.contains("*Dzięki za zwoje. Kolego*")
    assert loot == []


async def test_hazard_needs_five_scrolls(game_db, loot, monkeypatch):
    game = game_db
    first_choice(monkeypatch)
    poor, nobody, rich = game.add_member("Biedak"), game.add_member("Nowy"), game.add_member("Bogacz")
    await give_scrolls(game, poor, 4)
    await give_scrolls(game, rich, 7)
    clicked = clicks_on(game, "Zaryzykuj!", (poor, WINNING), (nobody, LOSING), (rich, WINNING))
    assert await events.spawn_hazard(game.scene()) is True
    poor_click, nobody_click, _ = await clicked[0]
    # Each refusal is a channel message addressed to the player; the cups stay for the others.
    assert poor_click.public == [f"<@{poor.id}> {events.MSG_HAZARD_POOR}"]
    assert nobody_click.public == [f"<@{nobody.id}> {events.MSG_HAZARD_POOR}"]
    assert game.channel.count(events.MSG_HAZARD_POOR) == 2
    assert await scrolls_of(game, poor) == 4
    assert await scrolls_of(game, nobody) is None
    assert await scrolls_of(game, rich) == 12
    assert [call[1] for call in loot] == [rich]


async def test_hazard_nobody_plays(game_db, loot, short_timeouts):
    assert await events.spawn_hazard(game_db.scene()) is False
    assert game_db.channel.contains("nie ma pracy dla oszustów z moimi talentami")
    assert no_live_buttons(game_db)


async def test_hazard_database_down_propagates_and_cleans_up(game, loot, monkeypatch):
    ala = game.add_member("Ala")
    clicks_on(game, "Zaryzykuj!", (ala, WINNING))

    def transaction():
        raise DatabaseUnavailable("down")

    monkeypatch.setattr(game.bot.db, "transaction", transaction)
    with pytest.raises(DatabaseUnavailable):
        await events.spawn_hazard(game.scene())
    assert no_listeners(game) and no_live_buttons(game)


# -- Dova's prizes on a won game (items_decisions.md §12) ---------------------------------------

async def stored_item(game, member, key):
    value = await game.bot.db.fetchval("SELECT COUNT FROM PLAYER_ITEMS WHERE PLAYER_ID = $1 AND ITEM = $2",
                                       member.id, key)
    return int(value or 0)


PRIZE_LINE = "Dova sięga pod stół, mruczy coś o przegranym zakładzie i wciska Ci w dłoń: "


@pytest.mark.parametrize("seed, prizes", [(0, []), (1, ["dova_die"]), (7, ["dova_card"]),
                                          (4, ["dova_die", "dova_card"])])
async def test_hazard_win_rolls_dovas_two_prizes_each_on_its_own(game_db, loot, monkeypatch, seed, prizes):
    """Seeded: the cups and the loot are rigged as in test_hazard_won, so the win's only draws from the global
    generator are the two prizes (the Kość first, then the Karta - each on its own at 30%)."""
    game = game_db
    first_choice(monkeypatch)
    high_rolls(monkeypatch)
    ala = game.add_member("Ala")
    await give_scrolls(game, ala, 10)
    clicks_on(game, "Zaryzykuj!", (ala, WINNING))
    twin = random.Random(seed)
    assert dova.DIE_CHANCE == 0.30 and dova.CARD_CHANCE == 0.30
    assert [key for key, chance in (("dova_die", dova.DIE_CHANCE), ("dova_card", dova.CARD_CHANCE))
            if twin.random() < chance] == prizes                          # (the seed says what the test expects)
    random.seed(seed)
    assert await events.spawn_hazard(game.scene()) is True
    # The win as it always was: +5 scrolls, the monster's epic loot.
    assert await scrolls_of(game, ala) == 15 and loot == [(2, ala, 55)]
    assert [await stored_item(game, ala, key) for key in ("dova_die", "dova_card")] == [
        int("dova_die" in prizes), int("dova_card" in prizes)]
    won = next(m for m in game.channel.messages if m.content.startswith(events.MSG_HAZARD_WON))
    shown = {"dova_die": "🎲 **Kość Dovy**", "dova_card": "🃏 **Karta Dovy**"}
    assert won.content == (events.MSG_HAZARD_WON if not prizes
                           else events.MSG_HAZARD_WON + "\n" + PRIZE_LINE + " i ".join(shown[key] for key in prizes))
    assert "<@" not in won.content and game.bot.owner_log == []


async def test_a_lost_game_rolls_no_prize(game_db, loot, monkeypatch):
    game = game_db
    first_choice(monkeypatch)
    rolled = []
    monkeypatch.setattr(random, "random", lambda: rolled.append(1) or 0.0)    # (0.0 would win every prize)
    ala = game.add_member("Ala")
    await give_scrolls(game, ala, 5)
    clicks_on(game, "Zaryzykuj!", (ala, LOSING))
    assert await events.spawn_hazard(game.scene()) is False
    assert rolled == [] and not game.channel.ever_sent(PRIZE_LINE)
    assert [await stored_item(game, ala, key) for key in ("dova_die", "dova_card")] == [0, 0]


async def test_a_prize_the_store_cannot_take_is_left_out_of_the_line(game_db, loot, monkeypatch):
    """No table: nothing to say.  A database that did not answer one prize: that one is the owner's to check."""
    game, bot = game_db, game_db.bot
    first_choice(monkeypatch)
    high_rolls(monkeypatch)
    monkeypatch.setattr(random, "random", lambda: 0.0)                          # both prizes, every game
    ala = game.add_member("Ala")
    await give_scrolls(game, ala, 20)
    bot._player_items = (False, items.clock.monotonic())                        # the table is not there
    clicks_on(game, "Zaryzykuj!", (ala, WINNING))
    assert await events.spawn_hazard(game.scene()) is True
    assert game.channel.messages[-1].content == events.MSG_HAZARD_WON and bot.owner_log == []

    bot._player_items = None
    real_grant = items.grant

    async def grant(bot_, player_id, key, number=1):
        if getattr(key, "key", key) == "dova_die":
            raise DatabaseUnavailable("the reply was lost")
        return await real_grant(bot_, player_id, key, number)

    monkeypatch.setattr(items, "grant", grant)
    game.channel.send_hooks.clear()                                             # (Ala clicks the new cups once)
    clicks_on(game, "Zaryzykuj!", (ala, WINNING))
    assert await events.spawn_hazard(game.scene()) is True
    won = [m for m in game.channel.messages if m.content.startswith(events.MSG_HAZARD_WON)][-1]
    assert won.content == events.MSG_HAZARD_WON + "\n" + PRIZE_LINE + "🃏 **Karta Dovy**"
    assert await stored_item(game, ala, "dova_card") == 1 and await stored_item(game, ala, "dova_die") == 0
    assert bot.owner_log == [dova.LOG_PRIZE_UNSURE.format(name="Kość Dovy", player_id=ala.id,
                                                          error="DatabaseUnavailable", key="dova_die")]


# -- hunting -------------------------------------------------------------------------

@pytest.fixture
def hunt(monkeypatch):
    calls: list[tuple] = []

    async def send_mob_image(scene, rarity):
        calls.append(("image", rarity))
        return True, "Szef"

    async def hunt_mobs(scene, rarity, is_player_boss, player_boss, player=None):
        calls.append(("hunt", rarity, is_player_boss, player_boss, player))
        return True

    monkeypatch.setattr(events.daily, "send_mob_image", send_mob_image)
    monkeypatch.setattr(events.daily, "hunt_mobs", hunt_mobs)
    return calls


@pytest.mark.parametrize("roll, rarity", [(96, 1), (97, 2)])
async def test_hunting_first_volunteer_hunts(game, hunt, monkeypatch, roll, rarity):
    monkeypatch.setattr(random, "randint", lambda low, high: roll)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    clicks_on(game, "Mroczny las!", (game.bot.user, "join"), (ala, "join"), (ola, "join"))
    assert await events.spawn_hunting(game.scene()) is True
    assert hunt == [("image", rarity), ("hunt", rarity, True, "Szef", ala)]
    assert no_listeners(game) and no_live_buttons(game)


async def test_hunting_nobody_comes(game, hunt, short_timeouts):
    assert await events.spawn_hunting(game.scene()) is False
    assert hunt == []
    assert game.channel.contains("*Las pozostaje tajemnicą...*")
    assert files(game) == ["0.png", "1.png"]


# -- duel ----------------------------------------------------------------------------

@pytest.fixture
def duels(monkeypatch):
    """Scripted ``boss.duel_fight``: the first fighter wins unless ``duels.results`` says otherwise."""
    record = SimpleNamespace(calls=[], results=[])

    async def duel_fight(scene, participants, ban=True):
        record.calls.append((list(participants), ban))
        if record.results:
            return record.results.pop(0)
        return True, participants[0]

    monkeypatch.setattr(events.boss, "duel_fight", duel_fight)
    return record


async def test_duel_two_volunteers_fight(game, loot, duels):
    ala, ola, ela = game.add_member("Ala"), game.add_member("Ola"), game.add_member("Ela")
    clicked = clicks_on(game, "Pojedynek!", (game.bot.user, "join"), (ala, "join"), (ala, "join"),
                        (ola, "join"), (ela, "join"))
    assert await events.spawn_duel(game.scene()) is True
    assert duels.calls == [([ala, ola], True)]
    assert game.channel.count("wyzywa wszystkich na pojedynek!") == 1
    assert game.channel.contains("to Ala otrzyma skarb oraz 6 punktów rankingowych!")
    assert loot == []
    again = (await clicked[0])[2]
    assert again.answers == [events.MSG_ALREADY_SIGNED]
    arena = panel(game, "Pojedynek!")
    # The refusal was posted under the arena before Ola joined: the sign-up went on all the same.
    assert game.channel.messages.index(arena) < game.channel.messages.index(again.replies[0].message)
    assert "Do walki stają (2/2)" in arena.text and ala.mention in arena.text and ola.mention in arena.text
    assert no_listeners(game) and no_live_buttons(game)


async def test_duel_draw_is_reported(game, loot, duels):
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    duels.results.append((False, None))
    join_on(game, "Pojedynek!", ala, ola)
    assert await events.spawn_duel(game.scene()) is False
    assert loot == []


async def test_duel_lone_challenger_wins_by_walkover(game_db, loot, loot_sources, duels, short_timeouts):
    game = game_db
    game.bot.debug = False
    ala = game.add_member("O'Brien")
    join_on(game, "Pojedynek!", ala)
    assert await events.spawn_duel(game.scene()) is False
    assert duels.calls == []
    assert loot == [(2, ala, 0)] and loot_sources == ["duel"]            # a walkover's loot is a duel's
    assert game.channel.contains(f"O'Brien wyzywa <@&{game.bot.ids.hunters_role}> na pojedynek!")
    assert game.channel.contains("*Arena pozostaje pusta...*")
    row = await game.bot.db.fetchrow("SELECT NICK, POINTS FROM RANKING WHERE ID = $1", str(ala.id))
    assert row["nick"] == "O'Brien" and int(row["points"]) == 6


async def test_duel_nobody_comes(game, loot, duels, short_timeouts):
    assert await events.spawn_duel(game.scene()) is False
    assert loot == [] and duels.calls == []
    assert game.channel.contains("*Arena pozostaje pusta...*")
    assert files(game) == ["pvp.png", "0.png"]


# -- assassin ------------------------------------------------------------------------

async def add_ranked(game, member_or_id, points) -> None:
    user_id = getattr(member_or_id, "id", member_or_id)
    await game.bot.db.execute("INSERT INTO RANKING (ID, NICK, POINTS) VALUES ($1, $2, $3)",
                              str(user_id), "O'Nick", points)


def confirms(game, member, text="!potwierdzam") -> None:
    def hook(message) -> None:
        if "Czy jesteś pewien" in message.content:
            game.say_later(member, text, 0)
    game.channel.send_hooks.append(hook)


def is_dead(game, member) -> bool:
    return any(role.id == game.bot.ids.dead_hunter_role for role in member.roles)


async def test_assassin_needs_an_account(game_db):
    game = game_db
    ala = game.add_member("Ala")
    assert await events.assasinate(game.scene(ala, "!zabojca")) is False
    assert game.channel.contains("Nie masz jeszcze peta/konta w systemie.")


async def test_assassin_needs_thirty_scrolls(game_db):
    game = game_db
    ala = game.add_member("Ala")
    await give_scrolls(game, ala, 29)
    assert await events.assasinate(game.scene(ala, "!zabojca")) is False
    assert game.channel.contains("Potrzebujesz minimum 30 zwojów odrodzenia.")
    assert await scrolls_of(game, ala) == 29


async def test_assassin_not_confirmed(game_db, short_timeouts):
    game = game_db
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await give_scrolls(game, ala, 40)
    await add_ranked(game, ola, 10)
    confirms(game, ola)  # somebody else cannot confirm
    assert await events.assasinate(game.scene(ala, "!zabojca")) is False
    assert game.channel.contains("Nie potwierdzono zlecenia zabójstwa.")
    assert await scrolls_of(game, ala) == 40
    assert not is_dead(game, ola)


async def test_assassin_kills_a_ranked_member(game_db, monkeypatch):
    game = game_db
    first_choice(monkeypatch)
    monkeypatch.setattr(random, "random", lambda: 0.2)  # not below 20 %
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await give_scrolls(game, ala, 40)
    await add_ranked(game, 123456789, 99)  # top of the ranking, left the server
    await add_ranked(game, ala, 50)
    await add_ranked(game, ola, 10)
    confirms(game, ala, "$POTWIERDZAM")
    assert await events.assasinate(game.scene(ala, "!zabojca")) is True
    assert await scrolls_of(game, ala) == 10
    assert is_dead(game, ola) and not is_dead(game, ala)
    assert game.channel.contains(f"Zabójca wykonał zlecenie. Ofiarą został <@{ola.id}>")
    confirmation = next(m for m in game.channel.messages if m.content == "$POTWIERDZAM")
    assert confirmation.reactions == [events.RIP]


async def test_assassin_betrays_the_client(game_db, monkeypatch):
    game = game_db
    monkeypatch.setattr(random, "random", lambda: 0.19)
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await give_scrolls(game, ala, 30)
    await add_ranked(game, ola, 10)
    confirms(game, ala)
    assert await events.assasinate(game.scene(ala, "!zabojca")) is True
    assert await scrolls_of(game, ala) == 0
    assert is_dead(game, ala) and not is_dead(game, ola)
    assert game.channel.contains("Zabójca Cię zdradził!")


async def test_assassin_without_a_target_costs_nothing(game_db, monkeypatch):
    game = game_db
    monkeypatch.setattr(random, "random", lambda: 0.0)
    ala = game.add_member("Ala")
    await give_scrolls(game, ala, 30)
    await add_ranked(game, ala, 50)
    await add_ranked(game, 123456789, 99)  # left the server
    confirms(game, ala)
    assert await events.assasinate(game.scene(ala, "!zabojca")) is False  # legacy: scrolls were gone
    assert game.channel.contains("Zabójca nie znalazł żadnego celu.")
    assert await scrolls_of(game, ala) == 30
    assert not is_dead(game, ala)


async def test_assassin_scrolls_spent_while_confirming(game_db, monkeypatch):
    game = game_db
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await give_scrolls(game, ala, 30)
    await add_ranked(game, ola, 10)
    confirms(game, ala)
    pick = events._pick_victim

    async def spend_then_pick(bot, author_id):
        await bot.db.execute("UPDATE PETOWNER SET REROLL_SCROLL = 3 WHERE PLAYER_ID = $1", ala.id)
        return await pick(bot, author_id)

    monkeypatch.setattr(events, "_pick_victim", spend_then_pick)
    assert await events.assasinate(game.scene(ala, "!zabojca")) is False
    assert await scrolls_of(game, ala) == 3
    assert not is_dead(game, ola)


async def test_assassin_database_down_propagates(game, monkeypatch):
    ala = game.add_member("Ala")

    async def fetchrow(*args, **kwargs):
        raise DatabaseUnavailable("down")

    monkeypatch.setattr(game.bot.db, "fetchrow", fetchrow)
    with pytest.raises(DatabaseUnavailable):
        await events.assasinate(game.scene(ala, "!zabojca"))


# -- tournament ----------------------------------------------------------------------

async def test_tournament_cancelled_without_players(game, loot, duels, short_timeouts):
    assert await events.spawn_pvp_tournament(game.scene()) is None
    assert game.channel.contains("PvP turniej anulowany. Za mało uczestników.")
    assert loot == [] and duels.calls == []
    assert no_listeners(game) and no_live_buttons(game)


async def test_tournament_single_player_gets_loot(game, loot, loot_sources, duels, quick):
    ala = game.add_member("Ala")
    clicked = clicks_on(game, "Tygodniowy turniej PvP!", (ala, "join"), (ala, "join"), (game.bot.user, "join"))
    assert await events.spawn_pvp_tournament(game.scene()) == ala
    assert (await clicked[0])[1].answers == [events.MSG_ALREADY_SIGNED]
    assert loot == [(2, ala, 0)] and loot_sources == ["duel"]            # the lone player's loot is a duel's
    assert duels.calls == []
    roster = panel(game, "Tygodniowy turniej PvP!")
    assert "Zapisani (1)" in roster.text and ala.mention in roster.text and events.MSG_SIGN_UP_CLOSED in roster.text
    assert game.channel.contains(f"🏆 **Zwycięzca turnieju:** {ala.mention}! Gratulacje! 🏆")


async def test_tournament_bracket_with_bye_and_draw(game, loot, duels, quick, monkeypatch):
    monkeypatch.setattr(random, "shuffle", lambda players: None)
    monkeypatch.setattr(random, "choice", lambda pool: pool[1])
    game.bot.debug = False
    ala, ola, ela = game.add_member("Ala"), game.add_member("Ola"), game.add_member("Ela")
    join_on(game, "Tygodniowy turniej PvP!", ala, ola, ela)
    duels.results.extend([(False, None), (True, ola)])
    assert await events.spawn_pvp_tournament(game.scene()) == ola
    # Round 1: Ela has a bye, Ala - Ola is a draw decided by lot; round 2: Ela - Ola.
    assert duels.calls == [([ala, ola], False), ([ela, ola], False)]
    assert game.channel.sent[0].content == f"<@&{game.bot.ids.hunters_role}>"
    assert game.channel.contains(f"Uczestnicy: {ala.mention}, {ola.mention}, {ela.mention}")
    assert game.channel.contains(f"{ela.mention} szczęśliwie przechodzi do kolejnej rundy!")
    assert game.channel.contains(f"Remis. Losowy zwycięzca: {ola.mention}.")
    assert game.channel.contains("**Turniej PvP - Runda 2**")
    assert game.channel.count(f"**Zwycięzca:** {ola.mention}") == 2
    assert files(game) == ["0.png", "1.gif"]
    assert loot == []


# -- ritual --------------------------------------------------------------------------

@pytest.fixture
def boss_image(monkeypatch):
    calls: list[int] = []

    async def send_boss_image(scene, rarity):
        calls.append(rarity)
        return True, "Szef"

    monkeypatch.setattr(events.boss, "send_boss_image", send_boss_image)
    return calls


@pytest.mark.parametrize("players, expected", [(0, 0), (3, 1), (6, 2), (12, 4), (40, 4)])
def test_ritual_rarity_grows_with_the_circle(monkeypatch, players, expected):
    monkeypatch.setattr(random, "gauss", lambda mu, sigma: mu)
    assert events.ritual_rarity(players) == expected


def test_ritual_rarity_is_clamped(monkeypatch):
    monkeypatch.setattr(random, "gauss", lambda mu, sigma: mu - 3)
    assert events.ritual_rarity(1) == 0
    monkeypatch.setattr(random, "gauss", lambda mu, sigma: mu + 9)
    assert events.ritual_rarity(1) == 4


async def test_ritual_without_players_fails(game, boss_image, quick):
    assert await events.spawn_ritual(game.scene()) == (False, 0, False, None, "")
    assert game.channel.contains("Rytuał przywołania nie powiódł się...")
    assert files(game)[-1] == "4.png"
    assert boss_image == []


@pytest.mark.parametrize("roll, boost, expected", [(2.6, 0, 4), (0.2, 1, 1), (2.0, 0, 2), (4.0, 2, 4)])
async def test_ritual_summons_a_boss(game, boss_image, quick, monkeypatch, roll, boost, expected):
    monkeypatch.setattr(random, "gauss", lambda mu, sigma: roll)
    modifiers = await events.modifiers.load_modifiers(game.bot)
    game.bot.store.modifiers.save({**modifiers, "rarity_boost": boost})
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    clicked = clicks_on(game, "Rozpoczął się rytuał przywołania!", (ala, "join"), (ola, "join"),
                        (game.bot.user, "join"), (ola, "join"))
    assert await events.spawn_ritual(game.scene()) == (True, expected, True, "Szef", "")
    assert boss_image == [expected]
    assert not game.channel.contains("Rozpoczął się rytuał przywołania!")  # channel cleared for the boss
    refusal = f"<@{ola.id}> {events.MSG_ALREADY_SIGNED}"
    assert (await clicked[0])[3].public == [refusal]
    assert game.channel.ever_sent(refusal) and not game.channel.contains(refusal)  # cleared with the rest
    assert no_listeners(game) and no_live_buttons(game)


async def test_ritual_message_deleted(game, boss_image, quick, monkeypatch):
    ala = game.add_member("Ala")
    join_on(game, "Rozpoczął się rytuał przywołania!", ala)

    async def fetch_message(message_id):
        raise not_found()

    monkeypatch.setattr(game.channel, "fetch_message", fetch_message)
    assert await events.spawn_ritual(game.scene()) == (False, 0, False, None, "")
    assert boss_image == []
    assert not game.channel.contains("nie powiódł się")


# -- through the events cog: flags are always released -------------------------------

async def test_cog_runs_an_event_and_releases_the_flags(game, hunt, short_timeouts):
    from altermmo.cogs.events import EventsCog

    cog = EventsCog(game.bot)
    assert await cog.run_one_event(forced=EventType.HUNTING) is EventType.HUNTING
    assert game.channel.contains("*Las pozostaje tajemnicą...*")
    assert not game.bot.state.busy and game.bot.state.free_for_activity


async def test_cog_survives_a_discord_error_in_the_middle(game, hunt, monkeypatch):
    from altermmo.cogs.events import EventsCog

    ala = game.add_member("Ala")
    join_on(game, "Mroczny las!", ala)

    async def broken(scene, rarity):
        raise RuntimeError("Discord 503")

    monkeypatch.setattr(events.daily, "send_mob_image", broken)
    cog = EventsCog(game.bot)
    assert await cog.run_one_event(forced=EventType.HUNTING) is None
    assert not game.bot.state.busy and game.bot.state.free_for_activity
    assert no_listeners(game) and no_live_buttons(game)
    assert any("HUNTING" in line for line in game.bot.owner_log)


# -- how often events come -----------------------------------------------------------

async def test_events_come_a_fifth_more_often(game, monkeypatch):
    """Owner, 2026-11-23: "Zwiększ częstotliwość eventów o 20%" - the pauses between the event
    slots are the old ones (2000 .. 3000 s) divided by 1.2."""
    from altermmo import settings
    from altermmo.cogs import events as events_cog

    production = settings.Timings()
    assert (production.event_gap_min, production.event_gap_max) == (1667.0, 2500.0)
    assert (production.event_gap_min, production.event_gap_max) == (round(2000 / 1.2), round(3000 / 1.2))
    # A fifth more event slots in the same time (to within the rounding to whole seconds).
    old_mean, new_mean = (2000 + 3000) / 2, (production.event_gap_min + production.event_gap_max) / 2
    assert old_mean / new_mean == pytest.approx(1.2, abs=0.0005)
    # Only the events: the boss's timing and the debug run are what they were.
    assert (production.respawn_min, production.respawn_max, production.respawn_multiplier) == (150, 3600, 12)
    assert (production.post_fight_pause, production.pre_spawn_min, production.pre_spawn_max,
            production.summoned_min_stay) == (3600.0, 240.0, 420.0, 600.0)
    debug = settings._DEBUG_TIMINGS
    assert (debug.event_gap_min, debug.event_gap_max) == (15.0, 20.0)

    # The loop waits one such pause before every event slot - nothing else spaces the slots.
    game.bot.timings = production
    cog = events_cog.EventsCog(game.bot)
    drawn: list[tuple[float, float]] = []
    slots: list[int] = []

    def uniform(low: float, high: float) -> float:
        drawn.append((low, high))
        return 0.0

    async def slot(forced=None):
        slots.append(len(drawn))
        if len(slots) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(events_cog, "random", SimpleNamespace(uniform=uniform))
    monkeypatch.setattr(cog, "run_one_event", slot)
    with pytest.raises(asyncio.CancelledError):
        await cog.event_loop()
    assert drawn == [(1667.0, 2500.0)] * 3 and slots == [1, 2, 3]
