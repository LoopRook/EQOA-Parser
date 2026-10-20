"""Attribution tests (session B): synthetic records plus the real-capture oracle counts."""
from __future__ import annotations

import io
from collections import Counter
from pathlib import Path

import pytest

from eqoa_parser.config import load_config
from eqoa_parser.net.attribution import Attributor, ConnectionRouter
from eqoa_parser.net.protocol import HpChange, Swing
from eqoa_parser.net.sources import PcapReplaySource

REAL = Path(__file__).resolve().parent.parent / "fixtures" / "real"
P, D, PET, OTHER = 0x13FF89D8, 0x01312F0A, 0x01374606, 0x01000001
H = P & 0xFFFF
B_PLAYER = 0x1751CB82


def hp(ts, src, amt, tgt, ext=False, conn=1, h16=H, seq=0):
    return HpChange(ts, conn, h16, seq, src, amt, tgt, ext, b"\xdb\x00\x01")


def sw(ts, att, tgt, conn=1, h16=H):
    return Swing(ts, conn, h16, 0, att, tgt, 768000, b"\xb1\x00")


def test_player_damage_and_target_names():
    a = Attributor()
    (e,) = a.feed(hp(1.0, P, -598, D))
    assert (e.actor, e.target, e.amount, e.kind, e.ability, e.ts) == ("you", "0x01312F0A", 598, "damage", None, 1.0)
    assert e.raw == "db 00 01"
    assert a.player_id == P


def test_player_is_identified_even_as_target_first():
    a = Attributor()
    (e,) = a.feed(hp(1.0, D, -21, P))
    assert a.player_id == P and (e.kind, e.actor, e.target, e.amount) == ("damage_taken", "0x01312F0A", "you", 21)


def test_extended_player_record_is_pet_damage_zero_counts():
    a = Attributor()
    a.feed(hp(0.5, P, -1, D))
    e1, = a.feed(hp(1.0, P, -25, D, ext=True))
    e2, = a.feed(hp(2.0, P, 0, D, ext=True))
    assert (e1.actor, e1.amount, e1.kind) == ("pet", 25, "damage")
    assert (e2.actor, e2.amount, e2.kind) == ("pet", 0, "damage")


def test_heal_event():
    a = Attributor()
    (e,) = a.feed(hp(1.0, D, 300, P))
    assert (e.kind, e.actor, e.target, e.amount) == ("heal", "0x01312F0A", "you", 300)
    a.feed(hp(2.0, P, -5, D))
    (e,) = a.feed(hp(3.0, P, 40, P))
    assert (e.actor, e.target) == ("you", "you")


def test_pet_discovery_from_swing_then_extended_hit_within_one_second():
    a = Attributor()
    a.feed(hp(0.0, P, -1, D))
    a.feed(sw(1.0, PET, D))
    a.feed(hp(1.5, P, -25, D, ext=True))
    assert a.pet_id == PET
    (e,) = a.feed(hp(2.0, D, -7, PET))
    assert (e.kind, e.actor, e.target, e.amount) == ("damage_taken", "0x01312F0A", "pet", 7)


def test_pet_not_learned_when_too_late_wrong_target_player_swing_or_non_extended():
    for swings, rec in [
        ([sw(0.0, PET, D)], hp(1.5, P, -25, D, ext=True)),         # > 1.0 s
        ([sw(1.0, PET, OTHER)], hp(1.2, P, -25, D, ext=True)),     # other target
        ([sw(1.0, P, D)], hp(1.2, P, -25, D, ext=True)),           # the player's own swing
        ([sw(1.0, PET, D)], hp(1.2, P, -25, D, ext=False)),        # not extended
    ]:
        a = Attributor()
        a.feed(hp(0.0, P, -1, D))
        for s in swings:
            a.feed(s)
        a.feed(rec)
        assert a.pet_id is None


def test_first_pet_wins():
    a = Attributor()
    a.feed(hp(0.0, P, -1, D))
    a.feed(sw(1.0, PET, D))
    a.feed(hp(1.1, P, -25, D, ext=True))
    a.feed(sw(2.0, OTHER, D))
    a.feed(hp(2.1, P, -25, D, ext=True))
    assert a.pet_id == PET


def test_unrelated_combat_produces_no_event_and_is_counted():
    a = Attributor()
    a.feed(hp(0.0, P, -1, D))
    assert a.feed(hp(1.0, OTHER, -30, D)) == [] and a.feed(hp(1.5, OTHER, 30, D)) == []
    assert a.dropped_unrelated == 2


def test_character_change_on_same_connection_resets_state():
    a = Attributor()
    a.feed(hp(0.0, P, -1, D))
    other_player = 0x1751CB82
    (e,) = a.feed(hp(5.0, other_player, -9, D, h16=other_player & 0xFFFF))
    assert a.player_id == other_player and e.actor == "you"


def test_router_follows_configured_port_only():
    cfg = load_config()
    cfg.net.local_port = 2
    r = ConnectionRouter(cfg, log=io.StringIO())
    assert r.feed(hp(1.0, P, -5, D, conn=1)) == []
    assert len(r.feed(hp(1.0, P, -5, D, conn=2))) == 1


def test_router_default_follows_all_connections_primary_is_first_event():
    log = io.StringIO()
    r = ConnectionRouter(load_config(), log=log)
    assert r.feed(sw(0.5, PET, D, conn=9)) == []         # swings alone produce no Event, so no primary yet
    assert r.selected is None
    assert [e.actor for e in r.feed(hp(1.0, P, -5, D, conn=7))] == ["you"]
    assert r.selected == 7
    (e,) = r.feed(hp(2.0, B_PLAYER, -8, D, conn=8, h16=B_PLAYER & 0xFFFF))
    assert e.actor == "char:8" and e.kind == "damage"
    assert [e.actor for e in r.feed(hp(3.0, P, -5, D, conn=7))] == ["you"]
    out = log.getvalue()
    assert out.count("eqoa net: following connection") == 1 and out.count("also following") == 1 and "char:8" in out
    assert r.own_actors() == ["you", "char:8"] and r.connections() == [7, 8]


def test_label_uses_name_known_at_first_event_and_is_fixed():
    from eqoa_parser.net.names import NameTable
    from _net_builders import SERVER as S
    t = NameTable(S)
    t._names[B_PLAYER] = "Zecron"
    r = ConnectionRouter(load_config(), log=io.StringIO(), names=t)
    r.feed(hp(1.0, P, -5, D, conn=7))
    assert r.feed(hp(2.0, B_PLAYER, -8, D, conn=8, h16=B_PLAYER & 0xFFFF))[0].actor == "Zecron"
    t._names[B_PLAYER] = "Renamed"
    assert r.feed(hp(3.0, B_PLAYER, -8, D, conn=8, h16=B_PLAYER & 0xFFFF))[0].actor == "Zecron"


def test_non_primary_pet_label_and_target_that_is_other_connections_player():
    r = ConnectionRouter(load_config(), log=io.StringIO())
    r.feed(hp(1.0, P, -5, D, conn=7))
    h = B_PLAYER & 0xFFFF
    r.feed(hp(2.0, B_PLAYER, -1, D, conn=8, h16=h))
    r.feed(sw(3.0, 0x0170, D, conn=8, h16=h))
    r.feed(hp(3.5, B_PLAYER, -25, D, ext=True, conn=8, h16=h))
    assert r.attributor(8).pet_id == 0x0170
    e = r.feed(hp(4.0, B_PLAYER, -25, D, ext=True, conn=8, h16=h))[0]
    assert e.actor == "char:8's pet"
    # a hostile hit on the other character, seen on the primary connection: target uses that label
    (e,) = r.feed(hp(5.0, D, -9, P, conn=7))
    assert (e.kind, e.target) == ("damage_taken", "you")
    # a heal from the primary's player onto the other character, seen on connection 8
    (e,) = r.feed(hp(6.0, P, 30, B_PLAYER, conn=8, h16=h))
    assert (e.kind, e.actor, e.target) == ("heal", "you", "char:8")


def test_no_double_counting_two_characters_same_mob(tmp_path):
    """Each connection also receives the other's records; only records involving its own player/pet count."""
    from _net_builders import CLIENT, SERVER, hp_body, msg, payload
    from eqoa_parser.net.pcapng import UdpDatagram
    from eqoa_parser.net.sources import PacketSource

    class S(PacketSource):
        pass

    cfg = load_config()
    s = S(cfg)
    hb = B_PLAYER & 0xFFFF

    def both(ts, src, amt, seq):
        body = hp_body(src, -amt, D)
        a = UdpDatagram(ts, SERVER, 10070, CLIENT, 111, payload(H, msg(seq, body)))
        b = UdpDatagram(ts, SERVER, 10070, CLIENT, 222, payload(hb, msg(seq, body)))
        return s.events_from(a) + s.events_from(b)

    evs = []
    for k in range(4):
        evs += both(10.0 + k, P, 100, 1 + 2 * k)
        evs += both(10.5 + k, B_PLAYER, 50, 2 + 2 * k)
    assert sorted(Counter((e.actor, e.amount) for e in evs).items()) == [(("char:222", 50), 4), (("you", 100), 4)]
    from eqoa_parser.pipeline import Engine
    closed = []
    e = Engine(cfg, s, on_closed=closed.append)
    for ev in sorted(evs, key=lambda x: x.ts):
        e.step(ev.ts, ev)
    e.finish()
    (f,) = closed
    assert f.combined_total == 600 and f.you.total == 400 and f.actor("char:222").total == 200
    assert f.targets == ["0x01312F0A"] and len(f.top_abilities) == 2


def test_explicit_port_ignores_others_and_other_connections_never_claim_primary():
    cfg = load_config()
    cfg.net.local_port = 8
    r = ConnectionRouter(cfg, log=io.StringIO())
    assert r.feed(hp(1.0, P, -5, D, conn=7)) == []
    (e,) = r.feed(hp(2.0, B_PLAYER, -8, D, conn=8, h16=B_PLAYER & 0xFFFF))
    assert e.actor == "you" and r.selected == 8 and r.own_actors() == ["you"]


# ---- real captures ---------------------------------------------------------------------------------
def events(name, port=None):
    cfg = load_config()
    cfg.net.local_port = port
    src = PcapReplaySource(REAL / name, cfg)
    return [e for _, e in src if not isinstance(e, str)]


def summary(evs):
    return Counter((e.actor, e.kind, e.target, e.amount) for e in evs)


def test_cap1_thirteen_damage_events_in_order():
    evs = events("cap1_necro_spells.pcapng")
    assert [(e.actor, e.kind, e.target) for e in evs] == [("you", "damage", "0x01312F0A")] * 13
    assert [e.amount for e in evs] == [598, 598, 599, 599, 599, 444, 599, 599, 598, 460, 460, 467, 598]
    assert sum(e.amount for e in evs) == 7218


def test_cap2_bard_connection():
    evs = events("cap2_bard_melee_necro_pet.pcapng", 21173)
    kinds = Counter(e.kind for e in evs)
    assert kinds == {"damage": 56, "damage_taken": 56, "heal": 56}
    assert {e.actor for e in evs if e.kind == "damage"} == {"you"}
    assert {(e.amount, e.target) for e in evs if e.kind == "damage_taken"} == {(21, "you")}
    assert {e.amount for e in evs if e.kind == "heal"} == {300}


def test_cap2_necro_connection():
    evs = events("cap2_bard_melee_necro_pet.pcapng", 20605)
    assert summary(evs) == {("pet", "damage", "0x01312F0A", 25): 27, ("pet", "damage", "0x01312F0A", 0): 9}


def test_cap2_default_follows_both_connections(tmp_path):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"))
    src = PcapReplaySource(REAL / "cap2_bard_melee_necro_pet.pcapng", cfg)
    from eqoa_parser.pipeline import Engine
    closed = []
    Engine(cfg, src, on_closed=closed.append).run()
    f1, f2 = closed
    assert (f1.you.total, f1.you.hits, f1.combined_total) == (20670, 56, 20670) and f1.damage_taken == 1176
    # the necro connection is not primary: its pet is "<character name>'s pet" (name known at the pet's first Event)
    assert list(f2.actors) == ["Zecron's pet"] and f2.actor("Zecron's pet").total == 675 and f2.actor("Zecron's pet").hits == 36
    assert f2.you.total == 0 and f2.combined_total == 675
    assert src.own_actors() == ["you", "Zecron's pet"] and src.router.selected == 21173


def test_self_damage_is_damage_taken_not_dps():
    """User report: damage you do to yourself (e.g. HP-cost spells) was counted as DPS."""
    a = Attributor()
    a.feed(hp(1.0, P, -100, D))                      # learn the player with a normal hit
    (e,) = a.feed(hp(2.0, P, -50, P))                # you damage yourself
    assert (e.actor, e.target, e.amount, e.kind) == ("you", "you", 50, "damage_taken")


def test_self_damage_counts_in_taken_not_in_damage_done(tmp_path):
    from eqoa_parser.segmenter import Segmenter
    from eqoa_parser.stats import summarize_fight
    a, seg = Attributor(), Segmenter(8.0)
    for r in (hp(1.0, P, -100, D), hp(2.0, P, -50, P), hp(3.0, D, -20, P)):
        for ev in a.feed(r):
            seg.feed(ev)
    (fight,) = seg.flush()
    s = summarize_fight(fight)
    assert s.you.total == 100 and s.combined_total == 100
    assert s.damage_taken == 70
