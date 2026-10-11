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
from typing import Callable

from .llm import LedgerFailed
from .render import BIOGRAPHY, Narrator, sentences_in as _sentences_in
from .resolve import (
    PLAYER_ID,
    SPAWN_PLACE_ID,
    Ledger,
    Opening,
    Outcome,
    Resolver,
    Scene,
    draw_station,
    not_a_fact,
)
from .store import Fact, FactConflict, Life, Store, StoreError


# Spec 4: two to four sentences, for the whole turn. A turn can reach the player
# in two beats, and two beats of "two to four" made six-sentence turns live.
SENTENCES_A_TURN = 4


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
class _Committed:
    """What the stream managed to commit, for play() to pick up afterwards.

    The objects commit from inside the resolver's stream, so play() needs
    somewhere to find them again — above all when the stream then dies and the
    turn has to be salvaged rather than replayed.
    """

    opening: Opening | None = None
    scene: Scene | None = None
    beats: list[str] = field(default_factory=list)
    noticed: list[int] = field(default_factory=list)
    # What the player was told at the end of the previous turn.
    said_before: str | None = None

    @property
    def narration(self) -> str:
        return " ".join(beat for beat in self.beats if beat)


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

    def play(
        self,
        player_said: str,
        life: Life,
        on_text: Callable[[str], None] | None = None,
    ) -> TurnResult:
        """Resolve, commit, render — with the render moved inside the stream.

        The resolver emits three objects. The opening is small on purpose, so
        it commits and starts narrating seconds before the rest of the turn
        exists; the scene commits and narrates a second beat while the ledger
        is still being generated. Everything the narrator sees is committed
        before it speaks, so rule 1 holds, and nothing that lands afterwards
        can change what the player has already heard.

        `on_text` receives each narrated sentence as it clears, so speech can
        begin before the sentence after it has been written.
        """
        rows = self.store.recent_turns(life.id, limit=6)
        recent = [f"{row['player_said']} -> {row['narration']}" for row in rows]
        # Six of ten turns in a live run re-described the same kitchen window,
        # because the narrator had no idea what it had already said. It knows
        # what it said inside a turn; this gives it the turn before.
        last_said = rows[-1]["narration"] if rows else None
        dropped: list[str] = []
        committed = _Committed(said_before=last_said)

        # The narrator never sees this; it is the audit trail on every fact the
        # turn writes, so the player's own words are the better fallback.
        def on_object(index: int, obj) -> None:
            if index == 0:
                if not obj.intent:
                    obj.intent = player_said[:80]
                self._commit_opening(obj, committed, dropped, on_text)
            elif index == 1:
                self._commit_scene(obj, committed, dropped, on_text)

        try:
            opening, scene, ledger = self.resolver.resolve(
                player_said, recent, on_object
            )
        except LedgerFailed as exc:
            # Part of this is already true and already spoken, so losing the
            # narration on top of losing the tail is the worst of both. Keep
            # what happened and report what didn't.
            if committed.opening is None:
                raise
            opening = committed.opening
            lost = "ledger" if committed.scene is not None else "scene and ledger"
            scene = committed.scene or Scene(summary=opening.intent)
            ledger = Ledger()
            dropped.append(f"{lost} unusable, consequences missing: {exc}")

        cause = f"turn: {opening.intent}"
        roll_record = None
        summary = scene.summary
        ends_life = ledger.ends_life
        dialogue = scene.dialogue
        said_aloud = scene.you_said_aloud

        with self.store.transaction():
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
                committed.noticed += self._perceive(branch.perceived, dropped)
                summary, ends_life = branch.summary, branch.ends_life
                dialogue, said_aloud = branch.dialogue, branch.you_said_aloud

            self._apply_ledger(ledger, opening, scene, cause, dropped)

        # A roll's outcome is not known until here, so that beat waits for it.
        # The opening still narrated what was true either way, which is why a
        # risky turn no longer sits in silence for the whole call.
        #
        # The second branch is the floor: every turn says something. A turn that
        # settled nothing and went nowhere — paying for the beer already in your
        # hand — still has to answer, and the earlier beats are allowed to pass
        # precisely because this catches what they skip.
        if ledger.roll is not None or not committed.beats:
            self._narrate(committed, opening.intent, dialogue, said_aloud, on_text)

        self.store.log_turn(
            life_id=life.id,
            player_said=player_said,
            intent=opening.intent,
            roll_json=roll_record.as_json() if roll_record else None,
            narration=committed.narration,
        )
        return TurnResult(
            player_said=player_said,
            intent=opening.intent,
            narration=committed.narration,
            duration_minutes=opening.duration_minutes,
            roll=roll_record,
            summary=summary,
            ends_life=ends_life,
            noticed_fact_ids=committed.noticed,
            dropped=dropped,
        )

    # ---- the three objects, each committed then narrated ----

    def _commit_opening(
        self,
        opening: Opening,
        committed: _Committed,
        dropped: list[str],
        on_text: Callable[[str], None] | None,
    ) -> None:
        # One transaction, so a write the store refuses partway through cannot
        # leave the clock advanced over a half-written world. It closes before
        # the narrator is called, which is what keeps rule 1 true: everything
        # the narrator sees is committed before it speaks.
        with self.store.transaction():
            # The clock moves only by the resolver's duration (rule 7).
            self.store.advance(opening.duration_minutes)
            cause = f"turn: {opening.intent}"
            self._entities(opening.new_entities, dropped)
            landed = self._facts(opening.new_facts, cause, dropped)
            moved = self._place_player(opening.player_location, dropped)
            noticed = self._perceive(opening.perceived, dropped)
            noticed += self._notice_obvious(landed, noticed)
        committed.opening = opening
        committed.noticed = noticed
        # Only speak if there is something to speak about. An opening that
        # settled nothing and went nowhere would have the narrator inventing a
        # beat out of an empty context, which is the filler the vision refuses.
        if noticed or moved:
            self._narrate(committed, opening.intent, [], None, on_text)

    def _commit_scene(
        self,
        scene: Scene,
        committed: _Committed,
        dropped: list[str],
        on_text: Callable[[str], None] | None,
    ) -> None:
        with self.store.transaction():
            cause = f"turn: {committed.opening.intent}"
            self._entities(scene.new_entities, dropped)
            landed = self._facts(scene.new_facts, cause, dropped)
            noticed = self._perceive(scene.perceived, dropped)
            noticed += self._notice_obvious(landed, committed.noticed + noticed)
        committed.scene = scene
        committed.noticed += noticed
        if committed.opening.uncertain:
            return  # the outcome decides this beat, and it has not been rolled
        # A second beat has to be a genuinely different thing to say: someone
        # spoke, or someone is there who was not accounted for. More facts
        # about the same room are not — the first beat already described it,
        # and a live turn answered "what's in this kitchen?" by narrating the
        # kitchen twice, word for word, because new facts alone let this fire.
        if scene.dialogue or scene.you_said_aloud or self._new_faces(scene):
            self._narrate(
                committed,
                committed.opening.intent,
                scene.dialogue,
                scene.you_said_aloud,
                on_text,
                only_new=noticed,
            )

    def _new_faces(self, scene: Scene) -> bool:
        """Whether the scene brought a person into the room with the player."""
        player = self.store.entity(PLAYER_ID)
        here = player.location_id if player else None
        return any(
            e.kind == "person" and e.location_id == here for e in scene.new_entities
        )

    def _narrate(
        self,
        committed: _Committed,
        intent: str,
        dialogue,
        said_aloud: str | None,
        on_text: Callable[[str], None] | None,
        only_new: list[int] | None = None,
    ) -> None:
        spent = sum(len(_sentences_in(beat)) for beat in committed.beats)
        if committed.beats and spent >= SENTENCES_A_TURN:
            # The turn has already said its four. A beat with no allowance left
            # would be a model call that cannot be spoken, so it is not made —
            # the facts behind it are committed either way, and the next turn
            # has them.
            return
        beat = self.narrator.narrate(
            intent,
            dialogue,
            only_new if only_new is not None else committed.noticed,
            said_aloud,
            already_said=committed.narration or None,
            recently_said=committed.said_before,
            max_sentences=max(SENTENCES_A_TURN - spent, 1),
            on_text=on_text,
        )
        committed.beats.append(beat)

    # ---- committing the pieces ----

    def _place_player(self, location_id: str, dropped: list[str]) -> bool:
        """The store decides where the player is, not the narration.

        Returns whether they actually moved, which is on its own worth
        narrating: going through a door is something that happened.
        """
        if self.store.entity(location_id) is None:
            dropped.append(f"player_location {location_id}: no such place, left in place")
        elif self.store.entity(PLAYER_ID).location_id != location_id:
            try:
                self.store.move(PLAYER_ID, location_id)
                return True
            except StoreError as exc:
                # Every other proposal in the turn is drained and reported this
                # way. This one escaped, which turned a nonsense location — the
                # player inside themselves — into a lost turn instead of a
                # dropped write.
                dropped.append(f"player_location {location_id}: {exc}")
        return False

    def _apply_ledger(self, ledger, opening, scene, cause: str, dropped: list[str]) -> None:
        """Consequences the player did not perceive, committed after narration."""
        for problem in self.resolver.check_deltas(ledger, opening, scene):
            dropped.append(problem)
        self._apply_writes(
            ledger.superseded, ledger.deltas, ledger.moves, ledger.learned,
            cause, dropped,
        )

    def _entities(self, new_entities, dropped: list[str]) -> None:
        """Add proposed entities, parents before children.

        Models list a room before the building that holds it often enough that
        a single pass drops half a nested place. Keep passing while progress is
        being made, then deal with whatever still has nowhere to live.
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
                    self._reparent(proposed, dropped)
                return
            pending = waiting

    def _reparent(self, proposed, dropped: list[str]) -> None:
        """Hang an entity whose parent never materialised off the city instead.

        Models keep inventing a container they never settle — `place:downtown`,
        `place:neighborhood`, `place:apartment-building-garage`. Dropping the
        entity looked tidy and played badly: a café that failed to exist meant
        the player could not walk to it, and two turns later the resolver had
        put a barista in their kitchen rather than contradict itself.

        Keeping the café and deciding it sits in the city is the smaller
        invention. The model proposed that the place exists, which is the part
        that matters; code only picks the most plausible container when the one
        it named was never real (spec 2.2).
        """
        outermost = SPAWN_PLACE_ID
        walked = self.store.entity(PLAYER_ID)
        seen: set[str] = set()
        while walked is not None and walked.id not in seen:
            seen.add(walked.id)
            if walked.location_id is None:
                outermost = walked.id
                break
            walked = self.store.entity(walked.location_id)

        if self.store.entity(outermost) is None:
            dropped.append(
                f"entity {proposed.id}: no such location "
                f"{proposed.location_id!r}, and nowhere to put it instead"
            )
            return
        try:
            self.store.add_entity(
                proposed.id, proposed.kind, proposed.name, outermost
            )
        except StoreError as exc:
            dropped.append(f"entity {proposed.id}: {exc}")
            return
        dropped.append(
            f"entity {proposed.id}: {proposed.location_id!r} was never settled, "
            f"so it was placed in {outermost} instead"
        )

    def _facts(self, new_facts, cause: str, dropped: list[str]) -> list[Fact]:
        """Commit proposed facts. Returns the ones that actually landed."""
        landed = []
        for proposed in new_facts:
            refusal = not_a_fact(proposed.key)
            if refusal is not None:
                # Dropped rather than retried: this arrives in the opening, and
                # the player should not wait three seconds for a key the world
                # is better off without.
                dropped.append(f"fact {proposed.entity_id}.{proposed.key}: {refusal}")
                continue
            try:
                landed.append(
                    self.store.add_fact(
                        proposed.entity_id,
                        proposed.key,
                        proposed.value,
                        proposed.source,
                        proposed.visibility,
                        cause,
                    )
                )
            except FactConflict as exc:
                # Spec 2.2: a stored fact beats model knowledge. Keep the old one.
                dropped.append(f"fact {proposed.entity_id}.{proposed.key}: {exc}")
            except StoreError as exc:
                dropped.append(f"fact {proposed.entity_id}.{proposed.key}: {exc}")
        return landed

    def _notice_obvious(self, landed: list[Fact], already: list[int]) -> list[int]:
        """Perceive what `obvious` already means (spec 2.1, 2.4).

        An obvious fact is one "anyone present notices", so a freshly settled
        obvious fact about the character, the room they are in, or something in
        it with them does not also need listing in `perceived`. That array was
        a sixth of the opening object, repeating what visibility already said —
        and every token in the opening is a second the player spends in silence.

        `noticeable` and `hidden` still need saying explicitly, which is the
        whole point of the distinction.
        """
        player = self.store.entity(PLAYER_ID)
        in_scope = {PLAYER_ID}
        if player is not None and player.location_id:
            in_scope.add(player.location_id)
            in_scope |= {e.id for e in self.store.contents(player.location_id)}
        in_scope |= {e.id for e in self.store.contents(PLAYER_ID)}

        seen = set(already)
        noticed = []
        for fact in landed:
            if fact.id in seen or fact.visibility != "obvious":
                continue
            if fact.entity_id not in in_scope:
                continue
            self.store.learn(PLAYER_ID, fact.id, "you can see it")
            noticed.append(fact.id)
        return noticed

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
    store: Store,
    resolver: Resolver,
    narrator: Narrator,
    spawn_city: str,
    rng: random.Random,
    on_text: Callable[[str], None] | None = None,
) -> tuple[Life, str]:
    """Spawn a character and narrate the opening (build step 4).

    Code creates the city and the player shell so the model cannot get their
    ids wrong, and draws how comfortable the life is before asking, then the
    resolver fills in the person and their situation.
    """
    # Idempotent: a spawn that failed on the model call leaves these two behind
    # with no life started, and without this the world could never be opened
    # again.
    if store.entity(SPAWN_PLACE_ID) is None:
        store.add_entity(SPAWN_PLACE_ID, "place", spawn_city)
    if store.entity(PLAYER_ID) is None:
        store.add_entity(PLAYER_ID, "person", "you", location_id=SPAWN_PLACE_ID)

    spawned = resolver.spawn(draw_station(rng))
    dropped: list[str] = []
    turn = Turn(store, resolver, narrator, rng)
    noticed = []

    # A spawn is one world or none of it: half a life is not playable.
    with store.transaction():
        turn._entities(spawned.new_entities, dropped)
        turn._facts(spawned.new_facts, "spawn", dropped)

        # Stand them somewhere specific, so the whole town isn't in the room.
        if spawned.player_starts_in and store.entity(spawned.player_starts_in):
            store.move(PLAYER_ID, spawned.player_starts_in)

        # A person knows themselves, except for what is hidden from them too.
        # `hidden` is one scale, and the subject is on it: an undiagnosed
        # illness or a warrant nobody has served is true about this character
        # and not theirs to know. Teaching them everything on themselves put a
        # warrant straight into the opening narration.
        for fact in store.live_facts(PLAYER_ID):
            if fact.visibility == "hidden":
                continue
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
        "", [], noticed, prompt_name="opening.md", omit_keys=BIOGRAPHY,
        on_text=on_text,
    )
    store.log_turn(life.id, "", "spawn", None, narration)
    return life, narration
