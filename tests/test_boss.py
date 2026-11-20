"""Boss fights, loot, duels and the boss slayer perks (altermmo/features/boss.py)."""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import re
import sys
from decimal import Decimal
from types import SimpleNamespace

import discord
import pytest

import altermmo.features
from altermmo.cogs import slayer
from altermmo.core import challenge, io, timing
from altermmo.core.challenge import Outcome, RoundResult
from altermmo.core.db import DatabaseUnavailable
from altermmo.core.state import BossInfo, BossPhase
from altermmo.features import achievements, boss, database, fight_types, modifiers, patrons, xpsteal

from fakes import FakeAttachment, FakeMember, FakeMessage, requested_word

NO_PET = {"CRIT_PERC": 0, "REPLACE_PERC": 0, "DEF_PERC": 0, "DROP_PERC": 0,
          "LOWHP_PERC": 0, "SLOW_PERC": 0, "INIT_PERC": 0, "DETECTION": False}

EXP_ONLY = [{"id": 0, "descr": "250 doświadczenia na Discordzie", "weight": 100}]


class PetStub:
    """Stands in for altermmo.features.pets and altermmo.features.skills."""

    def __init__(self) -> None:
        self.skills: dict[int, dict] = {}
        self.calls: list[tuple[str, int]] = []
        self.refuse: set[str] = set()
        self.errors: dict[str, BaseException] = {}

    async def get_pet_skills(self, bot, player_id):
        return {**NO_PET, **self.skills.get(player_id, {})}

    async def _give(self, name: str, player_id: int) -> bool:
        self.calls.append((name, player_id))
        if name in self.errors:
            raise self.errors[name]
        return name not in self.refuse

    async def assign_pet(self, scene, hunter):
        return await self._give("assign_pet", hunter.id)

    async def level_up_pet(self, scene, player_id):
        return await self._give("level_up_pet", player_id)

    async def assign_rebirth_stone(self, scene, number, player_id):
        assert number == 1
        return await self._give("assign_rebirth_stone", player_id)

    async def assign_mirror(self, scene, number, player_id):
        assert number == 1
        return await self._give("assign_mirror", player_id)

    async def assign_skill(self, scene, hunter):
        return await self._give("assign_skill", hunter.id)

    def count(self, name: str) -> int:
        return sum(1 for call in self.calls if call[0] == name)


@pytest.fixture
def real_pets():
    """Request this fixture to run a test against the real pets module instead of the stub."""
    return pytest.importorskip("altermmo.features.pets")


@pytest.fixture(autouse=True)
def pets(request, monkeypatch):
    if "real_pets" in request.fixturenames:
        return None
    stub = PetStub()
    for name in ("pets", "skills"):
        monkeypatch.setitem(sys.modules, f"altermmo.features.{name}", stub)
        monkeypatch.setattr(altermmo.features, name, stub, raising=False)
    return stub


@pytest.fixture(autouse=True)
def loot(monkeypatch, tmp_path):
    """A small, predictable loot config; ``loot(Normal=[...])`` rewrites it."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(boss, "CONFIG_DIR", config_dir)

    def write(**tables) -> None:
        data = {f"loot_details_{name}": tables.get(name, EXP_ONLY)
                for name in ("Normal", "Rare", "Epic", "Legend", "Mythic")}
        (config_dir / "lootConfig.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    write()
    return write


def prompts(channel) -> list[str]:
    """Every numbered fight prompt the bot posted."""
    return [m.content for m in channel.sent if re.match(r"^(~~)?\d+\. ", m.content)]


def said(channel, pattern: str) -> bool:
    """True when the bot ever posted a message matching the regular expression."""
    return any(re.search(pattern, m.text) for m in channel.sent)


def on_bot_message(game, fragment: str, action) -> None:
    """Run ``action()`` every time the bot posts a message containing ``fragment``."""
    def hook(message):
        if fragment in message.text:
            action()
    game.channel.send_hooks.append(hook)


def team_answers(game, players, *, wrong_rounds=(), silent_rounds=(), intruder_rounds=()) -> None:
    """Everybody answers only the prompts addressed to them (``N. **Name**: ...``)."""
    by_name = {player.name: player for player in players}
    counter = itertools.count(1)

    def hook(message):
        match = re.match(r"^\d+\. \*\*(.+?)\*\*: ", message.content)
        word = requested_word(message.content)
        if match is None or word is None:
            return
        number = next(counter)
        addressed = by_name[match.group(1)]
        if number in silent_rounds:
            return
        if number in wrong_rounds:
            game.say_later(addressed, "zlaodpowiedz", 0)
        elif number in intruder_rounds:
            game.say_later(next(p for p in players if p.id != addressed.id), word, 0)
        else:
            game.say_later(addressed, word, 0)

    game.channel.send_hooks.append(hook)


def http_error(status: int = 403) -> discord.HTTPException:
    return discord.HTTPException(SimpleNamespace(status=status, reason="nope"), "nope")


# -- rarity and hp -----------------------------------------------------------------------

@pytest.mark.parametrize("seconds, rarity", [
    (0, 0), (14399, 0), (14400, 1), (28799, 1), (28800, 2), (39599, 2), (39600, 3), (45000, 3),
    (45001, 0), (-5, 0),
])
def test_rarity_for_respawn_time_production(seconds, rarity):
    assert boss.rarity_for_respawn_time(seconds, debug=False) == rarity


@pytest.mark.parametrize("seconds, rarity", [
    (0, 0), (11, 0), (12, 1), (16, 1), (17, 2), (20, 2), (21, 3), (25, 3), (26, 0), (80, 0),
])
def test_rarity_for_respawn_time_debug(seconds, rarity):
    assert boss.rarity_for_respawn_time(seconds, debug=True) == rarity
    assert boss.rarity_for_respawn_time(str(seconds), debug=True) == rarity


@pytest.mark.parametrize("rarity, low, high", [(0, 4, 6), (1, 6, 9), (2, 9, 12), (3, 15, 20), (4, 15, 20)])
def test_random_boss_hp_ranges(monkeypatch, rarity, low, high):
    monkeypatch.setattr(random, "randint", lambda a, b: a)
    assert boss.random_boss_hp(rarity) == low
    monkeypatch.setattr(random, "randint", lambda a, b: b)
    assert boss.random_boss_hp(rarity) == high


def test_hp_scaling_is_exact_and_never_below_one():
    assert boss._scaled_hp(20, 15) == 23
    assert boss._scaled_hp(5, -10) == 4          # 4.5 is cut down, as before
    assert boss._scaled_hp(6, -100) == 1
    assert boss._scaled_hp(6, -250.5) == 1
    assert boss._num(3000.0) == "3000" and boss._num(Decimal("7.50")) == "7.5"


def test_word_lists_are_consistent():
    assert len(boss.SPELL_WORDS) == 30 and len(set(boss.SPELL_WORDS)) == 30
    assert (len(boss.BOSS_ACTIONS), len(boss.MYTHIC_ACTIONS), len(boss.DUEL_ACTIONS)) == (15, 10, 20)
    for word, prompt in (*boss.BOSS_ACTIONS, *boss.MYTHIC_ACTIONS, *boss.DUEL_ACTIONS):
        assert requested_word(prompt) == word


def test_real_loot_config_is_readable(monkeypatch):
    from altermmo import settings
    monkeypatch.setattr(boss, "CONFIG_DIR", settings.CONFIG_DIR)
    for rarity in range(6):
        table = boss._loot_table(rarity)
        assert table and all({"id", "descr", "weight"} <= set(item) for item in table)
    assert boss._loot_table(5) == boss._loot_table(3)


# -- the boss picture --------------------------------------------------------------------

async def test_boss_image_of_a_monster(game, monkeypatch):
    monkeypatch.setattr(random, "randint", lambda a, b: a)     # 0 % -> no player boss, picture 0
    game.add_member("Patron", roles=[patrons.PATRON_ROLES[0]])
    assert await boss.send_boss_image(game.scene(), 2) == (False, None)
    picture, embed_message = game.channel.messages[-2:]
    assert picture.files == ["100.gif"]
    embed = embed_message.embeds[0]
    assert embed.title == "💀 🟪 Epicki boss! 🟪 💀"
    assert embed.description == ("Pojawił się epicki boss! Zabij go natychmiast, żeby zgarnąć nagrody! "
                                 "Kliknij **Zaatakuj** albo wpisz **$zaatakuj**, żeby rozpocząć walkę! ⚔️")
    assert embed.colour.value == 0x800080
    assert embed_message.button_labels == ["Zaatakuj"]                  # the button is under the boss


async def test_boss_image_of_a_patron(game, monkeypatch):
    monkeypatch.setattr(random, "randint", lambda a, b: b)     # 100 % -> a patron is the boss
    patron = game.add_member("Patron", roles=[patrons.CRAFTER_ROLE])
    assert await boss.send_boss_image(game.scene(), 3) == (True, patron)
    avatar, embed_message = game.channel.messages[-2:]
    assert avatar.content == patron.display_avatar.url
    embed = embed_message.embeds[0]
    assert embed.title == "💀 🟧 Legendarny Patron! 🟧 💀"
    assert "będziesz potrzebował kompanów spośród <@&985071779787730944>!" in embed.description
    assert embed.description.endswith("*Bossem jest gracz, a to oznacza, że jeśli nie zostanie pokonany, "
                                      "to ten gracz zgarnia 2000 doświadczenia!*")


async def test_boss_image_without_patrons_falls_back_to_a_monster(game, monkeypatch):
    monkeypatch.setattr(random, "randint", lambda a, b: b)
    assert await boss.send_boss_image(game.scene(), 4) == (False, None)
    assert game.channel.messages[-2].files == ["10007.gif"]
    assert game.channel.messages[-1].embeds[0].title == "💀 🟥 Mityczny boss! 🟥 💀"
    assert "Pojawił się MITYCZNY boss!" in game.channel.messages[-1].text


async def test_boss_chosen_at_the_shrine_is_a_player_boss(game, monkeypatch):
    monkeypatch.setattr(random, "randint", lambda a, b: a)     # the draw says "monster"...
    chosen = game.add_member("Wybraniec")
    await modifiers.modify_modifiers(game.bot, "player_id", chosen.id)
    await modifiers.modify_modifiers(game.bot, "drop_boost_perc", 20)
    assert await boss.send_boss_image(game.scene(), 1) == (True, chosen)   # ...the shrine wins
    text = game.channel.messages[-1].text
    assert "💀 🟦 Rzadki Wybraniec! 🟦 💀" in text
    assert "to ten gracz zgarnia 1000 doświadczenia!*" in text
    assert "Drop zwiększony o 20 %" in text and f"Bossem będzie gracz <@{chosen.id}>" in text


async def test_shrine_player_who_left_becomes_a_monster(game, monkeypatch):
    monkeypatch.setattr(random, "randint", lambda a, b: b)     # even when a patron was drawn
    game.add_member("Patron", roles=[patrons.CRAFTER_ROLE])
    await modifiers.modify_modifiers(game.bot, "player_id", 123456789)
    assert await boss.send_boss_image(game.scene(), 0) == (False, None)
    assert game.channel.messages[-2].files == ["7.gif"]
    assert game.channel.messages[-1].embeds[0].title == "💀 🟩 Zwykły boss! 🟩 💀"


# -- loot --------------------------------------------------------------------------------

FULL_TABLE = [
    {"id": 0, "descr": "250 doświadczenia na Discordzie", "weight": 100},
    {"id": 10, "descr": "Jajko", "weight": 100},
    {"id": 13, "descr": "Kamień", "weight": 60},
    {"id": 12, "descr": "Poziom", "weight": 60},
    {"id": 1, "descr": "Figa z makiem", "weight": 60},
    {"id": 2, "descr": "Ukradnij wybranemu użytkownikowi 2500 doświadczenia na Discordzie", "weight": 60},
    {"id": 15, "descr": "Klejnot", "weight": 60},
    {"id": 14, "descr": "Lustro", "weight": 10},
]


async def test_loot_normal_drop(game, pets, loot, monkeypatch):
    loot(Normal=FULL_TABLE)
    monkeypatch.setattr(random, "random", lambda: 0.5)       # 50: only weights >= 50 drop
    player = game.add_member("Ala")
    message = await boss.rand_loot(game.scene(), 0, player, 0)
    assert message == ("👉 250 doświadczenia na Discordzie\n👉 Jajko\n👉 Kamień\n👉 Poziom\n"
                       "👉 Figa z makiem\n"
                       "👉 Ukradnij wybranemu użytkownikowi 2500 doświadczenia na Discordzie\n"
                       + xpsteal.COMMAND_HINT + "\n👉 Klejnot\n" + boss.SKILL_GEM_HINT + "\n")
    assert pets.calls == [("assign_pet", player.id), ("assign_rebirth_stone", player.id),
                          ("level_up_pet", player.id), ("assign_skill", player.id)]
    assert game.bot.store.exp_summary.load() == {str(player.id): 250}
    assert xpsteal.pending(game.bot, player.id) == [2500]
    embed = game.channel.messages[-2].embeds[0]
    assert embed.title == "Boss Drop - Ala" and embed.footer.text == "Gratulacje!"
    assert embed.description == "Boss wydropił:\n" + message
    # Right under the drop: how to use the theft (a ping), and the same privately.
    how_to = game.channel.messages[-1].content
    assert how_to == xpsteal.MSG_HOW_TO.format(thief=player.id, amount=2500) and "$kradnijxp nick" in how_to
    assert player.dms == [xpsteal.DM_HOW_TO.format(amount=2500, channel=game.bot.ids.boss_channel)]


async def test_loot_bonus_drops_really_grant_a_second_item(game, pets, loot, monkeypatch):
    loot(Rare=FULL_TABLE)
    monkeypatch.setattr(random, "random", lambda: 0.1)
    pets.refuse.add("level_up_pet")                          # e.g. the pet is at its maximum level
    player = game.add_member("Ala")
    message = await boss.rand_loot(game.scene(), 1, player, 100.9)    # boost is cut down to 100 %
    assert message == (
        "👉 250 doświadczenia na Discordzie\n👉 250 doświadczenia na Discordzie (Bonus)\n"
        "👉 Jajko\n"                                         # an egg never comes twice
        "👉 Kamień\n👉 Kamień (Bonus)\n"
        # "Poziom": nothing was granted, so nothing is announced
        "👉 Figa z makiem\n👉 Figa z makiem (Bonus)\n"
        "👉 Ukradnij wybranemu użytkownikowi 2500 doświadczenia na Discordzie\n"
        + xpsteal.COMMAND_HINT + "\n"
        "👉 Ukradnij wybranemu użytkownikowi 2500 doświadczenia na Discordzie (Bonus)\n"
        "👉 Klejnot\n" + boss.SKILL_GEM_HINT + "\n👉 Klejnot (Bonus)\n"
        "👉 Lustro\n")                                       # 20 % after the boost, 10 <= 20
    assert pets.count("assign_pet") == 1
    assert pets.count("assign_rebirth_stone") == 2
    assert pets.count("level_up_pet") == 2
    assert pets.count("assign_skill") == 2
    assert pets.count("assign_mirror") == 1
    assert game.bot.store.exp_summary.load() == {str(player.id): 500}
    assert xpsteal.pending(game.bot, player.id) == [2500, 2500]


async def test_loot_item_that_fails_does_not_cost_the_others(game, pets, loot, monkeypatch):
    loot(Epic=FULL_TABLE)
    monkeypatch.setattr(random, "random", lambda: 0.5)
    pets.errors["assign_pet"] = ConnectionError("database gone")
    player = game.add_member("Ala")
    message = await boss.rand_loot(game.scene(), 2, player, 0)
    assert "Jajko" not in message and "👉 Kamień\n" in message and "👉 Klejnot\n" in message
    assert any("Nie udało się przyznać łupu" in line and "Jajko" in line for line in game.bot.owner_log)


async def test_loot_survives_a_failed_embed_and_unknown_rarity(game, loot):
    loot(Legend=[{"id": 1, "descr": "Legendarna figa", "weight": 100}])
    player = game.add_member("Ala")
    game.channel.fail_with, game.channel.fail_sends = RuntimeError("discord broke"), 1
    assert await boss.rand_loot(game.scene(), 9, player, 0) == "👉 Legendarna figa\n"
    assert any("Nie udało się pokazać łupu" in line for line in game.bot.owner_log)


# -- single lobby ------------------------------------------------------------------------

def test_single_lobby_waits_one_minute():
    """Owner's request of 2026-11-20: a rival has one minute (it used to be two) to object."""
    assert boss.LOBBY_STEPS * boss.LOBBY_STEP == 60
    assert boss.LOBBY_STEPS * boss.LOBBY_STEP_DEBUG < 60          # the test channel stays quick


async def test_single_lobby_deadline_is_a_minute_away(game, monkeypatch):
    """What the lobby message counts down to is that minute."""
    attacker = game.add_member("Ala")
    shown = []
    monkeypatch.setattr(boss, "LOBBY_STEP_DEBUG", boss.LOBBY_STEP)   # the test bot runs in debug mode
    monkeypatch.setattr(boss.ui, "when", lambda seconds: shown.append(seconds) or "<t:0:R>")

    async def nobody(inbox, wanted, window):
        shown.append(("waited", window))
        return None

    monkeypatch.setattr(boss, "_next_message", nobody)
    assert await boss.single_init(game.scene(attacker, "$zaatakuj"), 0) is attacker
    assert shown == [60, ("waited", 60)]


async def test_single_init_nobody_objects(game, short_timeouts):
    attacker = game.add_member("Ala")
    other = game.add_member("Ola")
    robot = FakeMember("Robot", bot=True, guild=game.guild)
    listeners = len(game.bot.extra_events.get("on_message", []))

    def noise():
        game.say_later(attacker, "$zaatakuj", 0)                              # the attacker again
        game.say_later(robot, "$zaatakuj", 0)                                 # a bot
        game.say_later(other, "$zaatakuj", 0, channel=game.announce_channel)  # another channel
        game.say_later(other, "zaatakuj", 0)                                  # not the command

    posted = []
    on_bot_message(game, "Jesteś sam?", lambda: posted.append(game.channel.sent[-1].content))
    on_bot_message(game, "Jesteś sam?", noise)
    assert await boss.single_init(game.scene(attacker, "$zaatakuj"), 0) is attacker
    # One lobby message whose deadline Discord counts down (no more "... 105..." messages).
    assert len(game.channel.sent) == 1
    assert re.fullmatch(r"Jesteś sam\?\.\.\. \*Inny gracz może kliknąć \*\*Zaatakuj\*\* albo wpisać "
                        r"\*\*\$zaatakuj\*\*, jeśli chce zawalczyć o bossa!\* "
                        r"Zgłoszenia kończą się <t:\d+:R>\.", posted[0])
    lobby = game.channel.sent[0]
    assert lobby.content == boss.MSG_SINGLE_LOBBY_ALONE.format(user_id=attacker.id) and lobby.view is None
    assert len(game.bot.extra_events.get("on_message", [])) == listeners


async def test_single_init_spell_decides(game):
    attacker = game.add_member("Ala")
    rival = game.add_member("Ola")
    bystander = game.add_member("Ela")
    on_bot_message(game, "Jesteś sam?", lambda: game.say_later(rival, "$ZAATAKUJ", 0))

    def cast():
        spell = requested_word(game.channel.sent[-1].content)
        game.say_later(attacker, "abrakadabra", 0)          # a wrong try does not end anything
        game.say_later(bystander, spell.upper(), 0.01)      # anybody on the channel may take the boss
        game.say_later(rival, spell, 0.02)

    on_bot_message(game, "Zaklęcie to", cast)
    assert await boss.single_init(game.scene(attacker), 1) is bystander
    assert game.channel.sent[0].content == boss.MSG_SINGLE_LOBBY_RIVAL.format(user_id=rival.id)
    assert game.channel.contains('"**SPOKÓJ!!!**" - *słyszyscie głos w swojej głowie.*')
    assert game.channel.contains('"Pierwszy, który PŁYNNIE wypowie zaklęcie, które zaraz zdradzę, '
                                 'będzie godzien walki ze mną!"')
    spell_line = next(m.content for m in game.channel.sent if "Zaklęcie to" in m.content)
    assert requested_word(spell_line) in [word.lower() for word in boss.SPELL_WORDS]


async def test_single_init_nobody_types_the_spell(game, monkeypatch):
    # The two-minute window is one wait now: keep it above Windows' 15.6 ms timer resolution.
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.05)
    attacker = game.add_member("Ala")
    rival = game.add_member("Ola")
    await modifiers.modify_modifiers(game.bot, "drop_boost_perc", 25)
    await game.bot.state.set_boss_phase(BossPhase.ENGAGED)
    on_bot_message(game, "Jesteś sam?", lambda: game.say_later(rival, "$zaatakuj", 0))
    on_bot_message(game, "Zaklęcie to", lambda: game.say_later(rival, "nie wiem", 0))
    assert await boss.single_init(game.scene(attacker), 1) is None
    assert game.channel.messages[-1].content == '"Pfff... Miernoty. Nikt z Was nie jest godzien."'
    assert "Cała grupa nie zdążyła wpisac hasła." in game.bot.owner_log
    # Nothing is reset here - that is the boss cycle's job.
    assert (await modifiers.load_modifiers(game.bot))["drop_boost_perc"] == 25
    assert game.bot.state.boss_phase is BossPhase.ENGAGED


async def test_single_init_releases_its_listener_when_discord_fails(game):
    attacker = game.add_member("Ala")
    listeners = len(game.bot.extra_events.get("on_message", []))
    game.channel.fail_with, game.channel.fail_sends = RuntimeError("discord broke"), 1
    with pytest.raises(RuntimeError):
        await boss.single_init(game.scene(attacker), 0)
    assert len(game.bot.extra_events.get("on_message", [])) == listeners


# -- single fight ------------------------------------------------------------------------

async def test_single_fight_won(game_db, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("O'Brien")
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 3)
    game_db.auto_answer(hunter)
    result = await boss.single_fight(game_db.scene(hunter), hunter, 1, False, None)
    assert result == fight_types.FightResult(won=True)
    assert len(prompts(game_db.channel)) == 3 and prompts(game_db.channel)[0].startswith("1. ")
    channel = game_db.channel
    assert channel.contains(f"Zaatakowałeś bossa <@{hunter.id}>! <:REEeee:790963160495947856>")
    assert channel.contains(f"Brawo <@{hunter.id}>! Pokonałeś bossa! <:POGGIES:790963160491753502>")
    assert channel.contains("Zabicie bossa zajęło Ci: 0.") and channel.contains("Jedna tura zajęła Ci średnio 0.")
    assert channel.contains("Pobiłeś rekord i zgarniasz dodatkowe 1500 doświadczenia na discordzie!")
    assert channel.contains("Boss Drop - O'Brien")

    assert await bot.db.fetchval("SELECT POINTS FROM RANKING WHERE ID = $1", str(hunter.id)) == 3
    assert (await database.read_record_table(bot))[1] == "O'Brien"
    assert (await database.read_history_table(bot))[1] == "O'Brien"
    assert await bot.db.fetchval("SELECT BOSS_KILLS FROM ACHIEVEMENTS WHERE PLAYER_ID = $1", hunter.id) == 1
    # 250 from the loot; the 1500 for the record only goes to the owner's log, as in legacy
    # (the owner hands it out from that line - the exp summary would pay it a second time).
    assert bot.store.exp_summary.load() == {str(hunter.id): 250}
    assert "O'Brien otrzymał: 1500 expa za rekord" in bot.owner_log
    assert "O'Brien otrzymał: \n👉 250 doświadczenia na Discordzie\n" in bot.owner_log
    assert bot.state.boss_phase is BossPhase.IDLE        # fights never touch the phase


async def test_single_fight_mistake_loses_and_pays_the_player_boss(game_db, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    patron = game_db.add_member("Patron")
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 5)
    game_db.auto_answer(hunter, wrong_on_round=2)
    result = await boss.single_fight(game_db.scene(hunter), hunter, 1, True, patron)
    assert result == fight_types.FightResult(won=False, losers_to_ban=[])
    assert game_db.channel.messages[-1].content == ("Pomyliłeś się! <:PepeHands:783992337377918986> "
                                                    "Boss pojawi się później! <:RIP:912797982917816341>")
    assert bot.owner_log == ["Ala pomylił się i nie zabił bossa. Bossem był Patron i należy mu się 1000 expa."]
    assert bot.store.exp_summary.load() == {str(patron.id): 1000}
    assert await bot.db.fetchval("SELECT count(*) FROM RANKING") == 0


async def test_single_fight_timeout_loses_and_bans_when_the_modifier_is_on(game_db, monkeypatch, short_timeouts):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    await modifiers.modify_modifiers(bot, "ban_loser", 1)
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 5)
    game_db.auto_answer(hunter, silent_on_round=3)
    result = await boss.single_fight(game_db.scene(hunter), hunter, 0, False, None)
    assert not result.won and result.losers_to_ban == [hunter]
    assert game_db.channel.messages[-1].content == ("Niestety nie zdążyłeś! <:Bedge:970576892874854400> "
                                                    "Boss pojawi się później! <:RIP:912797982917816341>")
    assert bot.owner_log == ["Ala nie zdążył wpisać komend i boss uciekł."]
    assert bot.store.exp_summary.load() == {}
    # The modifiers are still there: resetting them is the boss cycle's job.
    assert (await modifiers.load_modifiers(bot))["ban_loser"] == 1


async def test_player_boss_cannot_farm_experience_by_losing_to_himself(game_db, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("Cwaniak")
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 5)
    game_db.auto_answer(hunter, wrong_on_round=1)
    result = await boss.single_fight(game_db.scene(hunter), hunter, 2, True, hunter)
    assert not result.won
    assert bot.store.exp_summary.load() == {}
    assert bot.owner_log == ["Cwaniak pomylił się i nie zabił bossa. Bossem był Cwaniak, ale sam brał udział "
                             "w walce, więc nagroda mu się nie należy."]


async def test_single_fight_pet_skills(game_db, pets, monkeypatch):
    """Protection after a mistake, critical hits and a pet attacking instead of the player."""
    hunter = game_db.add_member("Ala")
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 5)
    # 5 hp lowered by 20 % -> 4; every hit is critical -> two words are enough.
    pets.skills[hunter.id] = {"DEF_PERC": Decimal("100"), "CRIT_PERC": Decimal("100"),
                              "LOWHP_PERC": Decimal("20")}
    game_db.auto_answer(hunter, wrong_on_round=1)
    assert (await boss.single_fight(game_db.scene(hunter), hunter, 0, False, None)).won
    assert game_db.channel.contains("Pomyliłeś się, ale Twój towarzysz Cię chroni!")
    assert [p.split(".")[0] for p in prompts(game_db.channel)] == ["1", "2", "4"]

    game_db.channel.sent.clear()
    game_db.channel.send_hooks.clear()
    pets.skills[hunter.id] = {"REPLACE_PERC": Decimal("100")}
    assert (await boss.single_fight(game_db.scene(hunter), hunter, 0, False, None)).won
    pet_lines = prompts(game_db.channel)
    assert len(pet_lines) == 5 and all(line.startswith("~~") for line in pet_lines)
    assert pet_lines[0].endswith("~~. Twój towarzysz wyprowadza atak!")


async def test_single_fight_hp_never_drops_below_one(game_db, monkeypatch):
    hunter = game_db.add_member("Ala")
    await modifiers.modify_modifiers(game_db.bot, "hp_reduced_perc", 150)
    game_db.auto_answer(hunter)
    assert (await boss.single_fight(game_db.scene(hunter), hunter, 0, False, None)).won
    assert len(prompts(game_db.channel)) == 1


async def test_mythic_kill_by_a_patron_who_left_meanwhile(game_db, monkeypatch):
    bot = game_db.bot
    earlier = game_db.add_member("Weteran", roles=[bot.ids.mythic_slayer_role])
    hunter = game_db.add_member("Ala", roles=[patrons.CRAFTER_ROLE])
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 2)
    game_db.auto_answer(hunter)
    game_db.guild.members.remove(hunter)                  # left the server during the fight
    result = await boss.single_fight(game_db.scene(hunter), hunter, 4, False, None)
    assert result.won
    assert all(requested_word(p) in dict(boss.MYTHIC_ACTIONS) for p in prompts(game_db.channel))
    # Ranking falls back to the id as the nick; the role cannot be given, the owner is told.
    row = await bot.db.fetchrow("SELECT NICK, POINTS FROM RANKING WHERE ID = $1", str(hunter.id))
    assert (row["nick"], row["points"]) == (str(hunter.id), 12)
    assert any("Nie udało się nadać roli pogromcy mitycznego bossa" in line for line in bot.owner_log)
    assert game_db.channel.contains(f"<@&985071779787730944>! Łowca <@{hunter.id}> zabił mitycznego bossa! "
                                    "<:POGGERS:936907543849078844> Do tej pory udało się to tylko 1 łowcom!")
    assert earlier in bot.role(bot.ids.mythic_slayer_role).members


async def test_disrupted_round_is_repeated_without_punishment(game_db, monkeypatch):
    hunter = game_db.add_member("Ala")
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 3)
    real_ask = challenge.ask
    calls = itertools.count(1)

    async def flaky(bot, channel, prompt, **kwargs):
        if next(calls) in (2, 3):                         # two lost rounds in a row
            return RoundResult(Outcome.DISRUPTED)
        return await real_ask(bot, channel, prompt, **kwargs)

    monkeypatch.setattr(challenge, "ask", flaky)
    game_db.auto_answer(hunter)
    assert (await boss.single_fight(game_db.scene(hunter), hunter, 0, False, None)).won
    assert game_db.channel.count(challenge.MSG_DISRUPTED) == 2
    assert [p.split(".")[0] for p in prompts(game_db.channel)] == ["1", "2", "3"]


async def test_repeated_disruptions_interrupt_the_fight(game_db, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")

    async def dead_line(bot, channel, prompt, **kwargs):
        return RoundResult(Outcome.DISRUPTED)

    monkeypatch.setattr(challenge, "ask", dead_line)
    with pytest.raises(fight_types.FightInterrupted):
        await boss.single_fight(game_db.scene(hunter), hunter, 0, True, game_db.add_member("Patron"))
    assert game_db.channel.count(challenge.MSG_DISRUPTED) == challenge.MAX_CONSECUTIVE_DISRUPTIONS - 1
    assert bot.store.exp_summary.load() == {} and bot.owner_log == []


async def test_technical_failure_before_the_kill_propagates(game_db, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 5)
    game_db.auto_answer(hunter)
    original = game_db.channel.send

    async def broken_on_third_prompt(content=None, **kwargs):
        if content and content.startswith("3. "):
            raise RuntimeError("discord broke")
        return await original(content, **kwargs)

    game_db.channel.send = broken_on_third_prompt
    with pytest.raises(RuntimeError):
        await boss.single_fight(game_db.scene(hunter), hunter, 0, False, None)
    assert await bot.db.fetchval("SELECT count(*) FROM RANKING") == 0
    assert bot.store.exp_summary.load() == {}

    # The same for the database going away before the fight starts.
    async def no_database(*args, **kwargs):
        raise DatabaseUnavailable("database gone")

    monkeypatch.setattr(database, "update_history_table", no_database)
    with pytest.raises(DatabaseUnavailable):
        await boss.single_fight(game_db.scene(hunter), hunter, 0, False, None)
    assert bot.store.exp_summary.load() == {} and bot.owner_log == []


async def test_single_fight_pet_protects_after_a_timeout(game_db, pets, monkeypatch, short_timeouts):
    hunter = game_db.add_member("Ala")
    pets.skills[hunter.id] = {"DEF_PERC": Decimal("100")}
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 3)
    game_db.auto_answer(hunter, silent_on_round=1)
    assert (await boss.single_fight(game_db.scene(hunter), hunter, 0, False, None)).won
    assert game_db.channel.contains("Nie zdążyłeś, ale Twój towarzysz Cię chroni!")
    assert [p.split(".")[0] for p in prompts(game_db.channel)] == ["1", "2", "3"]


async def test_won_fight_stays_won_when_bookkeeping_fails(game_db, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 2)

    async def no_ranking(*args, **kwargs):
        raise ConnectionError("database gone")

    monkeypatch.setattr(database, "update_ranking_table", no_ranking)
    original = game_db.channel.send

    async def no_cheering(content=None, **kwargs):
        if content and "Brawo" in content:
            raise RuntimeError("discord broke")
        return await original(content, **kwargs)

    game_db.channel.send = no_cheering
    game_db.auto_answer(hunter)
    result = await boss.single_fight(game_db.scene(hunter), hunter, 0, False, None)
    assert result.won
    assert any("ogłoszenie zwycięstwa" in line and "RuntimeError" in line for line in bot.owner_log)
    assert any("punkty rankingowe" in line and "ConnectionError" in line for line in bot.owner_log)
    # Everything else was still handed out.
    assert bot.store.exp_summary.load() == {str(hunter.id): 250}
    assert game_db.channel.contains("Boss Drop - Ala")
    assert await bot.db.fetchval("SELECT BOSS_KILLS FROM ACHIEVEMENTS WHERE PLAYER_ID = $1", hunter.id) == 1


async def test_no_record_no_bonus(game_db, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    await database.update_record_table(bot, "Mistrz", "0:00:00.000000")
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 2)
    game_db.auto_answer(hunter)
    assert (await boss.single_fight(game_db.scene(hunter), hunter, 0, False, None)).won
    assert not game_db.channel.contains("Pobiłeś rekord")
    assert bot.store.exp_summary.load() == {str(hunter.id): 250}
    assert (await database.read_record_table(bot))[1] == "Mistrz"


async def test_fight_and_loot_with_the_real_pets_module(game_db, real_pets, loot, monkeypatch):
    """The calls into features/pets.py (written in parallel) really fit together."""
    bot = game_db.bot
    loot(Normal=[*EXP_ONLY,
                 {"id": 10, "descr": "Jajko", "weight": 100},
                 {"id": 13, "descr": "Kamień", "weight": 100},
                 {"id": 14, "descr": "Lustro", "weight": 100}])
    hunter = game_db.add_member("O'Brien")
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 2)
    game_db.auto_answer(hunter)
    assert (await boss.single_fight(game_db.scene(hunter), hunter, 0, False, None)).won
    assert not any("Nie udało się przyznać łupu" in line or "Błąd po wygranej walce" in line
                   for line in bot.owner_log), bot.owner_log
    drop = bot.owner_log[-1]
    assert "👉 250 doświadczenia" in drop and "👉 Jajko" in drop
    assert "👉 Kamień" in drop and "👉 Lustro" in drop
    row = await bot.db.fetchrow("SELECT PET_ID, REBIRTH_STONES, MIRRORS FROM PETOWNER WHERE PLAYER_ID = $1",
                                hunter.id)
    assert row is not None and int(row["pet_id"]) > 0
    assert (int(row["rebirth_stones"]), int(row["mirrors"])) == (1, 1)


# -- group lobby -------------------------------------------------------------------------

def as_posted(game) -> list[tuple[str, str]]:
    """(content, full text) of every bot message at the moment it was posted (edits change them later)."""
    posted: list[tuple[str, str]] = []
    game.channel.send_hooks.append(lambda message: posted.append((message.content, message.text)))
    return posted


def roster(message) -> tuple[str, str]:
    """(name, value) of the party field of a legendary lobby panel."""
    field = message.embeds[0].fields[0]
    return field.name, field.value


async def test_group_init_gathers_and_confirms_four_players(game):
    players = [game.add_member(name) for name in ("Ala", "Ola", "Ela", "Ula")]
    leader, *others = players

    def join():
        for player in others:                              # everybody at the same moment
            game.say_later(player, "$zaatakuj", 0)
        game.say_later(others[0], "$zaatakuj", 0)          # impatient: typed twice

    def confirm():
        game.say_later(game.add_member("Obcy"), "$tak", 0)  # not in the party
        for player in players:
            game.say_later(player, "$TAK", 0)

    posted = as_posted(game)
    on_bot_message(game, "Znajdź więcej chętnych", join)
    on_bot_message(game, "Jesteście gotowi", confirm)
    scene = game.scene(leader, "$zaatakuj")
    assert await boss.group_init(scene, 3) == players
    assert scene.message.reactions == ["⚔️"]
    mentions = " " + "".join(f"<@{p.id}> " for p in players)
    # One panel, posted anew once - with the mentions, so that the party is pinged for the ready check.
    first, ready = game.channel.sent
    assert first.deleted and not ready.deleted
    assert posted[0][0] == "" and boss.VOICE_START in posted[0][1]
    assert "Kliknij **Dołącz** albo wpisz **$zaatakuj**" in posted[0][1] and "<t:" in posted[0][1]
    assert posted[1][0] == "Jesteście gotowi do ataku?" + mentions
    assert "Kliknijcie **Gotowy** albo wpiszcie **$tak**" in posted[1][1]
    # The outcome stays on the panel; its buttons are gone.
    assert ready.view is None and ready.content == "Do walki z legendarnym bossem stają:" + mentions
    assert roster(ready) == ("Drużyna (4/4)", "\n".join(f"✅ <@{p.id}>" for p in players))
    confirmations = [m for m in game.channel.messages if m.content == "$TAK"]
    assert len(confirmations) == 4 and all(m.reactions == ["⚔️"] for m in confirmations)


async def test_group_init_boss_leaves_when_nobody_joins(game, monkeypatch):
    monkeypatch.setattr(timing, "TIMEOUT_SCALE", 0.0001)
    leader = game.add_member("Ala")
    posted = as_posted(game)
    assert await boss.group_init(game.scene(leader), 3) is None
    # The panel, the panel again with the last call to the hunters (a ping), the boss's verdict.
    first, last_call, verdict = game.channel.sent
    assert first.deleted
    assert posted[1][0] == ("Ostatnia szansa, <@&985071779787730944>! "
                            "Kliknij **Dołącz** albo wpisz **$zaatakuj**.")
    assert boss.VOICE_LAST_CHANCE in posted[1][1]
    assert last_call.view is None and last_call.content == "Drużyna się nie zebrała."
    assert boss.MSG_GROUP_GAVE_UP in last_call.text
    assert verdict.content == ("**Miernoty... Wiedziałem, że nie jesteście godni walki ze mną.**  - "
                               "*słyszycie głos w swojej głowie.*")
    assert game.bot.extra_events.get("on_message", []) == []
    assert game.bot.boss_cycle.lobby_door is None


async def test_group_init_drops_players_who_do_not_confirm(game, short_timeouts):
    ala, ola, ela, ula, iza, ada = (game.add_member(name) for name in ("Ala", "Ola", "Ela", "Ula", "Iza", "Ada"))
    rounds = itertools.count(1)

    def confirm():
        if next(rounds) == 1:
            for player in (ala, ola):                       # Ela and Ula went for tea
                game.say_later(player, "$tak", 0)
        else:
            for player in (ala, ola, iza, ada):
                game.say_later(player, "$tak", 0)

    posted = as_posted(game)
    on_bot_message(game, "Znajdź więcej chętnych",
                   lambda: [game.say_later(p, "$zaatakuj", 0) for p in (ola, ela, ula)])
    on_bot_message(game, "Jesteście gotowi", confirm)
    on_bot_message(game, "Zbierzcie drużynę",
                   lambda: [game.say_later(p, "$zaatakuj", 0) for p in (iza, ada, ala)])
    assert await boss.group_init(game.scene(ala), 3) == [ala, ola, iza, ada]
    regroup = [content for content, _ in posted if content.startswith("Zbierzcie drużynę")]
    assert regroup == [f"Zbierzcie drużynę przed atakiem... <@{ala.id}> <@{ola.id}> "]   # pings who stayed
    assert [m for m in game.channel.sent if not m.deleted] == [game.channel.sent[-1]]   # one panel left


# -- group fight -------------------------------------------------------------------------

async def test_group_fight_won(game_db, monkeypatch):
    bot = game_db.bot
    players = [game_db.add_member(name) for name in ("Ala", "O'la", "Ela", "Ula")]
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 3)
    team_answers(game_db, players)
    result = await boss.group_fight(game_db.scene(players[0]), players, False, None)
    assert result == fight_types.FightResult(won=True)
    mentions = " " + "".join(f"<@{p.id}> " for p in players)
    channel = game_db.channel
    assert channel.contains("Zaatakowaliście bossa" + mentions + "! <:REEeee:790963160495947856>")
    assert channel.contains("Wow! Świetnie" + mentions + "! Pokonaliście legendarnego bossa!")
    assert channel.contains("Zabicie bossa zajęło Wam: 0.")
    assert channel.contains("Pobiliście rekord i zgarniacie dodatkowe 1500 doświadczenia na discordzie!")
    assert len(prompts(channel)) == 3

    rows = await bot.db.fetch("SELECT ID, NICK, POINTS FROM RANKING")
    assert {(r["id"], r["nick"], int(r["points"])) for r in rows} == {(str(p.id), p.name, 4) for p in players}
    assert (await database.read_record_table(bot))[1] == "Party Ala"
    assert (await database.read_history_table(bot))[1] == "Party Ala"
    assert bot.store.exp_summary.load() == {str(p.id): 250 for p in players}
    assert mentions + " otrzymali: 1500 expa za rekord" in bot.owner_log
    for player in players:
        assert f"{player.name} otrzymał: 👉 250 doświadczenia na Discordzie\n" in bot.owner_log
        assert channel.contains(f"Boss Drop - {player.name}")
    # Legacy rule: a legendary kill is not counted as a personal boss kill.
    assert await bot.db.fetchval("SELECT count(*) FROM ACHIEVEMENTS") == 0


async def test_boss_fights_give_only_the_mythic_slayer_role(game_db, monkeypatch):
    """Owner's decision of 2026-11-20: no speed / legendary boss role any more; the mythic slayer stays."""
    bot = game_db.bot
    for role_id, name in achievements.RETIRED_ROLES.items():        # (a server that was not tidied yet)
        game_db.guild.add_role(role_id, name)
    retired = set(achievements.RETIRED_ROLES)
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 2)

    # A supporter kills a mythic boss - in well under 0.75 s per turn (the old speed role).
    slayer = game_db.add_member("Ala", roles=[patrons.CRAFTER_ROLE])
    game_db.auto_answer(slayer)
    assert (await boss.single_fight(game_db.scene(slayer), slayer, 4, False, None)).won
    assert slayer in bot.role(bot.ids.mythic_slayer_role).members
    assert not {role.id for role in slayer.roles} & retired

    # A legendary party of supporters: no legendary boss role either.
    game_db.channel.send_hooks.clear()
    party = [game_db.add_member(name, roles=[patrons.CRAFTER_ROLE]) for name in ("Ola", "Ela", "Ula", "Iza")]
    team_answers(game_db, party)
    assert (await boss.group_fight(game_db.scene(party[0]), party, False, None)).won
    assert not any({role.id for role in player.roles} & retired for player in party)
    assert not any(player in bot.role(bot.ids.mythic_slayer_role).members for player in party)
    assert not game_db.channel.contains("zdobywa osiągnięcie")


async def test_group_fight_second_mistake_loses(game_db, monkeypatch):
    bot = game_db.bot
    players = [game_db.add_member(name) for name in ("Ala", "Ola", "Ela", "Ula")]
    patron = game_db.add_member("Patron")
    await modifiers.modify_modifiers(bot, "ban_loser", 1)
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 9)
    team_answers(game_db, players, wrong_rounds={2}, intruder_rounds={4})
    result = await boss.group_fight(game_db.scene(players[0]), players, True, patron)
    assert not result.won and result.losers_to_ban == players
    # The first mistake used the spare chance silently; the word typed by the wrong player lost.
    assert len(prompts(game_db.channel)) == 4
    assert game_db.channel.count("Pomyliłeś się!") == 1
    assert bot.owner_log == ["Party Ala pomyliło się i nie zabili bossa. "
                             "Bossem był Patron i należy mu się 2000 expa."]
    assert bot.store.exp_summary.load() == {str(patron.id): 2000}


async def test_group_fight_timeouts_and_party_member_as_the_boss(game_db, monkeypatch, short_timeouts):
    bot = game_db.bot
    players = [game_db.add_member(name) for name in ("Ala", "Ola", "Ela", "Ula")]
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 9)
    team_answers(game_db, players, silent_rounds={1, 3})
    result = await boss.group_fight(game_db.scene(players[0]), players, True, players[2])
    assert not result.won and result.losers_to_ban == []
    assert game_db.channel.messages[-1].content.startswith("Niestety nie zdążyłeś! <:Bedge:970576892874854400>")
    assert bot.store.exp_summary.load() == {}             # a party member posing as the boss earns nothing
    assert "sam brał udział w walce" in bot.owner_log[0]


async def test_group_fight_critical_hits_count(game_db, pets, monkeypatch):
    players = [game_db.add_member(name) for name in ("Ala", "Ola", "Ela", "Ula")]
    for player in players:
        pets.skills[player.id] = {"CRIT_PERC": Decimal("100"), "LOWHP_PERC": Decimal("5")}
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 20)   # 20 hp - 4 x 5 % = 16
    team_answers(game_db, players)
    assert (await boss.group_fight(game_db.scene(players[0]), players, False, None)).won
    assert len(prompts(game_db.channel)) == 8             # every hit counts twice


# -- duel --------------------------------------------------------------------------------

def duel_setup(game, first, second, *, starter=None, **team_kwargs):
    if starter is not None:
        on_bot_message(game, "Zaklęcie to",
                       lambda: game.say_later(starter, requested_word(game.channel.sent[-1].content), 0))
    team_answers(game, [first, second], **team_kwargs)


async def test_duel_winner_loser_and_ban(game_db, pets):
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("O'la")
    pets.skills[ala.id] = {"CRIT_PERC": Decimal("100"), "LOWHP_PERC": Decimal("50")}
    duel_setup(game_db, ala, ola, starter=ala)
    assert await boss.duel_fight(game_db.scene(), [ala, ola]) == (True, ala)

    channel = game_db.channel
    assert channel.contains(f"Rozpoczyna się walka pomiędzy <@{ala.id}> oraz <@{ola.id}>!")
    red, blue = [m.embeds[0] for m in channel.sent if m.embeds][:2]
    assert red.title == "🟥 Ala" and red.description.startswith("❤️ HP: 3000\n\n")
    assert "💥 Szansa na krytyczne uderzenie: 100 %" in red.description
    assert blue.title == "🟦 O'la" and blue.description.startswith("❤️ HP: 1500\n\n")
    assert blue.thumbnail.url == ola.display_avatar.url
    assert channel.contains("Zaczyna **Ala**.")
    # (5 s - reaction time) x 100, doubled by the critical hit; the clock may tick during the round.
    assert said(channel, r"Ala zadał (99\d|1000) obrażeń \(zredukowane o 0\)\.\n\nAla 🟥 HP: 3000\nO'la 🟦 HP: 50\d\n\n")
    assert channel.contains("**Ala** wygrał walkę z **O'la**.")
    assert channel.contains("Boss Drop - Ala")

    points = {r["id"]: int(r["points"]) for r in await bot.db.fetch("SELECT ID, POINTS FROM RANKING")}
    assert points == {str(ala.id): 6, str(ola.id): -6}
    assert bot.role(bot.ids.dead_hunter_role) in ola.roles
    assert game_db.notice_channel.contains(f"poległeś z ręki drugiego gracza <@{ola.id}>")


async def test_duel_out_of_turn_and_no_ban(game_db):
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    duel_setup(game_db, ala, ola, starter=ola, intruder_rounds={1}, wrong_rounds={2})
    finished, winner = await boss.duel_fight(game_db.scene(), [ala, ola], False)
    assert finished and winner is ola                      # Ola started and Ala gave her a free hit
    assert game_db.channel.contains("Zaczyna **Ola**.")
    assert game_db.channel.contains("Nie Twoja kolej! <:PepeHands:783992337377918986> "
                                    "Przeciwnik zadaje maksymalne obrażenia!")
    assert game_db.channel.contains("Ola zadał 500 obrażeń (zredukowane o 0).\n\nAla 🟥 HP: 2500\nOla 🟦 HP: 3000")
    assert game_db.channel.contains("Pomyliłeś się! <:PepeHands:783992337377918986> Nie zadajesz obrażeń!")
    assert game_db.channel.contains("Ala zadał 0 obrażeń (zredukowane o 0).")
    assert bot.role(bot.ids.dead_hunter_role) not in ala.roles
    assert not game_db.notice_channel.messages


async def test_duel_draw_returns_a_tuple(game_db, monkeypatch, short_timeouts):
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    monkeypatch.setattr(boss, "DUEL_MAX_TURNS", 3)
    monkeypatch.setattr(random, "choice", lambda options: options[-1])
    assert await boss.duel_fight(game_db.scene(), [ala, ola]) == (False, None)
    channel = game_db.channel
    assert channel.contains('"Ten, który atakuje pierwszy, będzie wybrany losowo."')
    assert "Timeout podczas rozpoczynania pojedynku." in game_db.bot.owner_log
    assert channel.contains("Zaczyna **Ola**.")
    assert channel.count("Niestety nie zdążyłeś! <:Bedge:970576892874854400> Nie zadajesz obrażeń!") == 3
    assert channel.messages[-1].content == "Walka trwa zbyt długo. Remis!"
    assert await game_db.bot.db.fetchval("SELECT count(*) FROM RANKING") == 0


async def test_duel_pet_attack_and_interruption(game_db, pets, monkeypatch):
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    pets.skills[ala.id] = {"REPLACE_PERC": Decimal("100")}
    duel_setup(game_db, ala, ola, starter=ala)

    async def dead_line(bot, channel, prompt, **kwargs):
        return RoundResult(Outcome.DISRUPTED)

    monkeypatch.setattr(challenge, "ask", dead_line)
    assert await boss.duel_fight(game_db.scene(), [ala, ola]) == (False, None)
    assert game_db.channel.sent[-1].content == boss.MSG_DUEL_INTERRUPTED
    assert any(m.content.startswith("~~1. Ala:") and m.content.endswith("~~ Twój towarzysz wyprowadza atak!")
               for m in game_db.channel.sent)
    assert said(game_db.channel, r"Ala zadał (49\d|500) obrażeń")
    assert await game_db.bot.db.fetchval("SELECT count(*) FROM RANKING") == 0


async def test_duel_pets_defend_and_soften_mistakes(game_db, pets):
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    pets.skills[ala.id] = {"DEF_PERC": Decimal("100")}      # Ala's own mistakes still hurt a little...
    pets.skills[ola.id] = {"DEF_PERC": Decimal("40")}       # ...and Ola's pet absorbs 40 % of every hit
    duel_setup(game_db, ala, ola, starter=ala, wrong_rounds={1}, intruder_rounds={2})
    finished, winner = await boss.duel_fight(game_db.scene(), [ala, ola], False)
    assert finished and winner is not None
    channel = game_db.channel
    assert channel.contains("Pomyliłeś się, ale Twój towarzysz i tak wyprowadza słaby atak!")
    # (5 s - reaction) / 3 x 100 = 166, of which 40 % is absorbed.
    assert said(channel, r"Ala zadał (99|100) obrażeń \(zredukowane o 6[67]\)\.\n\nAla 🟥 HP: 3000\nOla 🟦 HP: 29\d\d\n")
    # Ala wrote during Ola's turn; Ola's pet rolled 40 % against "random() * 100 < 40" -> see below.
    assert (channel.contains("Nie Twoja kolej! <:PepeHands:783992337377918986> Przeciwnik zadaje maksymalne obrażenia!")
            or channel.contains("Nie Twoja kolej, ale Twój towarzysz Cię broni!"))


# -- roles -------------------------------------------------------------------------------

async def test_set_boss_slayer_moves_the_role(game):
    bot = game.bot
    old = game.add_member("Stary", roles=[bot.ids.boss_slayer_role])
    new = game.add_member("Nowy")
    role = bot.role(bot.ids.boss_slayer_role)
    assert await boss.set_boss_slayer(bot, str(new.id))
    assert role.members == [new] and not boss.is_boss_slayer(bot, old) and boss.is_boss_slayer(bot, new)
    assert await boss.set_boss_slayer(bot, new.id)          # winning twice in a row keeps it
    assert role.members == [new]


async def test_set_boss_slayer_winner_left_the_server(game):
    bot = game.bot
    old = game.add_member("Stary", roles=[bot.ids.boss_slayer_role])
    assert not await boss.set_boss_slayer(bot, 424242)
    assert bot.role(bot.ids.boss_slayer_role).members == [] and old.roles == []
    assert any("opuścił serwer" in line and "<@424242>" in line for line in bot.owner_log)


async def test_dead_hunters(game):
    bot = game.bot
    role = bot.role(bot.ids.dead_hunter_role)
    ala, ola, ela = game.add_member("Ala"), game.add_member("Ola"), game.add_member("Ela")
    assert await boss.set_dead_hunters(bot, ala)
    assert await boss.set_dead_players(bot, ola.id)
    assert await boss.set_dead_assassin(bot, str(ela.id))
    assert set(role.members) == {ala, ola, ela}
    notices = [m.content for m in game.notice_channel.messages]
    assert notices == [
        f"Na kanale <#970684202880204831> poległeś z ręki {killer} <@{member.id}>, a groziło to banem... "
        "<:RIP:912797982917816341> Musisz odpocząć od przygód aż do momentu zabicia następnego bossa."
        for killer, member in (("potwora", ala), ("drugiego gracza", ola), ("zabójcy", ela))
    ]
    await boss.reset_dead_hunters(bot)
    assert role.members == []


async def test_dead_hunters_never_raise(game):
    bot = game.bot
    ala = game.add_member("Ala")
    gone = FakeMember("Zniknął")                            # not on the server any more
    assert not await boss.set_dead_hunters(bot, gone)
    assert not await boss.set_dead_players(bot, 424242)
    assert not await boss.set_dead_assassin(bot, None)
    assert not game.notice_channel.messages

    game.notice_channel.fail_with, game.notice_channel.fail_sends = RuntimeError("discord broke"), 1
    assert await boss.set_dead_hunters(bot, ala)            # the role matters, the notice is a bonus
    del bot.fake_channels[bot.ids.notice_channel]
    assert await boss.set_dead_assassin(bot, ala)
    game.guild.roles.clear()                                # even the role is gone
    assert not await boss.set_dead_players(bot, ala)
    await boss.reset_dead_hunters(bot)


async def test_set_mythic_slayer(game):
    bot = game.bot
    ala, ola = game.add_member("Ala"), game.add_member("Ola")
    await boss.set_mythic_slayer(game.scene(), ala.id)
    assert game.channel.messages[-1].content == (
        f"<@&985071779787730944>! Łowca <@{ala.id}> zabił mitycznego bossa! <:POGGERS:936907543849078844> "
        "Jest to pierwszy łowca, który tego dokonał!")
    await boss.set_mythic_slayer(game.scene(), ola.id)
    assert game.channel.messages[-1].content.endswith("Do tej pory udało się to tylko 1 łowcom!")
    assert set(bot.role(bot.ids.mythic_slayer_role).members) == {ala, ola}


async def test_reset_end(game_db, monkeypatch):
    bot = game_db.bot
    dead = game_db.add_member("Ala", roles=[bot.ids.dead_hunter_role])
    await modifiers.modify_modifiers(bot, "hp_boost_perc", 10)
    await database.update_boss_table(bot, 2, 500, True)
    await boss.reset_end(bot)
    assert await modifiers.load_modifiers(bot) == modifiers.INIT_MODIFIERS
    row = await database.read_boss_table(bot)
    assert (row.rarity, row.respawn_started) == (0, False)
    assert dead.roles == []

    # One failing step does not stop the others; the error is raised afterwards.
    dead.roles.append(bot.role(bot.ids.dead_hunter_role))
    await modifiers.modify_modifiers(bot, "hp_boost_perc", 10)

    async def no_database(*args, **kwargs):
        raise ConnectionError("database gone")

    monkeypatch.setattr(database, "update_boss_table", no_database)
    with pytest.raises(ConnectionError):
        await boss.reset_end(bot)
    assert await modifiers.load_modifiers(bot) == modifiers.INIT_MODIFIERS
    assert dead.roles == []


# -- with the real boss cycle ------------------------------------------------------------

async def test_cycle_runs_a_lost_fight_and_bans_the_loser_after_the_reset(game_db, monkeypatch, short_timeouts):
    bot = game_db.bot
    dead_role = bot.role(bot.ids.dead_hunter_role)
    hunter = game_db.add_member("Ala")
    revived = game_db.add_member("Ola", roles=[bot.ids.dead_hunter_role])
    await modifiers.modify_modifiers(bot, "ban_loser", 1)
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 5)
    bot.state.boss = BossInfo(rarity=0)
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    game_db.auto_answer(hunter, wrong_on_round=2)
    await bot.boss_cycle.attack(game_db.scene(hunter, "$zaatakuj"))
    assert bot.state.boss_phase is BossPhase.IDLE and not bot.state.busy
    assert dead_role.members == [hunter] and revived.roles == []
    assert await modifiers.load_modifiers(bot) == modifiers.INIT_MODIFIERS
    assert game_db.notice_channel.contains(f"poległeś z ręki potwora <@{hunter.id}>")


async def test_cycle_keeps_the_boss_after_a_crashed_fight(game_db, monkeypatch, short_timeouts):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 4)
    bot.state.boss = BossInfo(rarity=1)
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    game_db.auto_answer(hunter)
    original = game_db.channel.send
    failures = iter([RuntimeError("discord broke")])

    async def broken_once(content=None, **kwargs):
        if content and content.startswith("2. "):
            for error in failures:
                raise error
        return await original(content, **kwargs)

    game_db.channel.send = broken_once
    listeners = len(bot.extra_events.get("on_message", []))
    await bot.boss_cycle.attack(game_db.scene(hunter, "$zaatakuj"))
    await asyncio.sleep(0.05)
    # Nothing is stuck: the boss is still there, nothing was reset, no listener leaked.
    assert bot.state.boss_phase is BossPhase.VISIBLE and not bot.state.busy
    assert bot.state.boss.rarity == 1
    assert len(bot.extra_events.get("on_message", [])) == listeners
    assert game_db.channel.contains("walka została przerwana")
    assert await bot.db.fetchval("SELECT count(*) FROM RANKING") == 0

    await bot.boss_cycle.attack(game_db.scene(hunter, "$zaatakuj"))      # the second try works
    assert bot.state.boss_phase is BossPhase.IDLE
    assert await bot.db.fetchval("SELECT POINTS FROM RANKING WHERE ID = $1", str(hunter.id)) == 3


async def test_cycle_runs_a_won_fight(game_db, monkeypatch, short_timeouts):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 2)
    bot.state.boss = BossInfo(rarity=2)
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    game_db.auto_answer(hunter)
    await bot.boss_cycle.attack(game_db.scene(hunter, "$zaatakuj"))
    assert bot.state.boss_phase is BossPhase.IDLE
    assert await bot.db.fetchval("SELECT POINTS FROM RANKING WHERE ID = $1", str(hunter.id)) == 6
    assert bot.owner_log[-1].startswith("Ala otrzymał: \n👉 250")


# -- changed commands (shrine modifier) --------------------------------------------------

CHANGED_WORDS = [word for word, _ in modifiers.CHANGED_ACTIONS]
BOSS_WORDS = [word for word, _ in boss.BOSS_ACTIONS]
MYTHIC_WORDS = [word for word, _ in boss.MYTHIC_ACTIONS]
DUEL_WORDS = [word for word, _ in boss.DUEL_ACTIONS]


def walk_every_action(monkeypatch, *tables) -> None:
    """``random.randint`` picks index 0, 1, 2, ... of a word table in turn (each table its own walk).

    Only the draws ``randint(0, len(table) - 1)`` of the given tables are pinned; every other
    draw stays random, so a fight that draws from another table is not steered.
    """
    real_randint = random.randint
    walks = {len(table) - 1: itertools.count() for table in tables}

    def randint(a, b):
        if a == 0 and b in walks:
            return next(walks[b]) % (b + 1)
        return real_randint(a, b)

    monkeypatch.setattr(random, "randint", randint)


def asked_words(channel) -> list[str]:
    return [requested_word(prompt) for prompt in prompts(channel)]


@pytest.mark.parametrize("rarity", [0, 1, 2])
async def test_changed_commands_replace_every_word_of_a_single_fight(game_db, monkeypatch, rarity):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    await modifiers.modify_modifiers(bot, modifiers.COMMANDS_CHANGED, 1)
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: len(modifiers.CHANGED_ACTIONS))
    walk_every_action(monkeypatch, modifiers.CHANGED_ACTIONS, boss.BOSS_ACTIONS)
    game_db.auto_answer(hunter)
    result = await boss.single_fight(game_db.scene(hunter), hunter, rarity, False, None)
    assert result == fight_types.FightResult(won=True)
    assert game_db.channel.contains(boss.MSG_SINGLE_WON.format(user_id=hunter.id))
    # Every one of the 13 words, once each, with "Boss" in the templates that name the foe.
    assert asked_words(game_db.channel) == CHANGED_WORDS
    assert prompts(game_db.channel) == [f"{number}. " + template.format(foe="Boss")
                                        for number, (_, template) in enumerate(modifiers.CHANGED_ACTIONS, 1)]
    assert sum("Boss " in prompt for prompt in prompts(game_db.channel)) == 9
    assert not set(asked_words(game_db.channel)) & set(BOSS_WORDS)
    assert not any("{foe}" in m.content or "Potwór" in m.content for m in game_db.channel.sent)
    # The fight does not use the modifier up: the boss cycle resets it afterwards.
    assert (await modifiers.load_modifiers(bot))[modifiers.COMMANDS_CHANGED] == 1


async def test_single_fight_without_changed_commands_keeps_the_boss_words(game_db, monkeypatch):
    hunter = game_db.add_member("Ala")
    await modifiers.modify_modifiers(game_db.bot, "ban_loser", 1)          # some other modifier is on
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: len(boss.BOSS_ACTIONS))
    walk_every_action(monkeypatch, boss.BOSS_ACTIONS, modifiers.CHANGED_ACTIONS)
    game_db.auto_answer(hunter)
    assert (await boss.single_fight(game_db.scene(hunter), hunter, 1, False, None)).won
    assert asked_words(game_db.channel) == BOSS_WORDS
    assert prompts(game_db.channel) == [f"{number}. {template}"
                                        for number, (_, template) in enumerate(boss.BOSS_ACTIONS, 1)]


async def test_changed_commands_do_not_apply_to_the_mythic_boss(game_db, monkeypatch):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    await modifiers.modify_modifiers(bot, modifiers.COMMANDS_CHANGED, 1)
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 2 * len(boss.MYTHIC_ACTIONS))
    walk_every_action(monkeypatch, boss.MYTHIC_ACTIONS, modifiers.CHANGED_ACTIONS, boss.BOSS_ACTIONS)
    game_db.auto_answer(hunter)
    assert (await boss.single_fight(game_db.scene(hunter), hunter, 4, False, None)).won
    assert asked_words(game_db.channel) == MYTHIC_WORDS * 2
    assert not set(asked_words(game_db.channel)) & set(CHANGED_WORDS)
    assert (await modifiers.load_modifiers(bot))[modifiers.COMMANDS_CHANGED] == 1


async def test_changed_commands_replace_every_word_of_the_group_fight(game_db, monkeypatch):
    players = [game_db.add_member(name) for name in ("Ala", "Ola", "Ela", "Ula")]
    await modifiers.modify_modifiers(game_db.bot, modifiers.COMMANDS_CHANGED, 1)
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: 2 * len(modifiers.CHANGED_ACTIONS))
    walk_every_action(monkeypatch, modifiers.CHANGED_ACTIONS, boss.BOSS_ACTIONS)
    team_answers(game_db, players)
    result = await boss.group_fight(game_db.scene(players[0]), players, False, None)
    assert result == fight_types.FightResult(won=True)
    assert asked_words(game_db.channel) == CHANGED_WORDS * 2
    names = {player.name for player in players}
    for number, prompt in enumerate(prompts(game_db.channel), 1):
        match = re.match(r"^(\d+)\. \*\*(.+?)\*\*: (.*)$", prompt)
        assert match is not None and int(match.group(1)) == number and match.group(2) in names
        template = modifiers.CHANGED_ACTIONS[(number - 1) % len(modifiers.CHANGED_ACTIONS)][1]
        assert match.group(3) == template.format(foe="Boss")
    assert not set(asked_words(game_db.channel)) & set(BOSS_WORDS)


async def test_changed_commands_do_not_apply_to_duels(game_db, monkeypatch):
    bot = game_db.bot
    ala, ola = game_db.add_member("Ala"), game_db.add_member("Ola")
    await modifiers.modify_modifiers(bot, modifiers.COMMANDS_CHANGED, 1)
    walk_every_action(monkeypatch, boss.DUEL_ACTIONS, modifiers.CHANGED_ACTIONS)
    duel_setup(game_db, ala, ola, starter=ala)
    assert await boss.duel_fight(game_db.scene(), [ala, ola], False) == (True, ala)
    asked = asked_words(game_db.channel)
    assert len(asked) >= 5 and asked == DUEL_WORDS[:len(asked)]
    assert not set(asked) & set(CHANGED_WORDS)
    assert (await modifiers.load_modifiers(bot))[modifiers.COMMANDS_CHANGED] == 1


@pytest.mark.parametrize("won", [True, False])
async def test_changed_commands_end_with_the_boss_fight(game_db, monkeypatch, short_timeouts, won):
    bot = game_db.bot
    hunter = game_db.add_member("Ala")
    await modifiers.modify_modifiers(bot, modifiers.COMMANDS_CHANGED, 1)
    assert "Komendy w walce są zmienione" in await modifiers.load_desc_modifiers(bot)
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: len(modifiers.CHANGED_ACTIONS))
    walk_every_action(monkeypatch, modifiers.CHANGED_ACTIONS, boss.BOSS_ACTIONS)
    bot.state.boss = BossInfo(rarity=2)
    await bot.state.set_boss_phase(BossPhase.VISIBLE)
    game_db.auto_answer(hunter, wrong_on_round=None if won else 4)
    await bot.boss_cycle.attack(game_db.scene(hunter, "$zaatakuj"))
    assert bot.state.boss_phase is BossPhase.IDLE and not bot.state.busy
    assert asked_words(game_db.channel) == (CHANGED_WORDS if won else CHANGED_WORDS[:4])
    assert (await bot.db.fetchval("SELECT count(*) FROM RANKING")) == (1 if won else 0)

    # The reset after the fight switched the modifier off - in the file as well.
    assert (await modifiers.load_modifiers(bot))[modifiers.COMMANDS_CHANGED] == 0
    assert bot.store.modifiers.load()[modifiers.COMMANDS_CHANGED] == 0
    assert await modifiers.load_desc_modifiers(bot) == ""

    # ...so the next boss asks for the usual words again.
    game_db.channel.sent.clear()
    game_db.channel.send_hooks.clear()
    monkeypatch.setattr(boss, "random_boss_hp", lambda rarity: len(boss.BOSS_ACTIONS))
    game_db.auto_answer(hunter)
    assert (await boss.single_fight(game_db.scene(hunter), hunter, 1, False, None)).won
    assert asked_words(game_db.channel) == BOSS_WORDS


# -- boss slayer perks -------------------------------------------------------------------

async def test_flex_gif_respects_the_upload_limit(game, monkeypatch, tmp_path):
    folder = tmp_path / "assets" / "flexgifs"
    folder.mkdir(parents=True)
    (folder / "big.gif").write_bytes(b"x" * 5000)
    monkeypatch.setattr(boss, "ASSETS_DIR", tmp_path / "assets")
    game.guild.filesize_limit = 1000
    assert not await boss.flex_gif(game.scene())            # only a file that is too big
    assert game.channel.sent == []
    (folder / "small.gif").write_bytes(b"x" * 100)
    for _ in range(5):
        assert await boss.flex_gif(game.scene())
    assert {tuple(m.files) for m in game.channel.sent} == {("small.gif",)}


def test_real_flex_gifs_fit_the_default_limit():
    from altermmo import settings
    sizes = [p.stat().st_size for p in (settings.ASSETS_DIR / "flexgifs").iterdir() if p.is_file()]
    assert any(size <= boss.DEFAULT_UPLOAD_LIMIT for size in sizes)


async def test_change_color(game):
    bot = game.bot
    slayer_member = game.add_member("Ala", roles=[bot.ids.boss_slayer_role])
    role = bot.role(bot.ids.boss_slayer_role)
    scene = game.scene(slayer_member)
    assert await boss.change_color(scene, "ff0000")
    assert role.edits[-1] == {"colour": discord.Colour(0xFF0000), "reason": "Zmiana przez użytkownika."}
    assert await boss.change_color(scene, "#00Ff00") and role.edits[-1]["colour"].value == 0x00FF00
    assert game.channel.sent == []                          # silent on success, as before

    for bad in ("zielony", "", None, "ff00000", "12 34"):
        assert not await boss.change_color(scene, bad)
        assert game.channel.messages[-1].content == boss.MSG_BAD_COLOUR
    assert len(role.edits) == 2

    async def forbidden(**kwargs):
        raise http_error(403)

    role.edit = forbidden
    assert not await boss.change_color(scene, "0000ff")
    assert game.channel.messages[-1].content == boss.MSG_COLOUR_FAILED


async def test_change_icon(game):
    bot = game.bot
    slayer_member = game.add_member("Ala", roles=[bot.ids.boss_slayer_role])
    role = bot.role(bot.ids.boss_slayer_role)
    scene = game.scene(slayer_member, "$ikona")

    assert not await boss.change_icon(scene)
    assert game.channel.messages[-1].content == "Dodaj ikonę w wiadomości z komendą - format .png <:madge:882184635474386974>"

    scene.message.attachments = [FakeAttachment(filename="icon.jpg", content_type="image/jpeg")]
    assert not await boss.change_icon(scene)
    assert game.channel.messages[-1].content == (f"<@{slayer_member.id}>, zły format ikony. Poprawny format "
                                                 "to .png. <:madge:882184635474386974>")

    scene.message.attachments = [FakeAttachment(data=b"\x89PNG-icon")]
    assert await boss.change_icon(scene)
    assert role.edits == [{"display_icon": b"\x89PNG-icon", "reason": "Zmiana przez użytkownika."}]
    assert game.channel.messages[-1].content == "Ikona została zmieniona Wielki Wojowniku. <:peepoglad:895774173887094874>"

    async def refused(**kwargs):
        raise http_error(400)                               # Discord did not like the picture

    role.edit = refused
    assert not await boss.change_icon(scene)
    assert game.channel.messages[-1].content == boss.MSG_ICON_REJECTED


class CountedAttachment(FakeAttachment):
    """An attachment that knows its size (as Discord tells it) and counts its downloads."""

    def __init__(self, data: bytes = b"\x89PNG", *, size: int | None = None, error: BaseException | None = None):
        super().__init__(data=data)
        self.size = len(data) if size is None else size
        self.error = error
        self.reads = 0

    async def read(self) -> bytes:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.data


@pytest.fixture
def quick_retries(monkeypatch):
    """io.retry without its back-off pauses (the retries themselves still happen)."""
    real_retry = io.retry

    async def retry(factory, **kwargs):
        return await real_retry(factory, **{**kwargs, "first_delay": 0.0})

    monkeypatch.setattr(io, "retry", retry)


async def test_change_icon_too_big_is_refused_before_the_download(game):
    bot = game.bot
    champion = game.add_member("Mistrz", roles=[bot.ids.boss_slayer_role])
    role = bot.role(bot.ids.boss_slayer_role)
    cog = slayer.SlayerCog(bot)
    big = CountedAttachment(size=boss.ROLE_ICON_LIMIT + 1)
    ctx, resets = make_ctx(game, champion, attachments=[big])
    await cog.icon.callback(cog, ctx)
    assert game.channel.messages[-1].content == boss.MSG_ICON_TOO_BIG
    assert big.reads == 0 and role.edits == [] and resets == [ctx]      # the cooldown is given back

    # Discord did not say the size: the downloaded bytes are measured instead.
    unknown = CountedAttachment(b"\x89PNG" + b"x" * boss.ROLE_ICON_LIMIT, size=0)
    scene = game.scene(champion, "$ikona")
    scene.message.attachments = [unknown]
    assert not await boss.change_icon(scene)
    assert unknown.reads == 1 and role.edits == []
    assert game.channel.messages[-1].content == boss.MSG_ICON_TOO_BIG


def _icon_refusal(kind: str) -> BaseException:
    response = SimpleNamespace(status=403 if kind == "forbidden" else 400, reason="nope")
    if kind == "forbidden":
        return discord.Forbidden(response, {"code": 50013, "message": "Missing Permissions"})
    if kind == "boosts":
        return discord.HTTPException(response, {"code": 50101, "message": "needs more boosts"})
    if kind == "rejected":
        return discord.HTTPException(response, {"code": 50035, "message": "Invalid Form Body"})
    return asyncio.TimeoutError()


@pytest.mark.parametrize("kind, answer", [
    ("forbidden", boss.MSG_ICON_FORBIDDEN),
    ("boosts", boss.MSG_ICONS_UNAVAILABLE),
    ("rejected", boss.MSG_ICON_REJECTED),
    ("network", boss.MSG_ICON_NOT_SENT),        # the picture was downloaded; sending it to Discord failed
])
async def test_change_icon_tells_apart_why_discord_refused(game, quick_retries, kind, answer):
    bot = game.bot
    champion = game.add_member("Mistrz", roles=[bot.ids.boss_slayer_role])
    role = bot.role(bot.ids.boss_slayer_role)
    tries = []

    async def edit(**kwargs):
        tries.append(kwargs)
        raise _icon_refusal(kind)

    role.edit = edit
    cog = slayer.SlayerCog(bot)
    ctx, resets = make_ctx(game, champion, attachments=[CountedAttachment()])
    await cog.icon.callback(cog, ctx)
    assert game.channel.messages[-1].content == answer
    assert resets == [ctx]                                   # change_icon said False: no half-hour wait
    assert len(tries) == (3 if kind == "network" else 1)    # only the network error is retried
    owner_lines = [m.content for m in game.log_channel.messages]
    if kind == "forbidden":
        assert len(owner_lines) == 1 and owner_lines[0].startswith(f"<@{bot.ids.owner}>!")
        assert "$ikona" in owner_lines[0]
    else:
        assert owner_lines == []


async def test_change_icon_download_that_keeps_failing(game, quick_retries):
    bot = game.bot
    champion = game.add_member("Mistrz", roles=[bot.ids.boss_slayer_role])
    role = bot.role(bot.ids.boss_slayer_role)
    broken = CountedAttachment(error=asyncio.TimeoutError())
    scene = game.scene(champion, "$ikona")
    scene.message.attachments = [broken]
    assert not await boss.change_icon(scene)
    assert broken.reads == 3 and role.edits == []            # retried, then given up
    assert game.channel.messages[-1].content == boss.MSG_ICON_DOWNLOAD_FAILED


def make_ctx(game, author, *, attachments=None):
    message = FakeMessage(game.channel, author, "$komenda", attachments=attachments)
    resets: list = []
    ctx = SimpleNamespace(bot=game.bot, channel=game.channel, author=author, message=message,
                          guild=game.guild, command=SimpleNamespace(reset_cooldown=resets.append))
    return ctx, resets


async def test_slayer_commands(game, monkeypatch, tmp_path):
    bot = game.bot
    folder = tmp_path / "assets" / "flexgifs"
    folder.mkdir(parents=True)
    (folder / "flex.gif").write_bytes(b"gif")
    monkeypatch.setattr(boss, "ASSETS_DIR", tmp_path / "assets")
    champion = game.add_member("Mistrz", roles=[bot.ids.boss_slayer_role])
    nobody = game.add_member("Ktoś")
    role = bot.role(bot.ids.boss_slayer_role)
    cog = slayer.SlayerCog(bot)

    # $flex
    ctx, _ = make_ctx(game, nobody)
    await cog.flex.callback(cog, ctx)
    assert game.channel.messages[-1].content == "<:KEKW:936907435921252363> **Miernota** <:2Head:882184634572627978>"
    ctx, _ = make_ctx(game, champion)
    await cog.flex.callback(cog, ctx)
    assert game.channel.messages[-2].files == ["flex.gif"]
    assert game.channel.messages[-1].content == (
        f"Potężny <:GigaChad:970665721321381958> <@{champion.id}> napina swe sprężyste, naoliwione muskuły! "
        "Co za widok, robi wrażenie! <:pogu:882182966372106280>")

    # $kolor
    ctx, resets = make_ctx(game, nobody)
    await cog.colour.callback(cog, ctx, "ff0000")
    assert game.channel.messages[-1].content == ("<:KEKW:936907435921252363> **Kpisz sobie, miernoto?** "
                                                 "<:2Head:882184634572627978>")
    assert role.edits == [] and resets == []
    ctx, resets = make_ctx(game, champion)
    await cog.colour.callback(cog, ctx, "ff0000")
    assert role.edits[-1]["colour"].value == 0xFF0000 and resets == []
    await cog.colour.callback(cog, ctx, "czerwony")          # a typo gives the cooldown back
    await cog.colour.callback(cog, ctx)
    assert resets == [ctx, ctx] and len(role.edits) == 1

    # $ikona
    ctx, resets = make_ctx(game, nobody, attachments=[FakeAttachment()])
    await cog.icon.callback(cog, ctx)
    assert game.channel.messages[-1].content.startswith("<:KEKW:936907435921252363> **Kpisz sobie, miernoto?**")
    assert len(role.edits) == 1
    ctx, resets = make_ctx(game, champion, attachments=[FakeAttachment()])
    await cog.icon.callback(cog, ctx)
    assert role.edits[-1]["display_icon"] == b"\x89PNG" and resets == []
    ctx, resets = make_ctx(game, champion)
    await cog.icon.callback(cog, ctx)                        # forgot the attachment
    assert resets == [ctx]


async def test_slayer_cog_registers_the_legacy_commands(game):
    bot = game.bot
    await bot.load_extension("altermmo.cogs.slayer")
    for name in ("flex", "kolor", "ikona"):
        command = bot.get_command(name)
        assert command is not None and command.cog_name == "Slayer"
        cooldown = command._buckets._cooldown
        assert (cooldown.rate, cooldown.per) == (1, 1800)
        assert command._buckets.type is discord.ext.commands.BucketType.user
        assert command.checks == []                          # cosmetic: any channel, never blocked by events
