"""The resolver: what is true this turn (spec 3.1).

These pydantic shapes are the contract between the model and the store. The
model proposes; turn.py validates and commits. Nothing here writes to the store.

Created by __main__.py and called by turn.py.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field, model_validator

from .budget import SEAM, as_prompt, estimate_tokens, take_within
from .config import Config, Corrections
from .llm import RETRY_ROOM, _BaseLLM
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


# Spec 2.1's split, decided by one question:
#
#     would this still be true tomorrow if nobody did anything about it?
#
# Yes, and it is a fact: a broken wrist, a debt, a landlord's decision, the
# bartender's name. All of those survive the night on their own, so rule 3
# applies and a change is a supersession citing its cause.
#
# No, and it is transient state: posture, where in the room, what is in hand,
# out of breath. None of those outlive the moment, so there is nothing for
# history to keep and the only sensible response to a new value is to take it.
# A live playtest settled `posture = "sitting on a bench"` as a fact; every
# correction after it was refused as a duplicate, and the character was still
# sitting on that bench at home in their kitchen.
#
# The hard cases fall out of the question. "Drunk" is state — sober by
# tomorrow. "Alcoholic" is a fact. "Bleeding" is state, "a knife wound" is a
# fact. "Angry" is state, "hates you" is a fact.
TRANSIENT_KEYS = frozenset(
    {"posture", "position", "stance", "facing", "activity", "action",
     "current_action", "currently", "doing", "busy_with", "holding",
     "in_hand", "carrying_now", "phone_action", "expression", "mood",
     "frustration", "hunger_level", "thirst", "out_of_breath", "state",
     "vantage", "attention", "looking_at", "seated_at", "standing_on"}
)

# Transient state that belongs to the place it was set in. You cannot still be
# sitting on a bench you walked away from, so these are cleared when the entity
# moves rather than waiting for a model to remember to correct them. What is in
# your hand is deliberately not here: the beer comes out of the bar with you.
PLACE_BOUND_STATE = frozenset(
    {"posture", "position", "stance", "facing", "activity", "action",
     "current_action", "currently", "doing", "busy_with", "phone_action",
     "vantage", "attention", "looking_at", "seated_at", "standing_on"}
)

# Refused outright, because the store already models these structurally: an
# entity's place is `location_id`, and what is in a place comes from its
# contents. A copy in a fact or in state only goes stale.
STRUCTURAL_KEYS = frozenset(
    {"location", "location_id", "whereabouts", "occupants", "contents",
     "inside", "contains"}
)


# A journey that takes real time has to end somewhere else. A live turn drove a
# delivery across town, advanced the clock twenty-two minutes, and left the
# character standing at her own work table — and code took it, because
# `player_location` named a real place, just the wrong one. The next turn looked
# around "the venue" and found her back room. Half a compound action resolved,
# and nothing noticed.
#
# Both conditions are needed and that is the whole point. "I walk around the
# car" is a travel verb and a minute, and ends where it began, correctly.
# "I sleep until morning" is eight hours in one spot, correctly. Only a
# declared journey that also billed for the travel is a contradiction.
TRAVEL_MINUTES = 5

TRAVELS = re.compile(
    r"\b(?:go|goes|going|gone|head|heads|heading|walk|walks|walking|"
    r"drive|drives|driving|drove|run|runs|running|ran|ride|rides|riding|rode|"
    r"fly|flies|flying|flew|travel|travels|travelling|traveling|"
    r"leave|leaves|leaving|left|return|returns|returning)\b",
    re.IGNORECASE,
)


def declares_travel(said: str) -> bool:
    """Whether the player said they were going somewhere."""
    return bool(TRAVELS.search(said))


# The player says what their character does. They do not say how it goes —
# that is the world's to decide, and the vision's "not a yes-man" is exactly
# this line. A live session typed "I successfuly get the money into my
# account" and the world agreed, because the assertion arrived as if it were
# the action.
ASSERTS_OUTCOME = re.compile(
    r"\b(?:"
    r"succes+\w*|"                       # successful, successfully, succesfuly
    r"manage[sd]? to|"
    r"it works|and it works|"
    r"without (?:a |any )?(?:problem|issue|trouble|hitch)|"
    r"(?:no one|nobody|no-one) (?:sees|notices|stops|catches)|"
    r"unnoticed|undetected|"
    r"and (?:i|it) (?:get|gets|got) away"
    r")\b",
    re.IGNORECASE,
)


def asserts_outcome(said: str) -> bool:
    """Whether the player tried to settle the result of their own action."""
    return bool(ASSERTS_OUTCOME.search(said))


def is_transient(key: str) -> bool:
    """Whether this key is current state rather than a fact (spec 2.1)."""
    return key.lower() in TRANSIENT_KEYS


def not_a_fact(key: str) -> str | None:
    """Why this key belongs in neither half of the store, or None if it does.

    Transient keys are not refused here any more: code routes them into the
    state table instead, so a model that writes posture as a fact still gets a
    world that changes. Only what the store already holds structurally is
    dropped.
    """
    if key.lower() in STRUCTURAL_KEYS:
        return (
            f"{key!r} restates where something is, which the store already "
            "holds as the entity's location. Move it, do not describe it"
        )
    return None


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


def _no_nulls(data):
    """Read `"dialogue": null` as "there was none".

    A live turn lost its scene and its ledger to exactly that: the model wrote
    null where it had nothing to add, which is what null means. The objects
    after the opening cannot be retried, so leniency costs nothing and
    strictness cost the turn's consequences.
    """
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if v is not None}
    return data


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
    # Defaulted for the same reason NewFact defaults them: a live playtest lost
    # a whole ledger — the player's move and their money with it — because the
    # model wrote a supersession without these two. The ledger arrives after the
    # turn is committed and narrated, so it cannot be retried, which makes
    # leniency here worth far more than precision.
    source: Source = "invented"
    visibility: Visibility = "obvious"

    @model_validator(mode="after")
    def quantities_are_never_superseded(self) -> Supersede:
        if is_quantity(self.key):
            raise ValueError(
                f"{self.key!r} is a quantity; change it with a delta, never by "
                "writing a new total"
            )
        self.value = as_text(self.value)
        return self


# Facts, entities and perceptions go on the wire as arrays, so a model that has
# been writing arrays all turn writes them for the ledger's entries too. These
# used to be dict-only, and a `moves` entry written as an array took the whole
# ledger down with it — which, arriving after the turn is committed, cannot be
# retried. Every shape the ledger carries accepts both forms.
class Delta(BaseModel):
    """A change to a quantity. The model never writes the new total."""

    entity_id: str
    key: str
    delta: float

    @model_validator(mode="before")
    @classmethod
    def accept_compact(cls, data):
        return _expand(data, ("entity_id", "key", "delta"), {})


class Move(BaseModel):
    entity_id: str
    location_id: str

    @model_validator(mode="before")
    @classmethod
    def accept_compact(cls, data):
        return _expand(data, ("entity_id", "location_id"), {})


class Learned(BaseModel):
    """What someone other than the player came to know, truly or otherwise."""

    mind_id: str
    entity_id: str
    key: str
    how: str
    belief: str | float | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_compact(cls, data):
        return _expand(
            data, ("mind_id", "entity_id", "key", "how", "belief"), {}
        )


class StateChange(BaseModel):
    """One transient value, as it stands at the end of this turn (spec 2.1).

    Overwrites whatever was there, so there is no supersession and no conflict.
    This is where the payoff of an action lands: climbing onto the table makes
    `posture` "standing on the table" and `vantage` "you can see over the crowd
    to the back door", and the narrator closes the turn on exactly that.
    """

    entity_id: str
    key: str
    value: str | float

    @model_validator(mode="before")
    @classmethod
    def accept_compact(cls, data):
        return _expand(data, ("entity_id", "key", "value"), {})

    @model_validator(mode="after")
    def as_words(self) -> StateChange:
        self.value = as_text(self.value)
        return self


class Scheduling(BaseModel):
    """Something put on the clock, `in_minutes` from now (spec 6.2).

    Minutes rather than a timestamp because the arithmetic is code's job: a
    model asked for an ISO time in the world's own zone got it wrong often
    enough that a deadline could land in the past.
    """

    in_minutes: float = Field(ge=0)
    what: str
    reaches_player: bool = False

    @model_validator(mode="before")
    @classmethod
    def accept_compact(cls, data):
        return _expand(data, ("in_minutes", "what", "reaches_player"), {})


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
    # Where the action left everyone's posture and vantage in this branch.
    state: list[StateChange] = Field(default_factory=list)
    ends_life: bool = False


class Roll(BaseModel):
    p_success: float = Field(ge=0.0, le=1.0)
    why: str
    on_success: Outcome
    on_failure: Outcome


# How close the roll was, in words. Code knows the margin and the narrator must
# never see a number (spec 3.2), so the margin is banded here and the band is
# what travels. A near miss that reads exactly like a hopeless one is the same
# turn twice, and the difference between them is most of what makes a risk feel
# like a risk.
#
# Banded on how far through the range the roll landed, not on the raw distance
# to the threshold. Raw distance reads the odds as if they were not there: at
# 45% a draw of 0.24 is two fifths of the way into the success range, an
# ordinary win, and the raw margin of 0.21 called it "better than it had any
# right to".
BY_A_HAIR = 0.85
COMFORTABLY = 0.33


def margin_band(p_success: float, rolled: float, succeeded: bool) -> str:
    """One line on how it went, for the narrator. Never the odds."""
    if succeeded:
        # 0 is the far end of the range it needed, 1 is the threshold itself.
        share = rolled / p_success if p_success > 0 else 1.0
        if share >= BY_A_HAIR:
            return "it works, but only barely — a hair either way and it would not have"
        if share >= COMFORTABLY:
            return "it works, with some effort"
        return "it works easily, better than it had any right to"
    share = (rolled - p_success) / (1 - p_success) if p_success < 1 else 1.0
    if share <= 1 - BY_A_HAIR:
        return "it fails by a hair — it was almost there, and the near miss shows"
    if share <= 1 - COMFORTABLY:
        return "it clearly fails"
    return "it fails badly, worse than expected"


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

    # Not required, though it is the first field asked for. A live turn was
    # refused outright because the model wrote the location and the facts and
    # simply left this out, twice. It is a convenience — a label for the log and
    # context for the narrator — and losing a whole turn over a label is the
    # worst trade in the engine. Code falls back to the player's own words,
    # which never reach the narrator.
    @model_validator(mode="before")
    @classmethod
    def drop_nulls(cls, data):
        return _no_nulls(data)

    intent: str = ""
    duration_minutes: float = Field(ge=0)
    player_location: str
    # True when a roll decides the turn. The opening then holds only what is
    # true either way, and the outcome is narrated once code has rolled.
    uncertain: bool = False
    new_entities: list[NewEntity] = Field(default_factory=list)
    new_facts: list[NewFact] = Field(default_factory=list)
    perceived: list[Perceived] = Field(default_factory=list)
    # Posture and vantage belong here for the same reason `player_location`
    # does: both are where the action left the character, and both have to
    # survive a stream that dies before the scene closes.
    state: list[StateChange] = Field(default_factory=list)


class Scene(BaseModel):
    """The second object: the rest of the truth, and who said what.

    Committed and narrated as a second beat, while the ledger is still being
    written. Anything in here that the character perceives reaches them a
    moment after the opening did, which is also how a room actually lands —
    the place first, then the person in it.
    """

    @model_validator(mode="before")
    @classmethod
    def drop_nulls(cls, data):
        return _no_nulls(data)

    summary: str = ""
    you_said_aloud: str | None = None
    new_entities: list[NewEntity] = Field(default_factory=list)
    new_facts: list[NewFact] = Field(default_factory=list)
    perceived: list[Perceived] = Field(default_factory=list)
    # What the action left different: posture, vantage, what is in hand. The
    # narrator closes the turn on this, which is what stops an action from
    # reading as an effect with no payoff (step 2d of the feel pass).
    state: list[StateChange] = Field(default_factory=list)
    dialogue: list[Line] = Field(default_factory=list)
    scene_change: bool = False


class Ledger(BaseModel):
    """The second object: consequences the player does not perceive this turn.

    Committed after the narration has already gone out, because none of it
    changes what this turn sounds like.
    """

    @model_validator(mode="before")
    @classmethod
    def drop_nulls(cls, data):
        return _no_nulls(data)

    superseded: list[Supersede] = Field(default_factory=list)
    deltas: list[Delta] = Field(default_factory=list)
    moves: list[Move] = Field(default_factory=list)
    learned: list[Learned] = Field(default_factory=list)
    # Dated consequences (spec 6.2). The deadline a turn creates has to actually
    # arrive, or pressure is only ever a line of description.
    schedule: list[Scheduling] = Field(default_factory=list)
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


def _state_lines(store: Store, entity_id: str) -> dict:
    """An entity's transient state as plain key-to-value (spec 2.1)."""
    return {held.key: held.value for held in store.state(entity_id)}


def _regroup(entries: list[tuple[str, str, dict]]) -> dict:
    """Facts back into the nested shape resolver.md documents."""
    out: dict[str, dict] = {}
    for entity_id, key, value in entries:
        out.setdefault(entity_id, {})[key] = value
    return out


def draw_station(rng: random.Random) -> str:
    """Pick a station in life, weighted. Called by turn.begin_life."""
    return rng.choices(
        [description for _, description in STATIONS],
        weights=[weight for weight, _ in STATIONS],
    )[0]


# A life opens in the middle of something, so the spawn has to settle both
# halves of that: what is already happening, and what is already hidden.
SECRETS_WANTED = (2, 4)
EVENTS_WANTED = (1, 3)
# "A deadline inside the first in-game hour" (step 3a). Sixty-five rather than
# sixty so a model that writes "in an hour" is taken at its word.
SOON_MINUTES = 65


class SpawnOutput(BaseModel):
    new_entities: list[NewEntity] = Field(default_factory=list)
    new_facts: list[NewFact] = Field(default_factory=list)
    # Settled before the player can look, so digging finds something solid
    # rather than improvisation. Visibility is forced below: a secret the
    # character knows is not a secret, and a `hidden` fact about them is one
    # they do not know either (spec 2.1).
    secrets: list[NewFact] = Field(default_factory=list)
    # The pressure the life opens under, and it has to actually arrive, which is
    # what the minimal clock is for (spec 6.2).
    schedule: list[Scheduling] = Field(default_factory=list)
    # Posture and vantage at the moment the world opens, so the first turn
    # starts from a body somewhere rather than from nowhere.
    state: list[StateChange] = Field(default_factory=list)
    # Where the character actually is. Without this they stand in the bare city
    # entity, which makes everyone in town read as present in the room.
    player_starts_in: str | None = None
    opening_situation: str
    # A handful of words naming the kind of life, so the next spawn can be told
    # not to write this one again. Every life was a broke twenty-something in an
    # apartment until something kept score.
    premise: str = ""

    @model_validator(mode="after")
    def shape_the_spawn(self) -> SpawnOutput:
        # Trimmed rather than refused: too many is a model being generous, and
        # losing a whole life over it would be absurd. Too few is checked
        # against the store instead, where it can spend a retry.
        self.secrets = self.secrets[: SECRETS_WANTED[1]]
        for secret in self.secrets:
            secret.visibility = "hidden"
        self.schedule = self.schedule[: EVENTS_WANTED[1]]
        return self


class Resolver:
    """Builds the resolver's context from the truth and asks for a decision."""

    def __init__(
        self,
        llm: _BaseLLM,
        store: Store,
        config: Config,
        prompts_dir: Path,
        corrections: Corrections | None = None,
    ) -> None:
        self.llm = llm
        self.store = store
        self.config = config
        self.prompts_dir = prompts_dir
        self.corrections = corrections or Corrections()

    def _prompt(self, name: str) -> str:
        return (self.prompts_dir / name).read_text()

    def spawn(self, station: str, avoid: list[str] | None = None) -> SpawnOutput:
        """Build a life. `station` is drawn by code, not chosen by the model.

        `avoid` is the premises of recent lives. Asked cold for "a person with
        a situation in motion", a model writes the same hungover
        twenty-something in a one-bedroom every time; the station weights fixed
        how comfortable a life is and left what it is *about* untouched.
        """
        user = as_prompt(
            {
                "city": self.config.spawn_city,
                # UTC, and labelled as such. It used to be sent as "local_time"
                # while carrying UTC, so a 1:24pm world opened on "the last
                # light of day fading" and scheduled dinner five hours late.
                # The zone is not settled until the model writes it below, so
                # UTC with an honest name is the best that can be offered here.
                "utc_time": self.store.now().isoformat(),
                "player_entity_id": PLAYER_ID,
                "city_entity_id": SPAWN_PLACE_ID,
                "station_in_life": station,
                "recent_premises_do_not_repeat": avoid or [],
            }
        )
        # Strict once, then take what arrives. A refused spawn is not a worse
        # life, it is no game at all — the player gets "the world could not be
        # started" and nothing to play. So the check spends the one retry with
        # its reason attached and then stands down; the rejected attempt and
        # why it was rejected are in the call log either way (rule 9).
        asked_once = []

        def check(spawned: SpawnOutput) -> None:
            if asked_once:
                return
            asked_once.append(True)
            self.check_spawn(spawned)

        return self.llm.json(
            "resolver", self._prompt("spawn.md"), user, SpawnOutput, check
        )

    def check_spawn(self, spawned: SpawnOutput) -> None:
        """A life has to open under pressure, with something already hidden.

        Raising spends the spawn's one retry (spec 3.3) with the reason
        attached, which is the only way the model learns it wrote a life that
        is merely described rather than already happening.
        """
        if len(spawned.secrets) < SECRETS_WANTED[0]:
            raise ValueError(
                f"only {len(spawned.secrets)} secrets. Write "
                f"{SECRETS_WANTED[0]} to {SECRETS_WANTED[1]} things that are "
                "true about this life and that the character does not know, in "
                "`secrets`. They are what makes digging find something."
            )
        if len(spawned.schedule) < EVENTS_WANTED[0]:
            raise ValueError(
                "`schedule` is empty. Put the thing that is already happening "
                "on the clock: what it is, and how many minutes from now."
            )
        if not any(e.in_minutes <= SOON_MINUTES for e in spawned.schedule):
            soonest = min(e.in_minutes for e in spawned.schedule)
            raise ValueError(
                f"the soonest scheduled thing is {soonest:.0f} minutes away. "
                f"One of them has to land within {SOON_MINUTES} minutes, or the "
                "life opens with nothing pressing on it."
            )

    def resolve(
        self,
        player_said: str,
        recent: list[str],
        on_object: Callable[[int, object], None],
        mode: str = "action",
        events: list[str] | None = None,
    ) -> tuple[Opening, Scene, Ledger]:
        """Stream one call. `on_object` fires as each of the three is valid.

        `mode` is how code read the input (spec 3.1): an action, speech, or a
        question the player put to the narrator that needs something settled
        before it can be answered. `events` are timeline entries the clock has
        just passed, which is how a deadline actually arrives.
        """
        # Imported here, not at module scope: sheet.py reads PLAYER_ID from
        # this module, so a top-level import would close the loop.
        from .sheet import time_of_day

        # Spec 5.2's priority order: the instructions have the first claim on
        # the budget, then what the player just said, then the facts where they
        # are standing, then the scene so far. So the oldest turn is what goes
        # when something has to.
        system = self._prompt("resolver.md")
        # The retry appends its explanation to this prompt, so the room for it
        # is held back rather than discovered afterwards.
        budget = (
            self.config.token_budgets["resolver"]
            - estimate_tokens(system)
            - RETRY_ROOM
            - SEAM
        )
        payload: dict = {
            "player_said": player_said,
            # How code read the input. The engine used to resolve every line as
            # an in-world action, so "What am I doing?" became the character
            # saying it out loud to an empty room.
            "input_mode": mode,
            # Local, in plain words, and not the raw UTC stamp this used to be.
            # The resolver settles the facts, so when it read 20:01 UTC as
            # evening it wrote "city skyline at dusk" onto a kitchen window at
            # three in the afternoon — and that is now permanently true.
            "the_time": time_of_day(self.store),
            "world_time_utc": self.store.now().isoformat(),
            # Timeline entries the clock has just passed (spec 6.2). These are
            # happening whether or not the player's action has anything to do
            # with them, and they have the first claim after the input itself.
            "happening_now": events or [],
            "your_state": _state_lines(self.store, PLAYER_ID),
            "recent_turns": [],
            "truth_here": {},
        }
        room = budget - estimate_tokens(payload)
        payload["truth_here"] = self.truth_here(budget_tokens=max(room, 0))
        room = budget - estimate_tokens(payload)
        payload["recent_turns"] = list(
            reversed(take_within(list(reversed(recent)), max(room, 0)))
        )
        # And then make it certain rather than estimated: the oldest turn goes
        # first, and once those are gone the least important fact slice does.
        # Trimming only the turns left the bound a token or two soft, because
        # the JSON keys around `facts` are not in the slices that were measured.
        while estimate_tokens(payload) > budget:
            if payload["recent_turns"]:
                payload["recent_turns"].pop(0)
            elif len(payload["truth_here"].get("facts", {})) > 1:
                payload["truth_here"]["facts"].popitem()
            else:
                break  # the instructions alone are over; nothing left to give
        user = as_prompt(payload)
        # Where they are before the turn, captured now: the opening's checks
        # run before anything is committed, so the store still holds it, but
        # only this frame knows it was the *starting* point.
        player = self.store.entity(PLAYER_ID)
        was_in = player.location_id if player else None

        # The yes-man check gets one shot, then stands down. The other two are
        # about coherence and a retry can always satisfy them; this one asks
        # for a roll, and "certain failure" is a legitimate answer that looks
        # identical from the opening — a barista has no path to ten million
        # dollars and `uncertain:false` is the right call for that. Held
        # strict, a model that read it that way lost the turn twice over and
        # the player got "say it again" instead of a scene where they tried.
        nudged: list[bool] = []

        def check(opening: Opening) -> None:
            # check_opening is not optional: it keeps the player somewhere that
            # exists, which is the store's coherence rather than a judgement
            # about the model.
            self.check_opening(opening)
            if not self.corrections.refuse_turns:
                return
            if not nudged:
                nudged.append(True)
                self.check_not_a_yes_man(opening, player_said)
            self.check_travel(opening, player_said, was_in)

        opening, scene, ledger = self.llm.stream_objects(
            "resolver",
            system,
            user,
            (Opening, Scene, Ledger),
            on_object,
            (check, None, None),
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
        proposed = {e.id: e.location_id for e in opening.new_entities}
        where = opening.player_location
        if self.store.entity(where) is None and where not in proposed:
            raise ValueError(
                f"player_location is {where!r}, which does not exist and is not "
                "created in this opening. Add it to new_entities, nested inside "
                "a place that does exist."
            )

        # Where the player ends up has to be reachable, all the way out. A live
        # playtest put a restaurant inside `place:downtown`, which had never
        # been settled: the restaurant was dropped, every fact on it went with
        # it, the player was left standing in their car — and the narration
        # walked them to a table anyway. The check above passed, because the
        # restaurant *was* in new_entities; nobody asked about its parent.
        #
        # Only the player's own chain is enforced. An entity off to one side
        # with nowhere to live is harmless and stays a reported dropped write;
        # this one is the difference between the truth and the narration.
        # Only the chain of places that do not exist yet is walked. A model
        # re-listing somewhere that is already in the store is harmless — code
        # skips an entity it already has — but reading its *proposed* parent as
        # though it mattered killed a live turn: the player was already in
        # `place:ambulance`, the resolver listed it again under a
        # `place:houston` it never settled, and a turn that needed to commit
        # nothing new was refused twice over a parent nobody was going to use.
        seen = {where}
        parent = None if self.store.entity(where) is not None else proposed.get(where)
        while parent is not None and self.store.entity(parent) is None:
            if parent not in proposed:
                raise ValueError(
                    f"player_location {where!r} is nested inside {parent!r}, "
                    "which does not exist and is not created here either, so "
                    "neither of them can be committed. Nest it inside a place "
                    "that already exists — put the building straight in the "
                    "city rather than inventing a neighbourhood to hold it."
                )
            if parent in seen:
                raise ValueError(
                    f"the places holding {where!r} are nested inside each "
                    "other in a loop: {sorted(seen)}. Nothing contains itself."
                )
            seen.add(parent)
            parent = proposed[parent]

    def check_not_a_yes_man(self, opening: Opening, player_said: str) -> None:
        """The player's confidence is not evidence (vision: not a yes-man).

        They declare the attempt; the world decides how it goes. A live
        session typed "I successfuly get the money into my account" and the
        resolver committed it with `uncertain` false and no roll, because the
        assertion arrived looking like the action.

        Raising spends the spec 3.3 retry. It does not demand failure — it
        demands that the question be put to the dice (rule 6), which is the
        only honest answer to a player who has written their own ending.

        It is asked once per turn and then stands down. Certain failure is a
        real resolution and looks exactly like certain success from here, so a
        model that answers "there is no path to this at all" is right, and
        holding the line a second time only costs the player the turn.
        """
        if not asserts_outcome(player_said):
            return
        if opening.uncertain:
            return  # already going to a roll, which is all this asks for
        raise ValueError(
            "The player's own words claim the attempt worked. That is not "
            "theirs to decide and it is not evidence: they say what the "
            "character does, the world says how it goes. Either the outcome "
            "is genuinely not in doubt for reasons in `truth_here` — their "
            "skill, their tools, the state of the place — or set "
            '"uncertain":true and write both branches in the ledger\'s roll. '
            "Saying it confidently is not one of the reasons."
        )

    def check_travel(self, opening: Opening, player_said: str, was_in: str) -> None:
        """A declared journey that billed for the travel has to arrive.

        Raising spends the spec 3.3 retry with the reason attached, which is
        the only thing that recovers the turn: the resolver resolved the first
        half of a compound action, charged the clock for the second, and
        committed a `player_location` that contradicts both.

        Deliberately narrow. It fires only when the player said they were
        going somewhere, the clock moved by more than it takes to cross a
        room, and they ended where they started. Each of those alone is
        ordinary; together they cannot all be true.
        """
        if opening.player_location != was_in:
            return
        if opening.duration_minutes < TRAVEL_MINUTES:
            return
        if not declares_travel(player_said):
            return
        raise ValueError(
            f"player_location is {was_in!r}, which is where they already were, "
            f"but this turn takes {opening.duration_minutes:.0f} minutes and "
            "they said they were going somewhere. Pick one. Either they got "
            "there — set player_location to the destination, creating it in "
            "new_entities inside a place that already exists — or they never "
            "set off, and duration_minutes is only the minute or two it took "
            "to get as far as they got. Never both: a turn that charges for "
            "the journey and does not move them leaves the world in the wrong "
            "place, and every turn after it is wrong too."
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
            # Transient state (spec 2.1), the player's own first. It is small,
            # it changes every turn, and a resolver that cannot see the
            # character is standing on a table writes the next turn as though
            # they were still on the floor.
            "state_now": {
                entity_id: lines
                for entity_id in [PLAYER_ID] + [e.id for e in present]
                for lines in [_state_lines(self.store, entity_id)]
                if lines
            },
        }
        if budget_tokens is None:
            budget_tokens = self.config.token_budgets["resolver"]

        ordered = [PLAYER_ID] + ([here] if here else [])
        ordered += [e.id for e in carried] + [e.id for e in present]

        # Flattened to one entry per fact, not one per entity, so the trim is
        # smooth. A place the player keeps returning to accumulates facts
        # without limit, and a whole-entity slice could blow the budget on its
        # own — the bar in the 500-turn test is a single entity holding 500
        # facts. Within an entity the oldest come first: the name and the shape
        # of the place were settled before the incidental detail.
        entries: list[tuple[str, str, dict]] = []
        seen: set[str] = set()
        for entity_id in ordered:
            if entity_id in seen:
                continue
            seen.add(entity_id)
            for key, value in facts_of(entity_id).items():
                entries.append((entity_id, key, value))

        room = budget_tokens - estimate_tokens(frame)
        kept = take_within(entries, max(room, 0))
        frame["facts"] = _regroup(kept)
        # Exactly as in render.context: the estimate above ignores the JSON
        # overhead of the keys themselves, and spec 5.2 is a hard bound.
        while estimate_tokens(frame) > budget_tokens and len(kept) > 1:
            kept.pop()
            frame["facts"] = _regroup(kept)
        return frame
