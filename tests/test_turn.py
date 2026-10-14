"""The turn cycle end to end, with FakeLLM (spec 3, spec 14)."""

from __future__ import annotations

import json
import random

import pytest

from worldsim.llm import FakeLLM
from worldsim.render import Narrator
from worldsim.resolve import PLAYER_ID, STATIONS, Resolver, draw_station
from worldsim.sheet import character_sheet
from worldsim.turn import Turn, begin_life
from worldsim.__main__ import PROMPTS_DIR, local_time
from helpers import IN_MOTION_SCHEDULE, IN_MOTION_SECRETS, reply, scripts

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
            {"entity_id": "person:you", "key": "cash_usd", "value": 1200, "source": "invented", "visibility": "noticeable"},
            {"entity_id": "place:spawn-city", "key": "timezone", "value": "America/Chicago", "source": "world-knowledge", "visibility": "obvious"},
            {"entity_id": "person:marisol", "key": "under_the_bar", "value": HIDDEN_VALUE, "source": "invented", "visibility": "hidden"},
        ],
        "secrets": IN_MOTION_SECRETS,
        "schedule": IN_MOTION_SCHEDULE,
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
        config, call_log, scripts(resolver_script, narrator_script)
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    return llm, resolver, narrator, Turn(store, resolver, narrator, random.Random(seed))


# ---- spawning and the opening ----


def test_spawning_builds_a_person_who_knows_themselves(store, config, call_log):
    llm, resolver, narrator, _ = build(
        store, config, call_log, [SPAWN], ["The kitchen is finally quiet."]
    )

    life, opening = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

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
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
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
    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    # The resolver's slice is the truth where the player stands, so they have to
    # be in the room with her for her facts to be in scope at all.
    store.move(PLAYER_ID, "place:bar")

    assert HIDDEN_VALUE in json.dumps(resolver.truth_here())


def test_a_belief_reaches_the_narrator_instead_of_the_truth(store, config, call_log):
    llm, resolver, narrator, _ = build(store, config, call_log, [SPAWN], ["Quiet."])
    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

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
        store, config, call_log, [SPAWN, RISKY_TURN],
        ["Quiet.", "Marisol is half watching the television.", "The door chimes."],
        seed=seed,
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

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
        store, config, call_log, [SPAWN, RISKY_TURN],
        ["Quiet.", "Marisol is half watching the television.", "The door chimes."],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    turn.play("I pocket the whiskey", life)

    logged = json.loads(store.recent_turns(life.id, limit=1)[0]["roll_json"])
    assert set(logged) == {
        "p_success", "why", "rolled", "succeeded", "on_success", "on_failure"
    }


def test_the_narration_never_mentions_the_roll(store, config, call_log):
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, RISKY_TURN],
        ["Quiet.", "Marisol is half watching the television.", "The door chimes behind you."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
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
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    before = store.now()

    turn.play("I walk to the bar", life)

    assert (store.now() - before).total_seconds() == 11 * 60


def test_local_time_uses_the_timezone_the_world_settled(store, config, call_log):
    llm, resolver, narrator, _ = build(store, config, call_log, [SPAWN], ["Quiet."])
    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

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
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

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
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    result = turn.play("I walk into the back room", life)

    assert store.entity("place:tavern") is not None
    assert store.entity("place:back-room").location_id == "place:tavern"
    assert store.entity("person:you").location_id == "place:back-room"
    # The one whose parent was never settled is kept and reparented to the
    # city, not dropped: dropping it made the place unreachable, and the world
    # then contradicted itself rather than admit the place did not exist.
    assert store.entity("place:orphan").location_id == "place:spawn-city"
    assert any("place:orphan" in item and "instead" in item for item in result.dropped)


def test_a_turn_is_logged_for_resuming(store, config, call_log):
    llm, resolver, narrator, turn = build(
        store, config, call_log,
        [SPAWN, CERTAIN_TURN],
        # A turn narrates twice: the attempt, then the outcome.
        ["Quiet.", "You set off down the block.", "You arrive."],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    turn.play("I walk to the bar", life)

    rows = store.recent_turns(life.id, limit=10)
    assert [row["intent"] for row in rows] == ["spawn", "walk to the bar"]
    assert rows[-1]["narration"] == "You set off down the block. You arrive."
    assert rows[-1]["mode"] == "action"


def test_a_concealed_thing_in_the_room_never_reaches_the_narrator(store, config, call_log):
    """Rule 5: a name is as revealing as a fact.

    Standing in a room used to count as perceiving everything in it, so a
    revolver taped under the sink was handed to the narrator by name — the
    world giving away what the character has not found.
    """
    spawn = json.dumps(
        {
            "new_entities": [
                {"id": "place:apt", "kind": "place", "name": "Apartment 3B", "location_id": "place:spawn-city"},
                {"id": "thing:revolver", "kind": "thing", "name": "a revolver taped under the sink", "location_id": "place:apt"},
                {"id": "person:roommate", "kind": "person", "name": "Trey", "location_id": "place:apt"},
            ],
            "new_facts": [
                {"entity_id": "person:you", "key": "name", "value": "Dale Rusk", "source": "invented", "visibility": "obvious"},
                {"entity_id": "person:roommate", "key": "mood", "value": "sullen", "source": "invented", "visibility": "obvious"},
            ],
            "secrets": IN_MOTION_SECRETS,
            "schedule": IN_MOTION_SCHEDULE,
            "player_starts_in": "place:apt",
            "opening_situation": "rent is late",
        }
    )
    llm, resolver, narrator, _ = build(store, config, call_log, [spawn], ["The tap drips."])
    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    prompt = [user for role, _, user in llm.prompts if role == "narrator"][-1]
    assert "revolver" not in prompt
    # The roommate is in the same room and plainly there, so he still is.
    assert "Trey" in prompt


def test_perceiving_it_is_what_makes_it_narratable(store, config, call_log):
    """The concealed thing becomes mentionable the moment it is found."""
    spawn = json.dumps(
        {
            "new_entities": [
                {"id": "place:apt", "kind": "place", "name": "Apartment 3B", "location_id": "place:spawn-city"},
                {"id": "thing:revolver", "kind": "thing", "name": "a revolver taped under the sink", "location_id": "place:apt"},
            ],
            "new_facts": [
                {"entity_id": "thing:revolver", "key": "condition", "value": "oiled, recently", "source": "invented", "visibility": "hidden"}
            ],
            "secrets": IN_MOTION_SECRETS,
            "schedule": IN_MOTION_SCHEDULE,
            "player_starts_in": "place:apt",
            "opening_situation": "rent is late",
        }
    )
    found_it = reply(
        intent="feel around under the sink",
        player_location="place:apt",
        summary="your hand finds tape",
        perceived=[{"entity_id": "thing:revolver", "key": "condition"}],
    )
    llm, resolver, narrator, turn = build(
        store, config, call_log, [spawn, found_it], ["The tap drips.", "Tape, and then metal."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    before = [user for role, _, user in llm.prompts if role == "narrator"][-1]
    assert "revolver" not in before

    turn.play("I feel around under the sink", life)

    after = [user for role, _, user in llm.prompts if role == "narrator"][-1]
    assert "revolver" in after


def test_the_clock_follows_the_player_out_of_the_spawn_city(store, config, call_log):
    """Spec 6.1: local time where the character is, not where they started."""
    llm, resolver, narrator, _ = build(store, config, call_log, [SPAWN], ["Quiet."])
    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    assert "America/Chicago" in local_time(store)

    store.add_entity("place:denver", "place", "Denver", location_id="place:spawn-city")
    store.add_fact(
        "place:denver", "timezone", "America/Denver", "world-knowledge", "obvious", "drove"
    )
    store.add_entity("place:motel", "place", "a motel off I-70", location_id="place:denver")
    store.move(PLAYER_ID, "place:motel")

    # The zone is settled on the city, not the motel room, so the lookup has to
    # walk outward through the containment chain.
    assert "America/Denver" in local_time(store)


def test_a_containment_cycle_does_not_hang_the_clock(store, config, call_log):
    llm, resolver, narrator, _ = build(store, config, call_log, [SPAWN], ["Quiet."])
    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    store.add_entity("place:a", "place", "A", location_id="place:spawn-city")
    store.add_entity("place:b", "place", "B", location_id="place:a")
    store.move("place:a", "place:b")  # the store permits this; the walk must not loop
    store.move(PLAYER_ID, "place:a")

    assert "America/Chicago" in local_time(store)  # falls back to the spawn city


def test_a_fact_hidden_from_you_is_not_yours_to_know(store, config, call_log):
    """Rule 5: `hidden` counts the player character among the people it hides from.

    Spawning used to teach the character every fact written on them, so an
    "outstanding warrant he has never been told about" went straight into the
    opening narration — the world handing over its own secret on turn zero.
    """
    spawn = json.dumps(
        {
            "new_entities": [
                {"id": "place:apt", "kind": "place", "name": "Apartment 3B", "location_id": "place:spawn-city"}
            ],
            "new_facts": [
                {"entity_id": "person:you", "key": "name", "value": "Dale Rusk", "source": "invented", "visibility": "obvious"},
                {"entity_id": "person:you", "key": "cash_usd", "value": 27, "source": "invented", "visibility": "noticeable"},
                {"entity_id": "person:you", "key": "warrant", "value": "a bench warrant in Jackson County, unserved", "source": "invented", "visibility": "hidden"},
            ],
            "secrets": IN_MOTION_SECRETS,
            "schedule": IN_MOTION_SCHEDULE,
            "player_starts_in": "place:apt",
            "opening_situation": "rent is late",
        }
    )
    llm, resolver, narrator, _ = build(store, config, call_log, [spawn], ["The tap drips."])
    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    # It is true in the world, and the resolver can see it.
    assert store.live_fact(PLAYER_ID, "warrant").value.startswith("a bench warrant")
    assert "bench warrant" in json.dumps(resolver.truth_here())

    # The character does not hold it, so it reaches neither narrator nor sheet.
    assert "warrant" not in {k.fact.key for k in store.known(PLAYER_ID)}
    for _role, system, user in llm.prompts:
        if _role == "narrator":
            assert "bench warrant" not in system + user
    assert "bench warrant" not in character_sheet(store)

    # What they would plainly know about themselves still reaches them.
    assert {"name", "cash_usd"} <= {k.fact.key for k in store.known(PLAYER_ID)}


def test_the_sheet_shows_what_you_believe_not_what_is_true(store, config, call_log):
    """Spec 5.5: the sheet is a view of what this character would know.

    It read the truth directly, so it was a second way around rule 5 — and it
    would have reported the right cash while the narrator reported the wrong.
    """
    llm, resolver, narrator, _ = build(store, config, call_log, [SPAWN], ["Quiet."])
    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    store.add_fact(
        "person:you", "landlord", "Hal Brackett", "invented", "obvious", "spawn"
    )
    told = store.live_fact("person:you", "landlord")
    store.learn(PLAYER_ID, told.id, "he introduced himself", belief="Hal Beckett")

    assert "Hal Beckett" in character_sheet(store)
    assert "Hal Brackett" not in character_sheet(store)


# ---- spawning across the range of lives (spec 9) ----


def test_the_station_is_drawn_by_code_and_reaches_the_prompt(store, config, call_log):
    """Rule 6: the dice are code's.

    A model asked for "a person with at least one source of friction" writes
    the same broke, lonely line cook every time, so how comfortable a life
    starts is drawn and handed to it as a constraint.
    """
    llm, resolver, narrator, _ = build(store, config, call_log, [SPAWN], ["Quiet."])
    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    spawn_prompts = [user for role, _, user in llm.prompts if role == "resolver"]
    assert "station_in_life" in spawn_prompts[0]


def test_the_extremes_are_edge_cases_not_the_standard():
    """The weights are the point, so they get a test rather than a comment."""
    drawn = [draw_station(random.Random(seed)) for seed in range(4000)]
    share = {
        description: drawn.count(description) / len(drawn)
        for _, description in STATIONS
    }
    hardest, *_, richest = [description for _, description in STATIONS]

    # Neither end is where most lives land.
    assert share[hardest] < 0.08, "destitute should be rare, not the default"
    assert share[richest] < 0.10, "wealthy should be rare too"
    # And the ordinary middle is the standard.
    middle = sum(share[d] for _, d in STATIONS[1:4])
    assert middle > 0.80

    # Every station really does come up: no band is decorative.
    assert all(count > 0 for count in share.values())


def test_the_weights_are_a_distribution():
    assert sum(weight for weight, _ in STATIONS) == 100
    assert all(weight > 0 for weight, _ in STATIONS)


# ---- transient state is not a fact (playtest-20261011, top finding) ----


def test_momentary_state_is_never_a_fact(store, config, call_log):
    """Facts are permanent (rule 3), so momentary state has no business being one.

    A live run settled `posture = "sitting on a bench"` on the player. A later
    turn proposing a new value was refused as a duplicate rather than treated
    as a change, so it still said that after they had walked home — and nothing
    could correct it. Transient state is its own table now, and a model that
    still writes it as a fact has it routed there rather than dropped.
    """
    stateful = reply(
        intent="walk home",
        # A real destination. Written as the spawn city before, which with the
        # travel check in place is the finding-4 contradiction: a declared walk
        # home that bills for the walk and ends where it started.
        player_location="place:bar",
        summary="you walk",
        new_facts=[
            ["person:you", "posture", "sitting on a bench"],
            ["person:you", "activity", "eating pickles from the jar"],
            ["person:you", "location", "place:kitchen"],
            ["place:bar", "occupants", "person:you"],
            # Lasting condition is still a fact.
            ["person:you", "condition", "a broken wrist, splinted"],
        ],
    )
    llm, resolver, narrator, turn = build(
        store, config, call_log, [SPAWN, stateful], ["Quiet.", "The street is empty."]
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    result = turn.play("I walk home", life)

    # Nothing momentary reached the facts table, where rule 3 would freeze it.
    for momentary in ("posture", "activity"):
        assert store.live_fact(PLAYER_ID, momentary) is None
    # Where something is stays the store's job, and is dropped outright.
    assert store.live_fact(PLAYER_ID, "location") is None
    assert store.live_fact("place:bar", "occupants") is None
    for structural in ("location", "occupants"):
        assert any(structural in item for item in result.dropped)
    # Posture was kept, as state, and is in the turn's record of what changed.
    assert store.state_of(PLAYER_ID, "activity") == "eating pickles from the jar"
    assert any("activity" in line for line in result.changed_state)
    # And the durable one landed.
    assert store.live_fact(PLAYER_ID, "condition").value == "a broken wrist, splinted"


def test_the_fact_or_state_boundary():
    """Would it still be true tomorrow if nobody did anything? (spec 2.1)"""
    from worldsim.resolve import is_transient, not_a_fact

    # No: current state, overwritten freely.
    for momentary in ("posture", "activity", "in_hand", "mood", "vantage"):
        assert is_transient(momentary), momentary
    # Yes: a fact, kept forever and superseded with a cause.
    for durable in ("name", "job", "condition", "wearing", "carrying", "cash_usd"):
        assert not is_transient(durable), durable
        assert not_a_fact(durable) is None, durable
    # And neither: the store already models where things are.
    assert not_a_fact("location") and "location" in not_a_fact("location")
    assert not_a_fact("occupants")
