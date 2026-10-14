"""Time to the first word, which is the only latency number that matters.

Measured on a real session, a turn took 9.1s before the player heard anything:
the resolver's first object was 68-83% of the whole call (6 of 6 streamed
calls), and the narrator then blocked until its last token. Speech is slower
than generation — 48 output tokens is about 14 seconds of audio against a 2-3s
tail — so everything after the first word already hides underneath it.

So the engine optimises one thing: how little has to exist before someone
speaks. These tests hold that shape in place. They assert ordering and size,
never wall-clock, because wall-clock here is provider noise.
"""

from __future__ import annotations

import json
import random

import pytest

from worldsim.budget import estimate_tokens
from worldsim.llm import FakeLLM
from worldsim.render import Narrator, _split_sentence
from worldsim.resolve import PLAYER_ID, Ledger, Opening, Resolver, Scene
from worldsim.turn import Turn, begin_life
from worldsim.__main__ import PROMPTS_DIR
from helpers import IN_MOTION_SCHEDULE, IN_MOTION_SECRETS, reply, scripts

SPAWN = json.dumps(
    {
        "new_entities": [
            {"id": "place:kitchen", "kind": "place", "name": "the line", "location_id": "place:spawn-city"}
        ],
        "new_facts": [
            {"entity_id": "person:you", "key": "name", "value": "Dale Rusk", "source": "invented", "visibility": "obvious"}
        ],
        "player_starts_in": "place:kitchen",
        "secrets": IN_MOTION_SECRETS,
        "schedule": IN_MOTION_SCHEDULE,
        "opening_situation": "rent is late",
    }
)

# A turn shaped the way resolver.md now asks for: the room in the opening, the
# person in it in the scene, the money in the ledger.
WALK_IN = (
    json.dumps(
        {
            "intent": "walk into the tavern",
            "duration_minutes": 11,
            "player_location": "place:tavern",
            "new_entities": [["place:tavern", "place", "The Double Luck", "place:spawn-city"]],
            "new_facts": [["place:tavern", "smell", "stale beer and fryer oil"]],
        }
    )
    + json.dumps(
        {
            "summary": "you arrive and Gary looks up",
            "you_said_aloud": "asks for a beer",
            "new_entities": [["person:gary", "person", "Gary", "place:tavern"]],
            "new_facts": [["person:gary", "beard", "graying, needs a trim"]],
            "dialogue": [{"speaker_id": "person:gary", "gist": "what'll it be"}],
        }
    )
    + json.dumps({"deltas": [{"entity_id": "person:you", "key": "cash_usd", "delta": -6}]})
)


class Timeline(FakeLLM):
    """Records the order of stream events across both roles."""

    def __init__(self, config, call_log, scripts):
        super().__init__(config, call_log, scripts)
        self.events: list[str] = []

    def _raw_stream(self, role, model, system, user):
        self.events.append(f"{role}:start")
        for chunk in super()._raw_stream(role, model, system, user):
            self.events.append(f"{role}:chunk")
            yield chunk
        self.events.append(f"{role}:end")


def build(store, config, call_log, resolver_script, narrator_script):
    llm = Timeline(
        config, call_log, scripts(resolver_script, narrator_script)
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    return llm, resolver, narrator, Turn(store, resolver, narrator, random.Random(7))


# ---- the opening is small, and it is what the player waits on ----


def test_the_opening_carries_only_what_lands_first():
    """The shape of the win: the first object is a fraction of the reply.

    If a change ever pushes entities, dialogue or the rest of the facts back
    into the opening, time-to-first-word goes with it.
    """
    opening, scene, ledger = (json.loads(part) for part in _objects(WALK_IN))

    assert estimate_tokens(opening) < estimate_tokens(scene) + estimate_tokens(ledger)
    # The room, not the people in it.
    assert "person:gary" not in json.dumps(opening)
    assert "dialogue" not in opening and "deltas" not in opening
    # And it is the whole of what the narrator needs to say something true.
    assert opening["player_location"] and opening["new_facts"]


def test_the_opening_fields_a_narration_needs_are_all_present():
    """Object one has to stand alone, because it is spoken before the rest."""
    required = {"intent", "duration_minutes", "player_location"}
    assert required <= set(Opening.model_fields)
    # And the things that would bloat it are somewhere else.
    for deferred in ("dialogue", "summary", "you_said_aloud", "scene_change"):
        assert deferred not in Opening.model_fields
        assert deferred in Scene.model_fields or deferred in Ledger.model_fields


# ---- the narrator speaks before the resolver has finished ----


def test_the_voice_starts_while_the_resolver_is_still_writing(store, config, call_log):
    llm, resolver, narrator, turn = build(
        store, config, call_log,
        [SPAWN, WALK_IN],
        ["Oil ticks as the fryer cools.", "Stale beer and fryer oil.", "Gary looks up."],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    llm.events.clear()

    turn.play("I walk into the tavern and ask for a beer", life)

    first_voice = llm.events.index("narrator:start")
    assert "resolver:chunk" in llm.events[first_voice:], (
        "the narrator waited for the whole resolver call"
    )
    # And it happened on the *first* object, not the second.
    assert llm.events[:first_voice].count("narrator:start") == 0


def test_a_risky_turn_speaks_before_the_roll(store, config, call_log):
    """Rolls used to sit in silence for the entire call.

    The opening holds what is true either way — the walk up, the weight of the
    bottle — so it can be spoken while the branch is still being written. Only
    the outcome waits for code to roll (rule 6).
    """
    risky = (
        json.dumps(
            {
                "intent": "pocket the bottle and walk out",
                "duration_minutes": 2,
                "player_location": "place:kitchen",
                "uncertain": True,
                "new_facts": [["place:kitchen", "cooler_hum", "the cooler kicks on"]],
            }
        )
        + json.dumps({"summary": "you make your move"})
        + json.dumps(
            {
                "roll": {
                    "p_success": 0.5,
                    "why": "he is half watching",
                    "on_success": {"summary": "out clean", "player_location": "place:spawn-city"},
                    "on_failure": {"summary": "he catches you", "player_location": "place:kitchen"},
                }
            }
        )
    )
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, risky],
        ["Oil ticks.", "The cooler kicks on behind you.", "The door swings shut."],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    llm.events.clear()

    result = turn.play("I pocket the bottle and walk out", life)

    assert result.roll is not None
    first_voice = llm.events.index("narrator:start")
    assert "resolver:chunk" in llm.events[first_voice:]
    # Both beats are in the record: the approach, then the outcome.
    assert "The cooler kicks on behind you." in result.narration
    assert "The door swings shut." in result.narration
    # And the narration still gives nothing away about the roll.
    for tell in ("roll", "odds", "chance", "0.5", "50%"):
        assert tell not in result.narration.lower()


# ---- two beats, not one block ----


def test_the_second_beat_is_told_what_the_first_already_said(store, config, call_log):
    llm, resolver, narrator, turn = build(
        store, config, call_log,
        [SPAWN, WALK_IN],
        ["Oil ticks.", "Stale beer and fryer oil.", "Gary looks up from the counter."],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    result = turn.play("I walk into the tavern", life)

    prompts = [user for role, _, user in llm.prompts if role == "narrator"]
    assert len(prompts) == 3, "spawn, then one beat per object that had something new"
    assert '"already_told_you":null' in prompts[1]
    assert "Stale beer and fryer oil." in prompts[2]
    assert result.narration == "Stale beer and fryer oil. Gary looks up from the counter."


def test_a_turn_is_two_beats_and_no_more(store, config, call_log):
    """Cost control: a turn makes exactly two narrator calls (spec 3.1).

    The attempt, spoken before the resolver is called at all, and the outcome,
    spoken when the scene closes. There used to be a beat per object with a
    guard on each, and the guard on the opening was the repetition the player
    reported: with nothing new to say it said the room again.
    """
    through_the_door = (
        json.dumps(
            {
                "intent": "step out into the alley",
                "duration_minutes": 1,
                "player_location": "place:alley",
                "new_entities": [["place:alley", "place", "the alley", "place:spawn-city"]],
                "new_facts": [["place:alley", "air", "cold, and it smells of rain"]],
            }
        )
        + json.dumps({"summary": "you step out"})
        + json.dumps({})
    )
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, through_the_door],
        [
            "Oil ticks.",
            "You push the back door open.",
            "Cold air, and the smell of rain off the asphalt.",
        ],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    result = turn.play("I step out back", life)

    prompts = [user for role, _, user in llm.prompts if role == "narrator"]
    assert len(prompts) == 3, "the spawn, then the attempt and the outcome"
    assert result.narration == (
        "You push the back door open. Cold air, and the smell of rain off the asphalt."
    )
    assert store.entity(PLAYER_ID).location_id == "place:alley"
    # The attempt is written before anything is resolved, so it can only have
    # been given what the character already knew: no alley, no rain.
    assert "alley" not in prompts[1]


# ---- every turn says something ----


def test_a_turn_that_settles_nothing_still_speaks(store, config, call_log):
    """A turn that settles nothing and goes nowhere still has to answer.

    Paying for the beer already in your hand: no new fact, no move, no roll.
    The outcome beat is the floor, and it fires whether or not the scene gave
    it anything.
    """
    nothing = reply(player_location="place:kitchen", summary="you pay")
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, nothing],
        ["Oil ticks.", "You count out the bills.", "Coins go across the counter."],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    result = turn.play("I pay for the beer", life)

    assert result.narration == "You count out the bills. Coins go across the counter."


# ---- sentence gating ----


@pytest.mark.parametrize(
    "text, first, rest",
    [
        ("One. Two.", "One.", "Two."),
        ('He stops. "Not tonight," he says.', "He stops.", '"Not tonight," he says.'),
        ("You owe $18.50. Mr. Brackett waits by the door.", "You owe $18.50.", "Mr. Brackett waits by the door."),
        ("No end yet", None, "No end yet"),
    ],
)
def test_sentences_are_split_where_a_voice_would_pause(text, first, rest):
    got, left = _split_sentence(text)
    assert got == first
    assert left.strip() == rest


def test_sentences_are_delivered_as_they_clear(store, config, call_log):
    """Each one goes out the moment it is checked, not at the end."""
    llm = FakeLLM(
        config, call_log,
        scripts([SPAWN], ["The door gives. Stale beer inside. Gary looks up."]),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    spoken: list[str] = []

    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7), on_text=spoken.append)

    assert spoken == ["The door gives.", "Stale beer inside.", "Gary looks up."]


def test_a_breach_in_the_first_sentence_is_caught_before_it_is_spoken(store, config, call_log):
    llm = FakeLLM(
        config, call_log,
        scripts([SPAWN], ["You say you'll have a beer. The glass lands.", "The glass lands on the bar."]),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    spoken: list[str] = []

    _, opening = begin_life(
        store, resolver, narrator, config.spawn_city, random.Random(7), on_text=spoken.append
    )

    # Nothing from the breaching attempt reached the player at all.
    assert all("You say" not in line for line in spoken)
    assert opening == "The glass lands on the bar."
    assert narrator.breaches == []


def test_a_breach_later_on_truncates_instead_of_regenerating(store, config, call_log):
    """The earlier sentences are already spoken, so they cannot be taken back.

    Stopping is allowed — "stop when the world stops" is the contract — and it
    beats either repeating the breach or speaking over it.
    """
    llm = FakeLLM(
        config, call_log,
        scripts([SPAWN], ["The glass lands on the bar. You decide to drink it."]),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    spoken: list[str] = []

    _, opening = begin_life(
        store, resolver, narrator, config.spawn_city, random.Random(7), on_text=spoken.append
    )

    assert spoken == ["The glass lands on the bar."]
    assert opening == "The glass lands on the bar."
    assert narrator.breaches == ["You decide cut the narration short"]


# ---- the instrumentation that makes the next decision measurable ----


def test_each_object_and_the_first_token_are_timed(store, config, call_log):
    """Six real streamed calls could not separate prefill from generation.

    Without that split, "fewer input tokens" and "fewer output tokens" are
    indistinguishable guesses, so both are recorded per call now.
    """
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, WALK_IN],
        ["Oil ticks.", "Stale beer.", "Gary looks up."],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    turn.play("I walk in", life)

    streamed = [r for r in call_log.records if r.object_seconds]
    assert streamed, "a streamed resolver call should record per-object timings"
    record = streamed[-1]
    assert len(record.object_seconds) == 3
    assert record.object_seconds == sorted(record.object_seconds)
    assert record.scene_seconds == record.object_seconds[0]
    assert record.ttft_seconds is not None
    assert record.ttft_seconds <= record.object_seconds[0]
    # Object sizes travel with the timings, so cost per phase is readable.
    assert len(record.object_tokens) == 3
    assert record.object_tokens[0] < sum(record.object_tokens[1:])

    narrated = [r for r in call_log.records if r.role == "narrator"]
    assert all(r.ttft_seconds is not None for r in narrated)


def _objects(reply_text: str) -> list[str]:
    """Split a scripted three-object reply, the way the stream splitter does."""
    from worldsim.llm import ObjectScanner, json_start

    out, cursor = [], 0
    while cursor < len(reply_text):
        start = json_start(reply_text[cursor:])
        if start is None:
            break
        cursor += start
        end = ObjectScanner().feed(reply_text[cursor:])
        if end is None:
            break
        out.append(reply_text[cursor : cursor + end])
        cursor += end
    return out


# ---- the instructions are most of the input, so they get a ceiling ----


def test_the_instructions_stay_small():
    """The system prompt is most of what a call has to prefill, every turn.

    The ceiling was first set at 1900 on the theory that prefill was what the
    player waited on. The 30-turn run measured the resolver's first token at
    0.8s against a 2,800-token input, so that theory was wrong: generation of
    the first object is the wait, and input tokens are nearly free in latency
    terms. The limits stay anyway, for the two reasons that survive — input is
    billed on every single turn, and a prompt that grows without bound dilutes
    the instructions that matter. They are a budget to spend deliberately, not
    a line that must never move: raising one is a decision, and it belongs in
    the same commit as the thing that earned it.
    """
    sizes = {
        path.name: estimate_tokens(path.read_text())
        for path in PROMPTS_DIR.glob("*.md")
    }
    # Raised in the feel pass, deliberately, and this is what earned it.
    # resolver.md: the fact/state split, what an action leaves different, the
    # input mode, the timeline, cut/linger/thread. narrator.md: the narration
    # contract rewritten. spawn.md: secrets, the clock, varied premises. About
    # 0.1c a turn, against a 2c budget, and the alternative was leaving the
    # playtest findings unfixed.
    assert sizes["resolver.md"] < 2700, sizes
    # narrator.md was raised a second time, in the same pass, for the one
    # defect the first playtest of it found: the outcome beat re-narrating the
    # attempt. Worth a hundred tokens, because the player notices it first.
    assert sizes["narrator.md"] < 1450, sizes
    assert sizes["spawn.md"] < 2600, sizes
    # The two beats of a turn have their own prompts, and both stay small on
    # purpose: the attempt is one sentence and an answer is one line.
    assert sizes["attempt.md"] < 600, sizes
    assert sizes["answer.md"] < 600, sizes
    # The opening narration is a one-off at spawn, so it can afford to be long,
    # but nothing should quietly become the biggest file.
    assert max(sizes.values()) == sizes["resolver.md"], sizes


def test_a_context_is_sent_compactly():
    """Pretty-printing cost 39% more input tokens for whitespace nobody reads."""
    from worldsim.budget import as_prompt

    payload = {"a": [1, 2], "b": {"c": "d"}}
    assert as_prompt(payload) == '{"a":[1,2],"b":{"c":"d"}}'
    assert "\n" not in as_prompt(payload)


# ---- the second beat must not say the first beat's lines again ----


def test_a_repeated_sentence_never_reaches_the_player(store, config, call_log):
    """`already_told_you` asks; this enforces.

    A live turn answered "what's in this kitchen?" by narrating the kitchen
    twice, word for word: the model had the first beat in its prompt and
    repeated it anyway. Streaming means a sentence cannot be taken back once
    spoken, so the check happens before it goes out.
    """
    with_gary = (
        json.dumps(
            {
                "intent": "walk into the tavern",
                "duration_minutes": 11,
                "player_location": "place:tavern",
                "new_entities": [["place:tavern", "place", "The Double Luck", "place:spawn-city"]],
                "new_facts": [["place:tavern", "smell", "stale beer"]],
            }
        )
        + json.dumps(
            {
                "summary": "Gary is behind the bar",
                "new_entities": [["person:gary", "person", "Gary", "place:tavern"]],
                "dialogue": [{"speaker_id": "person:gary", "gist": "what'll it be"}],
            }
        )
        + json.dumps({})
    )
    llm = FakeLLM(
        config, call_log,
        {
            "resolver": [SPAWN, with_gary],
            "narrator": [
                "Oil ticks.",
                "Stale beer and fryer oil. The television is on with the sound off.",
                # The second beat repeats the first and adds one new line.
                "Stale beer and fryer oil. Gary looks up from the counter.",
            ],
        },
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    spoken: list[str] = []

    result = turn.play("I walk into the tavern", life, on_text=spoken.append)

    assert spoken.count("Stale beer and fryer oil.") == 1, spoken
    assert "Gary looks up from the counter." in spoken
    assert result.narration.count("Stale beer and fryer oil.") == 1


def test_more_facts_about_the_same_room_do_not_earn_a_second_beat(store, config, call_log):
    """Only speech or a new face does. The room was already described."""
    more_kitchen = (
        json.dumps(
            {
                "intent": "look around properly",
                "duration_minutes": 1,
                "player_location": "place:kitchen",
                "new_facts": [["place:kitchen", "counters", "clean laminate"]],
            }
        )
        + json.dumps(
            {
                "summary": "you take the room in",
                "new_facts": [["place:kitchen", "cabinets", "wooden, above and below"]],
            }
        )
        + json.dumps({})
    )
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, more_kitchen],
        [
            "Oil ticks.",
            "You turn and take the room in.",
            "Clean laminate, and wooden cabinets above the sink.",
        ],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    turn.play("I look around the kitchen properly", life)

    prompts = [u for role, _, u in llm.prompts if role == "narrator"]
    assert len(prompts) == 3, "the kitchen was described once, not twice"
    # Both of the turn's facts landed, and both reach the one outcome beat
    # rather than earning a beat each.
    assert store.live_fact("place:kitchen", "cabinets") is not None
    assert "cabinets" in prompts[2] and "counters" in prompts[2]


def test_a_paraphrase_of_something_already_said_is_dropped(store, config, call_log):
    """Finding 5: exact repeats were caught, paraphrases were not.

    Tuned on the pair the 30-turn run left open. Sentences that genuinely say
    different things about the same room score well under the threshold, so the
    filter does not eat the narration.
    """
    from worldsim.render import Heard

    heard = Heard(["The fluorescent light above flickers."])
    assert heard.is_repeat("The fluorescent light above the sink flickers.")
    assert heard.is_repeat("the fluorescent light above flickers")  # and exact ones
    assert not heard.is_repeat("The fridge hums, and the window is dark.")
    assert not heard.is_repeat("Gary sets a pint down without a word.")

    # Both of these went out live, one beat after the other, in the first
    # playtest of the attempt-then-outcome shape. The first is a strict subset
    # of what was already said, and scores under the threshold because the
    # longer sentence dilutes the overlap. The second differed by `circling`
    # against `circle` alone, which is why the comparison is stemmed.
    said = Heard(
        [
            "You turn in the driver's seat and look back through the cab at the gurney.",
            "You open the car door and step out onto the pavement, circling the"
            " vehicle with your camera in hand.",
        ]
    )
    assert said.is_repeat("You look back through the cab at the gurney.")
    assert said.is_repeat(
        "You step out onto the pavement and circle the vehicle, camera in hand."
    )
    # And the sentences those turns should have had instead are left alone.
    assert not said.is_repeat("The metal gives a dull pop under your weight.")
    assert not said.is_repeat("Papers, a manual, a small flashlight.")

    # Only the last few sentences are remembered, so a place can be described
    # again on a later visit.
    heard = Heard([f"Sentence number {n} about nothing." for n in range(20)])
    assert len(heard.said) == 6


def test_a_turn_never_runs_past_its_allowance(store, config, call_log):
    """Length follows the moment (spec 4), and the voice ceiling is hard.

    Two beats of "two to four sentences" made six-sentence turns in a live run.
    The allowance is spent across the beats, and the gate stops the outcome
    when it runs out.
    """
    with_gary = (
        json.dumps(
            {
                "intent": "walk into the tavern",
                "duration_minutes": 11,
                "player_location": "place:tavern",
                "new_entities": [["place:tavern", "place", "The Double Luck", "place:spawn-city"]],
                "new_facts": [["place:tavern", "smell", "stale beer"]],
            }
        )
        + json.dumps(
            {
                "summary": "Gary is behind the bar",
                "new_entities": [["person:gary", "person", "Gary", "place:tavern"]],
                "dialogue": [{"speaker_id": "person:gary", "gist": "what'll it be"}],
            }
        )
        + json.dumps({})
    )
    llm = FakeLLM(
        config, call_log,
        scripts(
            [SPAWN, with_gary],
            [
                "Oil ticks.",
                "You push through the door. It swings back behind you.",
                "Gary looks up. He sets down the rag. He waits. The rail is three deep."
                " Somebody laughs at the far end. A television mutters.",
            ],
        ),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    spoken: list[str] = []

    result = turn.play("I walk in", life, on_text=spoken.append)

    from worldsim.render import sentences_in
    from worldsim.turn import ATTEMPT_SENTENCES, CHARGED_TURN

    # Gary speaks, so this is a charged turn and gets the whole ceiling — and
    # not a sentence past it, because the ceiling is what a voice can hold.
    assert len(sentences_in(result.narration)) <= CHARGED_TURN, result.narration
    assert len(spoken) <= CHARGED_TURN, spoken
    # The attempt is one sentence, whatever the model sends.
    assert spoken[:1] == ["You push through the door."]
    assert len(sentences_in(result.narration)) > ATTEMPT_SENTENCES


def test_dedup_never_silences_a_turn(store, config, call_log):
    """Silence is worse than a repeat.

    A live turn asking "how much money have I got left?" came back completely
    blank: the narrator restated the previous turn and the duplicate filter ate
    every sentence of it.
    """
    nothing_new = reply(player_location="place:kitchen", summary="you check")
    llm = FakeLLM(
        config, call_log,
        {
            "resolver": [SPAWN, nothing_new],
            # The turn, and then the regeneration, are both word for word the
            # narration before them. Only then does the repeat go out.
            "narrator": [
                "The fryer is still ticking.",
                "The fryer is still ticking.",
                "The fryer is still ticking.",
            ],
        },
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    spoken: list[str] = []

    result = turn.play("How much money have I got left?", life, on_text=spoken.append)

    assert result.narration == "The fryer is still ticking."
    assert spoken == ["The fryer is still ticking."]


def test_a_null_list_is_read_as_an_empty_one(store, config, call_log):
    """`"dialogue": null` took a whole scene and its ledger down live."""
    from worldsim.resolve import Ledger, Opening, Scene

    assert Scene.model_validate_json('{"summary":"x","dialogue":null}').dialogue == []
    assert Ledger.model_validate_json('{"deltas":null,"moves":null}').deltas == []
    nulled = Opening.model_validate_json(
        '{"intent":"x","duration_minutes":1,"player_location":"p","new_facts":null}'
    )
    assert nulled.new_facts == []
