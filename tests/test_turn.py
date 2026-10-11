"""The turn cycle end to end, with FakeLLM (spec 3, spec 14)."""

from __future__ import annotations

import json
import random

import pytest

from worldsim.llm import FakeLLM
from worldsim.render import Narrator
from worldsim.resolve import PLAYER_ID, Resolver
from worldsim.turn import Turn, begin_life
from worldsim.__main__ import PROMPTS_DIR, local_time
from helpers import reply

HIDDEN_VALUE = "a revolver taped under the bar"

SPAWN = json.dumps(
    {
        "new_entities": [
            {"id": "place:bar", "kind": "place", "name": "The Peanut", "location_id": "place:spawn-city"},
            {"id": "person:marisol", "kind": "person", "name": "Marisol", "location_id": "place:bar"},
        ],
        "new_facts": [
            {"entity_id": "person:you", "key": "name", "value": "Dale Rusk", "source": "invented", "visibility": "obvious"},
            {"entity_id": "person:you", "key": "job", "value": "line cook", "source": "invented", "visibility": "obvious"},
            {"entity_id": "person:you", "key": "cash_usd", "value": 1200, "source": "invented", "visibility": "hidden"},
            {"entity_id": "place:spawn-city", "key": "timezone", "value": "America/Chicago", "source": "world-knowledge", "visibility": "obvious"},
            {"entity_id": "person:marisol", "key": "under_the_bar", "value": HIDDEN_VALUE, "source": "invented", "visibility": "hidden"},
        ],
        "opening_situation": "rent was due four days ago",
    }
)

RISKY_TURN = reply(
    {
        "roll": {
            "p_success": 0.5,
            "why": "she is watching the TV, but the door chimes",
            "on_success": {
                "summary": "you get out with it under your coat",
                "player_location": "place:spawn-city",
            },
            "on_failure": {
                "summary": "she catches you at the door and you pay for it",
                "player_location": "place:spawn-city",
                "deltas": [{"entity_id": "person:you", "key": "cash_usd", "delta": -40}],
                "dialogue": [
                    {"speaker_id": "person:marisol", "gist": "come back to the counter", "true": True}
                ],
            },
        }
    },
    intent="pocket the whiskey and walk out",
    duration_minutes=2,
    uncertain=True,
    summary="you make your move",
    perceived=[{"entity_id": "person:marisol", "key": "mood"}],
    new_facts=[
        {"entity_id": "person:marisol", "key": "mood", "value": "bored, half watching", "source": "invented", "visibility": "obvious"}
    ],
)

CERTAIN_TURN = reply(
    intent="walk to the bar",
    duration_minutes=11,
    player_location="place:bar",
    summary="you arrive",
)


def build(store, config, call_log, resolver_script, narrator_script, seed=7):
    llm = FakeLLM(
        config, call_log, {"resolver": resolver_script, "narrator": narrator_script}
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, PROMPTS_DIR)
    return llm, resolver, narrator, Turn(store, resolver, narrator, random.Random(seed))


# ---- spawning and the opening ----


def test_spawning_builds_a_person_who_knows_themselves(store, config, call_log):
    llm, resolver, narrator, _ = build(
        store, config, call_log, [SPAWN], ["The kitchen is finally quiet."]
    )

    life, opening = begin_life(store, resolver, narrator, config.spawn_city)

    assert opening == "The kitchen is finally quiet."
    assert life.character_id == PLAYER_ID
    assert store.live_fact(PLAYER_ID, "name").value == "Dale Rusk"
    assert store.live_fact(PLAYER_ID, "cash_usd").value == 1200

    known_keys = {k.fact.key for k in store.known(PLAYER_ID)}
    assert {"name", "job", "cash_usd"} <= known_keys


# ---- rule 5: hidden facts never reach the narrator ----


def test_a_hidden_fact_never_appears_in_a_narrator_prompt(store, config, call_log):
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, CERTAIN_TURN], ["Quiet.", "You push the door open."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)
    turn.play("I walk to the bar", life)

    # The fact is true in the world...
    assert store.live_fact("person:marisol", "under_the_bar").value == HIDDEN_VALUE
    # ...and absent from every prompt the narrator ever saw.
    narrator_prompts = [
        system + user for role, system, user in llm.prompts if role == "narrator"
    ]
    assert narrator_prompts
    for prompt in narrator_prompts:
        assert HIDDEN_VALUE not in prompt
        assert "under_the_bar" not in prompt


def test_the_resolver_does_see_hidden_facts(store, config, call_log):
    llm, resolver, narrator, _ = build(store, config, call_log, [SPAWN], ["Quiet."])
    begin_life(store, resolver, narrator, config.spawn_city)

    # The resolver's slice is the truth where the player stands, so they have to
    # be in the room with her for her facts to be in scope at all.
    store.move(PLAYER_ID, "place:bar")

    assert HIDDEN_VALUE in json.dumps(resolver.truth_here())


def test_a_belief_reaches_the_narrator_instead_of_the_truth(store, config, call_log):
    llm, resolver, narrator, _ = build(store, config, call_log, [SPAWN], ["Quiet."])
    begin_life(store, resolver, narrator, config.spawn_city)

    truth = store.add_fact(
        "person:marisol", "name", "Marisol", "invented", "obvious", "turn 1"
    )
    store.learn(PLAYER_ID, truth.id, "she told me", belief="Sam")

    held = [
        entry
        for entry in narrator.context("hi", [], [])["your_knowledge"]
        if entry["entity_id"] == "person:marisol" and entry["key"] == "name"
    ]
    assert len(held) == 1
    assert held[0]["value"] == "Sam"


# ---- rolls (rule 6) ----


def test_the_committed_outcome_matches_the_branch_the_roll_picked(store, config, call_log):
    seed = 7
    expected_roll = random.Random(seed).random()
    expected_success = expected_roll < 0.5

    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, RISKY_TURN], ["Quiet.", "The door chimes."], seed=seed
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)

    result = turn.play("I pocket the whiskey and walk out", life)

    assert result.roll is not None
    assert result.roll.rolled == pytest.approx(expected_roll)
    assert result.roll.succeeded is expected_success
    assert result.roll.p_success == 0.5
    assert result.roll.on_success and result.roll.on_failure

    cash = store.live_fact(PLAYER_ID, "cash_usd").value
    if expected_success:
        assert result.summary.startswith("you get out")
        assert cash == 1200
    else:
        assert result.summary.startswith("she catches you")
        assert cash == 1160


def test_the_roll_is_logged_with_odds_both_outcomes_and_the_result(store, config, call_log):
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, RISKY_TURN], ["Quiet.", "The door chimes."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)
    turn.play("I pocket the whiskey", life)

    logged = json.loads(store.recent_turns(life.id, limit=1)[0]["roll_json"])
    assert set(logged) == {
        "p_success", "why", "rolled", "succeeded", "on_success", "on_failure"
    }


def test_the_narration_never_mentions_the_roll(store, config, call_log):
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, RISKY_TURN], ["Quiet.", "The door chimes behind you."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)
    turn.play("I pocket the whiskey", life)

    narrator_prompts = [u for role, s, u in llm.prompts if role == "narrator"]
    for prompt in narrator_prompts:
        for word in ("p_success", "rolled", "on_failure", "0.5"):
            assert word not in prompt


# ---- the clock (rule 7) ----


def test_an_action_moves_the_clock_by_its_duration(store, config, call_log):
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, CERTAIN_TURN], ["Quiet.", "You arrive."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)
    before = store.now()

    turn.play("I walk to the bar", life)

    assert (store.now() - before).total_seconds() == 11 * 60


def test_local_time_uses_the_timezone_the_world_settled(store, config, call_log):
    llm, resolver, narrator, _ = build(store, config, call_log, [SPAWN], ["Quiet."])
    begin_life(store, resolver, narrator, config.spawn_city)

    shown = local_time(store)
    assert "2026" in shown
    assert "America/Chicago" in shown or "UTC" in shown


# ---- a stored fact beats a proposal (spec 2.2) ----


def test_a_proposal_that_contradicts_a_stored_fact_is_dropped_not_committed(
    store, config, call_log
):
    contradiction = reply(
        intent="look at her again",
        duration_minutes=1,
        summary="nothing changes",
        new_facts=[
            {
                "entity_id": "person:you",
                "key": "name",
                "value": "Someone Entirely Different",
                "source": "invented",
                "visibility": "obvious",
            }
        ],
    )
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, contradiction], ["Quiet.", "She looks up."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)

    result = turn.play("I look at her again", life)

    assert store.live_fact(PLAYER_ID, "name").value == "Dale Rusk"
    assert any("person:you.name" in item for item in result.dropped)


def test_nested_places_land_even_when_listed_child_first(store, config, call_log):
    out_of_order = reply(
        intent="walk into the back room",
        duration_minutes=2,
        player_location="place:back-room",
        summary="you push through",
        new_entities=[
            {"id": "place:back-room", "kind": "place", "name": "Back room", "location_id": "place:tavern"},
            {"id": "place:tavern", "kind": "place", "name": "The Tavern", "location_id": "place:spawn-city"},
            {"id": "place:orphan", "kind": "place", "name": "Nowhere", "location_id": "place:never-made"},
        ],
    )
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, out_of_order], ["Quiet.", "The door swings shut."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)

    result = turn.play("I walk into the back room", life)

    assert store.entity("place:tavern") is not None
    assert store.entity("place:back-room").location_id == "place:tavern"
    assert store.entity("person:you").location_id == "place:back-room"
    # The one with no possible parent is reported, not crashed on.
    assert store.entity("place:orphan") is None
    assert any("place:orphan" in item for item in result.dropped)


def test_a_turn_is_logged_for_resuming(store, config, call_log):
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, CERTAIN_TURN], ["Quiet.", "You arrive."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)
    turn.play("I walk to the bar", life)

    rows = store.recent_turns(life.id, limit=10)
    assert [row["intent"] for row in rows] == ["spawn", "walk to the bar"]
    assert rows[-1]["narration"] == "You arrive."
