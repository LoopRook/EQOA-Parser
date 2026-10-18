"""Parser tests (session B). Patterns/lines are SYNTHETIC/ASSUMED."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from eqoa_parser.parser import Parser, Pattern, load_patterns
from eqoa_parser.sources import HEARTBEAT

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = ROOT / "patterns.yaml"

# One sample per pattern in patterns.yaml: name -> (line, (actor, target, amount, ability, kind)) or None (ignore)
SAMPLES = {
    "taken_you": ("A decaying skeleton hits you for 5 points of damage.", ("a decaying skeleton", "you", 5, "melee", "damage_taken")),
    "taken_pet": ("A rat bites your pet for 1 point of damage.", ("a rat", "pet", 1, "melee", "damage_taken")),
    "pet_ability": ("Your pet's Rend hits a rat for 9 points of damage.", ("pet", "a rat", 9, "Rend", "damage")),
    "pet_melee": ("Your pet bites a rat for 8 points of damage.", ("pet", "a rat", 8, "melee", "damage")),
    "you_melee": ("You slash a rat for 15 points of damage.", ("you", "a rat", 15, "melee", "damage")),
    "you_ability": ("Your Fire Bolt hits a rat for 40 points of damage.", ("you", "a rat", 40, "Fire Bolt", "damage")),
    "you_miss": ("You try to slash a rat, but miss!", ("you", "a rat", 0, "melee", "miss")),
    "you_slay": ("You have slain a rat!", ("you", "a rat", 0, None, "death")),
    "other_ability": ("Bob's Flame Lick hits a rat for 20 points of damage.", ("Bob", "a rat", 20, "Flame Lick", "damage")),
    "other_melee": ("Bob hits a rat for 14 points of damage.", ("Bob", "a rat", 14, "melee", "damage")),
    "xp_gain": ("You gain experience!", None),
}


def make(**kw):
    return Parser(PATTERNS, **kw)


def test_every_pattern_in_file_has_a_sample():
    names = [p.name for p in load_patterns(PATTERNS)]
    assert sorted(names) == sorted(SAMPLES)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_each_pattern_matches_its_sample_and_is_the_first_match(name):
    line, exp = SAMPLES[name]
    p = make()
    ev = p.parse(10.0, line)
    assert p.unmatched_count == 0
    first = next(x.name for x in load_patterns(PATTERNS) if re.fullmatch(x.regex, line))
    assert first == name
    if exp is None:
        assert ev is None
        return
    assert (ev.actor, ev.target, ev.amount, ev.ability, ev.kind) == exp
    assert ev.ts == 10.0 and ev.raw == line


def test_singular_point_and_strip_and_raw_untouched():
    ev = make().parse(1.0, "  You hit a rat for 1 point of damage.  \n")
    assert ev.amount == 1 and ev.raw == "  You hit a rat for 1 point of damage.  \n"


def test_heartbeat_and_blank_return_none_and_are_not_logged(tmp_path):
    log = tmp_path / "u.log"
    p = make(unmatched_path=log)
    assert p.parse(1.0, HEARTBEAT) is None
    assert p.parse(1.0, "   ") is None
    assert p.unmatched_count == 0
    assert log.read_text() == ""


def test_ignore_kind_not_logged(tmp_path):
    log = tmp_path / "u.log"
    p = make(unmatched_path=log)
    assert p.parse(1.0, "You gain party experience!") is None
    assert p.unmatched_count == 0 and log.read_text() == ""


def test_unmatched_logging_format_escaping_and_immediate_flush(tmp_path):
    log = tmp_path / "sub" / "u.log"
    p = make(unmatched_path=log)
    p.parse(5.5, "Bob tells you, 'hi'")
    # flushed immediately, without close()
    assert log.read_text(encoding="utf-8") == "5.5\tBob tells you, 'hi'\n"
    p.parse(6.0, "a\tb\nc\r\nd")
    lines = log.read_text(encoding="utf-8").split("\n")
    assert lines[1] == "6.0\ta\\tb\\nc\\r\\nd"
    assert p.unmatched_count == 2
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2
    p.close()
    p.close()


def test_unmatched_appends_across_parsers(tmp_path):
    log = tmp_path / "u.log"
    for i in range(2):
        p = make(unmatched_path=log)
        p.parse(float(i), "garbage")
        p.close()
    assert len(log.read_text().splitlines()) == 2


def test_no_unmatched_path_still_counts():
    p = make()
    p.parse(1.0, "garbage")
    assert p.unmatched_count == 1


def test_no_partial_match():
    p = make()
    assert p.parse(1.0, "You hit a rat for 5 points of damage. Extra") is None
    assert p.unmatched_count == 1


@pytest.mark.parametrize("who", ["You", "you", "YOU"])
def test_you_normalization_actor(who):
    pats = [Pattern("t", "damage", r"(?P<actor>\w+) hits (?P<target>.+?) for (?P<amount>\d+)")]
    assert Parser(pats).parse(1, f"{who} hits a rat for 3").actor == "you"


def test_target_normalization_yourself_and_your_pet_and_articles():
    pats = [Pattern("t", "damage", r"X hits (?P<target>.+?) for (?P<amount>\d+)", actor="Bob")]
    p = Parser(pats)
    assert p.parse(1, "X hits yourself for 3").target == "you"
    assert p.parse(1, "X hits Your pet for 3").target == "pet"
    assert p.parse(1, "X hits A Decaying Skeleton for 3").target == "a Decaying Skeleton"
    assert p.parse(1, "X hits An orc for 3").target == "an orc"
    assert p.parse(1, "X hits The Lich King for 3").target == "the Lich King"
    assert p.parse(1, "X hits Anvil for 3").target == "Anvil"  # not an article
    assert p.parse(1, "X hits Fluffy for 3").target == "Fluffy"


def test_pet_names_and_player_name_case_insensitive():
    pats = [Pattern("t", "damage", r"(?P<actor>.+?) hits (?P<target>.+?) for (?P<amount>\d+)")]
    p = Parser(pats, pet_names=["Fluffy", "Rex"], player_name="Zorg")
    ev = p.parse(1, "FLUFFY hits rex for 1")
    assert ev.actor == "pet" and ev.target == "pet"
    ev = p.parse(1, "zorg hits Bob for 1")
    assert ev.actor == "you" and ev.target == "Bob"
    assert Parser(pats).parse(1, "Zorg hits Bob for 1").actor == "Zorg"  # not configured -> kept


def test_missing_ability_is_none():
    pats = [Pattern("a", "damage", r"(?P<actor>\w+) hits (?P<target>\w+) (?P<amount>\d+)")]
    assert Parser(pats).parse(1, "Bob hits Al 3").ability is None


def test_first_match_wins():
    rx = r"(?P<actor>\w+) hits (?P<target>\w+) (?P<amount>\d+)"
    pats = [Pattern("first", "damage", rx, ability="one"), Pattern("second", "damage", rx, ability="two")]
    assert Parser(pats).parse(1, "Bob hits Al 3").ability == "one"


def test_pattern_path_accepted_as_str():
    assert Parser(str(PATTERNS)).parse(1, "You hit a rat for 2 points of damage.").amount == 2


# ---- load_patterns validation ----
def _write(tmp_path, patterns):
    f = tmp_path / "p.yaml"
    f.write_text(yaml.safe_dump({"version": 1, "patterns": patterns}), encoding="utf-8")
    return f


GOOD = {"name": "ok", "kind": "damage", "regex": r"(?P<actor>\w+) (?P<target>\w+) (?P<amount>\d+)"}


def test_load_patterns_real_file_ok():
    pats = load_patterns(PATTERNS)
    assert len(pats) == 11 and pats[0].name == "taken_you" and pats[0].target == "you"


@pytest.mark.parametrize(
    "bad,needle",
    [
        ({**GOOD, "name": "badkind", "kind": "bogus"}, "badkind"),
        ({**GOOD, "name": "badrx", "regex": "(unclosed"}, "badrx"),
        ({"name": "noregex", "kind": "damage"}, "noregex"),
        ({"kind": "damage", "regex": "x"}, "#0"),
        ({"name": "nokind", "regex": "x"}, "nokind"),
        ({**GOOD, "name": "noamount", "regex": r"(?P<actor>\w+) (?P<target>\w+)"}, "noamount"),
        ({**GOOD, "name": "notarget", "regex": r"(?P<actor>\w+) (?P<amount>\d+)"}, "notarget"),
        ({**GOOD, "name": "extra", "colour": "red"}, "extra"),
        ({**GOOD, "name": "badstatus", "status": "MAYBE"}, "badstatus"),
    ],
)
def test_load_patterns_errors_name_the_pattern(tmp_path, bad, needle):
    with pytest.raises(ValueError, match=needle):
        load_patterns(_write(tmp_path, [bad]))


def test_load_patterns_duplicate_and_shape_errors(tmp_path):
    with pytest.raises(ValueError, match="ok"):
        load_patterns(_write(tmp_path, [GOOD, GOOD]))
    f = tmp_path / "x.yaml"
    f.write_text("just: a mapping", encoding="utf-8")
    with pytest.raises(ValueError):
        load_patterns(f)
    f.write_text("a: [unclosed", encoding="utf-8")
    with pytest.raises(ValueError):
        load_patterns(f)


def test_parser_validates_pattern_objects():
    with pytest.raises(ValueError, match="bad"):
        Parser([Pattern("bad", "nonsense", "x")])
