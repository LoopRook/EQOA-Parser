"""Pet learning, unique labels and FightSummary.own_actors (session B, Phase 3 amendments). INFERRED rules."""
from __future__ import annotations

import io

from eqoa_parser.config import load_config
from eqoa_parser.net.attribution import Attributor, ConnectionRouter
from eqoa_parser.net.protocol import HpChange, Swing
from eqoa_parser.segmenter import Fight
from eqoa_parser.stats import FightSummary, summarize_fight

A, B, C = 0x174A1A40, 0x1751CB82, 0x1800AA11
PA, PB, PA2 = 0x01374606, 0x01445566, 0x01999999
MOB = 0x01312F0A


def hp(ts, src, amt, tgt, conn, ext=False):
    return HpChange(ts, conn, {1: A, 2: B, 3: C}[conn] & 0xFFFF, 0, src, amt, tgt, ext, b"\xdb\x00")


def sw(ts, att, tgt, conn):
    return Swing(ts, conn, {1: A, 2: B, 3: C}[conn] & 0xFFFF, 0, att, tgt, 0, b"\xb1\x00")


def router():
    r = ConnectionRouter(load_config(), log=io.StringIO())
    for conn, who in ((1, A), (2, B)):
        r.feed(hp(0.0, who, -1, MOB, conn))
    return r


def both(r, rec_fn, *args, **kw):
    out = []
    for conn in (1, 2):
        out += r.feed(rec_fn(*args, conn=conn, **kw))
    return out


def test_other_pet_swinging_later_is_not_my_pet_earliest_eligible_wins():
    r = router()
    both(r, sw, 10.0, PA, MOB)
    both(r, sw, 10.1, PB, MOB)
    r.feed(hp(10.5, A, -25, MOB, 1, True))
    assert r.attributor(1).pet_id == PA


def test_followed_player_and_other_connections_pet_are_never_my_pet():
    r = router()
    both(r, sw, 10.0, B, MOB)                   # B's player swinging
    r.feed(hp(10.4, A, -25, MOB, 1, True))
    assert r.attributor(1).pet_id is None
    both(r, sw, 20.0, PB, MOB)
    r.feed(hp(20.4, B, -25, MOB, 2, True))      # B learns PB
    assert r.attributor(2).pet_id == PB
    both(r, sw, 30.0, PB, MOB)                  # now PB is claimed: cannot become A's pet
    r.feed(hp(30.4, A, -25, MOB, 1, True))
    assert r.attributor(1).pet_id is None


def test_pet_replaced_only_after_two_hits_with_old_pet_absent():
    r = router()
    both(r, sw, 10.0, PA, MOB)
    r.feed(hp(10.4, A, -25, MOB, 1, True))
    assert r.attributor(1).pet_id == PA
    both(r, sw, 20.0, PA2, MOB)
    r.feed(hp(20.4, A, -25, MOB, 1, True))
    assert r.attributor(1).pet_id == PA            # one hit is not enough
    both(r, sw, 30.0, PA2, MOB)
    r.feed(hp(30.4, A, -25, MOB, 1, True))
    assert r.attributor(1).pet_id == PA2           # re-summoned pet takes over


def test_other_players_damage_is_not_turned_into_my_pet_damage():
    r = router()
    both(r, sw, 10.0, PA, MOB)
    both(r, sw, 10.1, B, MOB)
    ev = r.feed(hp(10.4, A, -25, MOB, 1, True))
    assert [e.actor for e in ev] == ["pet"]
    ev = both(r, hp, 11.0, B, -500, MOB)
    assert [(e.actor, e.amount) for e in ev] == [("char:2", 500)]       # once, from B's own connection


def test_record_sourced_by_my_pet_is_dropped():
    a = Attributor()
    a.feed(hp(0.0, A, -1, MOB, 1))
    a.feed(sw(1.0, PA, MOB, 1))
    a.feed(hp(1.4, A, -25, MOB, 1, True))
    assert a.pet_id == PA
    assert a.feed(hp(2.0, PA, -40, MOB, 1)) == [] and a.dropped_unrelated == 1


def test_non_primary_labels_are_unique_with_suffix():
    from eqoa_parser.net.names import NameTable
    t = NameTable()
    t._names.update({B: "Zecron", C: "Zecron"})
    r = ConnectionRouter(load_config(), log=io.StringIO(), names=t)
    r.feed(hp(0.0, A, -1, MOB, 1))
    actors = [r.feed(hp(1.0, B, -5, MOB, 2))[0].actor, r.feed(hp(2.0, C, -5, MOB, 3))[0].actor]
    assert actors == ["Zecron", "Zecron (2)"] and r.own_actors() == ["you", "Zecron", "Zecron (2)"]


def test_fight_summary_stores_own_actors_and_old_dicts_load():
    from eqoa_parser.events import Event
    f = Fight(1, 0.0, 1.0, [Event(0.0, "you", "x", 5, None, "damage", ""), Event(1.0, "Zed", "x", 7, None, "damage", "")])
    s = summarize_fight(f, own_actors=["you", "Zed"])
    assert s.own_actors == ["you", "Zed"] and s.combined_total == 12
    assert FightSummary.from_dict(s.to_dict()) == s
    assert summarize_fight(f).own_actors is None
    d = s.to_dict()
    del d["own_actors"]
    assert FightSummary.from_dict(d).own_actors is None
