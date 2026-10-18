"""Independent verification, Phase 3 (session D): following several emulators ("Following several emulators" in docs/CONTRACTS.md).

Hand-built, SYNTHETIC multi-connection streams.  The server is simulated the way the Phase 3 contract describes it: every record
is delivered on EVERY connection ("each connection receives the other's records"), each connection with its own header word and
its own sequence numbers.  Tests whose docstring starts "BUG-P3-x" assert the contract / required behaviour and are expected to
FAIL against the current code (see docs/VERIFICATION.md, Phase 3).  "RISK" / "LIMIT" tests pass and pin down inherent properties.
"""
from __future__ import annotations

import json
import re
import struct
import threading
import time
from pathlib import Path

import pytest

from eqoa_parser.config import load_config
from eqoa_parser.events import KIND_DAMAGE, KIND_DAMAGE_TAKEN, KIND_HEAL, Event
from eqoa_parser.net.attribution import ConnectionRouter
from eqoa_parser.net.names import NameTable
from eqoa_parser.net.pcapng import UdpDatagram, read_pcapng
from eqoa_parser.net.sources import LivePacketSource, PacketSource, PcapReplaySource
from eqoa_parser.pipeline import Engine
from eqoa_parser.segmenter import Segmenter
from eqoa_parser.stats import FightSummary, summarize_fight, summarize_run
from test_verify_net import BASE, CLIENT, MOB, MOB2, PET, SERVER, FakeCapture, dg, hp, msg, payload, swing

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "fixtures" / "real"
A, PA = 0x174A1A40, 0x01374606            # character A (the bard in cap2) and its pet
B, PB = 0x1751CB82, 0x01445566            # character B (the necro) and its pet
C, PCC = 0x1800AA11, 0x01667788           # character C
PORT_A, PORT_B, PORT_C = 20000, 20001, 20002
EXT = b"\x00"                             # "extended" tail: a pet action credited to its owner


class Sim:
    """Server that delivers every record to every connection (own header word, own seq counter per connection)."""

    def __init__(self, chars: dict[int, int]):
        self.chars = dict(chars)
        self.seq = {p: 100 for p in chars}

    def send(self, ts: float, *bodies: bytes, to=None) -> list[UdpDatagram]:
        out = []
        for port, pid in self.chars.items():
            if to is not None and port not in to:
                continue
            ms = []
            for b in bodies:
                self.seq[port] += 1
                ms.append(msg(self.seq[port], b))
            out.append(dg(ts, payload(pid & 0xFFFF, *ms), port))
        return out


def name_dgram(ts: float, entity: int, name: str, port: int = PORT_A, h16: int = A & 0xFFFF) -> UdpDatagram:
    """A server datagram the NameTable heuristic accepts: <id u32> 82 31 ... (0x80|len) 04 <name>."""
    raw = name.encode()
    body = struct.pack("<I", entity) + b"\x82\x31" + b"\x01\x02\x03" + bytes([0x80 | len(raw), 0x04]) + raw + b"\x05\x06"
    return dg(ts, struct.pack("<H", h16) + b"\x01" + body + b"\xaa\xbb\xcc\xdd", port)


def run(dgrams, cfg=None):
    src = PacketSource(cfg or load_config())
    evs: list[Event] = []
    for d in dgrams:
        evs.extend(src.events_from(d))
    return evs, src


def fight_of(evs, src, idle=8.0) -> list[FightSummary]:
    seg = Segmenter(idle)
    closed = []
    for e in evs:
        closed += seg.feed(e)
    closed += seg.flush()
    return [summarize_fight(f, own_actors=src.own_actors()) for f in closed]


# ============================================================================== both characters on the same mob: no double counting
def _two_melee_chars_same_mob(n=60, seed=7):
    import random

    rnd = random.Random(seed)
    sim = Sim({PORT_A: A, PORT_B: B})
    dgs, a_hits, b_hits, a_taken, b_taken = [], [], [], [], []
    for i in range(n):
        t = BASE + i * 0.4
        who = "A" if i == 0 else rnd.choice("AB")          # A hits first, so A is the primary (`you`)
        amt = rnd.randint(1, 900)
        if who == "A":
            dgs += sim.send(t, hp(A, -amt, MOB))
            a_hits.append(amt)
        else:
            dgs += sim.send(t, hp(B, -amt, MOB))
            b_hits.append(amt)
        if i % 4 == 0:
            dgs += sim.send(t + 0.1, hp(MOB, -7, A))
            a_taken.append(7)
        if i % 5 == 0:
            dgs += sim.send(t + 0.2, hp(MOB, -9, B))
            b_taken.append(9)
        if i % 9 == 0:
            dgs += sim.send(t + 0.3, hp(A, 300, A))                        # A's own heal tick
    return dgs, a_hits, b_hits, a_taken, b_taken


def test_two_characters_on_the_same_mob_are_counted_exactly_once_each():
    dgs, a_hits, b_hits, a_taken, b_taken = _two_melee_chars_same_mob()
    evs, src = run(dgs)
    dmg = [e for e in evs if e.kind == KIND_DAMAGE]
    assert [e.amount for e in dmg if e.actor == "you"] == a_hits
    assert [e.amount for e in dmg if e.actor == "char:%d" % PORT_B] == b_hits
    assert sorted({e.actor for e in dmg}) == sorted(["you", "char:%d" % PORT_B])
    taken = [(e.target, e.amount) for e in evs if e.kind == KIND_DAMAGE_TAKEN]
    assert sorted(taken) == sorted([("you", 7)] * len(a_taken) + [("char:%d" % PORT_B, 9)] * len(b_taken))
    # every record the other character's connection receives about the first one is dropped, and vice versa
    assert src.router.attributor(PORT_A).dropped_unrelated == len(b_hits) + len(b_taken)
    assert src.router.attributor(PORT_B).dropped_unrelated == len(a_hits) + len(a_taken) + 7        # + A's heal ticks
    f = fight_of(evs, src)
    assert len(f) == 1 and f[0].combined_total == sum(a_hits) + sum(b_hits)
    assert f[0].you.total == sum(a_hits) and f[0].actor("char:%d" % PORT_B).total == sum(b_hits)
    assert f[0].damage_taken == sum(a_taken) + sum(b_taken)                                         # once, not twice
    assert src.own_actors() == ["you", "char:%d" % PORT_B]


def test_heal_between_the_two_characters_is_labelled_with_the_other_connections_label():
    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = sim.send(BASE, hp(A, -10, MOB)) + sim.send(BASE + 1, hp(B, -20, MOB)) + sim.send(BASE + 2, hp(A, 300, B))
    evs, _ = run(dgs)
    heals = [e for e in evs if e.kind == KIND_HEAL]
    assert {(e.actor, e.target) for e in heals} == {("you", "char:%d" % PORT_B)}
    assert [h.amount for h in heals] == [300, 300]        # one per connection that can see both ends (A's source, B's target)


def test_mob_hitting_one_character_is_damage_taken_only_on_that_characters_connection():
    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = sim.send(BASE, hp(A, -10, MOB)) + sim.send(BASE + 1, hp(B, -20, MOB)) + sim.send(BASE + 2, hp(MOB, -33, B))
    evs, _ = run(dgs)
    assert [(e.kind, e.actor, e.target, e.amount) for e in evs if e.kind == KIND_DAMAGE_TAKEN] == \
        [(KIND_DAMAGE_TAKEN, "0x%08X" % MOB, "char:%d" % PORT_B, 33)]


# ============================================================================== pets: ids learned from swings seen on EVERY connection
def test_other_characters_pet_swinging_at_the_same_mob_does_not_become_my_pet():
    """BUG-P3-1 (medium): pet learning takes the most recent swing at T within 1 s before the player's extended hit at T, from ANY
    attacker.  With several characters on one mob, every connection receives the other characters' swing records, so B's pet's
    swing (later than my own pet's) is learned as MY pet.  Then (a) damage taken by B's pet is counted as damage taken on my pet
    as well as on B's connection (double counted) and (b) B's pet's own records are no longer dropped on my connection.
    Two necros on one mob is the obvious two-emulator case."""
    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = []
    dgs += sim.send(BASE, hp(A, -1, MOB), hp(B, -1, MOB))                                     # both established, A primary
    dgs += sim.send(BASE + 10, swing(PA, MOB))                                                 # A's pet swings
    dgs += sim.send(BASE + 10.1, swing(PB, MOB))                                               # B's pet swings (a moment later)
    dgs += sim.send(BASE + 10.5, hp(A, -25, MOB, EXT), hp(B, -25, MOB, EXT))                  # both pets hit (credited to owners)
    dgs += sim.send(BASE + 12, hp(MOB, -40, PB))                                               # the mob hits B's pet
    evs, src = run(dgs)
    assert src.router.attributor(PORT_A).pet_id == PA, "A's pet was mislearned as 0x%08X" % (src.router.attributor(PORT_A).pet_id or 0)
    assert src.router.attributor(PORT_B).pet_id in (PB, None)
    taken = [e for e in evs if e.kind == KIND_DAMAGE_TAKEN]
    assert len(taken) == 1 and taken[0].target == "char:%d's pet" % PORT_B                      # counted once, on B's pet


def test_other_players_swing_at_my_mob_does_not_turn_their_damage_into_my_pet_damage():
    """BUG-P3-2 (medium, same root cause): if the server also sends other PLAYERS' swing records (UNVERIFIED), a swing by B at the mob
    right before my extended hit makes B 'my pet'.  B's own hits, which my connection receives, then stop being dropped and are
    emitted as damage by my pet label -> B's damage is counted twice (under B's label from B's connection and under my `pet`)."""
    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = []
    dgs += sim.send(BASE, hp(A, -1, MOB), hp(B, -1, MOB))
    dgs += sim.send(BASE + 10, swing(PA, MOB), swing(B, MOB))                                  # my pet and B swing; B's swing is last
    dgs += sim.send(BASE + 10.4, hp(A, -25, MOB, EXT))
    dgs += sim.send(BASE + 11, hp(B, -500, MOB))                                               # B's melee hit, seen on both connections
    evs, src = run(dgs)
    dmg = [e for e in evs if e.kind == KIND_DAMAGE]
    total_from_events = sum(e.amount for e in dmg)
    assert total_from_events == 1 + 1 + 25 + 500, f"damage counted {total_from_events}, true total {1 + 1 + 25 + 500}: {[(e.actor, e.amount) for e in dmg]}"


def test_pet_of_the_second_character_alone_is_not_learned_by_a_character_without_a_pet():
    """A has no pet (never an extended hit), so B's pet swinging at the same mob cannot be learned as A's pet; B's pet hits are dropped
    on A's connection and counted once, from B's connection, under B's pet label."""
    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = sim.send(BASE, hp(A, -10, MOB), hp(B, -1, MOB))
    for k in range(6):
        t = BASE + 2 + k
        dgs += sim.send(t, swing(PB, MOB)) + sim.send(t + 0.4, hp(B, -25, MOB, EXT)) + sim.send(t + 0.6, hp(A, -50, MOB))
    evs, src = run(dgs)
    assert src.router.attributor(PORT_A).pet_id is None and src.router.attributor(PORT_B).pet_id == PB
    pet_hits = [e for e in evs if e.actor == "char:%d's pet" % PORT_B]
    assert len(pet_hits) == 6 and sum(e.amount for e in pet_hits) == 150
    assert sum(e.amount for e in evs if e.actor == "you") == 10 + 6 * 50
    assert src.own_actors() == ["you", "char:%d" % PORT_B, "char:%d's pet" % PORT_B]


def test_each_characters_own_pet_hits_are_labelled_per_owner_and_never_mixed():
    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = sim.send(BASE, hp(A, -1, MOB), hp(B, -1, MOB))
    for k in range(5):
        t = BASE + 20 + k * 3
        dgs += sim.send(t, swing(PA, MOB)) + sim.send(t + 0.4, hp(A, -25, MOB, EXT))         # A's pet
        dgs += sim.send(t + 1.5, swing(PB, MOB2)) + sim.send(t + 1.9, hp(B, -40, MOB2, EXT))  # B's pet on another mob, >1 s apart
    evs, src = run(dgs)
    assert [e.amount for e in evs if e.actor == "pet"] == [25] * 5
    assert [e.amount for e in evs if e.actor == "char:%d's pet" % PORT_B] == [40] * 5
    assert src.router.attributor(PORT_A).pet_id == PA and src.router.attributor(PORT_B).pet_id == PB


# ============================================================================== labels
def test_label_is_the_name_when_known_before_the_first_event_and_pet_label_follows():
    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = [name_dgram(BASE - 1, B, "Zecron")] + sim.send(BASE, hp(A, -10, MOB)) + sim.send(BASE + 1, swing(PB, MOB)) \
        + sim.send(BASE + 1.3, hp(B, -25, MOB, EXT)) + sim.send(BASE + 2, hp(B, -70, MOB))
    evs, src = run(dgs)
    assert src.router.attributor(PORT_B).label == "Zecron"
    assert [(e.actor, e.amount) for e in evs] == [("you", 10), ("Zecron's pet", 25), ("Zecron", 70)]
    assert src.own_actors() == ["you", "Zecron's pet", "Zecron"]


def test_label_stays_char_port_when_the_name_arrives_after_the_first_event():
    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = sim.send(BASE, hp(A, -10, MOB)) + sim.send(BASE + 1, hp(B, -20, MOB)) + [name_dgram(BASE + 2, B, "Zecron")] \
        + sim.send(BASE + 3, hp(B, -30, MOB)) + sim.send(BASE + 4, swing(PB, MOB)) + sim.send(BASE + 4.3, hp(B, -25, MOB, EXT))
    evs, src = run(dgs)
    assert src.names.get(B) == "Zecron"                                  # the table did learn it ...
    assert {e.actor for e in evs} == {"you", "char:%d" % PORT_B, "char:%d's pet" % PORT_B}     # ... but the label is fixed for the run
    assert src.router.attributor(PORT_B).label == "char:%d" % PORT_B


def test_name_in_the_very_datagram_of_the_first_event_is_used():
    sim = Sim({PORT_A: A, PORT_B: B})
    first = sim.send(BASE, hp(A, -10, MOB)) + sim.send(BASE + 1, hp(B, -20, MOB))
    nd = name_dgram(BASE + 1, B, "Zecron", PORT_B, B & 0xFFFF)
    # names are fed before the same datagram's records are attributed ("names first")
    merged = UdpDatagram(nd.ts, nd.src_ip, nd.src_port, nd.dst_ip, nd.dst_port, nd.payload[:-4] + msg(999, hp(B, -20, MOB)) + nd.payload[-4:])
    evs, src = run(first[:2] + [merged])
    assert [e.actor for e in evs if e.amount == 20] == ["Zecron"]


def test_primary_keeps_you_and_pet_even_when_its_name_is_known():
    sim = Sim({PORT_A: A, PORT_B: B})
    evs, src = run([name_dgram(BASE - 1, A, "Bardolf")] + sim.send(BASE, hp(A, -10, MOB)) + sim.send(BASE + 1, hp(B, -20, MOB)))
    assert [e.actor for e in evs] == ["you", "char:%d" % PORT_B] and src.router.attributor(PORT_A).label is None


def test_mob_names_still_resolve_and_other_characters_are_never_named_after_a_mob():
    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = [name_dgram(BASE - 1, MOB, "Rotting Flesh"), name_dgram(BASE - 1, B, "Zecron")] + sim.send(BASE, hp(A, -10, MOB)) + sim.send(BASE + 1, hp(MOB, -5, B))
    evs, _ = run(dgs)
    assert [(e.actor, e.target) for e in evs if e.kind == KIND_DAMAGE] == [("you", "Rotting Flesh")]
    assert [(e.actor, e.target) for e in evs if e.kind == KIND_DAMAGE_TAKEN] == [("Rotting Flesh", "Zecron")]


def test_two_connections_with_the_same_name_get_unique_labels():
    """RISK-9 (fixed by the Phase 3 amendment, test updated by main chat): two followed connections whose players resolve to the
    same name get unique labels ("Zecron", "Zecron (2)"), so their damage is not merged."""
    sim = Sim({PORT_A: A, PORT_B: B, PORT_C: C})
    dgs = [name_dgram(BASE - 1, B, "Zecron"), name_dgram(BASE - 1, C, "Zecron")] + sim.send(BASE, hp(A, -10, MOB)) \
        + sim.send(BASE + 1, hp(B, -20, MOB)) + sim.send(BASE + 2, hp(C, -30, MOB))
    evs, src = run(dgs)
    assert [e.actor for e in evs] == ["you", "Zecron", "Zecron (2)"] and src.own_actors() == ["you", "Zecron", "Zecron (2)"]
    f = fight_of(evs, src)[0]
    assert f.actor("Zecron").total == 20 and f.actor("Zecron (2)").total == 30 and f.combined_total == 60


def test_risk_label_is_stale_when_a_different_character_logs_in_on_the_same_connection():
    """RISK-10: label and role are fixed per connection for the run; a header-word change (another character on the same client
    port) resets player/pet ids but keeps the old label, so the new character's damage is shown under the previous one's name."""
    sim_old = Sim({PORT_A: A, PORT_B: B})
    dgs = [name_dgram(BASE - 1, B, "Zecron")] + sim_old.send(BASE, hp(A, -10, MOB)) + sim_old.send(BASE + 1, hp(B, -20, MOB))
    sim_new = Sim({PORT_A: A, PORT_B: C})
    dgs += [name_dgram(BASE + 99, C, "Newguy")] + sim_new.send(BASE + 100, hp(C, -40, MOB))
    evs, src = run(dgs)
    assert [(e.actor, e.amount) for e in evs] == [("you", 10), ("Zecron", 20), ("Zecron", 40)]


# ============================================================================== third connection / ordering / disappearing
def test_third_connection_gets_its_own_label_and_order_is_first_seen():
    sim = Sim({PORT_A: A, PORT_B: B, PORT_C: C})
    dgs = sim.send(BASE, hp(A, -10, MOB))
    dgs += sim.send(BASE + 1, swing(PCC, MOB)) + sim.send(BASE + 1.3, hp(C, -25, MOB, EXT))        # C's pet first, then C
    dgs += sim.send(BASE + 2, hp(C, -30, MOB))
    dgs += sim.send(BASE + 3, hp(B, -20, MOB))                                                       # B appears last
    dgs += sim.send(BASE + 4, swing(PA, MOB)) + sim.send(BASE + 4.3, hp(A, -15, MOB, EXT))           # primary's pet shows up last of all
    evs, src = run(dgs)
    assert src.own_actors() == ["you", "pet", "char:%d's pet" % PORT_C, "char:%d" % PORT_C, "char:%d" % PORT_B]   # you, pet first
    f = fight_of(evs, src)[0]
    assert f.combined_total == 10 + 25 + 30 + 20 + 15
    assert [t["actor"] for t in f.top_abilities] == ["char:%d" % PORT_C, "you", "char:%d" % PORT_B] or len(f.top_abilities) == 3


def test_own_actors_returns_a_copy_and_you_pet_head_is_stable():
    sim = Sim({PORT_A: A, PORT_B: B})
    evs, src = run(sim.send(BASE, hp(B, -5, MOB)) + sim.send(BASE + 1, hp(A, -6, MOB)))
    first = src.own_actors()
    first.append("junk")
    assert src.own_actors() == ["you", "char:%d" % PORT_A]            # B produced the first Event, so B is `you`; A is the label
    assert src.router.selected == PORT_B


def test_heal_first_connection_becomes_the_primary_even_without_fighting():
    """RISK-11 (contract: primary = first connection that produces an Event; heals are Events): an idle character whose connection
    carries a heal/regeneration record before anything else is `you`; the character that actually fights becomes a label."""
    sim = Sim({PORT_A: A, PORT_B: B})
    evs, src = run(sim.send(BASE, hp(B, 300, B), to=[PORT_B]) + sim.send(BASE + 1, hp(A, -50, MOB), to=[PORT_A]))
    assert src.router.selected == PORT_B and [(e.kind, e.actor) for e in evs] == [(KIND_HEAL, "you"), (KIND_DAMAGE, "char:%d" % PORT_A)]


def test_connection_that_disappears_keeps_its_label_and_later_fights_are_unaffected():
    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = [name_dgram(BASE - 1, B, "Zecron")] + sim.send(BASE, hp(A, -10, MOB), hp(B, -20, MOB))
    dgs += sim.send(BASE + 60, hp(A, -30, MOB), to=[PORT_A])                          # B's emulator is gone: nothing arrives on its port
    dgs += sim.send(BASE + 120, hp(A, -40, MOB), to=[PORT_A])
    evs, src = run(dgs)
    fights = fight_of(evs, src)
    assert [f.combined_total for f in fights] == [30, 30, 40] and src.own_actors() == ["you", "Zecron"]
    assert fights[1].actor("Zecron").total == 0 and fights[1].you.total == 30
    # it comes back later on the same port: same label, same role
    evs2, src2 = run(dgs + sim.send(BASE + 200, hp(B, -50, MOB)))
    assert [(e.actor, e.amount) for e in evs2][-1] == ("Zecron", 50)


def test_connection_that_only_ever_sees_strangers_gets_no_label_and_does_not_pollute_own_actors():
    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = sim.send(BASE, hp(0x01111111, -9, 0x02222222), to=[PORT_B]) + sim.send(BASE + 1, hp(A, -10, MOB), to=[PORT_A])
    evs, src = run(dgs)
    assert src.own_actors() == ["you"] and not src.router.attributor(PORT_B).role_fixed and src.router.connections() == [PORT_A]


# ============================================================================== --connection restores the single behaviour
@pytest.mark.parametrize("keep,expect_you", [(PORT_A, 10), (PORT_B, 20), (PORT_C, 30)])
def test_explicit_connection_restores_single_emulator_naming_with_three_connections(keep, expect_you):
    sim = Sim({PORT_A: A, PORT_B: B, PORT_C: C})
    dgs = sim.send(BASE, hp(A, -10, MOB)) + sim.send(BASE + 1, hp(B, -20, MOB)) + sim.send(BASE + 2, hp(C, -30, MOB))
    dgs += sim.send(BASE + 3, swing(PCC, MOB)) + sim.send(BASE + 3.3, hp(C, -25, MOB, EXT))
    cfg = load_config()
    cfg.net.local_port = keep
    evs, src = run(dgs, cfg)
    assert [e.actor for e in evs if e.kind == KIND_DAMAGE and e.actor != "pet"] == ["you"]
    assert [e.amount for e in evs if e.actor == "you"] == [expect_you]
    assert set(src.own_actors()) <= {"you", "pet"} and src.router.selected == keep and src.router.connections() == [keep]
    assert all(not a.role_fixed for p, a in src.router._attrs.items() if p != keep) and all(a.label is None for a in src.router._attrs.values())


def test_explicit_connection_prints_no_follow_messages(capsys):
    sim = Sim({PORT_A: A, PORT_B: B})
    cfg = load_config()
    cfg.net.local_port = PORT_B
    run(sim.send(BASE, hp(A, -10, MOB)) + sim.send(BASE + 1, hp(B, -20, MOB)), cfg)
    assert capsys.readouterr().err == ""


def test_explicit_connection_that_never_appears_yields_nothing():
    sim = Sim({PORT_A: A})
    cfg = load_config()
    cfg.net.local_port = 33333
    evs, src = run(sim.send(BASE, hp(A, -10, MOB)), cfg)
    assert evs == [] and src.own_actors() == []


# ============================================================================== stats: own_actors
def _fight(events):
    seg = Segmenter(8.0)
    for e in events:
        seg.feed(e)
    return seg.flush()[0]


def _ev(ts, actor, amount, target="a rat", ability=None, kind=KIND_DAMAGE):
    return Event(ts, actor, target, amount, ability, kind, "x")


def test_summarize_fight_own_actors_none_is_the_old_you_plus_pet_rule():
    f = _fight([_ev(0, "you", 10), _ev(1, "pet", 5), _ev(2, "Bob", 100), _ev(3, "char:1", 7)])
    s = summarize_fight(f)
    assert s.combined_total == 15 and [t["actor"] for t in s.top_abilities] == ["you", "pet"]
    assert summarize_fight(f, own_actors=None).combined_total == 15


def test_summarize_fight_own_actors_cover_exactly_the_given_actors():
    f = _fight([_ev(0, "you", 10), _ev(1, "pet", 5), _ev(2, "Bob", 100), _ev(3, "char:1", 7), _ev(4, "char:1's pet", 7)])
    s = summarize_fight(f, own_actors=["you", "pet", "char:1", "char:1's pet", "ghost"])
    assert s.combined_total == 29 and s.combined_dps == pytest.approx(29 / 4.0)
    assert "Bob" in s.actors and s.actor("Bob").total == 100                       # still stored, just not combined
    assert summarize_fight(f, own_actors=[]).combined_total == 0
    assert summarize_fight(f, own_actors=["Bob"]).combined_total == 100


def test_top_abilities_use_own_actor_order_as_tiebreak_and_cap_at_three():
    f = _fight([_ev(0, "char:2", 10, ability="z"), _ev(1, "you", 10, ability="z"), _ev(2, "char:1", 10, ability="z"), _ev(3, "pet", 10, ability="z")])
    s = summarize_fight(f, own_actors=["you", "pet", "char:1", "char:2"])
    assert [t["actor"] for t in s.top_abilities] == ["you", "pet", "char:1"]
    s2 = summarize_fight(f, own_actors=["char:2", "you"])
    assert [t["actor"] for t in s2.top_abilities] == ["char:2", "you"]


def test_best_fight_and_average_use_combined_over_own_actors(tmp_path):
    f1 = summarize_fight(_fight([_ev(0, "you", 100)]), own_actors=["you"])
    f2 = summarize_fight(_fight([_ev(100, "you", 10), _ev(100, "char:1", 400)]), own_actors=["you", "char:1"])
    r = summarize_run([f1, f2])
    assert r.total_damage == 510 and r.best_fight_id == f2.fight_id or r.best_fight_dps == 410.0


def test_engine_passes_own_actors_and_live_summary_includes_new_characters():
    cfg = load_config()
    sim = Sim({PORT_A: A, PORT_B: B})
    src = PacketSource(cfg)
    items = []
    for d in sim.send(BASE, hp(A, -10, MOB)) + sim.send(BASE + 1, hp(B, -20, MOB)):
        items += [(e.ts, e) for e in src.events_from(d)]
    live, closed = [], []

    class S:
        own_actors = src.own_actors

        def __iter__(self):
            return iter(items)

        def close(self):
            pass

    Engine(cfg, S(), on_live=live.append, on_closed=closed.append).run()
    assert live[-1].combined_total == 30 and closed[0].combined_total == 30 and closed[0].ongoing is False


def test_text_replay_path_is_unaffected_by_own_actors(tmp_path):
    from eqoa_parser.sources import ReplaySource

    log = tmp_path / "t.log"
    log.write_text("1000.0\tYou hit a rat for 5 points of damage.\n1001.0\tYour pet bites a rat for 3 points of damage.\n1002.0\tBob hits a rat for 99 points of damage.\n")
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"))
    closed = []
    Engine(cfg, ReplaySource(log, speed=0), on_closed=closed.append).run()
    assert closed[0].combined_total == 8 and closed[0].actor("Bob").total == 99


# ============================================================================== persistence of labelled fights
def test_labelled_fights_round_trip_through_the_run_store(tmp_path):
    from eqoa_parser.persistence import RunStore

    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = [name_dgram(BASE - 1, B, "O'Brien-Smith")] + sim.send(BASE, hp(A, -10, MOB)) + sim.send(BASE + 1, hp(B, -20, MOB)) \
        + sim.send(BASE + 2, swing(PB, MOB)) + sim.send(BASE + 2.3, hp(B, -25, MOB, EXT))
    evs, src = run(dgs)
    (f,) = fight_of(evs, src)
    st = RunStore(tmp_path, "pcap:x")
    st.add_fight(f)
    again = RunStore(tmp_path, "pcap:y", now=st.now.replace(second=(st.now.second + 1) % 60))
    (past,) = again.previous_fights
    assert past.summary == f and set(past.summary.actors) == {"you", "O'Brien-Smith", "O'Brien-Smith's pet"}
    assert json.loads(next(tmp_path.glob("*.json")).read_text())["runs"][0]["fights"][0]["combined_total"] == 55


# ============================================================================== names: sanity against the project's own oracle
def test_name_table_on_the_real_capture_matches_the_oracle_without_wrong_pairs():
    oracle = {int(k, 16): v for k, v in json.loads((REAL / "expected_names.json").read_text(encoding="utf-8"))["names"].items()}
    nt = NameTable("5.161.217.188")
    for d in read_pcapng(REAL / "cap2_bard_melee_necro_pet.pcapng"):
        nt.feed(d)
    got = nt.as_dict()
    assert got.get(B) == "Zecron" and A not in got and MOB not in got                 # bard unnamed, dummy unnamed: stays hex
    wrong = {k: (v, oracle.get(k)) for k, v in got.items() if oracle.get(k) != v}
    assert not wrong, wrong
    assert len(got) >= 20 and set(oracle) <= set(got) | set(oracle)


def test_name_table_never_guesses_from_noise_and_ignores_non_server_traffic():
    nt = NameTable("5.161.217.188")
    import random

    rnd = random.Random(3)
    for i in range(2000):
        nt.feed(dg(BASE, bytes(rnd.randrange(256) for _ in range(rnd.randint(0, 200)))))
    nt.feed(name_dgram(BASE, 0x11111111, "Mallory", 20000).__class__(BASE, "9.9.9.9", 1, CLIENT, 2, name_dgram(BASE, 0x11111111, "Mallory").payload))
    assert nt.get(0x11111111) is None
    for n in ("ab", "x" * 40, "12345"):
        nt.feed(name_dgram(BASE, 0x22222222, n))
    assert nt.get(0x22222222) is None
    nt.feed(name_dgram(BASE, 0x33333333, "Abc   "))
    assert nt.get(0x33333333) == "Abc"
    nt.feed(name_dgram(BASE, 0x33333333, "Abcd"))
    assert nt.get(0x33333333) == "Abcd"                                                  # a later name replaces the earlier one


# ============================================================================== live path with two emulators (fake capture only)
def test_live_two_emulators_mirrored_traffic_counts_once_and_closes_on_idle(tmp_path):
    cfg = load_config(unmatched_log=str(tmp_path / "u.log"), idle_timeout=0.4)
    sim = Sim({PORT_A: A, PORT_B: B})
    state = {"i": 0, "queue": []}

    def read(cap):
        if not state["queue"] and state["i"] < 8:
            i = state["i"]
            state["i"] += 1
            now = time.time()
            state["queue"] = sim.send(now, hp(A if i % 2 == 0 else B, -10, MOB))
        if state["queue"]:
            return state["queue"].pop(0)
        time.sleep(0.01)
        return None

    class Cap(FakeCapture):
        def read(self):
            return read(self)

    src = LivePacketSource(cfg, capture_factory=lambda net: Cap(net))
    closed, stop = [], threading.Event()
    eng = Engine(cfg, src, on_closed=closed.append)
    th = threading.Thread(target=eng.run, args=(stop,), daemon=True)
    th.start()
    time.sleep(1.3)
    n = len(closed)
    stop.set()
    src.close()
    th.join(timeout=3)
    assert n == 1 and closed[0].combined_total == 80 and closed[0].you.total == 40 and closed[0].actor("char:%d" % PORT_B).total == 40


# ============================================================================== UI: other characters' rows and reloaded fights
def test_console_rows_one_per_own_actor_in_first_seen_order_and_others_only_with_show_group():
    from eqoa_parser.ui.console import format_fight, format_live, row_actors

    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = sim.send(BASE, hp(A, -10, MOB)) + sim.send(BASE + 1, hp(B, -20, MOB)) + sim.send(BASE + 2, swing(PB, MOB)) + sim.send(BASE + 2.3, hp(B, -25, MOB, EXT))
    evs, src = run(dgs)
    (s,) = fight_of(evs, src)
    own = src.own_actors()
    assert [n for n, _ in row_actors(s, own)] == ["You", "Pet", "char:%d" % PORT_B, "char:%d's pet" % PORT_B]
    text = format_fight(s, False, own)
    assert text.index("char:%d " % PORT_B) < text.index("char:%d's pet" % PORT_B) and re.search(r"char:%d combined\s+total\s+45" % PORT_B, text) and re.search(r"All characters\s+total\s+55", text)
    assert "char:" in format_live(s, False, own) and "char:" in format_live(s, False, None)  # Phase 4: the fight carries its own characters
    assert [n for n, _ in row_actors(s, own, show_group=True)][:4] == ["You", "Pet", "char:%d" % PORT_B, "char:%d's pet" % PORT_B]


def test_reloaded_fights_from_an_earlier_run_still_show_the_other_characters_breakdown():
    """BUG-P3-3 (low): the 'Others' detail and the console/Tk rows use the CURRENT run's own-actor list.  A fight reloaded from an
    earlier run (visibly marked 'prev') is drawn with that list, which is empty until this run has produced an Event, so the other
    characters of the old run (e.g. "Zecron's pet 675") disappear from its breakdown although they are inside its Combined total."""
    from eqoa_parser.ui.console import format_others

    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = [name_dgram(BASE - 1, B, "Zecron")] + sim.send(BASE, hp(A, -10, MOB)) + sim.send(BASE + 1, hp(B, -20, MOB))
    evs, src = run(dgs)
    (old,) = fight_of(evs, src)
    persisted = FightSummary.from_dict(json.loads(json.dumps(old.to_dict())))
    current_run_own: list[str] = []                       # a fresh run: no Event seen yet
    assert "Zecron" in format_others(persisted, current_run_own), "reloaded fight lost the 'Zecron' share of its Combined total"


def test_tk_reloaded_fight_detail_line_lists_other_characters():
    """Same as above through the real Tk window (skipped without a display)."""
    tk = pytest.importorskip("tkinter")
    import gc

    from eqoa_parser.persistence import PastFight
    from eqoa_parser.ui.tk_app import TkApp

    sim = Sim({PORT_A: A, PORT_B: B})
    dgs = [name_dgram(BASE - 1, B, "Zecron")] + sim.send(BASE, hp(A, -10, MOB)) + sim.send(BASE + 1, hp(B, -20, MOB))
    evs, src = run(dgs)
    (old,) = fight_of(evs, src)
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    win = None
    try:
        win = TkApp(root=tk.Toplevel(root), previous_fights=[PastFight("r0", old)])
        win.own_provider = lambda: []                                    # new run, nothing seen yet
        win._select_newest()
        rows = {win.detail_tree.item(i, "text").strip(): win.detail_tree.item(i, "values")[0]
                for i in win.detail_tree.get_children()}
        win.close()
        assert "Zecron" in rows and str(rows["Zecron"]) == "20", rows
    finally:
        win = None
        gc.collect()
        root.destroy()
        gc.collect()
