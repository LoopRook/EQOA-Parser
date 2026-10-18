"""Event contract. Core module: no UI imports.

OWNER: main chat. Do not change field names/order without updating docs/CONTRACTS.md.
"""
from __future__ import annotations

from dataclasses import dataclass

# Canonical actor names. Any other string is a named actor (group member, NPC).
ACTOR_YOU = "you"
ACTOR_PET = "pet"
MVP_ACTORS = (ACTOR_YOU, ACTOR_PET)

# Event kinds.
KIND_DAMAGE = "damage"              # friendly actor (you/pet/group member) dealt `amount` to `target`
KIND_DAMAGE_TAKEN = "damage_taken"  # hostile actor dealt `amount` to a friendly `target`
KIND_MISS = "miss"                  # actor attacked target and missed; amount is 0
KIND_DEATH = "death"                # target died; actor is the killer if known; amount is 0
KIND_HEAL = "heal"                  # actor restored `amount` HP to target (never starts or extends a fight)
KNOWN_KINDS = frozenset({KIND_DAMAGE, KIND_DAMAGE_TAKEN, KIND_MISS, KIND_DEATH, KIND_HEAL})

# Only these kinds start or extend a fight (segmenter) -- "damage dealt or received".
ACTIVITY_KINDS = frozenset({KIND_DAMAGE, KIND_DAMAGE_TAKEN})


@dataclass(frozen=True, slots=True)
class Event:
    ts: float              # seconds (epoch for live; whatever the replay file says for replay)
    actor: str             # "you", "pet", or any name -- free string so group members need no schema change
    target: str | None     # who was hit; "you"/"pet" when the player/pet is the target
    amount: int            # damage amount; 0 for miss/death
    ability: str | None    # ability/spell name, "melee" for plain melee, None if unknown
    kind: str              # one of KNOWN_KINDS
    raw: str               # the original text line, untouched
