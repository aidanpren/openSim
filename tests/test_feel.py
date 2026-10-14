"""The feel pass: milestone 1b's done-when list, held by tests.

Four things that are checked here and nowhere else:

- transient state is current state, overwritten freely and cleared by a move
  (spec 2.1)
- a question to the narrator is answered from knowledge, with no roll, no time
  and no new facts (spec 3.1)
- a turn is the attempt, the outcome, and what is different now (spec 4)
- a near miss reads differently from a clean success (spec 3.2)

Nothing here touches the network.
"""

from __future__ import annotations

import json
import random

import pytest

from worldsim.llm import FakeLLM
from worldsim.modes import ACTION, LOOK, QUESTION, SPEECH, classify
from worldsim.render import Heard, Narrator, narrated_the_player, sentences_in
from worldsim.resolve import (
    PLACE_BOUND_STATE,
    PLAYER_ID,
    Opening,
    Resolver,
    margin_band,
)
from worldsim.turn import A_QUESTION, RollRecord, Turn, begin_life, recent_premises
from worldsim.__main__ import PROMPTS_DIR
from helpers import IN_MOTION_SCHEDULE, IN_MOTION_SECRETS, reply, scripts

SPAWN = json.dumps(
    {
        "new_entities": [
            {"id": "place:park", "kind": "place", "name": "Liberty Square Park", "location_id": "place:spawn-city"},
            {"id": "place:kitchen", "kind": "place", "name": "your kitchen", "location_id": "place:spawn-city"},
        ],
        "new_facts": [
            {"entity_id": "person:you", "key": "name", "value": "Dale Rusk", "source": "invented", "visibility": "obvious"},
            {"entity_id": "person:you", "key": "cash_usd", "value": 39.5, "source": "invented", "visibility": "noticeable"},
            {"entity_id": "place:spawn-city", "key": "timezone", "value": "America/Chicago", "source": "world-knowledge", "visibility": "obvious"},
        ],
        "secrets": IN_MOTION_SECRETS,
        "schedule": IN_MOTION_SCHEDULE,
        "state": [["person:you", "posture", "standing at the counter"]],
        "player_starts_in": "place:park",
        "opening_situation": "the van has to be back at the yard by six",
        "premise": "a mover with somebody else's van",
    }
)


def build(store, config, call_log, resolver_script, narrator_script, seed=7):
    llm = FakeLLM(config, call_log, scripts(resolver_script, narrator_script))
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    return llm, resolver, narrator, Turn(store, resolver, narrator, random.Random(seed))


def start(store, config, call_log, resolver_script, narrator_script):
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN] + resolver_script, narrator_script
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    return llm, resolver, narrator, turn, life


# ---- step 1: transient state (spec 2.1) ----


def test_sitting_on_a_bench_does_not_follow_you_home(store, config, call_log):
    """The top finding of the 30-turn playtest, closed.

    `posture = "sitting on a bench"` was settled as a fact, and because a
    second live fact for a key is refused rather than treated as a change, the
    character was still sitting on that bench after walking home — and nothing
    could correct it.
    """
    sit_down = reply(
        intent="sit down on a bench",
        player_location="place:park",
        summary="you sit",
        state=[["person:you", "posture", "sitting on a bench"]],
    )
    walk_home = reply(
        intent="walk home",
        player_location="place:kitchen",
        summary="you get home",
    )
    llm, resolver, narrator, turn, life = start(
        store, config, call_log,
        [sit_down, walk_home],
        ["Oil ticks.", "You lower yourself onto it.", "The slats are warm."],
    )

    turn.play("I find a bench and sit down", life)
    assert store.state_of(PLAYER_ID, "posture") == "sitting on a bench"
    # No history, no supersession, and above all no fact.
    assert store.live_fact(PLAYER_ID, "posture") is None

    turn.play("I get up and walk home", life)

    assert store.entity(PLAYER_ID).location_id == "place:kitchen"
    assert store.state_of(PLAYER_ID, "posture") is None, "you walked away from it"
    # And the narrator is told the truth of the moment, not the old one.
    last = [u for role, _, u in llm.prompts if role == "narrator"][-1]
    assert "sitting on a bench" not in last


def test_climbing_onto_the_table_and_back_down_both_commit(store, config, call_log):
    """The example the player gave, both directions (step 1's second box)."""
    up = reply(
        intent="jump onto the table",
        player_location="place:park",
        summary="you get up onto it",
        state=[
            ["person:you", "posture", "standing on the table"],
            ["person:you", "vantage", "you can see over the crowd to the back door"],
        ],
    )
    down = reply(
        intent="get down off the table",
        player_location="place:park",
        summary="you step down",
        state=[["person:you", "posture", "standing on the floor"]],
    )
    llm, resolver, narrator, turn, life = start(
        store, config, call_log,
        [up, down],
        ["Oil ticks.", "You haul yourself up.", "From up here the room is wider."],
    )

    first = turn.play("I jump on the table", life)
    assert store.state_of(PLAYER_ID, "posture") == "standing on the table"
    # The payoff reaches the narrator, which is what makes the action worth it.
    assert any("vantage" in line for line in first.changed_state)
    outcome = [u for role, _, u in llm.prompts if role == "narrator"][-1]
    assert "over the crowd" in outcome

    second = turn.play("I get down", life)
    assert store.state_of(PLAYER_ID, "posture") == "standing on the floor"
    assert any("posture" in line for line in second.changed_state)
    # Getting down did not move them, so the vantage is the resolver's to
    # correct; what matters is that neither value was ever refused.
    assert not any("already has a live" in item for item in second.dropped)


def test_no_transient_value_is_ever_refused_as_a_duplicate(store, config, call_log):
    """Twenty of the thirty dropped writes in the playtest were this."""
    again = reply(
        player_location="place:park",
        summary="you shift",
        state=[
            ["person:you", "posture", "sitting"],
            ["person:you", "posture", "sitting forward now"],
            ["person:you", "posture", "sitting back again"],
        ],
    )
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [again],
        ["Oil ticks.", "You shift on the bench.", "The slats creak."],
    )

    result = turn.play("I shift around on the bench", life)

    assert not result.dropped, result.dropped
    assert store.state_of(PLAYER_ID, "posture") == "sitting back again"


def test_a_supersede_of_something_transient_is_just_the_new_value(
    store, config, call_log
):
    """Found live. There is no history to supersede, so it is the new value.

    A florist put her shears down on the work table and the write was dropped
    as "no live 'in_hand' to supersede", leaving them in her hand for the rest
    of the session.
    """
    put_down = reply(
        {"superseded": [["person:you", "in_hand", "nothing"]]},
        player_location="place:park",
        summary="you set them down",
    )
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [put_down],
        ["Oil ticks.", "You set them down.", "The table is clear."],
    )
    store.set_state(PLAYER_ID, "in_hand", "a pair of floral shears", "a test")

    result = turn.play("I put the shears down", life)

    assert store.state_of(PLAYER_ID, "in_hand") == "nothing"
    assert not any("in_hand" in item for item in result.dropped), result.dropped
    assert any("in hand" in line for line in result.changed_state)


def test_the_turn_log_keeps_what_state_changed(store, config, call_log):
    """Transient state has no history of its own, so the log is the history."""
    shift = reply(
        player_location="place:park",
        summary="you stand",
        state=[["person:you", "posture", "on your feet"]],
    )
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [shift],
        ["Oil ticks.", "You stand.", "The bench creaks behind you."],
    )

    turn.play("I stand up", life)

    row = store.recent_turns(life.id, limit=1)[0]
    assert "on your feet" in row["state_json"]
    assert json.loads(row["state_json"])  # readable, not just present


def test_what_is_in_your_hand_comes_with_you(store, config, call_log):
    """The boundary of the clearing rule, which is the part worth testing.

    Posture belongs to the place; the bottle does not. A move clears the first
    and leaves the second, or walking out of a bar would empty your hands.
    """
    assert "posture" in PLACE_BOUND_STATE
    assert "in_hand" not in PLACE_BOUND_STATE

    take_it = reply(
        player_location="place:park",
        summary="you pick it up",
        state=[
            ["person:you", "posture", "leaning on the rail"],
            ["person:you", "in_hand", "a half-finished bottle"],
        ],
    )
    leave = reply(player_location="place:kitchen", summary="you go home")
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [take_it, leave],
        ["Oil ticks.", "You pick it up.", "It is still cold."],
    )

    turn.play("I pick up the bottle", life)
    turn.play("I walk home", life)

    assert store.state_of(PLAYER_ID, "posture") is None
    assert store.state_of(PLAYER_ID, "in_hand") == "a half-finished bottle"


# ---- step 2a: input modes (spec 3.1) ----


@pytest.mark.parametrize(
    "said, someone_here, mode",
    [
        ("What am I doing?", False, QUESTION),
        ("What am I doing?", True, QUESTION),  # never a line to the bartender
        ("Where am I?", False, QUESTION),
        ("What's in my pocket?", False, QUESTION),
        ("How much money do I have?", True, QUESTION),
        ("Am I bleeding?", False, QUESTION),
        ("What's nearby?", False, LOOK),
        ("How much for a beer?", True, SPEECH),
        ('"Evening, Gary."', True, SPEECH),
        ("I jump on the table", False, ACTION),
        ("I ask the bartender what time it is", True, ACTION),
        ("How about I head down to the bar and see if Gary is there tonight", True, ACTION),
    ],
)
def test_input_is_read_on_the_right_channel(said, someone_here, mode):
    assert classify(said, someone_here) == mode


def test_a_question_is_answered_without_resolving_anything(store, config, call_log):
    """"What am I doing?" used to be spoken aloud to an empty room.

    The world answered honestly — "your voice echoes off the walls to no return
    in answer" — because the engine was faithful to the wrong channel.
    """
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [],
        ["Oil ticks.", "You're standing at the counter with nothing in your hands."],
    )
    before = store.now()
    facts_before = len(store.live_facts(PLAYER_ID))

    result = turn.play("What am I doing?", life)

    assert result.mode == QUESTION
    assert result.duration_minutes == 0
    assert store.now() == before, "a question costs no time"
    assert len(store.live_facts(PLAYER_ID)) == facts_before, "and settles nothing"
    assert result.roll is None
    assert len(sentences_in(result.narration)) <= A_QUESTION
    # No resolver call was made at all: there was nothing for one to settle.
    assert [role for role, _, _ in llm.prompts].count("resolver") == 1  # the spawn


def test_a_question_is_answered_from_the_characters_own_state(store, config, call_log):
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [], ["Oil ticks.", "You are on your feet."]
    )
    store.set_state(PLAYER_ID, "activity", "counting the till", "a test")

    turn.play("What am I doing?", life)

    asked = [u for role, _, u in llm.prompts if role == "narrator"][-1]
    assert "counting the till" in asked
    assert "a question to the narrator" not in asked  # that label is for the log


def test_a_question_turn_carries_no_hidden_fact(store, config, call_log):
    """Rule 5 holds on the new channel too, which is the point of testing it.

    The answer comes from knowledge, and a `hidden` fact about this character
    is one they do not hold, so there is no path for it into the prompt.
    """
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [], ["Oil ticks.", "Nothing in your hands."]
    )
    secret = IN_MOTION_SECRETS[0]["value"]
    assert store.live_fact(PLAYER_ID, "unserved_warrant").value == secret

    turn.play("What am I doing?", life)

    for role, system, user in llm.prompts:
        if role == "narrator":
            assert secret not in system + user
            assert "bench warrant" not in system + user


# ---- step 2b: the contract catches inventions, not declared actions ----


@pytest.mark.parametrize(
    "line",
    [
        "You decide to wave him over.",
        "You think about the rent.",
        "You wonder whether she believed it.",
        "You nod at him.",
        "You shrug and look away.",
        "You hesitate at the top of the steps.",
    ],
)
def test_what_the_player_never_gave_is_still_a_breach(line):
    assert narrated_the_player(line, declared="I walk to the bar")


def test_an_invented_feeling_is_the_prompts_to_hold_not_the_gates():
    """Worth saying out loud, because the line is not where it looks.

    "You feel proud" is a breach and "you feel the cold on your face" is
    exactly what the contract allows, and no pattern separates those two. So
    the gate catches decisions, thoughts and chosen gestures, which are
    recognisable by their verbs, and the invented feeling is left to the
    prompt and to the playtest read-through.
    """
    assert not narrated_the_player("The cold goes through your jacket.", "I go out")
    assert not narrated_the_player("You feel the cold on your face.", "I go out")


@pytest.mark.parametrize(
    "line, declared",
    [
        # The action the player declared, told as it happens. This is the voice
        # the player asked for, and the old single pattern blocked all of it.
        ("You muster your strength and jump onto the table.", "I jump on the table"),
        ("You nod at him and keep walking.", "I nod at him and keep walking"),
        ("You ask about the back room.", "I ask about the back room"),
        # Sensations the world causes were always allowed.
        ("Your hands are shaking by the time you reach the door.", "I walk to the door"),
        ("The cold goes straight through your jacket.", "I step outside"),
    ],
)
def test_the_declared_action_is_the_narrators_job(line, declared):
    assert not narrated_the_player(line, declared=declared), line


def test_speech_the_player_gave_may_be_voiced_or_reported():
    """The check that caught "You say, 'I'll have a beer'" is narrowed.

    Invented speech is still a breach. Speech the resolver settled is not: it
    happened, and reporting it is one of the honest ways to narrate it.
    """
    line = "You tell her you'll have a beer, and she pulls it without a word."
    assert narrated_the_player(line, declared="I order a beer", spoke=False)
    assert not narrated_the_player(line, declared="I order a beer", spoke=True)
    # A thought is never allowed, spoken turn or not.
    assert narrated_the_player("You decide she is lying.", "I order a beer", spoke=True)


# ---- step 2c and 2d: the attempt, the outcome, what's different ----


def test_a_turn_is_the_attempt_then_the_outcome(store, config, call_log):
    """The shape of every turn (step 2d), and the order it reaches the player."""
    jump = reply(
        intent="jump onto the table",
        player_location="place:park",
        summary="you are up on it",
        state=[["person:you", "vantage", "you can see over the crowd"]],
    )
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [jump],
        [
            "Oil ticks.",
            "You plant a hand on the edge and haul yourself up.",
            "The table takes your weight. From here you can see over the crowd.",
        ],
    )
    spoken: list[str] = []

    result = turn.play("I jump on the table", life, on_text=spoken.append)

    assert spoken[0] == "You plant a hand on the edge and haul yourself up."
    assert result.narration.startswith(spoken[0])
    # The attempt went out before the resolver was called, so the resolver's
    # own prompt is the later one.
    roles = [role for role, _, _ in llm.prompts]
    assert roles.index("narrator", 1) < roles.index("resolver", 1)


def test_the_attempt_cannot_introduce_anything_new(store, config, call_log):
    """It is written before resolution, so rules 2 and 5 hold of it for free."""
    arrive = reply(
        intent="walk into the tavern",
        player_location="place:tavern",
        summary="you arrive",
        new_entities=[["place:tavern", "place", "The Double Luck", "place:spawn-city"]],
        new_facts=[["place:tavern", "smell", "stale beer and fryer oil"]],
    )
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [arrive],
        ["Oil ticks.", "You push the door open.", "Stale beer and fryer oil."],
    )

    turn.play("I walk into the tavern", life)

    attempt = [
        user
        for role, system, user in llm.prompts
        if role == "narrator" and "Say the attempt" in system
    ][-1]
    # The player's own word for where they are going is theirs and is in
    # there; nothing the resolver then settled about the place is.
    assert "tavern" in attempt
    for unknown in ("Double Luck", "stale beer", "fryer oil"):
        assert unknown not in attempt, unknown


def test_the_outcome_closes_on_what_is_different_now(store, config, call_log):
    pick_up = reply(
        player_location="place:park",
        summary="the wallet is in your hand",
        state=[["person:you", "in_hand", "his wallet, still warm"]],
    )
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [pick_up],
        ["Oil ticks.", "Your fingers close on it.", "It comes free."],
    )

    result = turn.play("I lift the wallet out of his pocket", life)

    outcome = [u for role, _, u in llm.prompts if role == "narrator"][-1]
    assert "his wallet, still warm" in outcome
    assert any("in hand" in line for line in result.changed_state)


# ---- step 2e: the roll has texture ----


def test_a_near_miss_reads_differently_from_a_clean_one():
    """Code knows the margin; the narrator gets the band and never the number."""
    barely = margin_band(0.5, 0.48, succeeded=True)
    with_effort = margin_band(0.5, 0.25, succeeded=True)
    comfortably = margin_band(0.5, 0.02, succeeded=True)
    just_missed = margin_band(0.5, 0.52, succeeded=False)
    badly = margin_band(0.5, 0.98, succeeded=False)

    assert len({barely, with_effort, comfortably, just_missed, badly}) == 5
    assert "barely" in barely
    assert "hair" in just_missed

    # Banded on how far through the range the roll landed, so the odds are
    # taken into account. Nearly blowing a 90% is a near thing; an ordinary
    # win on a coin flip is not "better than it had any right to".
    assert margin_band(0.9, 0.88, succeeded=True) == barely
    assert margin_band(0.45, 0.24, succeeded=True) == with_effort
    # And neither end divides by zero.
    assert margin_band(0.0, 0.5, succeeded=False)
    assert margin_band(1.0, 0.5, succeeded=True)

    for band in (barely, with_effort, comfortably, just_missed, badly):
        # No numbers, and no game language, ever (spec 3.2).
        assert not any(ch.isdigit() for ch in band), band
        for forbidden in ("roll", "dice", "odds", "chance", "percent"):
            assert forbidden not in band.lower(), band


def test_the_band_reaches_the_narrator_and_the_number_does_not(store, config, call_log):
    risky = reply(
        {
            "roll": {
                "p_success": 0.4,
                "why": "she is half watching the door",
                "on_success": {
                    "summary": "you get out with it",
                    "player_location": "place:park",
                },
                "on_failure": {
                    "summary": "she catches you at the door",
                    "player_location": "place:park",
                },
            }
        },
        intent="walk out with the bottle",
        uncertain=True,
        player_location="place:park",
        summary="you make your move",
    )
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [risky],
        ["Oil ticks.", "You start for the door.", "A hand closes on your collar."],
    )

    result = turn.play("I walk out with the bottle", life)

    assert result.roll is not None
    outcome = [u for role, _, u in llm.prompts if role == "narrator"][-1]
    assert result.roll.band in outcome
    assert f"{result.roll.p_success}" not in outcome
    assert f"{result.roll.rolled}" not in outcome


def test_the_dice_readout_never_contradicts_the_outcome():
    """`/dice` is a reading of the roll that happened, not a second roll."""
    for p in (0.0, 0.05, 0.2, 0.5, 0.8, 0.95, 1.0):
        for rolled in (0.0, 0.049, 0.199, 0.5, 0.799, 0.949, 0.999):
            record = RollRecord(
                p_success=p, why="", rolled=rolled, succeeded=rolled < p,
                on_success="", on_failure="",
            )
            text = record.as_dice()
            face = int(text.split("d20: ")[1].split(",")[0])
            needed = int(text.split("needed ")[1].rstrip(")"))
            assert 1 <= face <= 20, text
            assert (face >= needed) == record.succeeded, (p, rolled, text)


# ---- step 2f and 2g: repeats, and length following the moment ----


def test_a_quiet_turn_is_shorter_than_a_charged_one(store, config, call_log):
    from worldsim.turn import CHARGED_TURN, QUIET_TURN, sentences_for

    assert sentences_for(charged=False) == QUIET_TURN
    assert sentences_for(charged=True) == CHARGED_TURN
    assert QUIET_TURN < CHARGED_TURN

    long_reply = (
        "One thing happens. Then another. Then a third. A fourth. A fifth. A sixth."
    )
    quiet = reply(player_location="place:park", summary="you wait")
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [quiet],
        ["Oil ticks.", "You settle in to wait.", long_reply],
    )

    result = turn.play("I wait", life)

    assert len(sentences_in(result.narration)) <= QUIET_TURN, result.narration


def test_the_repeat_filter_spans_turns(store, config, call_log):
    """The window is the last six sentences, wherever they came from."""
    first = reply(player_location="place:park", summary="you look")
    second = reply(player_location="place:park", summary="you look again")
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [first, second],
        [
            "Oil ticks.",
            "You turn your head.",
            "The fluorescent light above flickers.",
            "You look again.",
            # A paraphrase of a sentence from the turn before: dropped, and the
            # regeneration is asked for something new.
            "The fluorescent light above the sink flickers.",
            "A moth knocks against the tube.",
        ],
    )

    turn.play("I look up", life)
    result = turn.play("I look up again", life)

    assert "above the sink" not in result.narration
    assert "moth" in result.narration


# ---- step 3: lives in motion (spec 6.2, 9) ----


def test_a_life_opens_with_secrets_already_settled(store, config, call_log):
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [], ["The engine is still running outside."]
    )

    secrets = [f for f in store.live_facts(PLAYER_ID) if f.visibility == "hidden"]
    assert len(secrets) >= 2
    # True in the world, and not the character's to know (spec 2.1).
    held = {k.fact.key for k in store.known(PLAYER_ID)}
    for secret in secrets:
        assert secret.key not in held, secret.key


def test_a_life_opens_with_something_on_the_clock(store, config, call_log):
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [], ["The engine is still running outside."]
    )

    ahead = store.pending()
    assert ahead, "a life with nothing coming is a life merely described"
    soonest = min((e.at - store.now()).total_seconds() / 60 for e in ahead)
    assert soonest <= 65, "the first thing has to land inside the hour"


def test_a_spawn_with_no_pressure_is_refused(store, config, call_log):
    """The check spends the spawn's one retry rather than opening a flat life."""
    flat = json.dumps(
        {
            "new_entities": [],
            "new_facts": [],
            "player_starts_in": None,
            "opening_situation": "nothing much",
        }
    )
    llm, resolver, narrator, _ = build(
        store, config, call_log, [flat, SPAWN], ["The engine ticks."]
    )

    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    # The retry carried the reason, so the model could fix it.
    retry = [u for role, _, u in llm.prompts if role == "resolver"][1]
    assert "secrets" in retry
    assert store.pending()


def test_the_clock_fires_into_the_resolvers_context(store, config, call_log):
    """A deadline that is only described is a deadline the world never keeps."""
    long_wait = reply(
        intent="wait around",
        duration_minutes=120,
        player_location="place:park",
        summary="time passes",
    )
    after = reply(
        intent="look up", player_location="place:park", summary="you look up"
    )
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [long_wait, after],
        ["Oil ticks.", "You settle in.", "The light goes orange.",
         "You lift your head.", "Somebody is coming up the path."],
    )
    assert store.pending()

    turn.play("I wait a couple of hours", life)
    result = turn.play("I look up", life)

    # Everything due has fired, and the resolver was told about it.
    assert result.fired, "the clock passed something and said nothing"
    resolver_prompt = [u for role, _, u in llm.prompts if role == "resolver"][-1]
    assert "happening_now" in resolver_prompt
    assert any(event in resolver_prompt for event in result.fired)
    # And it fires once.
    assert not store.due()


def test_a_fired_event_is_not_swallowed_by_a_turn_that_never_commits(
    store, config, call_log
):
    """Held until the opening commits, so a refused turn leaves them due."""
    llm, resolver, narrator, turn, life = start(
        store, config, call_log,
        ["not json at all", "still not json"],
        ["Oil ticks.", "You wait."],
    )
    store.schedule(store.now(), "the phone rings", "a test", reaches_player=True)
    assert len(store.due()) == 1

    with pytest.raises(Exception):
        turn.play("I wait", life)

    assert len(store.due()) == 1, "the event is still coming"


def test_somewhere_that_already_exists_needs_no_parent(store, config, call_log):
    """Found live, on the first turn of the first feel-pass playtest.

    The player was already in `place:ambulance`. The resolver listed it again
    in `new_entities`, under a `place:houston` it never settled, and the
    opening check walked that proposed parent as though it mattered — so a
    turn that needed to commit nothing new was refused twice. Code skips an
    entity it already has, so only the chain of places that do not exist yet
    is any of the check's business.
    """
    relisted = reply(
        intent="look around",
        player_location="place:ambulance",
        summary="you take it in",
        new_entities=[["place:ambulance", "place", "Medic 12", "place:houston"]],
    )
    llm, resolver, narrator, turn, life = start(
        store, config, call_log, [relisted],
        ["Oil ticks.", "You lift your head.", "The gurney straps swing."],
    )
    store.add_entity("place:ambulance", "place", "Medic 12", "place:spawn-city")
    store.move(PLAYER_ID, "place:ambulance")

    result = turn.play("I look around and get my bearings", life)

    assert result.narration, "the turn was refused over a parent nobody uses"
    assert store.entity(PLAYER_ID).location_id == "place:ambulance"

    # And a place that genuinely does not exist yet is still refused, which is
    # the check doing the job it was added for.
    with pytest.raises(ValueError):
        resolver.check_opening(
            Opening(
                duration_minutes=1,
                player_location="place:attic",
                new_entities=[["place:attic", "place", "the attic", "place:nowhere"]],
            )
        )


def test_recent_premises_are_remembered_so_lives_differ(store, config, call_log, tmp_path):
    path = tmp_path / "premises.txt"
    assert recent_premises(path) == []

    llm, resolver, narrator, _ = build(
        store, config, call_log, [SPAWN], ["The engine is still running."]
    )
    begin_life(
        store, resolver, narrator, config.spawn_city, random.Random(7),
        premises_path=path,
    )

    assert recent_premises(path) == ["a mover with somebody else's van"]
    # And the next spawn is told about it.
    spawned = [u for role, _, u in llm.prompts if role == "resolver"][0]
    assert "recent_premises_do_not_repeat" in spawned
