"""The resolver: what is true this turn (spec 3.1).

These pydantic shapes are the contract between the model and the store. The
model proposes; turn.py validates and commits. Nothing here writes to the store.

Created by __main__.py and called by turn.py.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field, model_validator

from .budget import as_prompt, estimate_tokens, take_within
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


def as_text(value: str | float) -> str:
    """Render a value as text without pydantic's float tail: 29, not 29.0."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


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


# Facts are the bulk of what the player waits for, so they have a compact wire
# form: ["person:gary", "beard", "graying", "i", "o"] instead of five quoted
# keys. Both forms are accepted — a format slip would cost a whole retry, which
# is far more expensive than the tokens saved.
SOURCE_LETTERS = {"r": "real-data", "w": "world-knowledge", "i": "invented"}
VISIBILITY_LETTERS = {"o": "obvious", "n": "noticeable", "h": "hidden"}


def _expand(data, fields: tuple[str, ...], codes: dict):
    """Turn a positional list into the keyword form, leaving dicts alone."""
    if not isinstance(data, (list, tuple)):
        return data
    out = {}
    for index, name in enumerate(fields):
        if index >= len(data):
            break
        value = data[index]
        if name in codes and isinstance(value, str):
            value = codes[name].get(value.lower(), value)
        out[name] = value
    return out


class NewEntity(BaseModel):
    id: str
    kind: str
    name: str
    location_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_compact(cls, data):
        return _expand(data, ("id", "kind", "name", "location_id"), {})


class NewFact(BaseModel):
    entity_id: str
    key: str
    value: str | float
    source: Source = "invented"
    visibility: Visibility = "obvious"

    @model_validator(mode="before")
    @classmethod
    def accept_compact(cls, data):
        return _expand(
            data,
            ("entity_id", "key", "value", "source", "visibility"),
            {"source": SOURCE_LETTERS, "visibility": VISIBILITY_LETTERS},
        )

    @model_validator(mode="after")
    def quantities_must_be_numbers(self) -> NewFact:
        if is_quantity(self.key):
            self.value = as_number(self.key, self.value)
        return self


class Supersede(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def accept_compact(cls, data):
        return _expand(
            data,
            ("entity_id", "key", "value", "source", "visibility"),
            {"source": SOURCE_LETTERS, "visibility": VISIBILITY_LETTERS},
        )

    entity_id: str
    key: str
    # Models send a bare number here for things like an age or a house number.
    # A live turn was lost to that, so accept it and render it as text; a real
    # quantity is refused below either way.
    value: str | float
    source: Source
    visibility: Visibility

    @model_validator(mode="after")
    def quantities_are_never_superseded(self) -> Supersede:
        if is_quantity(self.key):
            raise ValueError(
                f"{self.key!r} is a quantity; change it with a delta, never by "
                "writing a new total"
            )
        self.value = as_text(self.value)
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

    @model_validator(mode="before")
    @classmethod
    def accept_compact(cls, data):
        return _expand(data, ("entity_id", "key"), {})


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


class Opening(BaseModel):
    """The first of three objects, and the only one the player waits on.

    Where the character ends up, how long it took, and the two or three things
    they register first. Nothing else — every token in here is a second of
    silence. Measured across six streamed calls, the first object was 68-83% of
    the whole resolver call, so this is the one piece of output that decides
    time-to-first-word.

    Field order matters: the model emits them in roughly this order, and the
    sooner the last one lands the sooner the player hears something.
    """

    intent: str
    duration_minutes: float = Field(ge=0)
    player_location: str
    # True when a roll decides the turn. The opening then holds only what is
    # true either way, and the outcome is narrated once code has rolled.
    uncertain: bool = False
    new_entities: list[NewEntity] = Field(default_factory=list)
    new_facts: list[NewFact] = Field(default_factory=list)
    perceived: list[Perceived] = Field(default_factory=list)


class Scene(BaseModel):
    """The second object: the rest of the truth, and who said what.

    Committed and narrated as a second beat, while the ledger is still being
    written. Anything in here that the character perceives reaches them a
    moment after the opening did, which is also how a room actually lands —
    the place first, then the person in it.
    """

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


# How comfortable a life starts, and how often. Code draws this rather than the
# model, for two reasons. Rule 6: the dice belong to code. And a model asked for
# "a person with at least one source of friction" writes the same broke, lonely,
# exhausted line cook every time — which is not what friction means and is not
# what most lives look like. The weights are the whole point: the ordinary
# middle is the standard, and both extremes are edge cases.
STATIONS: tuple[tuple[int, str], ...] = (
    (4, "barely holding on: no savings, somewhere precarious to sleep, and a "
        "week that could go wrong in a single move"),
    (28, "working with no cushion: this month's bills are paid and nothing "
         "beyond them"),
    (40, "ordinary and reasonably settled: the money works out, which leaves "
         "the room taken up by everything money does not fix"),
    (22, "comfortable: property, savings, and choices most people do not have"),
    (6, "wealthy: money is not a question this person has to ask, which is its "
        "own kind of trap"),
)


def draw_station(rng: random.Random) -> str:
    """Pick a station in life, weighted. Called by turn.begin_life."""
    return rng.choices(
        [description for _, description in STATIONS],
        weights=[weight for weight, _ in STATIONS],
    )[0]


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

    def spawn(self, station: str) -> SpawnOutput:
        """Build a life. `station` is drawn by code, not chosen by the model."""
        user = as_prompt(
            {
                "city": self.config.spawn_city,
                "local_time": self.store.now().isoformat(),
                "player_entity_id": PLAYER_ID,
                "city_entity_id": SPAWN_PLACE_ID,
                "station_in_life": station,
            }
        )
        return self.llm.json("resolver", self._prompt("spawn.md"), user, SpawnOutput)

    def resolve(
        self,
        player_said: str,
        recent: list[str],
        on_object: Callable[[int, object], None],
    ) -> tuple[Opening, Scene, Ledger]:
        """Stream one call. `on_object` fires as each of the three is valid."""
        # Spec 5.2's priority order: the instructions have the first claim on
        # the budget, then what the player just said, then the facts where they
        # are standing, then the scene so far. So the oldest turn is what goes
        # when something has to.
        system = self._prompt("resolver.md")
        budget = self.config.token_budgets["resolver"] - estimate_tokens(system)
        payload: dict = {
            "player_said": player_said,
            "world_time": self.store.now().isoformat(),
            "recent_turns": [],
            "truth_here": {},
        }
        room = budget - estimate_tokens(payload)
        payload["truth_here"] = self.truth_here(budget_tokens=max(room, 0))
        room = budget - estimate_tokens(payload)
        payload["recent_turns"] = list(
            reversed(take_within(list(reversed(recent)), max(room, 0)))
        )
        # And then make it certain rather than estimated, oldest turn first.
        while estimate_tokens(payload) > budget and payload["recent_turns"]:
            payload["recent_turns"].pop(0)
        user = as_prompt(payload)
        opening, scene, ledger = self.llm.stream_objects(
            "resolver",
            system,
            user,
            (Opening, Scene, Ledger),
            on_object,
            (self.check_opening, None, None),
        )
        return opening, scene, ledger

    def check_opening(self, opening: Opening) -> None:
        """Checks the schema can't make, run before anything is committed.

        Raising here spends the one retry (spec 3.3) with the reason attached,
        which is the only way the model learns it proposed an incoherent world.
        Only the opening can be checked this way: by the time the objects after
        it arrive it is already committed and narrated, so their problems are
        reported as dropped writes instead.
        """
        will_exist = {e.id for e in opening.new_entities}
        where = opening.player_location
        if self.store.entity(where) is None and where not in will_exist:
            raise ValueError(
                f"player_location is {where!r}, which does not exist and is not "
                "created in this opening. Add it to new_entities, nested inside "
                "a place that does exist."
            )

    def check_deltas(self, ledger: Ledger, *settled_in: object) -> list[str]:
        """Deltas that cannot be applied, for the dev view and the log."""
        settled = {
            (f.entity_id, f.key)
            for obj in settled_in
            for f in getattr(obj, "new_facts", ())
            if is_quantity(f.key)
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

    def truth_here(self, budget_tokens: int | None = None) -> dict:
        """The truth at the player's location, hidden facts included.

        Only the resolver ever sees this. render.py builds a different, smaller
        context for the narrator (rule 5).

        It is trimmed to a budget (spec 5.2) because it had no upper bound: a
        player standing in the bare city entity put every place and person in
        town in the room, each with all of its facts, and a place the player
        keeps returning to accumulates facts forever. Order is the priority
        order — the character, the room, what they carry, then whoever else is
        here, people before scenery — so the last thing dropped is the thing
        least likely to matter.
        """
        player = self.store.entity(PLAYER_ID)
        here = player.location_id if player else None
        present = [e for e in (self.store.contents(here) if here else []) if e.id != PLAYER_ID]
        carried = self.store.contents(PLAYER_ID)
        present.sort(key=lambda e: (e.kind != "person", e.id))

        def facts_of(entity_id: str) -> dict:
            return {
                fact.key: {"value": fact.value, "visibility": fact.visibility}
                for fact in self.store.live_facts(entity_id)
            }

        frame = {
            "you_are_in": here,
            "present": [{"id": e.id, "kind": e.kind, "name": e.name} for e in present],
            "you_carry": [{"id": e.id, "name": e.name} for e in carried],
        }
        if budget_tokens is None:
            budget_tokens = self.config.token_budgets["resolver"]

        ordered = [PLAYER_ID] + ([here] if here else [])
        ordered += [e.id for e in carried] + [e.id for e in present]
        seen: set[str] = set()
        slices = []
        for entity_id in ordered:
            if entity_id in seen:
                continue
            seen.add(entity_id)
            slices.append((entity_id, facts_of(entity_id)))

        room = budget_tokens - estimate_tokens(frame)
        kept = take_within(slices, max(room, 0))
        frame["facts"] = dict(kept)
        # Exactly as in render.context: the estimate above ignores the JSON
        # overhead of the keys themselves, and spec 5.2 is a hard bound.
        while estimate_tokens(frame) > budget_tokens and len(kept) > 1:
            kept.pop()
            frame["facts"] = dict(kept)
        return frame
