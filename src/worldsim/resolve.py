"""The resolver: what is true this turn (spec 3.1).

These pydantic shapes are the contract between the model and the store. The
model proposes; turn.py validates and commits. Nothing here writes to the store.

Created by __main__.py and called by turn.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field, model_validator

from .config import Config
from .llm import _BaseLLM
from .store import Store

# The player character always has this id, so code never has to guess it.
PLAYER_ID = "person:you"
SPAWN_PLACE_ID = "place:spawn-city"

Source = Literal["real-data", "world-knowledge", "invented"]
Visibility = Literal["obvious", "noticeable", "hidden"]

# Models write numbers as strings given any chance, which would land cash in the
# store as text. A text total can be superseded, and that is a hole straight
# through rule 4, so quantity keys are forced numeric here at the boundary.
QUANTITY_SUFFIXES = ("_usd", "_cents", "_count", "_qty")


def is_quantity(key: str) -> bool:
    return key.endswith(QUANTITY_SUFFIXES)


def as_number(key: str, value: str | float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        raise ValueError(
            f"{key!r} is a quantity, so its value must be a number, not {value!r}"
        ) from None


class NewEntity(BaseModel):
    id: str
    kind: str
    name: str
    location_id: str | None = None


class NewFact(BaseModel):
    entity_id: str
    key: str
    value: str | float
    source: Source
    visibility: Visibility

    @model_validator(mode="after")
    def quantities_must_be_numbers(self) -> NewFact:
        if is_quantity(self.key):
            self.value = as_number(self.key, self.value)
        return self


class Supersede(BaseModel):
    entity_id: str
    key: str
    value: str
    source: Source
    visibility: Visibility

    @model_validator(mode="after")
    def quantities_are_never_superseded(self) -> Supersede:
        if is_quantity(self.key):
            raise ValueError(
                f"{self.key!r} is a quantity; change it with a delta, never by "
                "writing a new total"
            )
        return self


class Delta(BaseModel):
    """A change to a quantity. The model never writes the new total."""

    entity_id: str
    key: str
    delta: float


class Move(BaseModel):
    entity_id: str
    location_id: str


class Learned(BaseModel):
    """What someone other than the player came to know, truly or otherwise."""

    mind_id: str
    entity_id: str
    key: str
    how: str
    belief: str | float | None = None


class Line(BaseModel):
    speaker_id: str
    gist: str
    true: bool = True


class Perceived(BaseModel):
    entity_id: str
    key: str


class Outcome(BaseModel):
    summary: str
    # Required on purpose. Models skip what they aren't asked for, and this
    # being absent is what let a turn narrate a trip to a bar that the store
    # never heard about. Omitting it is a schema error, so the retry fires.
    player_location: str
    # What the player themselves said or did audibly this turn, already decided
    # here so the narrator never has to invent words for them.
    you_said_aloud: str | None = None
    new_entities: list[NewEntity] = Field(default_factory=list)
    new_facts: list[NewFact] = Field(default_factory=list)
    superseded: list[Supersede] = Field(default_factory=list)
    deltas: list[Delta] = Field(default_factory=list)
    moves: list[Move] = Field(default_factory=list)
    learned: list[Learned] = Field(default_factory=list)
    dialogue: list[Line] = Field(default_factory=list)
    # What the player notices in this branch specifically. The scene's own
    # `perceived` covers what was true before the roll decided anything.
    perceived: list[Perceived] = Field(default_factory=list)
    ends_life: bool = False


class Roll(BaseModel):
    p_success: float = Field(ge=0.0, le=1.0)
    why: str
    on_success: Outcome
    on_failure: Outcome


class Scene(BaseModel):
    """The first of two objects the resolver emits.

    Everything the narrator needs and nothing else, so narration can start
    while the ledger is still being written. Field order matters: the model
    emits them in roughly this order, and the sooner the last one lands the
    sooner the player hears something.
    """

    intent: str
    duration_minutes: float = Field(ge=0)
    player_location: str
    # True when a roll decides the turn. Narration then waits for the ledger,
    # because which branch happened is not known until code rolls.
    uncertain: bool = False
    summary: str
    you_said_aloud: str | None = None
    new_entities: list[NewEntity] = Field(default_factory=list)
    new_facts: list[NewFact] = Field(default_factory=list)
    perceived: list[Perceived] = Field(default_factory=list)
    dialogue: list[Line] = Field(default_factory=list)
    scene_change: bool = False


class Ledger(BaseModel):
    """The second object: consequences the player does not perceive this turn.

    Committed after the narration has already gone out, because none of it
    changes what this turn sounds like.
    """

    superseded: list[Supersede] = Field(default_factory=list)
    deltas: list[Delta] = Field(default_factory=list)
    moves: list[Move] = Field(default_factory=list)
    learned: list[Learned] = Field(default_factory=list)
    ends_life: bool = False
    roll: Roll | None = None


class SpawnOutput(BaseModel):
    new_entities: list[NewEntity] = Field(default_factory=list)
    new_facts: list[NewFact] = Field(default_factory=list)
    # Where the character actually is. Without this they stand in the bare city
    # entity, which makes everyone in town read as present in the room.
    player_starts_in: str | None = None
    opening_situation: str


class Resolver:
    """Builds the resolver's context from the truth and asks for a decision."""

    def __init__(
        self, llm: _BaseLLM, store: Store, config: Config, prompts_dir: Path
    ) -> None:
        self.llm = llm
        self.store = store
        self.config = config
        self.prompts_dir = prompts_dir

    def _prompt(self, name: str) -> str:
        return (self.prompts_dir / name).read_text()

    def spawn(self) -> SpawnOutput:
        user = json.dumps(
            {
                "city": self.config.spawn_city,
                "local_time": self.store.now().isoformat(),
                "player_entity_id": PLAYER_ID,
                "city_entity_id": SPAWN_PLACE_ID,
            },
            indent=2,
        )
        return self.llm.json("resolver", self._prompt("spawn.md"), user, SpawnOutput)

    def resolve(
        self,
        player_said: str,
        recent: list[str],
        on_scene: Callable[[Scene], None],
    ) -> tuple[Scene, Ledger]:
        """Stream one call. `on_scene` fires the moment the scene is valid."""
        user = json.dumps(
            {
                "player_said": player_said,
                "world_time": self.store.now().isoformat(),
                "recent_turns": recent,
                "truth_here": self.truth_here(),
            },
            indent=2,
        )
        return self.llm.stream_pair(
            "resolver",
            self._prompt("resolver.md"),
            user,
            Scene,
            Ledger,
            on_scene,
            self.check_scene,
        )

    def check_scene(self, scene: Scene) -> None:
        """Checks the schema can't make, run before the scene is committed.

        Raising here spends the one retry (spec 3.3) with the reason attached,
        which is the only way the model learns it proposed an incoherent world.
        The ledger cannot be checked this way: by the time it arrives the scene
        is already committed and narrated, so its problems are reported as
        dropped writes instead.
        """
        will_exist = {e.id for e in scene.new_entities}
        where = scene.player_location
        if self.store.entity(where) is None and where not in will_exist:
            raise ValueError(
                f"player_location is {where!r}, which does not exist and is not "
                "created in this scene. Add it to new_entities, nested inside a "
                "place that does exist."
            )

    def check_deltas(self, ledger: Ledger, scene: Scene) -> list[str]:
        """Deltas that cannot be applied, for the dev view and the log."""
        settled = {
            (f.entity_id, f.key) for f in scene.new_facts if is_quantity(f.key)
        }
        problems = []
        for delta in ledger.deltas:
            if (delta.entity_id, delta.key) in settled:
                continue
            fact = self.store.live_fact(delta.entity_id, delta.key)
            if fact is None:
                problems.append(
                    f"delta {delta.entity_id}.{delta.key} by {delta.delta}: no such "
                    "quantity was ever settled"
                )
            elif not isinstance(fact.value, (int, float)):
                problems.append(
                    f"delta {delta.entity_id}.{delta.key}: that fact is text"
                )
        return problems

    def truth_here(self) -> dict:
        """The full truth at the player's location, hidden facts included.

        Only the resolver ever sees this. render.py builds a different, smaller
        context for the narrator (rule 5).
        """
        player = self.store.entity(PLAYER_ID)
        here = player.location_id if player else None
        present = self.store.contents(here) if here else []
        carried = self.store.contents(PLAYER_ID)

        def facts_of(entity_id: str) -> dict:
            return {
                fact.key: {"value": fact.value, "visibility": fact.visibility}
                for fact in self.store.live_facts(entity_id)
            }

        entities = {PLAYER_ID: facts_of(PLAYER_ID)}
        if here:
            entities[here] = facts_of(here)
        for entity in present + carried:
            entities[entity.id] = facts_of(entity.id)

        return {
            "you_are_in": here,
            "present": [
                {"id": e.id, "kind": e.kind, "name": e.name}
                for e in present
                if e.id != PLAYER_ID
            ],
            "you_carry": [{"id": e.id, "name": e.name} for e in carried],
            "facts": entities,
        }
