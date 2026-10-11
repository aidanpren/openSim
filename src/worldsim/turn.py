"""One turn: resolve, roll, commit, render (spec 3).

Commit order matters. Entities exist before facts land on them, facts exist
before anyone can perceive them, and the clock moves by the resolver's duration
and never otherwise. A stored fact beats anything the model proposes, so a
conflicting proposal is dropped and reported, not committed and not fatal.

Created by __main__.py, which owns the loop and the commands.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

from .render import BIOGRAPHY, Narrator
from .resolve import (
    PLAYER_ID,
    SPAWN_PLACE_ID,
    Ledger,
    Outcome,
    Resolver,
    Scene,
)
from .store import FactConflict, Life, Store, StoreError


@dataclass
class RollRecord:
    """Logged with its odds, both outcomes, and the result (spec 3.2)."""

    p_success: float
    why: str
    rolled: float
    succeeded: bool
    on_success: str
    on_failure: str

    def as_json(self) -> str:
        return json.dumps(self.__dict__)


@dataclass
class TurnResult:
    player_said: str
    intent: str
    narration: str
    duration_minutes: float
    roll: RollRecord | None
    summary: str
    ends_life: bool
    noticed_fact_ids: list[int] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


class Turn:
    def __init__(
        self,
        store: Store,
        resolver: Resolver,
        narrator: Narrator,
        rng: random.Random,
    ) -> None:
        self.store = store
        self.resolver = resolver
        self.narrator = narrator
        self.rng = rng

    def play(self, player_said: str, life: Life) -> TurnResult:
        """Resolve, commit, render — with the render moved inside the stream.

        The scene commits and narrates as soon as it has arrived, while the
        ledger is still being generated. Everything the narrator sees is in the
        store before it speaks, so rule 1 holds; the writes that land afterwards
        cannot change what this turn sounds like.
        """
        recent = [
            f"{row['player_said']} -> {row['narration']}"
            for row in self.store.recent_turns(life.id, limit=6)
        ]
        dropped: list[str] = []
        sofar: dict = {"noticed": [], "narration": None}

        def on_scene(scene: Scene) -> None:
            # The clock moves only by the resolver's in-world duration (rule 7).
            self.store.advance(scene.duration_minutes)
            cause = f"turn: {scene.intent}"
            self._entities(scene.new_entities, dropped)
            self._facts(scene.new_facts, cause, dropped)
            self._place_player(scene.player_location, dropped)
            sofar["noticed"] = self._perceive(scene.perceived, dropped)
            if not scene.uncertain:
                sofar["narration"] = self.narrator.narrate(
                    player_said, scene.dialogue, sofar["noticed"], scene.you_said_aloud
                )

        scene, ledger = self.resolver.resolve(player_said, recent, on_scene)
        cause = f"turn: {scene.intent}"

        roll_record = None
        summary = scene.summary
        ends_life = ledger.ends_life
        dialogue = scene.dialogue
        said_aloud = scene.you_said_aloud

        if ledger.roll is not None:
            # Both outcomes exist in full before code rolls (rule 6).
            rolled = self.rng.random()
            succeeded = rolled < ledger.roll.p_success
            branch = ledger.roll.on_success if succeeded else ledger.roll.on_failure
            roll_record = RollRecord(
                p_success=ledger.roll.p_success,
                why=ledger.roll.why,
                rolled=rolled,
                succeeded=succeeded,
                on_success=ledger.roll.on_success.summary,
                on_failure=ledger.roll.on_failure.summary,
            )
            self._apply_outcome(branch, cause, dropped)
            self._place_player(branch.player_location, dropped)
            sofar["noticed"] += self._perceive(branch.perceived, dropped)
            summary, ends_life = branch.summary, branch.ends_life
            dialogue, said_aloud = branch.dialogue, branch.you_said_aloud

        self._apply_ledger(ledger, scene, cause, dropped)

        # Roll turns could not narrate from the scene alone, so they narrate here.
        narration = sofar["narration"]
        if narration is None:
            narration = self.narrator.narrate(
                player_said, dialogue, sofar["noticed"], said_aloud
            )
        self.store.log_turn(
            life_id=life.id,
            player_said=player_said,
            intent=scene.intent,
            roll_json=roll_record.as_json() if roll_record else None,
            narration=narration,
        )
        return TurnResult(
            player_said=player_said,
            intent=scene.intent,
            narration=narration,
            duration_minutes=scene.duration_minutes,
            roll=roll_record,
            summary=summary,
            ends_life=ends_life,
            noticed_fact_ids=sofar["noticed"],
            dropped=dropped,
        )

    # ---- committing the pieces ----

    def _place_player(self, location_id: str, dropped: list[str]) -> None:
        """The store decides where the player is, not the narration."""
        if self.store.entity(location_id) is None:
            dropped.append(f"player_location {location_id}: no such place, left in place")
        elif self.store.entity(PLAYER_ID).location_id != location_id:
            self.store.move(PLAYER_ID, location_id)

    def _apply_ledger(self, ledger, scene, cause: str, dropped: list[str]) -> None:
        """Consequences the player did not perceive, committed after narration."""
        for problem in self.resolver.check_deltas(ledger, scene):
            dropped.append(problem)
        self._apply_writes(
            ledger.superseded, ledger.deltas, ledger.moves, ledger.learned,
            cause, dropped,
        )

    def _entities(self, new_entities, dropped: list[str]) -> None:
        """Add proposed entities, parents before children.

        Models list a room before the building that holds it often enough that
        a single pass drops half a nested place. Keep passing while progress is
        being made, then drop whatever still has nowhere to live.
        """
        pending = [e for e in new_entities if self.store.entity(e.id) is None]
        while pending:
            waiting = []
            for proposed in pending:
                if (
                    proposed.location_id is not None
                    and self.store.entity(proposed.location_id) is None
                ):
                    waiting.append(proposed)
                    continue
                try:
                    self.store.add_entity(
                        proposed.id, proposed.kind, proposed.name, proposed.location_id
                    )
                except StoreError as exc:
                    dropped.append(f"entity {proposed.id}: {exc}")
            if len(waiting) == len(pending):
                for proposed in waiting:
                    dropped.append(
                        f"entity {proposed.id}: no such location "
                        f"{proposed.location_id!r}"
                    )
                return
            pending = waiting

    def _facts(self, new_facts, cause: str, dropped: list[str]) -> None:
        for proposed in new_facts:
            try:
                self.store.add_fact(
                    proposed.entity_id,
                    proposed.key,
                    proposed.value,
                    proposed.source,
                    proposed.visibility,
                    cause,
                )
            except FactConflict as exc:
                # Spec 2.2: a stored fact beats model knowledge. Keep the old one.
                dropped.append(f"fact {proposed.entity_id}.{proposed.key}: {exc}")
            except StoreError as exc:
                dropped.append(f"fact {proposed.entity_id}.{proposed.key}: {exc}")

    def _apply_outcome(self, outcome: Outcome, cause: str, dropped: list[str]) -> None:
        """A rolled branch, which carries its own facts as well as its writes."""
        self._entities(outcome.new_entities, dropped)
        self._facts(outcome.new_facts, cause, dropped)
        self._apply_writes(
            outcome.superseded, outcome.deltas, outcome.moves, outcome.learned,
            cause, dropped,
        )

    def _apply_writes(
        self, superseded, deltas, moves, learned, cause: str, dropped: list[str]
    ) -> None:
        for change in superseded:
            try:
                self.store.supersede(
                    change.entity_id,
                    change.key,
                    change.value,
                    change.source,
                    change.visibility,
                    cause,
                )
            except StoreError as exc:
                dropped.append(f"supersede {change.entity_id}.{change.key}: {exc}")

        for delta in deltas:
            try:
                self.store.apply_delta(
                    delta.entity_id, delta.key, delta.delta, cause
                )
            except StoreError as exc:
                dropped.append(f"delta {delta.entity_id}.{delta.key}: {exc}")

        for move in moves:
            try:
                self.store.move(move.entity_id, move.location_id)
            except StoreError as exc:
                dropped.append(f"move {move.entity_id}: {exc}")

        for entry in learned:
            fact = self.store.live_fact(entry.entity_id, entry.key)
            if fact is None:
                dropped.append(
                    f"learned {entry.mind_id} of {entry.entity_id}.{entry.key}: no such fact"
                )
                continue
            try:
                self.store.learn(entry.mind_id, fact.id, entry.how, entry.belief)
            except StoreError as exc:
                dropped.append(f"learned {entry.mind_id}: {exc}")

    def _perceive(self, perceived, dropped: list[str]) -> list[int]:
        """Put what the player noticed into their knowledge (spec 2.4)."""
        noticed = []
        for item in perceived:
            fact = self.store.live_fact(item.entity_id, item.key)
            if fact is None:
                dropped.append(f"perceived {item.entity_id}.{item.key}: no such fact")
                continue
            self.store.learn(PLAYER_ID, fact.id, "noticed it")
            noticed.append(fact.id)
        return noticed


def begin_life(
    store: Store, resolver: Resolver, narrator: Narrator, spawn_city: str
) -> tuple[Life, str]:
    """Spawn a character and narrate the opening (build step 4).

    Code creates the city and the player shell so the model cannot get their
    ids wrong, then the resolver fills in the person and their situation.
    """
    # Idempotent: a spawn that failed on the model call leaves these two behind
    # with no life started, and without this the world could never be opened
    # again.
    if store.entity(SPAWN_PLACE_ID) is None:
        store.add_entity(SPAWN_PLACE_ID, "place", spawn_city)
    if store.entity(PLAYER_ID) is None:
        store.add_entity(PLAYER_ID, "person", "you", location_id=SPAWN_PLACE_ID)

    spawned = resolver.spawn()
    dropped: list[str] = []
    turn = Turn(store, resolver, narrator, random.Random())
    turn._entities(spawned.new_entities, dropped)
    turn._facts(spawned.new_facts, "spawn", dropped)

    # Stand them somewhere specific, so the whole town isn't in the room.
    if spawned.player_starts_in and store.entity(spawned.player_starts_in):
        store.move(PLAYER_ID, spawned.player_starts_in)

    # A person knows themselves, and notices what is obvious where they stand.
    noticed = []
    for fact in store.live_facts(PLAYER_ID):
        store.learn(PLAYER_ID, fact.id, "you have always known this")
        noticed.append(fact.id)
    player = store.entity(PLAYER_ID)
    if player.location_id:
        for fact in store.live_facts(player.location_id):
            if fact.visibility == "obvious":
                store.learn(PLAYER_ID, fact.id, "you can see it")
                noticed.append(fact.id)

    life = store.start_life(PLAYER_ID)
    narration = narrator.narrate(
        "", [], noticed, prompt_name="opening.md", omit_keys=BIOGRAPHY
    )
    store.log_turn(life.id, "", "spawn", None, narration)
    return life, narration
