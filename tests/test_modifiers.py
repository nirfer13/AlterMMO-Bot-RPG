"""features/modifiers.py: the bossModifier.json file, the shrine and the prayers."""

from __future__ import annotations

import asyncio
import json
import random
import sys
import types

import pytest

from altermmo.features import modifiers

LEGACY_KEYS = ["hp_reduced_perc", "hp_boost_perc", "drop_boost_perc", "time_reduced_perc",
               "time_increased_perc", "rarity_boost", "ban_loser", "points_boost", "player_id"]
#: The file exactly as the legacy bot writes it (bossModifier.json in the old repository).
LEGACY_FILE = ('{"hp_reduced_perc": 0, "hp_boost_perc": 0, "drop_boost_perc": 0, "time_reduced_perc": 0, '
               '"time_increased_perc": 0, "rarity_boost": 0, "ban_loser": 0, "points_boost": 0, "player_id": 0}')
#: What v2 writes: the legacy keys in the legacy order, then the keys that are new in v2.
FILE_KEYS = LEGACY_KEYS + ["commands_changed"]
ZEROS = dict.fromkeys(FILE_KEYS, 0)


def write_file(game, text: str) -> None:
    path = game.bot.store.modifiers.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_file(game) -> dict:
    return json.loads(game.bot.store.modifiers.path.read_text(encoding="utf-8"))


def draw(monkeypatch, name: str, *, value: str = "max") -> list[list[str]]:
    """Make the next prayers draw ``name``; returns the pools ``random.choice`` was offered."""
    pools: list[list[str]] = []

    def choice(pool):
        pools.append(list(pool))
        return name

    monkeypatch.setattr(random, "choice", choice)
    monkeypatch.setattr(random, "randint", lambda low, high: high if value == "max" else low)
    return pools


@pytest.fixture
def fake_daily(monkeypatch):
    """Stand-in for features/daily.py (owned by another feature, imported lazily)."""
    import altermmo.features as features

    calls: list = []
    module = types.ModuleType("altermmo.features.daily")

    async def clear_daily_file(bot):
        calls.append(bot)

    module.clear_daily_file = clear_daily_file
    monkeypatch.setitem(sys.modules, "altermmo.features.daily", module)
    monkeypatch.setattr(features, "daily", module, raising=False)
    return calls


# -- the file ---------------------------------------------------------------------

async def test_missing_file_gives_initial_values(game):
    assert not game.bot.store.modifiers.path.exists()
    loaded = await modifiers.load_modifiers(game.bot)
    assert loaded == ZEROS and list(loaded) == FILE_KEYS
    assert await modifiers.load_desc_modifiers(game.bot) == ""


@pytest.mark.parametrize("content", ["", "   \n", "{\"hp_boost_perc\": 5", "[1, 2, 3]", "null", "\"tekst\"", "7"])
async def test_empty_or_corrupt_file_falls_back_to_initial_values(game, content):
    write_file(game, content)
    assert await modifiers.load_modifiers(game.bot) == ZEROS
    assert await modifiers.load_desc_modifiers(game.bot) == ""
    # ... and the next change heals the file instead of crashing on it.
    await modifiers.modify_modifiers(game.bot, "points_boost", 3)
    assert read_file(game) == {**ZEROS, "points_boost": 3}


async def test_incomplete_file_and_bad_values_are_repaired_on_read(game):
    write_file(game, json.dumps({"hp_boost_perc": 12, "drop_boost_perc": "15", "ban_loser": None,
                                 "time_reduced_perc": "dużo", "rarity_boost": 1.0, "reset_cd": 1,
                                 "something_else": 99}))
    loaded = await modifiers.load_modifiers(game.bot)
    assert loaded == {**ZEROS, "hp_boost_perc": 12, "drop_boost_perc": 15, "rarity_boost": 1}
    assert list(loaded) == FILE_KEYS                         # every key of the bot, nothing else
    assert all(type(value) is int for value in loaded.values())


async def test_legacy_file_is_read_and_written_in_the_legacy_format(game):
    write_file(game, LEGACY_FILE.replace('"rarity_boost": 0', '"rarity_boost": 1')
               .replace('"player_id": 0', '"player_id": 291836779495948288'))
    loaded = await modifiers.load_modifiers(game.bot)
    assert loaded["rarity_boost"] == 1                       # what the boss cycle reads
    assert loaded["player_id"] == 291836779495948288
    await modifiers.modify_modifiers(game.bot, "time_increased_perc", 40)
    assert loaded["commands_changed"] == 0                   # a key the old file lacks counts as 0
    # The legacy bot does json.load + ``modifiers[name] += value`` on this file: its keys come
    # first, in its order; what is new in v2 follows.
    stored = read_file(game)
    assert list(stored) == FILE_KEYS and list(stored)[:len(LEGACY_KEYS)] == LEGACY_KEYS
    assert stored == {**ZEROS, "rarity_boost": 1, "player_id": 291836779495948288, "time_increased_perc": 40}
    assert all(type(value) is int for value in stored.values())


async def test_init_modifiers_resets_everything(game):
    await modifiers.modify_modifiers(game.bot, "hp_boost_perc", 10)
    await modifiers.modify_modifiers(game.bot, "player_id", 123)
    await modifiers.init_modifiers(game.bot)
    assert read_file(game) == ZEROS == {**json.loads(LEGACY_FILE), "commands_changed": 0}
    assert list(read_file(game)) == FILE_KEYS
    assert await modifiers.load_modifiers(game.bot) == ZEROS


async def test_modify_modifiers_accumulates_like_the_legacy_code(game):
    await modifiers.modify_modifiers(game.bot, "drop_boost_perc", 40)
    await modifiers.modify_modifiers(game.bot, "drop_boost_perc", "7")     # legacy: value = int(value)
    await modifiers.modify_modifiers(game.bot, "points_boost", 3)
    loaded = await modifiers.load_modifiers(game.bot)
    assert loaded == {**ZEROS, "drop_boost_perc": 47, "points_boost": 3}


async def test_modify_modifiers_rejects_unknown_names_without_touching_the_file(game):
    await modifiers.modify_modifiers(game.bot, "hp_boost_perc", 5)
    for name in ("reset_cd", "hp_boost", ""):
        with pytest.raises(KeyError):
            await modifiers.modify_modifiers(game.bot, name, 1)
    assert read_file(game) == {**ZEROS, "hp_boost_perc": 5}


async def test_player_id_is_replaced_not_summed(game):
    await modifiers.modify_modifiers(game.bot, "player_id", 111)
    await modifiers.modify_modifiers(game.bot, "player_id", 222)
    assert (await modifiers.load_modifiers(game.bot))["player_id"] == 222


async def test_unwritable_store_read_error_does_not_crash_readers(game, monkeypatch):
    def broken_load():
        raise PermissionError("locked by antivirus")

    monkeypatch.setattr(game.bot.store.modifiers, "load", broken_load)
    assert await modifiers.load_modifiers(game.bot) == ZEROS
    assert await modifiers.load_desc_modifiers(game.bot) == ""


async def test_description_lists_active_modifiers_verbatim(game):
    for name, value in (("hp_reduced_perc", 7), ("hp_boost_perc", 8), ("drop_boost_perc", 25),
                        ("time_reduced_perc", 5), ("time_increased_perc", 40), ("rarity_boost", 1),
                        ("ban_loser", 1), ("points_boost", 3), ("player_id", 42), ("commands_changed", 1)):
        await modifiers.modify_modifiers(game.bot, name, value)
    assert await modifiers.load_desc_modifiers(game.bot) == (
        "\n\nDodatkowe statystyki:"
        "\n🔺 Życie jest zmniejszone o 7 %"
        "\n🔻 Życie jest zwiększone o 8 %"
        "\n🔺 Drop zwiększony o 25 %"
        "\n🔻 Czas na reakcję zmniejszony o 5 %"
        "\n🔺 Czas na reakcję zwiększony o 40 %"
        "\n🔺 Rzadkość zwiększona o 1 poziom (nie dotyczy potworów)"
        "\n🔻 Przegrany zostanie zbanowany"
        "\n🔺 Punkty za wygraną zwiększone o 3 (nie dotyczy potworów)"
        "\n🔺 Bossem będzie gracz <@42>"
        "\n🔀 Komendy w walce są zmienione (nie dotyczy mitycznego bossa)"
    )


async def test_description_skips_inactive_modifiers(game):
    await modifiers.modify_modifiers(game.bot, "ban_loser", 1)
    await modifiers.modify_modifiers(game.bot, "hp_boost_perc", -5)      # not positive -> not listed
    assert await modifiers.load_desc_modifiers(game.bot) == (
        "\n\nDodatkowe statystyki:\n🔻 Przegrany zostanie zbanowany")


# -- the shrine -------------------------------------------------------------------

async def test_shrine_posts_picture_and_embed(game, monkeypatch):
    monkeypatch.setattr(random, "randint", lambda low, high: 2)
    before = game.bot.state.snapshot()
    await modifiers.spawn_modifier_shrine(game.scene())
    picture, shrine = game.channel.messages[-2:]
    assert picture.files == ["2.png"] and not picture.embeds
    embed = shrine.embeds[0]
    assert embed.title == "🕯️ Kapliczka zmian! 🕯️"
    assert embed.description == (
        "Oto kapliczka zmian, która wpłynie na następnego bossa! "
        "Jeśli chcesz wpłynąć na następnego bossa, to pomódl się przyciskiem pod tym postem "
        "albo wpisując **!modlitwa**.\n\n"
        "*Tylko wspierający (Patroni, Crafterzy, subskrybenci Twitch i wspierający na YouTube) znają odpowiednią "
        "modlitwę, ale każdy może skorzystać"
        " z błogosławieństw rzuconych na potwora.*")
    assert embed.colour.value == 0x609AF7
    assert embed.thumbnail.url == "https://www.altermmo.pl/wp-content/uploads/Prayge.png"
    # The event flags and the button belong to the events cog.
    assert game.bot.state.snapshot() == before and not game.bot.state.event_alive
    assert shrine.view is None


async def test_shrine_returns_its_panel_with_the_given_buttons(game):
    import discord

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Pomódl się", custom_id="altermmo:test:pray"))
    panel = await modifiers.spawn_modifier_shrine(game.scene(), view=view)
    assert panel is game.channel.messages[-1] and panel.button_labels == ["Pomódl się"]
    view.stop()


async def test_every_shrine_picture_exists():
    for number in range(modifiers.SHRINE_IMAGES):
        assert (modifiers.ASSETS_DIR / "events" / "shrines" / f"{number}.png").is_file()


async def test_shrine_draws_one_of_the_four_pictures(game, monkeypatch):
    asked = []

    def randint(low, high):
        asked.append((low, high))
        return high

    monkeypatch.setattr(random, "randint", randint)
    await modifiers.spawn_modifier_shrine(game.scene())
    assert asked == [(0, 3)]
    assert game.channel.messages[-2].files == ["3.png"]


async def test_shrine_without_picture_file_still_appears(game, monkeypatch, tmp_path):
    monkeypatch.setattr(modifiers, "ASSETS_DIR", tmp_path / "no-assets")
    count = len(game.channel.messages)
    await modifiers.spawn_modifier_shrine(game.scene())
    assert len(game.channel.messages) == count + 1
    assert game.channel.messages[-1].embeds[0].title == "🕯️ Kapliczka zmian! 🕯️"


async def test_shrine_appears_when_only_the_picture_upload_fails(game):
    game.channel.fail_with = RuntimeError("413 Payload Too Large")
    game.channel.fail_sends = 1
    count = len(game.channel.messages)
    await modifiers.spawn_modifier_shrine(game.scene())
    assert len(game.channel.messages) == count + 1
    assert game.channel.contains("Kapliczka zmian!")


async def test_shrine_that_cannot_be_posted_raises_for_the_caller(game):
    game.channel.fail_with = RuntimeError("discord is down")
    game.channel.fail_sends = 2
    with pytest.raises(RuntimeError):
        await modifiers.spawn_modifier_shrine(game.scene())
    assert not game.channel.contains("Kapliczka zmian!")


# -- prayers ----------------------------------------------------------------------

PRAYER = "Modlitwa w kapliczce sprawiła, że "
OUTCOMES = [
    ("hp_reduced_perc", 15, "następny boss i potwory **będą miały o 15% mniej życia!**"),
    ("hp_boost_perc", 15, "następny boss i potwory **będą miały o 15% więcej życia!**"),
    ("rarity_boost", 1, "następny boss **będzie o 1 poziom rzadszy!**"),
    ("drop_boost_perc", 25, "następny boss **będzie miał o 25% rzadsze przedmioty!**"),
    ("ban_loser", 1, "**następny gracz, który przegra z bossem lub potworami zostanie zbanowany "
                     "do następnego bossa!**"),
    ("time_reduced_perc", 25, "podczas walki z następnym bossem i potworami **będzie o 25% mniej "
                              "czasu na reakcję!**"),
    ("time_increased_perc", 25, "podczas walki z następnym bossem i potworami **będzie o 25% więcej "
                                "czasu na reakcję!**"),
    ("points_boost", 5, "za wygraną z kolejnym bossem **będzie o 5 więcej punktów!**"),
    ("commands_changed", 1, "w walce z następnym bossem i potworami **obowiązują inne komendy!** "
                            "*(Nie dotyczy mitycznego bossa.)*"),
]


@pytest.mark.parametrize("name, value, text", OUTCOMES, ids=[row[0] for row in OUTCOMES])
async def test_prayer_outcomes(game, monkeypatch, name, value, text):
    patron = game.add_member("O'Neil")
    draw(monkeypatch, name)
    assert await modifiers.random_modifiers(game.scene(patron, "!modlitwa"), True) == name
    assert await modifiers.load_modifiers(game.bot) == {**ZEROS, name: value}
    embed = game.channel.messages[-1].embeds[0]
    assert embed.title == "Błogosławieństwo kapliczki!"
    assert embed.description == PRAYER + text
    assert embed.url == "https://www.altermmo.pl/wp-content/uploads/Prayge.png"
    assert embed.thumbnail.url == "https://www.altermmo.pl/wp-content/uploads/Prayge.png"
    assert embed.footer.text == "Powodzenia!"
    assert embed.colour.value == 0x609AF7


@pytest.mark.parametrize("name, low, high", [
    ("hp_reduced_perc", 5, 15), ("hp_boost_perc", 5, 15), ("drop_boost_perc", 5, 25),
    ("time_reduced_perc", 5, 25), ("time_increased_perc", 5, 25), ("points_boost", 1, 5),
])
async def test_prayer_value_ranges(game, monkeypatch, name, low, high):
    asked = []
    monkeypatch.setattr(random, "choice", lambda pool: name)
    monkeypatch.setattr(random, "randint", lambda a, b: asked.append((a, b)) or a)
    await modifiers.random_modifiers(game.scene(game.add_member("Ala")), False)
    assert asked == [(low, high)]
    assert (await modifiers.load_modifiers(game.bot))[name] == low


async def test_fresh_prayer_draws_from_the_ten_legacy_outcomes_and_the_changed_commands(game, monkeypatch):
    pools = draw(monkeypatch, "rarity_boost")
    await modifiers.random_modifiers(game.scene(game.add_member("Ala")), False)
    assert pools == [LEGACY_KEYS + ["commands_changed", "reset_cd"]]         # eleven, each equally likely
    assert modifiers.COMMANDS_CHANGED == "commands_changed"


async def test_changed_commands_are_not_drawn_twice(game, monkeypatch):
    pools = draw(monkeypatch, "commands_changed")
    await modifiers.random_modifiers(game.scene(game.add_member("Ala")), False)
    assert (await modifiers.load_modifiers(game.bot))["commands_changed"] == 1
    monkeypatch.setattr(random, "choice", lambda pool: pools.append(list(pool)) or "points_boost")
    await modifiers.random_modifiers(game.scene(game.add_member("Ola")), False)
    assert "commands_changed" in pools[0] and "commands_changed" not in pools[1]
    assert (await modifiers.load_modifiers(game.bot))["commands_changed"] == 1
    await modifiers.init_modifiers(game.bot)                                 # after the boss: the usual words again
    assert (await modifiers.load_modifiers(game.bot))["commands_changed"] == 0


async def test_sacrifice_can_change_the_commands_too(game, monkeypatch):
    draw(monkeypatch, "commands_changed")
    await modifiers.random_modifiers(game.scene(game.add_member("Ala")), True, True)
    embed = game.channel.messages[-1].embeds[0]
    assert embed.title == "Poświęcono towarzysza!"
    assert embed.description == ("Poświęcenie sprawiło, że w walce z następnym bossem i potworami "
                                 "**obowiązują inne komendy!** *(Nie dotyczy mitycznego bossa.)*")


def test_changed_commands_are_the_thirteen_words_the_owner_approved():
    words = [word for word, _ in modifiers.CHANGED_ACTIONS]
    assert words == ["sus", "mur", "hak", "kop", "ryk", "tnij", "rzut", "urok", "garda", "wypad", "zwrot",
                     "finta", "odwet"]
    assert "bok" not in words and "pad" not in words                         # removed by the owner
    for word, prompt in modifiers.CHANGED_ACTIONS:
        assert 3 <= len(word) <= 5 and word.isascii() and word.islower()
        assert prompt.endswith("**" + " ".join(word.upper()) + "**")         # the usual "wpisz **S U S**"
        assert prompt.count("**") == 2                                       # nothing else in bold


def test_changed_commands_share_no_word_with_the_other_sets():
    from altermmo.features import boss, daily

    others = {word for actions in (boss.BOSS_ACTIONS, boss.MYTHIC_ACTIONS, boss.DUEL_ACTIONS, daily.MOB_ACTIONS)
              for word, _ in actions}
    assert not others & {word for word, _ in modifiers.CHANGED_ACTIONS}


def test_fight_actions_swaps_the_whole_set():
    usual = (("unik", "Boss szarżuje na Ciebie! Wpisz **U N I K**"),)
    assert modifiers.fight_actions(dict(ZEROS), usual, "Boss") is usual
    assert modifiers.fight_actions({}, usual, "Boss") is usual               # an old dict without the key
    changed = modifiers.fight_actions({**ZEROS, "commands_changed": 1}, usual, "Boss")
    assert [word for word, _ in changed] == [word for word, _ in modifiers.CHANGED_ACTIONS]
    assert ("mur", "Boss ciska w Ciebie głazem, wpisz **M U R**") in changed
    mobs = modifiers.fight_actions({**ZEROS, "commands_changed": 1}, usual, "Potwór")
    assert ("mur", "Potwór ciska w Ciebie głazem, wpisz **M U R**") in mobs
    assert all("{" not in prompt and "Boss" not in prompt for _, prompt in mobs)


async def test_praying_player_becomes_the_next_boss_only_once(game, monkeypatch):
    first = game.add_member("Ala")
    second = game.add_member("Ola")
    pools = draw(monkeypatch, "player_id")
    await modifiers.random_modifiers(game.scene(first), True)
    assert (await modifiers.load_modifiers(game.bot))["player_id"] == first.id
    assert game.channel.messages[-1].embeds[0].description == (
        PRAYER + f"**zostaniesz następnym bossem i potworami <@{first.id}>!**")

    # Somebody already is the next boss: that outcome is no longer drawn (legacy: drawn again).
    monkeypatch.setattr(random, "choice", lambda pool: pools.append(list(pool)) or "hp_boost_perc")
    await modifiers.random_modifiers(game.scene(second), True)
    assert "player_id" in pools[0] and "player_id" not in pools[1]
    assert len(pools[1]) == 10
    loaded = await modifiers.load_modifiers(game.bot)
    assert loaded["player_id"] == first.id and loaded["hp_boost_perc"] == 15


async def test_ban_is_not_drawn_twice(game, monkeypatch):
    await modifiers.modify_modifiers(game.bot, "ban_loser", 1)
    pools = draw(monkeypatch, "points_boost")
    await modifiers.random_modifiers(game.scene(game.add_member("Ala")), False)
    assert "ban_loser" not in pools[0] and "player_id" in pools[0]
    assert (await modifiers.load_modifiers(game.bot))["ban_loser"] == 1


async def test_background_flow_without_a_player_cannot_draw_the_player_boss(game, monkeypatch):
    pools = draw(monkeypatch, "drop_boost_perc")
    await modifiers.random_modifiers(game.scene(), True)          # scene.author is None
    assert "player_id" not in pools[0]
    assert (await modifiers.load_modifiers(game.bot))["drop_boost_perc"] == 25


async def test_two_prayers_at_once_cannot_both_take_the_player_boss(game, monkeypatch):
    first = game.add_member("Ala")
    second = game.add_member("Ola")
    monkeypatch.setattr(random, "choice", lambda pool: "player_id" if "player_id" in pool else "ban_loser")
    original_send = game.channel.send

    async def slow_send(*args, **kwargs):
        await asyncio.sleep(0.02)                                 # the announcements overlap
        return await original_send(*args, **kwargs)

    game.channel.send = slow_send
    await asyncio.gather(modifiers.random_modifiers(game.scene(first), True),
                         modifiers.random_modifiers(game.scene(second), True))
    loaded = await modifiers.load_modifiers(game.bot)
    assert loaded["player_id"] == first.id and loaded["ban_loser"] == 1
    assert game.channel.count("zostaniesz następnym bossem") == 1


async def test_reset_cd_clears_the_daily_cooldown_and_stores_nothing(game, monkeypatch, fake_daily):
    draw(monkeypatch, "reset_cd")
    assert await modifiers.random_modifiers(game.scene(game.add_member("Ala")), True) == "reset_cd"
    assert fake_daily == [game.bot]
    assert await modifiers.load_modifiers(game.bot) == ZEROS
    assert not game.bot.store.modifiers.path.exists()             # legacy never wrote "reset_cd"
    assert game.channel.messages[-1].embeds[0].description == (
        PRAYER + "**wszyscy bohaterowie są wypoczęci i mogą wyruszyć na polowanie!**")


async def test_reset_cd_failure_is_not_announced_as_success(game, monkeypatch, fake_daily):
    async def broken(bot):
        raise OSError("disk full")

    monkeypatch.setattr(sys.modules["altermmo.features.daily"], "clear_daily_file", broken)
    draw(monkeypatch, "reset_cd")
    with pytest.raises(OSError):
        await modifiers.random_modifiers(game.scene(game.add_member("Ala")), True)
    assert not game.channel.contains("wypoczęci")


async def test_sacrifice_uses_its_own_wording(game, monkeypatch):
    draw(monkeypatch, "rarity_boost")
    await modifiers.random_modifiers(game.scene(game.add_member("Ala")), True, True)
    embed = game.channel.messages[-1].embeds[0]
    assert embed.title == "Poświęcono towarzysza!"
    assert embed.description == "Poświęcenie sprawiło, że następny boss **będzie o 1 poziom rzadszy!**"
    assert embed.footer.text == "Powodzenia!"


async def test_silent_prayers_of_the_skill_gem_stack(game, monkeypatch):
    player = game.add_member("Ala")
    scene = game.scene(player)
    count = len(game.channel.messages)
    draw(monkeypatch, "hp_reduced_perc", value="min")
    for _ in range(5):                                            # legacy skill: five silent draws
        await modifiers.random_modifiers(scene, False)
    assert len(game.channel.messages) == count
    assert (await modifiers.load_modifiers(game.bot))["hp_reduced_perc"] == 25


async def test_prayer_that_cannot_be_announced_still_counts_once(game, monkeypatch):
    patron = game.add_member("Ala")
    draw(monkeypatch, "points_boost")
    game.channel.fail_with = RuntimeError("discord is down")
    game.channel.fail_sends = 1
    # No exception: the caller must not reopen the shrine for a second blessing.
    assert await modifiers.random_modifiers(game.scene(patron), True) == "points_boost"
    assert (await modifiers.load_modifiers(game.bot))["points_boost"] == 5
    assert not game.channel.contains("Błogosławieństwo kapliczki!")
    assert any("będzie o 5 więcej punktów" in line for line in game.bot.owner_log)


async def test_real_randomness_always_terminates_and_keeps_the_file_valid(game, fake_daily):
    player = game.add_member("Ala")
    random.seed(20240527)
    for _ in range(60):
        name = await modifiers.random_modifiers(game.scene(player), False)
        assert name in modifiers.RANDOM_POOL
    stored = read_file(game)
    assert list(stored) == FILE_KEYS
    assert stored["player_id"] in (0, player.id) and stored["ban_loser"] in (0, 1)
    assert stored["commands_changed"] in (0, 1)
    assert all(type(value) is int and value >= 0 for value in stored.values())
