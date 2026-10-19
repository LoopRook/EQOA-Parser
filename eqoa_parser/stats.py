"""Statistics contract. Core module: no UI imports.

OWNER: main chat (dataclasses + serialization). SESSION B implements summarize_fight / summarize_run.
See docs/CONTRACTS.md section "stats".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from .events import ACTOR_PET, ACTOR_YOU, KIND_DAMAGE, KIND_DAMAGE_TAKEN
from .segmenter import Fight

# DPS uses max(duration, MIN_DPS_DURATION) so zero-duration (single-hit) fights never divide by zero.
MIN_DPS_DURATION = 1.0
TOP_ABILITIES = 3


def effective_duration(duration: float) -> float:
    return max(duration, MIN_DPS_DURATION)


@dataclass
class AbilityStats:
    name: str
    total: int = 0
    hits: int = 0
    max_hit: int = 0


@dataclass
class ActorStats:
    actor: str
    total: int = 0
    hits: int = 0
    max_hit: int = 0
    dps: float = 0.0
    abilities: dict[str, AbilityStats] = field(default_factory=dict)


@dataclass
class FightSummary:
    fight_id: int
    target: str | None          # primary target: most total KIND_DAMAGE received (ties: first hit)
    targets: list[str]          # every target of KIND_DAMAGE, in first-hit order
    start_ts: float
    end_ts: float
    duration: float             # end_ts - start_ts (may be 0)
    actors: dict[str, ActorStats]   # every actor with >=1 KIND_DAMAGE event (you, pet, group...)
    combined_total: int         # you + pet only
    combined_dps: float         # combined_total / effective_duration(duration)
    damage_taken: int           # sum of KIND_DAMAGE_TAKEN amounts
    top_abilities: list[dict[str, Any]]  # top 3 you+pet abilities by total: {"actor","ability","total"}
    ongoing: bool = False       # True for the live (not yet closed) fight
    own_actors: list[str] | None = None  # actor names that counted toward combined_* (None = you + pet rule)
    groups: list[dict[str, Any]] | None = None  # characters: [{"name","player","pet"}] as known at close (None = one "You")
    pet_names: dict[str, str] | None = None     # pet actor key -> pet entity name (PINE only; None = unknown)

    def actor(self, name: str) -> ActorStats:
        """Stats for `name`, or an all-zero ActorStats if the actor dealt no damage."""
        return self.actors.get(name) or ActorStats(actor=name)

    @property
    def you(self) -> ActorStats:
        return self.actor(ACTOR_YOU)

    @property
    def pet(self) -> ActorStats:
        return self.actor(ACTOR_PET)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["pet_names"] is None:
            del d["pet_names"]  # additive field: files written without PINE stay as they were
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FightSummary":
        d = dict(d)
        actors = {}
        for name, a in d.pop("actors").items():
            a = dict(a)
            abilities = {k: AbilityStats(**v) for k, v in a.pop("abilities").items()}
            actors[name] = ActorStats(abilities=abilities, **a)
        return cls(actors=actors, **d)


DEFAULT_GROUP_NAME = "You"
ALL_CHARACTERS = "All characters"


def pet_name(name: str, npc: str | None = None) -> str:
    """`Name's pet`; where no character name is known (the default group "You") the old wording `Pet`.
    With the pet entity's name (PINE): `Name's pet (Djinni Vizier)`."""
    base = "Pet" if name == DEFAULT_GROUP_NAME else f"{name}'s pet"
    return f"{base} ({npc})" if npc else base


@dataclass
class GroupRow:
    name: str                   # display name of the character
    player: ActorStats
    pet: ActorStats
    combined_total: int
    combined_dps: float         # combined_total / effective_duration(fight)
    key: str = ""               # stable identity of the character (its player actor key); the name may change
    pet_npc: str | None = None  # the pet entity's name when known (PINE)

    @property
    def pet_name(self) -> str:
        return pet_name(self.name, self.pet_npc)


def _legacy_groups(summary: FightSummary) -> list[dict[str, Any]]:
    """Groups for a fight stored without them: "You", plus the characters named by its ``own_actors`` (Phase 3
    files: ``Zecron`` / ``Zecron's pet`` keys, first-seen order), so older multi-character fights keep their rows."""
    groups: dict[str, dict[str, Any]] = {
        DEFAULT_GROUP_NAME: {"name": DEFAULT_GROUP_NAME, "player": ACTOR_YOU, "pet": ACTOR_PET}}
    for key in summary.own_actors or ():
        if key in (ACTOR_YOU, ACTOR_PET):
            continue
        owner = key[:-len("'s pet")] if key.endswith("'s pet") else key
        g = groups.setdefault(owner, {"name": owner, "player": owner, "pet": f"{owner}'s pet"})
        if key == owner:
            g["player"] = key
    return list(groups.values())


def group_rows(summary: FightSummary) -> list[GroupRow]:
    """One row per character of the fight. No stored groups (old files, text path) = one group "You" with the
    actors ``you`` / ``pet``. Combined = the group's player + pet, over effective_duration(duration)."""
    groups = summary.groups or _legacy_groups(summary)
    eff = effective_duration(summary.duration)
    rows = []
    for g in groups:
        p, q = summary.actor(g["player"]), summary.actor(g["pet"])
        total = p.total + q.total
        rows.append(GroupRow(g["name"], p, q, total, total / eff, g.get("key") or g["player"],
                             (summary.pet_names or {}).get(g["pet"])))
    return rows


@dataclass
class CharacterRun:
    name: str                   # a character, or ALL_CHARACTERS
    total: int = 0
    dps: float = 0.0            # total / sum(effective_duration) of the fights where it dealt damage
    best_fight_id: int | None = None
    best_dps: float = 0.0


def character_runs(summaries: Sequence[FightSummary]) -> list[CharacterRun]:
    """Per-character run totals (characters matched across fights by stable key, shown under the latest name, first-seen order), then one
    ALL_CHARACTERS line when there are 2+ characters. Best fight = highest combined DPS of that character."""
    runs: dict[str, CharacterRun] = {}
    secs: dict[str, float] = {}
    for f in summaries:
        for r in group_rows(f):
            c = runs.setdefault(r.key, CharacterRun(r.name))
            if r.name != DEFAULT_GROUP_NAME or c.name == DEFAULT_GROUP_NAME:
                c.name = r.name  # latest known name (a later "You" never replaces a real name)
            if r.combined_total <= 0:
                continue
            c.total += r.combined_total
            secs[r.key] = secs.get(r.key, 0.0) + effective_duration(f.duration)
            if c.best_fight_id is None or r.combined_dps > c.best_dps:
                c.best_fight_id, c.best_dps = f.fight_id, r.combined_dps
    out = list(runs.values())
    for k, c in runs.items():
        c.dps = c.total / secs[k] if secs.get(k) else 0.0
    if len(out) >= 2:
        total = sum(c.total for c in out)
        active = [f for f in summaries if any(r.combined_total > 0 for r in group_rows(f))]
        denom = sum(effective_duration(f.duration) for f in active)
        all_ = CharacterRun(ALL_CHARACTERS, total, total / denom if denom else 0.0)
        # all-characters best = the fight where the characters together did the most DPS
        for f in active:
            comb = sum(r.combined_total for r in group_rows(f)) / effective_duration(f.duration)
            if all_.best_fight_id is None or comb > all_.best_dps:
                all_.best_fight_id, all_.best_dps = f.fight_id, comb
        out.append(all_)
    return out


@dataclass
class RunSummary:
    fight_count: int
    total_damage: int           # sum of combined_total over fights
    average_dps: float          # total_damage / sum(effective_duration(f.duration)); 0.0 with no fights
    best_fight_id: int | None   # fight with highest combined_dps (ties: earliest)
    best_fight_target: str | None
    best_fight_dps: float
    characters: list[CharacterRun] = field(default_factory=list)  # per character, then ALL_CHARACTERS (2+)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize_fight(
    fight: Fight, *, ongoing: bool = False, own_actors: Sequence[str] | None = None,
    groups: Sequence[dict[str, Any]] | None = None, pet_names: dict[str, str] | None = None,
) -> FightSummary:
    """`own_actors`: actor names that count toward combined_total/combined_dps/top_abilities (several followed
    characters and their pets). None = the original rule: "you" + "pet"."""
    own = (ACTOR_YOU, ACTOR_PET) if own_actors is None else tuple(own_actors)
    duration = fight.duration
    eff = effective_duration(duration)
    actors: dict[str, ActorStats] = {}
    received: dict[str, int] = {}  # insertion order = first-hit order
    damage_taken = 0
    for e in fight.events:
        if e.kind == KIND_DAMAGE_TAKEN:
            damage_taken += e.amount
            continue
        if e.kind != KIND_DAMAGE:
            continue
        a = actors.get(e.actor)
        if a is None:
            a = actors[e.actor] = ActorStats(actor=e.actor)
        a.total += e.amount
        a.hits += 1
        a.max_hit = max(a.max_hit, e.amount)
        key = e.ability or "(unknown)"
        ab = a.abilities.get(key)
        if ab is None:
            ab = a.abilities[key] = AbilityStats(name=key)
        ab.total += e.amount
        ab.hits += 1
        ab.max_hit = max(ab.max_hit, e.amount)
        tgt = e.target if e.target is not None else "(unknown)"
        received[tgt] = received.get(tgt, 0) + e.amount
    for a in actors.values():
        a.dps = a.total / eff
    primary = max(received, key=received.__getitem__) if received else None  # max() keeps the first on ties
    combined_total = sum(actors[n].total for n in own if n in actors)
    rows = [
        {"actor": n, "ability": ab.name, "total": ab.total}
        for n in own if n in actors
        for ab in actors[n].abilities.values()
    ]
    rank = {n: i for i, n in enumerate(own)}
    rows.sort(key=lambda r: (-r["total"], rank[r["actor"]], r["ability"]))
    return FightSummary(
        fight_id=fight.fight_id, target=primary, targets=list(received),
        start_ts=fight.start_ts, end_ts=fight.end_ts, duration=duration, actors=actors,
        combined_total=combined_total, combined_dps=combined_total / eff, damage_taken=damage_taken,
        top_abilities=rows[:TOP_ABILITIES], ongoing=ongoing,
        own_actors=None if own_actors is None else list(own_actors),
        groups=None if groups is None else [dict(g) for g in groups],
        pet_names=dict(pet_names) if pet_names else None,
    )


def summarize_run(fights: Sequence[FightSummary]) -> RunSummary:
    if not fights:
        return RunSummary(0, 0, 0.0, None, None, 0.0)
    total = sum(f.combined_total for f in fights)
    denom = sum(effective_duration(f.duration) for f in fights)
    best = fights[0]
    for f in fights[1:]:
        if f.combined_dps > best.combined_dps:
            best = f
    return RunSummary(
        fight_count=len(fights), total_damage=total, average_dps=total / denom,
        best_fight_id=best.fight_id, best_fight_target=best.target, best_fight_dps=best.combined_dps,
        characters=character_runs(fights),
    )
