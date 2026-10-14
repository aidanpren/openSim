"""One turn: the attempt, resolve, roll, commit, the outcome (spec 3).

Commit order matters. Entities exist before facts land on them, facts exist
before anyone can perceive them, and the clock moves by the resolver's duration
and never otherwise. A stored fact beats anything the model proposes, so a
conflicting proposal is dropped and reported, not committed and not fatal.

A turn reaches the player in two beats, and which two changed in the feel pass.
The first is the **attempt**, narrated before the resolver is even called: the
player's declared action is the one thing known before the outcome exists, so
it is what the voice can start on. The second is the **outcome**, narrated once
the scene closes or once code has rolled, and it ends on what is different now.
The opening object no longer drives a beat of its own — it used to, and with
nothing new to say it restated the room, which is the repetition the player
reported.

Created by __main__.py, which owns the loop and the commands.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from .config import Corrections
from .llm import LedgerFailed
from .modes import ACTION, QUESTION, SPEECH, classify
from .render import BIOGRAPHY, Heard, Narrator, sentences_in as _sentences_in
from .resolve import (
    PLACE_BOUND_STATE,
    PLAYER_ID,
    SPAWN_PLACE_ID,
    Ledger,
    Opening,
    Outcome,
    Resolver,
    Scene,
    StateChange,
    draw_station,
    is_transient,
    margin_band,
    not_a_fact,
)
from .store import Fact, FactConflict, Life, Store, StoreError


# Length follows the moment (spec 4), rather than one fixed cap for everything.
# A fixed "two to four" made buying a candy bar play at the same resolution as
# a fight, and the brief's own example — "Twenty minutes later you're at your
# door. There's a note taped to it." — is two sentences on purpose.
ATTEMPT_SENTENCES = 1
QUIET_TURN = 3  # the attempt, what happened, what is different now
CHARGED_TURN = 6  # a roll, people talking, something at stake. The voice ceiling.
A_QUESTION = 2  # answered from what the character knows; usually one line


def sentences_for(charged: bool) -> int:
    return CHARGED_TURN if charged else QUIET_TURN


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

    @property
    def band(self) -> str:
        """How close it was, in words, for the narrator. Never the number."""
        return margin_band(self.p_success, self.rolled, self.succeeded)

    def as_dice(self) -> str:
        """The roll as a d20, for the `/dice` toggle. Text mode only.

        A reading of the roll that already happened, not a second roll. Code
        rolls low-is-good and a d20 reads high-is-good, so the face is
        inverted, and the target is clamped to leave a 1 that fails and a 20
        that succeeds the way any table does. Where rounding would make the
        face disagree with the branch that was actually committed, the face
        moves — this is a readout, and it is never allowed to contradict what
        happened.
        """
        needed = min(max(21 - round(self.p_success * 20), 2), 20)
        face = 20 - int(self.rolled * 20)
        if self.succeeded and face < needed:
            face = needed
        elif not self.succeeded and face >= needed:
            face = needed - 1
        return f"(d20: {face}, needed {needed})"


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
    # Transient changes this turn made, in words, for the closing sentence and
    # for the turn log. Transient state keeps no history of its own, so this is
    # the only record that it changed (spec 2.1).
    changed: list[str] = field(default_factory=list)
    # What the player was told at the end of the previous turn.
    said_before: str | None = None
    # Everything they have been told lately, for the near-duplicate filter. It
    # spans turns, so the turn owns it rather than each narrator call.
    heard: Heard = field(default_factory=Heard)
    # Whether the beat that says what actually happened has gone out. The
    # attempt always goes out first, so "have we narrated anything" stopped
    # being the same question.
    outcome_told: bool = False
    # Timeline entries the clock passed, held until the opening commits so a
    # turn that never commits does not swallow them (spec 6.2).
    events: list[object] = field(default_factory=list)

    @property
    def narration(self) -> str:
        return " ".join(beat for beat in self.beats if beat)

    @property
    def different_now(self) -> list[str]:
        """What changed, each thing once.

        The resolver often writes a posture in both `new_facts` and `state`,
        and the store takes both — the second is a no-op overwrite of the same
        value — but the narrator should not be handed the same change twice,
        and `/dev` should not print it twice.
        """
        seen: set[str] = set()
        out = []
        for line in self.changed:
            if line not in seen:
                seen.add(line)
                out.append(line)
        return out


@dataclass
class TurnResult:
    player_said: str
    intent: str
    narration: str
    duration_minutes: float
    roll: RollRecord | None
    summary: str
    ends_life: bool
    mode: str = ACTION
    noticed_fact_ids: list[int] = field(default_factory=list)
    changed_state: list[str] = field(default_factory=list)
    fired: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


class Turn:
    def __init__(
        self,
        store: Store,
        resolver: Resolver,
        narrator: Narrator,
        rng: random.Random,
        corrections: Corrections | None = None,
    ) -> None:
        self.store = store
        self.resolver = resolver
        self.narrator = narrator
        self.rng = rng
        self.corrections = corrections or Corrections()

    def play(
        self,
        player_said: str,
        life: Life,
        on_text: Callable[[str], None] | None = None,
    ) -> TurnResult:
        """The attempt, then resolve, commit, and the outcome.

        The attempt is narrated first, before the resolver is called at all.
        The player's declared action is the one thing that is true before the
        outcome exists, so it is the only honest thing to speak while the world
        is still deciding — and it takes the voice's first word from the close
        of the first JSON object to the first narrator token.

        Then the resolver emits three objects. The opening commits the clock,
        where the character ended up, and what they register first. The scene
        commits the rest and narrates the outcome, which ends on what is
        different now. A roll holds that beat until code has rolled.

        Everything the narrator sees is committed before it speaks, so rule 1
        holds, and nothing that lands afterwards can change what the player has
        already heard.

        `on_text` receives each narrated sentence as it clears, so speech can
        begin before the sentence after it has been written.
        """
        mode = classify(player_said, self._someone_present())
        if mode == QUESTION:
            # Answered from what this character knows. No roll, no time, no new
            # facts, and no resolver call at all: there is nothing for one to
            # settle (spec 3.1).
            return self._answer(player_said, life, on_text)

        rows = self.store.recent_turns(life.id, limit=6)
        recent = [f"{row['player_said']} -> {row['narration']}" for row in rows]
        # Six of ten turns in a live run re-described the same kitchen window,
        # because the narrator had no idea what it had already said. It knows
        # what it said inside a turn; this gives it the turn before.
        last_said = rows[-1]["narration"] if rows else None
        dropped: list[str] = []
        committed = _Committed(
            said_before=last_said,
            heard=self._heard_lately(rows),
            events=self.store.due(),
        )

        # Spoken before anything is resolved, so it can only say what the
        # character already knows, and it never assumes the attempt worked.
        self._narrate_attempt(player_said, mode, committed, on_text)

        # The narrator never sees this; it is the audit trail on every fact the
        # turn writes, so the player's own words are the better fallback.
        def on_object(index: int, obj) -> None:
            if index == 0:
                if not obj.intent:
                    obj.intent = player_said[:80]
                if mode == QUESTION:
                    obj.duration_minutes = 0
                self._commit_opening(obj, committed, dropped)
            elif index == 1:
                self._commit_scene(obj, committed, dropped, on_text, player_said)

        try:
            opening, scene, ledger = self.resolver.resolve(
                player_said, recent, on_object, mode,
                [self._as_event(e) for e in committed.events],
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
                committed.changed += self._apply_outcome(branch, cause, dropped)
                self._place_player(branch.player_location, dropped)
                committed.changed += self._reroute(branch.new_facts, cause, dropped)
                committed.changed += self._state(branch.state, cause, dropped)
                committed.noticed += self._perceive(branch.perceived, dropped)
                summary, ends_life = branch.summary, branch.ends_life
                dialogue, said_aloud = branch.dialogue, branch.you_said_aloud

            # The ledger lands after the outcome is spoken, so a transient
            # change that arrived here is in the store and in the log but not
            # in that sentence. The next turn reads it from the store.
            committed.changed += self._apply_ledger(
                ledger, opening, scene, cause, dropped
            )

        # A roll's outcome is not known until here, so the outcome beat waits
        # for it; the attempt has already been spoken, which is why a risky
        # turn no longer sits in silence for the whole call.
        #
        # The second branch is the floor: every turn says what happened. A
        # scene that never arrived, or a turn that settled nothing and went
        # nowhere — paying for the beer already in your hand — still has to
        # answer, and the beat inside the stream is allowed to pass precisely
        # because this catches what it skips.
        if ledger.roll is not None or not committed.outcome_told:
            self._narrate(
                committed, opening.intent, dialogue, said_aloud, on_text,
                declared=player_said,
                charged=bool(dialogue or ledger.roll is not None),
                how_it_went=roll_record.band if roll_record else None,
            )

        self.store.log_turn(
            life_id=life.id,
            player_said=player_said,
            intent=opening.intent,
            roll_json=roll_record.as_json() if roll_record else None,
            narration=committed.narration,
            mode=mode,
            state_json=(
                json.dumps(committed.different_now)
                if committed.changed else None
            ),
        )
        return TurnResult(
            player_said=player_said,
            intent=opening.intent,
            narration=committed.narration,
            duration_minutes=opening.duration_minutes,
            roll=roll_record,
            summary=summary,
            ends_life=ends_life,
            mode=mode,
            noticed_fact_ids=committed.noticed,
            changed_state=committed.different_now,
            fired=[self._as_event(e) for e in committed.events],
            dropped=dropped,
        )

    # ---- reading the input ----

    def _someone_present(self) -> bool:
        """Whether anybody is in the room, which decides ambiguous input."""
        player = self.store.entity(PLAYER_ID)
        if player is None or not player.location_id:
            return False
        return any(
            e.kind == "person" and e.id != PLAYER_ID
            for e in self.store.contents(player.location_id)
        )

    def _heard_lately(self, rows) -> Heard:
        """The last few sentences the player was actually told.

        The near-duplicate filter works on sentences, not turns, so the window
        is the last six sentences wherever they came from — which is about two
        turns of narration, and the span the repetition actually happened over.
        """
        heard = Heard()
        for row in rows[-3:]:
            heard.remember(row["narration"])
        return heard

    @staticmethod
    def _as_event(event) -> str:
        """A fired timeline entry as the resolver reads it (spec 6.2)."""
        reaching = " — this one reaches the player directly" if event.reaches_player else ""
        return f"{event.what}{reaching}"

    def _answer(
        self,
        player_said: str,
        life: Life,
        on_text: Callable[[str], None] | None,
    ) -> TurnResult:
        """A question put to the narrator, not to the room (spec 3.1, 4).

        "What am I doing?" used to be resolved as an in-world action, so the
        character said those words out loud and the world answered that nobody
        replied. There is nothing here to settle, so there is no resolver call:
        the answer is in what this character already knows and perceives. The
        clock does not move and no fact is written.
        """
        rows = self.store.recent_turns(life.id, limit=6)
        answer = self.narrator.narrate(
            player_said,
            [],
            [],
            prompt_name="answer.md",
            recently_said=rows[-1]["narration"] if rows else None,
            max_sentences=A_QUESTION,
            on_text=on_text,
            declared=player_said,
            # Deliberately not filtered against what was said lately. An answer
            # to "where am I?" is allowed to be the same answer as last time,
            # and a filter that ate it left the question silent.
            heard=Heard(),
        )
        self.store.log_turn(
            life_id=life.id,
            player_said=player_said,
            intent="a question to the narrator",
            roll_json=None,
            narration=answer,
            mode=QUESTION,
        )
        return TurnResult(
            player_said=player_said,
            intent="a question to the narrator",
            narration=answer,
            duration_minutes=0,
            roll=None,
            summary="asked what they already knew",
            ends_life=False,
            mode=QUESTION,
        )

    # ---- the two beats ----

    def _narrate_attempt(
        self,
        player_said: str,
        mode: str,
        committed: _Committed,
        on_text: Callable[[str], None] | None,
    ) -> None:
        """The declared action as it begins, before the world has decided.

        This is the only call that sees the player's raw words, and here that
        is the point: the attempt *is* what they said they were doing. It runs
        before the resolver, so it cannot introduce anything new — there is
        nothing new yet — which is what keeps rules 2 and 5 true of it.
        """
        beat = self.narrator.narrate(
            player_said,
            [],
            [],
            prompt_name="attempt.md",
            # Deliberately not told what was said last turn. It has one
            # sentence to write and one thing to write about, and a live run
            # showed what the previous narration does in a context that thin:
            # asked for "I finish dressing the wound" it wrote back "You turn
            # in the driver's seat and ask Jamal what he said", which was the
            # turn before, word for word. The filter still knows about it —
            # `heard` spans turns — so the echo would be dropped and the beat
            # spent on a regeneration. Better not to put it in front of the
            # model at all.
            recently_said=None,
            max_sentences=ATTEMPT_SENTENCES,
            on_text=on_text,
            declared=player_said,
            said_aloud=player_said if mode == SPEECH else None,
            heard=committed.heard,
        )
        committed.beats.append(beat)

    def _commit_opening(
        self, opening: Opening, committed: _Committed, dropped: list[str]
    ) -> None:
        """Commit the opening. It no longer speaks.

        It used to narrate a beat of its own, and with the outcome not yet
        written that beat had nothing to say but the room — so it said the room
        again. The attempt covers the same seconds with something true.
        """
        # One transaction, so a write the store refuses partway through cannot
        # leave the clock advanced over a half-written world.
        with self.store.transaction():
            # The clock moves only by the resolver's duration (rule 7).
            self.store.advance(opening.duration_minutes)
            cause = f"turn: {opening.intent}"
            self._entities(opening.new_entities, dropped)
            landed = self._facts(opening.new_facts, cause, dropped)
            # The move goes before the state, because moving clears the state
            # that belonged to where they were.
            self._place_player(opening.player_location, dropped)
            committed.changed += self._reroute(opening.new_facts, cause, dropped)
            committed.changed += self._state(opening.state, cause, dropped)

            noticed = self._perceive(opening.perceived, dropped)
            noticed += self._notice_obvious(landed, noticed)
            for event in committed.events:
                self.store.mark_fired(event.id)
        committed.opening = opening
        committed.noticed = noticed

    def _commit_scene(
        self,
        scene: Scene,
        committed: _Committed,
        dropped: list[str],
        on_text: Callable[[str], None] | None,
        player_said: str,
    ) -> None:
        with self.store.transaction():
            cause = f"turn: {committed.opening.intent}"
            self._entities(scene.new_entities, dropped)
            landed = self._facts(scene.new_facts, cause, dropped)
            committed.changed += self._reroute(scene.new_facts, cause, dropped)
            committed.changed += self._state(scene.state, cause, dropped)
            noticed = self._perceive(scene.perceived, dropped)
            noticed += self._notice_obvious(landed, committed.noticed + noticed)
        committed.scene = scene
        committed.noticed += noticed
        if committed.opening.uncertain:
            return  # the outcome decides this beat, and it has not been rolled
        self._narrate(
            committed,
            committed.opening.intent,
            scene.dialogue,
            scene.you_said_aloud,
            on_text,
            declared=player_said,
            charged=bool(scene.dialogue or self._new_faces(scene)),
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
        declared: str | None = None,
        charged: bool = False,
        how_it_went: str | None = None,
    ) -> None:
        """The outcome beat: what happened, ending on what is different now."""
        spent = sum(len(_sentences_in(beat)) for beat in committed.beats)
        allowance = sentences_for(charged)
        if spent >= allowance:
            # The turn has already said its share. A beat with no allowance
            # left would be a model call that cannot be spoken, so it is not
            # made — the facts behind it are committed either way, and the next
            # turn has them.
            committed.outcome_told = True
            return
        beat = self.narrator.narrate(
            intent,
            dialogue,
            committed.noticed,
            said_aloud,
            already_said=committed.narration or None,
            recently_said=committed.said_before,
            max_sentences=allowance - spent,
            on_text=on_text,
            declared=declared,
            different_now=committed.different_now,
            how_it_went=how_it_went,
            heard=committed.heard,
        )
        committed.beats.append(beat)
        committed.outcome_told = True

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
                self._leave_behind(PLAYER_ID)
                return True
            except StoreError as exc:
                # Every other proposal in the turn is drained and reported this
                # way. This one escaped, which turned a nonsense location — the
                # player inside themselves — into a lost turn instead of a
                # dropped write.
                dropped.append(f"player_location {location_id}: {exc}")
        return False

    def _leave_behind(self, entity_id: str) -> list[str]:
        """Drop the transient state that belonged to where they just were.

        You cannot still be sitting on a bench you have walked away from. The
        model is asked to correct posture when it changes and mostly does, but
        a world that depends on it remembering is a world that freezes — which
        is exactly what happened, for twenty turns, in the last playtest. So
        code settles it: a move clears what was bound to the place (spec 2.1).
        Nothing is lost, because transient state is never history.
        """
        return self.store.clear_state(entity_id, PLACE_BOUND_STATE)

    def _state(self, changes, cause: str, dropped: list[str]) -> list[str]:
        """Commit transient state. Overwrites, so nothing is ever a duplicate.

        Returns each change in words, which is what the narrator closes the
        turn on and what the turn log keeps as the only history of it.
        """
        written = []
        for change in changes:
            try:
                self.store.set_state(
                    change.entity_id, change.key, change.value, cause
                )
            except StoreError as exc:
                dropped.append(f"state {change.entity_id}.{change.key}: {exc}")
                continue
            who = "you" if change.entity_id == PLAYER_ID else change.entity_id
            written.append(f"{who}: {change.key.replace('_', ' ')} is now {change.value}")
        return written

    def _apply_ledger(
        self, ledger, opening, scene, cause: str, dropped: list[str]
    ) -> list[str]:
        """Consequences the player did not perceive, committed after narration."""
        for problem in self.resolver.check_deltas(ledger, opening, scene):
            dropped.append(problem)
        rerouted = self._apply_writes(
            ledger.superseded, ledger.deltas, ledger.moves, ledger.learned,
            cause, dropped,
        )
        self._schedule(ledger.schedule, cause, dropped)
        return rerouted

    def _schedule(self, entries, cause: str, dropped: list[str]) -> None:
        """Put dated consequences on the clock (spec 6.2)."""
        for entry in entries:
            try:
                self.store.schedule(
                    self.store.now() + timedelta(minutes=entry.in_minutes),
                    entry.what,
                    cause,
                    entry.reaches_player,
                )
            except StoreError as exc:
                dropped.append(f"schedule {entry.what!r}: {exc}")

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

    def _reroute(self, new_facts, cause: str, dropped: list[str]) -> list[str]:
        """Write the keys that are state, not facts, as state (spec 2.1).

        Routing rather than dropping is deliberate. The prompt asks for these
        in `state` and mostly gets it; when it doesn't, the choice is between a
        world that moves and a world that freezes, and dropping is what froze
        it. It runs after the move for the same reason the move runs early: a
        posture written for where they have arrived must not be cleared by the
        arrival.
        """
        if not self.corrections.route_writes:
            # Raw mode: the model is taken at its word, so a posture written as
            # a fact lands as one and freezes there under rule 3. That is the
            # breakage this arm of the experiment is measuring, not a bug in
            # it — do not play a world you want to keep in raw.
            return []
        return self._state(
            [
                StateChange(
                    entity_id=f.entity_id, key=f.key, value=f.value
                )
                for f in new_facts
                if is_transient(f.key)
            ],
            cause,
            dropped,
        )

    def _facts(self, new_facts, cause: str, dropped: list[str]) -> list[Fact]:
        """Commit proposed facts. Returns the ones that actually landed.

        Transient keys are skipped here and written by `_reroute` instead, so
        nothing momentary ever reaches a table where rule 3 would freeze it.
        """
        landed = []
        for proposed in new_facts:
            if self.corrections.route_writes and is_transient(proposed.key):
                continue
            refusal = not_a_fact(proposed.key) if self.corrections.route_writes else None
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

    def _apply_outcome(self, outcome: Outcome, cause: str, dropped: list[str]) -> list[str]:
        """A rolled branch, which carries its own facts as well as its writes.

        Its transient state is not written here: the caller writes it after the
        player has been moved, because arriving somewhere clears the posture
        that belonged to where they left.
        """
        self._entities(outcome.new_entities, dropped)
        self._facts(outcome.new_facts, cause, dropped)
        return self._apply_writes(
            outcome.superseded, outcome.deltas, outcome.moves, outcome.learned,
            cause, dropped,
        )

    def _apply_writes(
        self, superseded, deltas, moves, learned, cause: str, dropped: list[str]
    ) -> list[str]:
        """Returns transient changes that arrived by the wrong channel."""
        rerouted = []
        for change in superseded:
            if self.corrections.route_writes and is_transient(change.key):
                # A supersession of something transient. There is nothing to
                # supersede — state keeps no history — so this is just the new
                # value, by the wrong door. A live turn put down a pair of
                # shears and the write was dropped as "no live 'in_hand' to
                # supersede", leaving them still in hand.
                rerouted += self._state([change], cause, dropped)
                continue
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
                continue
            moved = self.store.entity(move.entity_id)
            if moved is not None and moved.kind == "person":
                # The same rule as the player's own move: nobody is still
                # sitting where they used to be. Things are left alone, because
                # the bottle in your hand comes out of the bar with you.
                self._leave_behind(move.entity_id)

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

        return rerouted

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


def recent_premises(path, keep: int = 8) -> list[str]:
    """What the last few lives were about, so the next one isn't the same.

    Kept outside any world, because the point is variety across lives and each
    life is its own world file. Missing file means no history, which is the
    first run.
    """
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip()][
        -keep:
    ]


def remember_premise(path, premise: str) -> None:
    if not premise.strip():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(premise.strip().replace("\n", " ") + "\n")


def begin_life(
    store: Store,
    resolver: Resolver,
    narrator: Narrator,
    spawn_city: str,
    rng: random.Random,
    on_text: Callable[[str], None] | None = None,
    premises_path=None,
    corrections: Corrections | None = None,
) -> tuple[Life, str]:
    """Spawn a character and narrate the opening (build step 4, spec 9).

    Code creates the city and the player shell so the model cannot get their
    ids wrong, and draws how comfortable the life is before asking, then the
    resolver fills in the person and their situation.

    A life now opens in the middle of something: what it is already under goes
    on the clock (spec 6.2) and two to four things about it are committed as
    hidden facts before the player can look, so digging finds something that
    was there rather than something improvised on the spot.
    """
    # Idempotent: a spawn that failed on the model call leaves these two behind
    # with no life started, and without this the world could never be opened
    # again.
    if store.entity(SPAWN_PLACE_ID) is None:
        store.add_entity(SPAWN_PLACE_ID, "place", spawn_city)
    if store.entity(PLAYER_ID) is None:
        store.add_entity(PLAYER_ID, "person", "you", location_id=SPAWN_PLACE_ID)

    avoid = recent_premises(premises_path) if premises_path else []
    spawned = resolver.spawn(draw_station(rng), avoid)
    dropped: list[str] = []
    turn = Turn(store, resolver, narrator, rng, corrections)
    noticed = []

    # A spawn is one world or none of it: half a life is not playable.
    with store.transaction():
        turn._entities(spawned.new_entities, dropped)
        turn._facts(spawned.new_facts, "spawn", dropped)
        # Settled before the player can look at anything (step 3a). Visibility
        # is already forced to `hidden`, so none of these reach the opening
        # narration or the sheet.
        turn._facts(spawned.secrets, "spawn: what this life is sitting on", dropped)

        # Stand them somewhere specific, so the whole town isn't in the room.
        if spawned.player_starts_in and store.entity(spawned.player_starts_in):
            store.move(PLAYER_ID, spawned.player_starts_in)

        turn._reroute(spawned.new_facts, "spawn", dropped)
        turn._state(spawned.state, "spawn", dropped)

        # The pressure the life opens under, dated so it actually arrives. A
        # deadline that is only described is a deadline the world never keeps.
        turn._schedule(spawned.schedule, f"spawn: {spawned.opening_situation}", dropped)

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
        on_text=on_text, max_sentences=QUIET_TURN,
    )
    store.log_turn(
        life.id, "", "spawn", None, narration,
        mode="spawn",
        state_json=json.dumps(spawned.opening_situation),
    )
    if premises_path:
        remember_premise(premises_path, spawned.premise or spawned.opening_situation)
    return life, narration
