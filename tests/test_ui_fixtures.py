"""Hand-built FightSummary objects shared by C's tests (no tests here)."""
from __future__ import annotations

from eqoa_parser.stats import AbilityStats, ActorStats, FightSummary, RunSummary


def actor(name, total, hits, max_hit, dps, abilities=None):
    ab = {n: AbilityStats(n, t, h, m) for n, (t, h, m) in (abilities or {}).items()}
    return ActorStats(name, total, hits, max_hit, dps, ab)


def make_fight(fid=1, target="a rat", you=50, pet=20, bob=0, start=100.0, duration=5.0, ongoing=False):
    actors = {"you": actor("you", you, 2, 30, you / max(duration, 1.0), {"melee": (you, 2, 30)})}
    if pet:
        actors["pet"] = actor("pet", pet, 1, pet, pet / max(duration, 1.0), {"bite": (pet, 1, pet)})
    if bob:
        actors["Bob"] = actor("Bob", bob, 1, bob, bob / max(duration, 1.0))
    combined = you + pet
    top = [{"actor": "you", "ability": "melee", "total": you}]
    if pet:
        top.append({"actor": "pet", "ability": "bite", "total": pet})
    return FightSummary(
        fight_id=fid, target=target, targets=[target] if target else [], start_ts=start, end_ts=start + duration,
        duration=duration, actors=actors, combined_total=combined,
        combined_dps=combined / max(duration, 1.0), damage_taken=3, top_abilities=top, ongoing=ongoing,
    )


def make_run(fights):
    best = max(fights, key=lambda f: f.combined_dps) if fights else None
    total = sum(f.combined_total for f in fights)
    dur = sum(max(f.duration, 1.0) for f in fights)
    return RunSummary(
        fight_count=len(fights), total_damage=total, average_dps=(total / dur if dur else 0.0),
        best_fight_id=best.fight_id if best else None, best_fight_target=best.target if best else None,
        best_fight_dps=best.combined_dps if best else 0.0,
    )
