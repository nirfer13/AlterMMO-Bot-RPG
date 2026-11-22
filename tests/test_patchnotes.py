"""features/patchnotes.py without Discord: the notes file, the spoiler validator, the marker, the messages.

Owner's request of 2026-11-21: "Po każdej aktualizacji i pierwszym uruchomieniu z aktualizacją wysyłaj
tutaj patch notesy po polsku. Bez spoilerów." - the notes come from ``config/patchNotes.json`` (checked
strictly: a broken file posts nothing), an entry with a spoiler is refused, and the bot finds what it
announced already by the marker in the footer of its own messages ("Patch notes nr 4-6").  The posting
itself (start-up, ``$patchnotes``, failures) is tested in test_patchnotes_flow.py.
"""

from __future__ import annotations

import json
import random
import re
from types import SimpleNamespace

import discord
import pytest

from altermmo.features import patchnotes, petcards
from altermmo.features.patchnotes import Note, NotesError
from altermmo.settings import CONFIG_DIR

BULLET = patchnotes.BULLET


def entry(note_id: int, *changes: str, title: str = "", day: str = "2026-11-21") -> dict:
    return {"id": note_id, "date": day, "title": title, "changes": list(changes) or [f"Zmiana {note_id}."]}


def note(note_id: int, *changes: str, title: str = "") -> Note:
    return Note(note_id, "2026-11-21", title, tuple(changes) or (f"Zmiana {note_id}.",))


def write(tmp_path, content, *, raw: bool = False, encoding: str = "utf-8"):
    path = tmp_path / "patchNotes.json"
    path.write_text(content if raw else json.dumps(content, ensure_ascii=False), encoding=encoding)
    return path


# -- the notes file -----------------------------------------------------------------------------

def test_the_real_notes_file_is_valid_ordered_and_spoiler_free():
    path = CONFIG_DIR / "patchNotes.json"
    assert patchnotes.NOTES_FILE == path
    notes = patchnotes.load_notes()
    assert notes and [item.id for item in notes] == list(range(1, len(notes) + 1))
    # (The content is the developers' and grows with every update: only its shape is checked.
    # Dates never go backwards - an entry is final once released, new changes get a new one.)
    assert [item.date for item in notes] == sorted(item.date for item in notes)
    assert patchnotes.validate_spoilers(notes) == []
    assert all(patchnotes.fits(post.description, post.footer) for post in patchnotes.plan_posts(notes))
    # Hand-written, so it must stay a plain list of objects with exactly the four keys.
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert all(set(item) <= {"id", "date", "title", "changes"} for item in raw)


def test_the_notes_promise_only_what_the_game_does(game):
    """Two lines the reviewers found promising too much (the notes are final once posted)."""
    from altermmo.features import petcards, xpsteal

    text = {item.id: " ".join(item.changes) for item in patchnotes.load_notes()}
    # An ordinary pet (no class) of a player who does not support stays the plain picture: only
    # the rarer ones are always cards.
    assert petcards.base_look({}).plain
    assert "towarzysze i jajka pokazują się jako karty" not in text[1]
    assert "rzadsze odmiany towarzyszy i ich jajka pokazują się jako karty" in text[1]
    # A right won before the update keeps its stored amount: the 10 000 is for drops from now on.
    xpsteal.grant(game.bot, 77, 2500)
    assert xpsteal.pending(game.bot, 77) == [2500]
    assert "Kradzież doświadczenia zdobyta od teraz w łupie z bossa jest warta 10 000" in text[4]
    assert "Każda kradzież zabiera tylko tyle, ile ofiara naprawdę ma" in text[4]
    # The pet panel: renaming opens a window, rebirth and transformation ask first, and some buttons
    # show only for some pets - not "everything with one click".
    assert "jednym kliknięciem" not in text[1]
    assert "Akcje towarzysza są teraz dostępne jako przyciski pod nim" in text[1]


def test_a_good_file_is_read_with_stripped_texts_and_an_optional_title(tmp_path):
    path = write(tmp_path, [entry(1, "  Pierwsza zmiana.  ", title=" Nowości "),
                            {"id": 2, "date": "2026-12-01", "changes": ["Druga."]}])
    assert patchnotes.load_notes(path) == [Note(1, "2026-11-21", "Nowości", ("Pierwsza zmiana.",)),
                                           Note(2, "2026-12-01", "", ("Druga.",))]


def test_an_empty_list_is_a_valid_file_with_nothing_to_post(tmp_path):
    assert patchnotes.load_notes(write(tmp_path, [])) == []


def test_a_byte_order_mark_from_a_windows_editor_is_fine(tmp_path):
    path = write(tmp_path, json.dumps([entry(1)]), raw=True, encoding="utf-8-sig")
    assert [item.id for item in patchnotes.load_notes(path)] == [1]


LONG = "x" * (patchnotes.LINE_LIMIT + 1)

BROKEN = [
    ({"id": 1}, "listę wpisów"),
    ("tekst", "listę wpisów"),
    ([["id", 1]], "nie jest obiektem"),
    ([{**entry(1), "change": ["literówka"]}], "nieznane pole change"),
    ([{"date": "2026-11-21", "changes": ["a"]}], "brak pola id"),
    ([{"id": 1, "changes": ["a"]}], "brak pola date"),
    ([{"id": 1, "date": "2026-11-21"}], "brak pola changes"),
    ([{**entry(1), "id": True}], "id musi być liczbą całkowitą"),
    ([{**entry(1), "id": "1"}], "id musi być liczbą całkowitą"),
    ([{**entry(1), "id": 1.0}], "id musi być liczbą całkowitą"),
    ([{**entry(1), "id": None}], "id musi być liczbą całkowitą"),
    ([entry(0)], "ma id 0, a powinien mieć 1"),
    ([entry(2)], "ma id 2, a powinien mieć 1"),
    ([entry(1), entry(1)], "ma id 1, a powinien mieć 2"),
    ([entry(1), entry(3)], "ma id 3, a powinien mieć 2"),
    ([entry(2), entry(1)], "ma id 2, a powinien mieć 1"),
    ([entry(1, day="21.11.2026")], "RRRR-MM-DD"),
    ([entry(1, day="2026-1-21")], "RRRR-MM-DD"),
    ([{**entry(1), "date": 20261121}], "RRRR-MM-DD"),
    ([entry(1, day="2026-02-30")], "nie ma takiego dnia 2026-02-30"),
    ([{**entry(1), "title": None}], "tytuł musi być tekstem"),
    ([entry(1, title="Dwie\nlinie")], "tytuł ma znak nowej linii"),
    ([entry(1, title="t" * (patchnotes.TITLE_LIMIT + 1))], "tytuł ma 201 znaków (najwyżej 200)"),
    ([{**entry(1), "changes": "jedna zmiana"}], "niepustą listą"),
    ([{**entry(1), "changes": []}], "niepustą listą"),
    ([{**entry(1), "changes": ["dobra", 5]}], "zmiana 2. musi być tekstem"),
    ([{**entry(1), "changes": ["dobra", "   "]}], "zmiana 2. jest puste"),
    ([{**entry(1), "changes": ["jedna\ndruga"]}], "zmiana 1. ma znak nowej linii"),
    ([{**entry(1), "changes": [LONG]}], "zmiana 1. ma 1001 znaków (najwyżej 1000)"),
    ([entry(1), {**entry(2), "changes": [None]}], "wpis nr 2: zmiana 1. musi być tekstem"),
]


@pytest.mark.parametrize("content, problem", BROKEN, ids=[problem for _, problem in BROKEN])
def test_every_broken_file_is_refused_with_the_exact_problem(tmp_path, content, problem):
    with pytest.raises(NotesError) as caught:
        patchnotes.load_notes(write(tmp_path, content))
    assert problem in str(caught.value)


def test_an_emoji_counts_as_two_characters_like_discord_counts_it(tmp_path):
    assert patchnotes.discord_len("🐉") == 2 and patchnotes.discord_len("ąę") == 2
    fits = "🐉" * (patchnotes.LINE_LIMIT // 2)
    assert patchnotes.load_notes(write(tmp_path, [entry(1, fits)]))[0].changes == (fits,)
    with pytest.raises(NotesError, match="1002 znaków"):
        patchnotes.load_notes(write(tmp_path, [entry(1, fits + "🐉")]))


@pytest.mark.parametrize("text, problem", [
    ("[{\"id\": 1, \"date\": \"2026-11-21\", \"changes\": [\"a\"],}]", "to nie jest poprawny JSON (linia 1"),
    ("[{\"id\": 1, \"id\": 2, \"date\": \"2026-11-21\", \"changes\": [\"a\"]}]", "pole \"id\" występuje dwa razy"),
    ("", "to nie jest poprawny JSON"),
])
def test_bad_json_and_a_repeated_key_are_refused(tmp_path, text, problem):
    with pytest.raises(NotesError, match=re.escape(problem)):
        patchnotes.load_notes(write(tmp_path, text, raw=True))


def test_a_missing_or_unreadable_file_is_refused(tmp_path):
    with pytest.raises(NotesError, match="nie ma pliku patchNotes.json"):
        patchnotes.load_notes(tmp_path / "patchNotes.json")
    path = tmp_path / "patchNotes.json"
    path.write_bytes(b"[\xff\xfe]")
    with pytest.raises(NotesError, match="UTF-8"):
        patchnotes.load_notes(path)
    with pytest.raises(NotesError, match="nie mogę przeczytać pliku"):
        patchnotes.load_notes(tmp_path)  # (a folder)


# -- no spoilers --------------------------------------------------------------------------------

def test_the_deny_list_starts_with_the_owners_words():
    assert patchnotes.FORBIDDEN_WORDS == ("holo", "pieczę", "arcyłowc", "tropiciel")
    assert set(patchnotes.FORBIDDEN_WORDS) <= set(patchnotes.forbidden_words())


@pytest.mark.parametrize("text, word", [
    ("Szansa na rzadkiego towarzysza to 5%.", "5%"),
    ("Szansa wzrosła do 12,5 %.", "12,5 %"),
    ("Drop: 3\u00a0%", "3\u00a0%"),
    ("Tylko 1 procent graczy to zobaczy.", "1 procent"),
    ("Aż 7％ więcej.", "7％"),
    ("Nowa folia HOLO!", "holo"),
    ("Odkryjcie Holografię.", "holo"),
    ("Nowa pieczęć dla towarzyszy.", "pieczę"),
    ("PIECZĘCIE świecą.", "pieczę"),
    ("Mityczny Arcyłowca.", "arcyłowc"),
    ("Pieczęć Tropiciela.", "pieczę"),
    ("Znak tropiciela.", "tropiciel"),
    ("Ukryte: ho**lo**", "holo"),
    ("Ukryte: h\u200bolo", "holo"),
    ("Ukryte: 5**%**", "5%"),
    # Percentages the way Polish writes them (review, second pass).
    ("Teraz 5-procentowa szansa na rzadki łup.", "5-procent"),
    ("Szansa wzrosła o 5 proc.", "5 proc."),
    ("Szansa wzrosła o 5 proc", "5 proc"),
    ("Aż 10–procentowy bonus.", "10–procent"),
    ("Szansa rośnie o 2 punkty procentowe.", "2 punkty procent"),
    ("Szansa rośnie o 2 pkt proc.", "2 pkt proc."),
    ("Szansa 5\\%", "5%"),                                   # Discord shows the escaped "5\%" as "5%"
    ("Szansa 5﹪", "5﹪"),                           # small percent sign
    ("Szansa ５%", "５%"),                           # a full-width digit
    ("Szansa 5٪", "5٪"),                           # Arabic percent sign
    ("Szansa 5‱", "5‱"),                           # per ten thousand
])
def test_spoilers_are_found_whatever_the_case_or_formatting(text, word):
    assert patchnotes.spoiler_in_text(text, patchnotes.FORBIDDEN_WORDS) == word
    assert patchnotes.spoiler_in(note(4, "Zwykła zmiana.", text)) is not None
    assert patchnotes.spoiler_in(note(4, title=text)) is not None


#: Characters a reader of Discord never sees.
INVISIBLE = ["\u00ad", "\u200b", "\u200c", "\u200d", "\u200e", "\u200f", "\u2060", "\u2061", "\u2062",
             "\u2063", "\u2064", "\u202a", "\u202c", "\u2066", "\u2069", "\ufeff", "\u034f", "\ufe0f",
             "\ufe00", "\U000e0001", "\U000e0100", "\u180e"]


@pytest.mark.parametrize("char", INVISIBLE, ids=[f"U+{ord(char):04X}" for char in INVISIBLE])
def test_invisible_characters_do_not_hide_a_spoiler(char):
    assert patchnotes.spoiler_in_text(f"Nowa folia Ho{char}lo.", patchnotes.FORBIDDEN_WORDS) == "holo"
    assert patchnotes.spoiler_in_text(f"Szansa 5{char}%.", patchnotes.FORBIDDEN_WORDS) is not None


def test_a_decomposed_polish_letter_does_not_hide_a_spoiler():
    """Text pasted from a macOS / web source may hold "ę" as e + U+0328: on Discord it looks the same."""
    import unicodedata

    composed = "Nowe pieczęcie dla towarzyszy."
    decomposed = unicodedata.normalize("NFD", composed)
    assert decomposed != composed and unicodedata.normalize("NFC", decomposed) == composed
    assert patchnotes.spoiler_in_text(decomposed, patchnotes.FORBIDDEN_WORDS) == "pieczę"
    assert patchnotes.spoiler_in(note(1, title=decomposed), patchnotes.FORBIDDEN_WORDS) == "pieczę"
    # ... and the deny-list itself may be written decomposed, too.
    assert patchnotes.spoiler_in_text(composed, (unicodedata.normalize("NFD", "pieczę"),)) is not None


def test_plain_text_is_what_a_reader_sees():
    assert patchnotes.plain_text("  Ho**lo**\u200e  \u00a0 ＦＯＬＩＡ \\_x ") == "holo folia x"


@pytest.mark.parametrize("text", [
    "Naprawiono błąd przy polowaniu.",
    "Łowcy dostają nowe powiadomienie o bossie.",
    "Boss ma teraz 3 fazy walki.",
    "Procentowe premie działają poprawnie.",
    "Rankingi odświeżają się szybciej.",
    "Poprawiono 3 procedury walki.",
    "Boss ma 2 procesy w tle.",
    "Wieża ma teraz 10 pięter - i 3 > 2.",
])
def test_ordinary_notes_pass(text):
    assert patchnotes.spoiler_in(note(1, text), patchnotes.FORBIDDEN_WORDS) is None


def test_validate_spoilers_names_every_refused_entry():
    notes = [note(1), note(2, "Folia holo."), note(3), note(4, title="50% więcej")]
    assert patchnotes.validate_spoilers(notes) == [(2, "holo"), (4, "50%")]


def test_frame_names_are_pulled_in_from_the_pet_cards_catalogue_when_it_has_one(monkeypatch):
    """How the deny-list grows by itself: every frame name of ``petcards.FRAMES`` is a spoiler."""
    frames = {"X1": SimpleNamespace(id="X1", name="Gwiazda Testowa"),
              "X1-A": SimpleNamespace(id="X1-A", name="Gwiazda Testowa"),
              "X2": SimpleNamespace(id="X2", name="  Oko Próby "),
              "X3": SimpleNamespace(id="X3", name=""), "X4": SimpleNamespace(id="X4")}
    monkeypatch.setattr(petcards, "FRAMES", frames, raising=False)
    assert patchnotes.catalogue_words() == ("Gwiazda Testowa", "Oko Próby")
    assert patchnotes.forbidden_words() == (*patchnotes.FORBIDDEN_WORDS, "Gwiazda Testowa", "Oko Próby")
    assert patchnotes.spoiler_in(note(1, "Nowa ramka: GWIAZDA TESTOWA!")) == "Gwiazda Testowa"
    assert patchnotes.validate_spoilers([note(1), note(2, "Zobacz oko próby.")]) == [(2, "Oko Próby")]


@pytest.mark.parametrize("catalogue", ["missing", None, 42, ["not a frame", SimpleNamespace(name="Lista Ram")]])
def test_without_a_usable_catalogue_only_the_fixed_words_count(monkeypatch, catalogue):
    if catalogue == "missing":
        monkeypatch.delattr(petcards, "FRAMES", raising=False)
    else:
        monkeypatch.setattr(petcards, "FRAMES", catalogue, raising=False)
    words = patchnotes.catalogue_words()
    assert words == (("Lista Ram",) if isinstance(catalogue, list) else ())
    assert patchnotes.forbidden_words()[:4] == patchnotes.FORBIDDEN_WORDS


#: The frame names of the owner's package (spec of 2026-11-21), as a catalogue would give them.
SPEC_FRAMES = ["Srebrne okucia", "Gwiazda Szronu", "Księżycowa stal", "Miedziane ciernie", "Przeszywana skóra",
               "Złoty relikwiarz", "Rubinowa Korona", "Złote wrota", "Rozbite zwierciadło",
               "Bursztynowy skarabeusz", "Astrolabium", "Kręgosłup Pustki", "Oko Otchłani",
               "Oryginalna ilustracja"]


@pytest.fixture
def spec_frames(monkeypatch):
    frames = {f"F{number}": SimpleNamespace(id=f"F{number}", name=name) for number, name in enumerate(SPEC_FRAMES)}
    monkeypatch.setattr(petcards, "FRAMES", frames, raising=False)


@pytest.mark.parametrize("text, name", [
    ("Nowa ramka Księżycowej stali.", "Księżycowa stal"),
    ("Dodaliśmy ramkę Złotego relikwiarza.", "Złoty relikwiarz"),
    ("Karty z Okiem Otchłani wyglądają lepiej.", "Oko Otchłani"),
    ("Ramka Rubinowej Korony błyszczy.", "Rubinowa Korona"),
    ("O Rubinowej Koronie słów kilka.", "Rubinowa Korona"),
    ("Gwiazda  Szronu.", "Gwiazda Szronu"),
    ("Gwiazda\u00a0Szronu.", "Gwiazda Szronu"),
    ("Dwie Gwiazdy Szronu.", "Gwiazda Szronu"),
    ("Za Złotymi wrotami.", "Złote wrota"),
    ("Blask Złotych wrót.", "Złote wrota"),
    ("Komplet Srebrnych okuć.", "Srebrne okucia"),
    ("Z Miedzianymi cierniami.", "Miedziane ciernie"),
    ("Kolor Przeszywanej skóry.", "Przeszywana skóra"),
    ("Pod Rozbitym zwierciadłem.", "Rozbite zwierciadło"),
    ("Ślad Bursztynowego skarabeusza.", "Bursztynowy skarabeusz"),
    ("Nowe astrolabia.", "Astrolabium"),
    ("Moc Kręgosłupa Pustki.", "Kręgosłup Pustki"),
    ("Z Oryginalną ilustracją.", "Oryginalna ilustracja"),
    ("Ramka Ksiezycowej stali (bez polskich liter).", "Księżycowa stal"),
    ("Ramka **Rubinowej** Korony.", "Rubinowa Korona"),
    ("Ramka Rubinowej-Korony.", "Rubinowa Korona"),
])
def test_frame_names_are_found_in_any_grammatical_case(spec_frames, text, name):
    assert patchnotes.spoiler_in_text(text) == name


@pytest.mark.parametrize("text", [
    "Łowcy dostają nowe powiadomienie o bossie.",
    "Złoto z wypraw trafia teraz prosto do sakwy.",
    "Złoto wróciło do sklepu.",
    "Okno ekwipunku otwiera się szybciej.",
    "Gwiazdka przy nicku oznacza mistrza wieży.",
    "Rubinowe pierścienie kosztują mniej złota.",
    "Ok, boss jest teraz trudniejszy.",
    "Srebrne monety można wymienić na złote.",
    "Naprawiono błąd w ilustracjach bossów.",
])
def test_ordinary_notes_pass_with_the_frame_catalogue_in_place(spec_frames, text):
    assert patchnotes.spoiler_in_text(text) is None


def test_the_current_pet_cards_catalogue_if_any_is_on_the_deny_list():
    frames = getattr(petcards, "FRAMES", None) or {}
    names = {frame.name.strip() for frame in frames.values() if len(getattr(frame, "name", "").strip()) >= 3}
    assert names <= set(patchnotes.forbidden_words())


# -- the marker ---------------------------------------------------------------------------------

@pytest.mark.parametrize("first, last, text, highest", [
    (1, None, "Patch notes nr 1", 1), (4, 4, "Patch notes nr 4", 4), (4, 6, "Patch notes nr 4-6", 6),
    (99, 1234, "Patch notes nr 99-1234", 1234),
])
def test_marker_round_trip(first, last, text, highest):
    assert patchnotes.marker(first, last) == text
    assert patchnotes.parse_marker(text) == highest
    assert patchnotes.parse_marker(f"  {text} \n") == highest  # (Discord trims the ends of a footer)


@pytest.mark.parametrize("text", [
    None, 5, "", "Patch notes", "Patch notes nr", "Patch notes nr ", "patch notes nr 4", "PATCH NOTES NR 4",
    "Patch notes nr 04", "Patch notes nr 0", "Patch notes nr 4-", "Patch notes nr -4", "Patch notes nr 6-4",
    "Patch notes nr 4-4", "Patch notes nr 4–6", "Patch notes nr 4 - 6", "Patch notes nr 4-6-8",
    "Patch notes nr ４", "Patch notes nr 4a", "Patch notes nr 4.", "Patch notes nr 4 (edytowane)",
    "Patch  notes nr 4", "Patch notes nr. 4", "> Patch notes nr 4", "**Patch notes nr 4**",
    "Cytat: Patch notes nr 4", "Patch notes nr 1234567890", patchnotes.FOOTER_TO_BE_CONTINUED,
])
def test_anything_else_is_not_a_marker(text):
    assert patchnotes.parse_marker(text) is None


BOT_ID = 7


def message(*, author_id=BOT_ID, content="", footer="Patch notes nr 3", title=patchnotes.MSG_TITLE,
            embeds: int = 1):
    made = []
    for _ in range(embeds):
        embed = discord.Embed(title=title, description="📅 **21.11.2026**\n• Zmiana.")
        if footer is not None:
            embed.set_footer(text=footer)
        made.append(embed)
    return SimpleNamespace(author=SimpleNamespace(id=author_id), content=content, embeds=made)


def test_only_the_bots_own_patch_notes_messages_count():
    assert patchnotes.marker_of(message(), BOT_ID) == 3
    assert patchnotes.marker_of(message(), None) is None                 # who is "the bot" is unknown
    assert patchnotes.marker_of(message(author_id=8), BOT_ID) is None    # a player / another bot quoting it
    assert patchnotes.marker_of(message(content="Patch notes nr 3"), BOT_ID) is None
    assert patchnotes.marker_of(message(title="📜 Patch notes (kopia)"), BOT_ID) is None
    assert patchnotes.marker_of(message(title=None), BOT_ID) is None
    assert patchnotes.marker_of(message(footer=None), BOT_ID) is None
    assert patchnotes.marker_of(message(footer="Patch notes nr 3 (edytowane)"), BOT_ID) is None
    assert patchnotes.marker_of(message(embeds=0), BOT_ID) is None
    assert patchnotes.marker_of(message(embeds=2), BOT_ID) is None
    assert patchnotes.marker_of(SimpleNamespace(author=None, content="", embeds=[]), BOT_ID) is None


ROLE = 985071779787730944
MENTION = f"<@&{ROLE}>"


def test_a_marker_message_may_carry_exactly_the_hunters_mention():
    """Owner, 2026-11-22: "W patch notesach oznaczaj rolę łowca potworów." - the first message of a run
    pings the role in its text; it must still count as the bot's patch-notes message."""
    assert patchnotes.marker_of(message(content=MENTION), BOT_ID, ROLE) == 3
    assert patchnotes.marker_of(message(), BOT_ID, ROLE) == 3              # old ones without text stay valid
    assert patchnotes.marker_of(message(), BOT_ID) == 3
    assert patchnotes.marker_of(message(content=MENTION), BOT_ID) is None  # (which role is unknown)
    assert patchnotes.marker_of(message(content=MENTION, author_id=8), BOT_ID, ROLE) is None


@pytest.mark.parametrize("content", [
    f"<@&{ROLE + 1}>",                       # a foreign role
    "<@291836779495948288>", f"<@{ROLE}>",   # a player
    "@everyone", "@here",
    f"{MENTION} Nowości!", f"Nowości {MENTION}", f"{MENTION}{MENTION}", f"{MENTION} <@&1>",
    f"{MENTION} ", f" {MENTION}", f"<@&{ROLE}>.", f"<@& {ROLE}>", f"<@&０{ROLE}>",
    "Patch notes nr 3", " ",
])
def test_any_other_content_is_not_a_marker_message(content):
    assert patchnotes.marker_of(message(content=content), BOT_ID, ROLE) is None


@pytest.mark.parametrize("role_id", [None, 0, -5, True, "985071779787730944", 1.5])
def test_a_mention_never_counts_without_a_usable_role(role_id):
    assert not patchnotes.content_allowed(MENTION, role_id)
    assert patchnotes.content_allowed("", role_id) and patchnotes.content_allowed(None, role_id)


def test_a_post_with_a_ping_carries_only_the_mention_and_allows_only_that_role():
    plain = patchnotes.Post("📅 **21.11.2026**\n• Zmiana.", "Patch notes nr 1", (1,))
    assert plain.content == "" and plain.allowed_mentions.to_dict() == discord.AllowedMentions.none().to_dict()
    pinged = patchnotes.Post(plain.description, plain.footer, (1,), ping_role=ROLE)
    assert pinged.content == MENTION
    mentions = pinged.allowed_mentions
    assert mentions.to_dict() == {"parse": [], "roles": [ROLE]}
    assert mentions.everyone is False and mentions.users is False and mentions.replied_user is False
    assert pinged.embed().to_dict() == plain.embed().to_dict()                    # the embed is the same


def _sent(content: str, post) -> SimpleNamespace:
    embed = post.embed()
    return SimpleNamespace(author=SimpleNamespace(id=BOT_ID), content=content, embeds=[embed], created_at=None)


def test_two_messages_that_differ_only_in_the_mention_are_not_the_same_message():
    plain = patchnotes.Post("📅 **21.11.2026**\n• Zmiana.", "Patch notes nr 1", (1,))
    pinged = patchnotes.Post(plain.description, plain.footer, (1,), ping_role=ROLE)
    assert patchnotes._same_post(_sent("", plain), plain, BOT_ID, None)
    assert patchnotes._same_post(_sent(MENTION, pinged), pinged, BOT_ID, None)
    assert not patchnotes._same_post(_sent("", plain), pinged, BOT_ID, None)
    assert not patchnotes._same_post(_sent(MENTION, pinged), plain, BOT_ID, None)
    assert not patchnotes._same_post(_sent(f"<@&{ROLE + 1}>", pinged), pinged, BOT_ID, None)


def test_the_posted_embed_carries_the_marker_and_nothing_technical_besides():
    (post,) = patchnotes.plan_posts([note(1, "Pierwsza."), note(2, "Druga.", title="Boss")])
    embed = post.embed()
    assert embed.title == "📜 Patch notes" and embed.footer.text == "Patch notes nr 1-2"
    assert embed.description == ("📅 **21.11.2026**\n• Pierwsza.\n\n"
                                 "📅 **21.11.2026** - **Boss**\n• Druga.")
    assert patchnotes.marker_of(SimpleNamespace(author=SimpleNamespace(id=BOT_ID), content="", embeds=[embed]),
                                BOT_ID) == 2


# -- the messages: Discord's limits ---------------------------------------------------------------

def bullets(posts) -> list[str]:
    """Every change line of the messages, in order (headers left out)."""
    lines = []
    for post in posts:
        lines += [line[len(BULLET):] for line in post.description.split("\n") if line.startswith(BULLET)]
    return lines


def check_plan(notes: list[Note]) -> list:
    posts = patchnotes.plan_posts(notes)
    # Every line exactly once, in order.
    assert bullets(posts) == [change for item in notes for change in item.changes]
    for post in posts:
        assert patchnotes.fits(post.description, post.footer)
        embed = post.embed()
        lengths = [patchnotes.discord_len(text) for text in (embed.title, embed.description, embed.footer.text)]
        assert lengths[0] <= 256 and lengths[1] <= 4096 and lengths[2] <= 2048 and sum(lengths) <= 6000
        assert len(embed) <= 6000  # (discord.py's own count)
    # Every entry is marked exactly once: by the message that holds its last line.
    covered = [note_id for post in posts for note_id in post.covers]
    assert covered == [item.id for item in notes]
    for post in posts:
        assert patchnotes.parse_marker(post.footer) == post.last_id
        if post.covers:
            assert post.footer == patchnotes.marker(post.covers[0], post.covers[-1])
            assert list(post.covers) == list(range(post.covers[0], post.covers[-1] + 1))
        else:
            assert post.footer == patchnotes.FOOTER_TO_BE_CONTINUED and post.part_of is not None
    # A message either holds whole entries or a part of ONE entry.
    for post in posts:
        heads = [line for line in post.description.split("\n") if line.startswith("📅")]
        if post.part_of is None:
            assert len(heads) == len(post.covers)
        else:
            assert len(heads) == 1 and post.covers in ((), (post.part_of,))
    return posts


def test_several_short_entries_go_together_in_one_message():
    posts = check_plan([note(4), note(5, "a", "b"), note(6, title="Wieża")])
    assert [(post.footer, post.covers) for post in posts] == [("Patch notes nr 4-6", (4, 5, 6))]


def test_entries_that_do_not_fit_together_are_split_on_entry_boundaries():
    line = "y" * 990
    notes = [note(1, line, line), note(2, line, line), note(3, line)]           # ~2000 / 2000 / 1000
    posts = check_plan(notes)
    assert [post.covers for post in posts] == [(1, 2), (3,)] or [post.covers for post in posts] == [(1,), (2, 3)]
    assert len(posts) == 2


def test_an_entry_too_long_for_one_message_is_split_and_only_its_last_part_is_marked():
    lines = [f"{number:02d} " + "z" * 900 for number in range(12)]                 # ~11 000 characters
    posts = check_plan([note(1), note(2, *lines, title="Duża aktualizacja"), note(3)])
    assert [post.footer for post in posts] == (["Patch notes nr 1"]
                                               + [patchnotes.FOOTER_TO_BE_CONTINUED] * (len(posts) - 3)
                                               + ["Patch notes nr 2", "Patch notes nr 3"])
    assert len(posts) >= 5
    parts = [post for post in posts if post.part_of == 2]
    assert parts[0].description.startswith("📅 **21.11.2026** - **Duża aktualizacja**\n")
    assert all(part.description.startswith("📅 **21.11.2026** - **Duża aktualizacja** (cd.)\n")
               for part in parts[1:])


def test_an_entry_exactly_at_the_limit_stays_one_message():
    head = patchnotes.header(note(1))
    room = patchnotes.EMBED_DESCRIPTION_LIMIT - patchnotes.discord_len(head)
    lines, used = [], 0
    while used + 1 + len(BULLET) + 500 <= room:
        lines.append("w" * 500)
        used += 1 + len(BULLET) + 500
    rest = room - used - 1 - len(BULLET)
    lines.append("v" * rest)
    exact = note(1, *lines)
    assert patchnotes.discord_len(patchnotes.block(exact)) == patchnotes.EMBED_DESCRIPTION_LIMIT
    (post,) = check_plan([exact])
    assert post.covers == (1,) and post.part_of is None
    longer = note(1, *lines[:-1], lines[-1] + "v")
    posts = check_plan([longer])
    assert len(posts) == 2 and posts[0].footer == patchnotes.FOOTER_TO_BE_CONTINUED


@pytest.mark.parametrize("seed", range(40))
def test_property_any_notes_are_planned_within_the_limits_every_line_once_in_order(seed):
    rng = random.Random(seed)
    alphabet = "abcdefghij ąęółżź🐉⚔\ufe0f"
    notes = []
    for note_id in range(1, rng.randint(1, 14) + 1):
        count = rng.choice([1, 1, 2, 3, 5, 8, 20, 40])
        changes = []
        for number in range(count):
            size = rng.choice([5, 40, 200, 600, 999])
            text = f"{note_id}.{number} " + "".join(rng.choice(alphabet) for _ in range(size))
            while patchnotes.discord_len(text) > patchnotes.LINE_LIMIT:
                text = text[:-1]
            changes.append(text.strip() or "x")
        title = rng.choice(["", "Nowości", "t" * patchnotes.TITLE_LIMIT, "🐉" * 100])
        notes.append(Note(note_id, "2026-11-21", title, tuple(changes)))
    check_plan(notes)


def test_a_line_that_could_never_fit_is_refused_by_the_planner():
    with pytest.raises(ValueError):
        patchnotes.plan_posts([note(1, "q" * 5000)])


def test_ranges_and_polish_counts():
    assert patchnotes.ranges([4, 5, 6, 8, 10, 11]) == "4-6, 8, 10-11"
    assert patchnotes.entries_text([1]) == "1 wpis (nr 1)"
    assert patchnotes.entries_text([1, 2, 3]) == "3 wpisy (nr 1-3)"
    assert patchnotes.entries_text(list(range(1, 6))) == "5 wpisów (nr 1-5)"
    assert patchnotes.entries_text(list(range(1, 13))) == "12 wpisów (nr 1-12)"
    assert patchnotes.entries_text(list(range(1, 23))) == "22 wpisy (nr 1-22)"
