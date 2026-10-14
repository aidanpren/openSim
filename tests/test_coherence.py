"""The three defects the first real playtest exposed.

1. A turn narrated a trip to a bar the store never heard about.
2. The narrator put words in the player's mouth.
3. The opening read as a stat block.

Each is now held by code rather than by asking a prompt nicely.
"""

from __future__ import annotations

import json
import random

import pytest

from worldsim.llm import FakeLLM, ModelCallFailed
from worldsim.render import BIOGRAPHY, Narrator, narrated_the_player
from worldsim.resolve import PLAYER_ID, Resolver
from worldsim.turn import Turn, begin_life
from worldsim.__main__ import PROMPTS_DIR
from helpers import IN_MOTION_SCHEDULE, IN_MOTION_SECRETS, reply, scripts

SPAWN = json.dumps(
    {
        "new_entities": [
            {"id": "place:kitchen", "kind": "place", "name": "The line", "location_id": "place:spawn-city"}
        ],
        "new_facts": [
            {"entity_id": "person:you", "key": "name", "value": "Marcus Turner", "source": "invented", "visibility": "obvious"},
            {"entity_id": "person:you", "key": "age", "value": "28", "source": "invented", "visibility": "obvious"},
            {"entity_id": "person:you", "key": "cash_usd", "value": 17.5, "source": "invented", "visibility": "noticeable"},
            {"entity_id": "person:you", "key": "condition", "value": "feet aching after a double", "source": "invented", "visibility": "noticeable"},
            {"entity_id": "place:kitchen", "key": "smell", "value": "old frying oil", "source": "invented", "visibility": "obvious"},
        ],
        "player_starts_in": "place:kitchen",
        "secrets": IN_MOTION_SECRETS,
        "schedule": IN_MOTION_SCHEDULE,
        "opening_situation": "rent was due four days ago",
    }
)


def build(store, config, call_log, resolver_script, narrator_script):
    llm = FakeLLM(config, call_log, scripts(resolver_script, narrator_script))
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    return llm, resolver, narrator, Turn(store, resolver, narrator, random.Random(7))


def a_turn(ledger=None, **scene):
    """A reply for a player who starts in place:kitchen (see SPAWN)."""
    scene.setdefault("player_location", "place:kitchen")
    return reply(ledger, **scene)


# ---- defect 1: the store decides where you are ----


def test_an_outcome_with_no_location_at_all_is_refused(store, config, call_log):
    no_location = json.dumps(
        {"intent": "walk to the bar", "duration_minutes": 15, "summary": "you get there"}
    ) + "{}"
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, no_location, no_location], ["Oil ticks.", "x"]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    with pytest.raises(ModelCallFailed):
        turn.play("I walk to the nearest bar", life)

    assert store.entity(PLAYER_ID).location_id == "place:kitchen"


def test_a_location_that_does_not_exist_is_refused_with_an_explanation(
    store, config, call_log
):
    invented = a_turn(player_location="place:bar-that-was-never-made")
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, invented, invented], ["Oil ticks.", "x"]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    with pytest.raises(ModelCallFailed):
        turn.play("I walk to the nearest bar", life)

    # The retry told it what was wrong, which is the whole point.
    retry = [u for role, s, u in llm.prompts if role == "resolver"][-1]
    assert "place:bar-that-was-never-made" in retry
    assert "does not exist" in retry
    assert store.entity(PLAYER_ID).location_id == "place:kitchen"


def test_the_retry_succeeds_when_the_second_try_creates_the_place(store, config, call_log):
    bad = a_turn(player_location="place:bar")
    good = a_turn(
        player_location="place:bar",
        new_entities=[{"id": "place:bar", "kind": "place", "name": "The Peanut", "location_id": "place:spawn-city"}],
    )
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, bad, good], ["Oil ticks.", "The door swings shut."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    turn.play("I walk to the nearest bar", life)

    assert store.entity("place:bar") is not None
    assert store.entity(PLAYER_ID).location_id == "place:bar"


def test_the_player_ends_where_the_outcome_says_even_with_no_move_listed(
    store, config, call_log
):
    # The bug: a trip narrated with no `moves` entry left the player behind.
    moved_by_location_alone = a_turn(
        player_location="place:bar",
        new_entities=[{"id": "place:bar", "kind": "place", "name": "The Peanut", "location_id": "place:spawn-city"}],
    )
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, moved_by_location_alone],
        ["Oil ticks.", "Smoke and old beer."],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    result = turn.play("I walk to the nearest bar", life)

    assert store.entity(PLAYER_ID).location_id == "place:bar"
    assert result.dropped == []


def test_spending_money_that_was_never_settled_is_reported(store, config, call_log):
    """The ledger arrives after narration, so a bad delta is dropped, not retried."""
    phantom = a_turn(
        {"deltas": [{"entity_id": "person:you", "key": "tab_usd", "delta": -9.0}]},
        player_location="place:kitchen",
    )
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, phantom], ["Oil ticks.", "She shrugs."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    result = turn.play("I settle my tab", life)

    assert store.live_fact("person:you", "tab_usd") is None
    assert any("tab_usd" in item for item in result.dropped)


def test_a_delta_on_a_quantity_settled_this_same_turn_is_allowed(store, config, call_log):
    both_at_once = a_turn(
        {"deltas": [{"entity_id": "person:you", "key": "tab_usd", "delta": -9.0}]},
        player_location="place:kitchen",
        new_facts=[{"entity_id": "person:you", "key": "tab_usd", "value": 20, "source": "invented", "visibility": "obvious"}],
    )
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, both_at_once], ["Oil ticks.", "She nods."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    result = turn.play("I settle my tab", life)

    assert store.live_fact(PLAYER_ID, "tab_usd").value == 11.0
    assert result.dropped == []


def test_a_world_whose_spawn_failed_can_still_be_started(store, config, call_log):
    """A failed spawn used to leave a world that crashed on every relaunch."""
    broken = json.dumps({"opening_situation": "no facts at all", "new_facts": [
        {"entity_id": "person:you", "key": "name", "value": "x",
         "source": "invented", "visibility": "world-knowledge"}
    ]})
    llm, resolver, narrator, _ = build(
        store, config, call_log, [broken, broken, SPAWN], ["Oil ticks."]
    )

    with pytest.raises(ModelCallFailed):
        begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    # The two entities code creates are already there; begin_life must not choke.
    life, opening = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    assert opening == "Oil ticks."
    assert life.character_id == PLAYER_ID


# ---- defect 2: your words are yours ----


@pytest.mark.parametrize(
    "line",
    [
        'You say, "I\'ll have a beer."',
        "You tell her you were home all night.",
        "You decide to head back to the bar.",
        "You realize the door is unlocked.",
        "You can't believe you got away with it.",
        "You wonder whether she noticed.",
    ],
)
def test_narrating_the_player_is_detected(line):
    assert narrated_the_player(line)


@pytest.mark.parametrize(
    "line",
    [
        "You feel the cold come up through the floor.",
        "You walk into the bar and the door chimes.",
        "Your heart is still racing.",
        "She says she hasn't seen him in a week.",
        "The fryer ticks as it cools.",
    ],
)
def test_legitimate_narration_is_not_flagged(line):
    assert narrated_the_player(line) == []


def test_the_narration_is_regenerated_once_when_it_speaks_for_you(
    store, config, call_log
):
    llm, resolver, narrator, _ = build(
        store, config, call_log, [SPAWN],
        [
            "Oil ticks as the fryer cools.",  # the opening, clean
            'You say, "I\'ll have a beer."',  # then the offending line
            "The bartender slides a glass across.",  # and its replacement
        ],
    )
    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    call_log.records.clear()

    result = narrator.narrate("order a beer", [], [])

    assert result == "The bartender slides a glass across."
    assert len(call_log.records) == 2
    assert "broke the contract" in llm.prompts[-1][2]


def test_a_breach_that_survives_regeneration_is_recorded(store, config, call_log):
    """A real run regenerated and got 'You say' back; that was accepted silently."""
    llm, resolver, narrator, _ = build(
        store, config, call_log, [SPAWN],
        [
            "Oil ticks as the fryer cools.",
            'You say you\'ll have a beer, please.',
            'You say it again, differently.',  # the model repeats itself
        ],
    )
    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    result = narrator.narrate("order a beer", [], [])

    assert result == "You say it again, differently."
    assert narrator.breaches == ["You say survived a regeneration"]


def test_a_clean_regeneration_records_no_breach(store, config, call_log):
    llm, resolver, narrator, _ = build(
        store, config, call_log, [SPAWN],
        ["Oil ticks.", "You decide to leave.", "The door sticks, then gives."],
    )
    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    narrator.narrate("leave", [], [])

    assert narrator.breaches == []


def test_the_resolver_hands_the_narrator_the_players_words(store, config, call_log):
    spoke = a_turn(player_location="place:kitchen", you_said_aloud="asks for a beer")
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, spoke], ["Oil ticks.", "She reaches for a glass."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    turn.play("I order a beer", life)

    last = [u for role, s, u in llm.prompts if role == "narrator"][-1]
    assert "asks for a beer" in last


# ---- defect 3: the opening is a moment, not a dossier ----


def test_the_opening_uses_its_own_prompt(store, config, call_log):
    llm, resolver, narrator, _ = build(store, config, call_log, [SPAWN], ["Oil ticks."])

    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    opening_system = [s for role, s, u in llm.prompts if role == "narrator"][0]
    assert "first words of a life" in opening_system
    assert "stat block" in opening_system


def test_the_opening_never_sees_the_biography(store, config, call_log):
    llm, resolver, narrator, _ = build(store, config, call_log, [SPAWN], ["Oil ticks."])

    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    opening_user = [u for role, s, u in llm.prompts if role == "narrator"][0]
    for recited in ("Marcus Turner", "17.5", '"age"', '"job"'):
        assert recited not in opening_user
    # What they can actually perceive is still there.
    assert "old frying oil" in opening_user
    assert "feet aching" in opening_user


def test_later_turns_do_see_the_biography_again(store, config, call_log):
    plain = a_turn(player_location="place:kitchen")
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, plain], ["Oil ticks.", "The pit is backed up."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    turn.play("I look at my hands", life)

    later = [u for role, s, u in llm.prompts if role == "narrator"][-1]
    assert "Marcus Turner" in later
    assert BIOGRAPHY  # the omission set is only applied to the opening


# ---- the detector has to be precise, not just sensitive ----
#
# Every false positive spends a second narrator call and risks trading good
# narration for worse, so a "you <verb>" inside someone else's clause is not a
# breach: the verb belongs to the speaker, not to the narrator.


@pytest.mark.parametrize(
    "line",
    [
        "Gary asks what you want.",
        "He repeats what you told him, slower this time.",
        "She looks like she knows what you think of the place.",
        "The bartender sets the glass down and asks if you want another.",
        "He remembers you asked about the car.",
        "Marisol says she heard you were back in town.",
    ],
)
def test_someone_else_talking_about_you_is_not_a_breach(line):
    assert narrated_the_player(line) == []


@pytest.mark.parametrize(
    "line",
    [
        "You say you will have a beer.",
        "The door closes. You decide to wait.",
        "He nods, and you tell him about the money.",
        "After a long moment you admit it.",
        "She waits; you say nothing.",
    ],
)
def test_the_narrator_speaking_for_you_still_is(line):
    assert narrated_the_player(line)


def test_the_narrator_is_never_given_the_players_own_words(store, config, call_log):
    """Defect 2, closed structurally instead of policed afterwards.

    The narrator used to receive the raw typed text, and run B read it straight
    back as "You say you'll have a beer, please" — twice, through a
    regeneration. It gets the resolver's one-line intent now, so the phrasing
    it was echoing is not in the prompt at all.

    The attempt beat is the one deliberate exception, and it has to be: it is
    written before anything is resolved, so the player's own line is the only
    thing it has, and telling the declared action is the whole of its job. It
    is also the one beat with nothing to read back, because nothing has been
    said yet.
    """
    said = "I lean over and tell her the money is coming on Friday, I swear"
    turn_reply = reply(
        intent="promise the bartender the money on Friday",
        player_location="place:bar",
        summary="you make a promise",
        you_said_aloud="promises the money by Friday",
        dialogue=[{"speaker_id": "person:marisol", "gist": "she has heard that before"}],
    )
    llm = FakeLLM(
        config, call_log,
        scripts([SPAWN, turn_reply], ["Quiet.", "She doesn't look up from the glass."]),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    store.add_entity("place:bar", "place", "The Peanut", location_id="place:spawn-city")

    turn.play(said, life)

    after_the_attempt = [
        user
        for role, system, user in llm.prompts
        if role == "narrator" and "Say the attempt" not in system
    ]
    assert after_the_attempt
    for prompt in after_the_attempt:
        assert "I swear" not in prompt
        assert said not in prompt
    # The resolver's reading of it does get through, so a question still lands.
    assert "promise the bartender the money on Friday" in after_the_attempt[-1]


# ---- defect 1, the second time (found live, playtest-20261011) ----


def test_a_destination_whose_parent_does_not_exist_is_refused(store, config, call_log):
    """The player was narrated into a restaurant they were never moved to.

    `check_opening` asked whether `player_location` existed or was being
    created, and the restaurant *was* being created — inside `place:downtown`,
    which had never been settled. So the restaurant was dropped, every fact on
    it was dropped, the player stayed in their car, and the narration walked
    them to a table. Rejecting it spends the retry instead.
    """
    unreachable = reply(
        intent="go into the restaurant",
        player_location="place:bistro",
        summary="you go in",
        new_entities=[
            ["place:bistro", "place", "Le Petit Bistro", "place:downtown"],
        ],
    )
    recovered = reply(
        intent="go into the restaurant",
        player_location="place:bistro",
        summary="you go in",
        new_entities=[["place:bistro", "place", "Le Petit Bistro", "place:spawn-city"]],
    )
    llm = FakeLLM(
        config, call_log,
        scripts([SPAWN, unreachable, recovered], ["Quiet.", "The door closes behind you."]),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    result = turn.play("I go inside", life)

    # The retry fired and the second attempt nested it somewhere real.
    assert store.entity("place:bistro") is not None
    assert store.entity(PLAYER_ID).location_id == "place:bistro"
    assert result.dropped == []


def test_the_reason_reaches_the_model_on_the_retry(store, config, call_log):
    unreachable = reply(
        player_location="place:bistro",
        new_entities=[["place:bistro", "place", "Le Petit Bistro", "place:downtown"]],
    )
    llm = FakeLLM(
        config, call_log,
        scripts([SPAWN, unreachable, unreachable], ["Quiet."]),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    with pytest.raises(ModelCallFailed):
        turn.play("I go inside", life)

    retry = [u for role, _, u in llm.prompts if role == "resolver"][-1]
    assert "place:downtown" in retry
    assert "nested inside" in retry
    # And nothing was committed on the way through.
    assert store.entity("place:bistro") is None


def test_an_entity_off_to_one_side_is_reparented_not_lost(store, config, call_log):
    """Only the player's own chain is fatal. The rest is corrected, not lost.

    A live run dropped a cafe whose parent `place:neighborhood` was never
    settled. The player then could not walk to it, and two turns later the
    resolver had put a barista in their kitchen rather than contradict itself.
    Keeping the cafe and deciding it sits in the city is the smaller invention.
    """
    with_orphan = reply(
        player_location="place:kitchen",
        new_entities=[["place:orphan", "place", "Nowhere", "place:never-made"]],
    )
    llm = FakeLLM(
        config, call_log,
        scripts([SPAWN, with_orphan], ["Quiet.", "You wait.", "The fryer ticks."]),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    result = turn.play("I wait", life)

    assert store.entity("place:orphan").location_id == "place:spawn-city"
    assert any("place:orphan" in item and "instead" in item for item in result.dropped)
    assert result.narration == "You wait. The fryer ticks."


# ---- the ledger arrives too late to retry, so it takes both forms ----


def test_the_ledger_takes_arrays_the_way_everything_else_does(store, config, call_log):
    """A live run lost a move and a supersession to the dict-only shapes.

    The ledger commits after the turn is narrated, so it cannot be retried: a
    format slip there costs real money and a real location.
    """
    from worldsim.resolve import Delta, Learned, Move, Supersede

    assert Move.model_validate(["person:you", "place:bedroom"]).location_id == "place:bedroom"
    assert Delta.model_validate(["person:you", "cash_usd", -40]).delta == -40
    assert Learned.model_validate(
        ["person:gary", "person:you", "debt", "watched you count it"]
    ).how == "watched you count it"
    # And a supersession without the two tags defaults them rather than dying.
    plain = Supersede.model_validate(
        {"entity_id": "person:you", "key": "wearing", "value": "a clean shirt"}
    )
    assert (plain.source, plain.visibility) == ("invented", "obvious")
