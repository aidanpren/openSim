"""Revisit, items, and relaunch through the whole turn cycle (spec 14).

The store-level versions of these live in test_store.py. These drive the real
turn cycle with FakeLLM, which is what the milestone's "done when" asks for.
"""

from __future__ import annotations

import json
import random

from worldsim.llm import FakeLLM
from worldsim.render import Narrator
from worldsim.resolve import PLAYER_ID, Resolver
from worldsim.sheet import character_sheet
from worldsim.store import Store
from worldsim.turn import Turn, begin_life
from worldsim.__main__ import PROMPTS_DIR, local_time
from helpers import IN_MOTION_SCHEDULE, IN_MOTION_SECRETS, reply, scripts

SPAWN = json.dumps(
    {
        "new_entities": [
            {"id": "place:apartment", "kind": "place", "name": "Apartment 3B", "location_id": "place:spawn-city"}
        ],
        "new_facts": [
            {"entity_id": "person:you", "key": "name", "value": "Dale Rusk", "source": "invented", "visibility": "obvious"},
            {"entity_id": "person:you", "key": "cash_usd", "value": 40, "source": "invented", "visibility": "noticeable"},
            {"entity_id": "place:spawn-city", "key": "timezone", "value": "America/Chicago", "source": "world-knowledge", "visibility": "obvious"},
        ],
        "player_starts_in": "place:apartment",
        "secrets": IN_MOTION_SECRETS,
        "schedule": IN_MOTION_SCHEDULE,
        "opening_situation": "rent is late",
    }
)

# Walking into a bar that doesn't exist yet, settling the bartender on arrival.
TO_THE_BAR = reply(
    intent="walk to the bar",
    duration_minutes=12,
    player_location="place:bar",
    summary="you get a stool",
    new_entities=[
        {"id": "place:bar", "kind": "place", "name": "The Peanut", "location_id": "place:spawn-city"},
        {"id": "person:marisol", "kind": "person", "name": "Marisol", "location_id": "place:bar"},
    ],
    new_facts=[
        {"entity_id": "person:marisol", "key": "name", "value": "Marisol", "source": "invented", "visibility": "obvious"},
        {"entity_id": "person:marisol", "key": "tattoo", "value": "a swallow on her forearm", "source": "invented", "visibility": "noticeable"},
    ],
    perceived=[
        {"entity_id": "person:marisol", "key": "name"},
        {"entity_id": "person:marisol", "key": "tattoo"},
    ],
)

HOME_AGAIN = reply(
    intent="walk home", duration_minutes=12,
    player_location="place:apartment", summary="you let yourself in",
)

BACK_TO_THE_BAR = reply(
    intent="go back to the bar", duration_minutes=12,
    player_location="place:bar", summary="the same stool",
)


def build(store, config, call_log, resolver_script, narrator_script):
    llm = FakeLLM(
        config, call_log, scripts(resolver_script, narrator_script)
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    return llm, resolver, narrator, Turn(store, resolver, narrator, random.Random(7))


# ---- revisit ----


def test_a_bar_is_the_same_bar_when_you_come_back(store, config, call_log):
    llm, resolver, narrator, turn = build(
        store, config, call_log,
        [SPAWN, TO_THE_BAR, HOME_AGAIN, BACK_TO_THE_BAR],
        ["Quiet.", "Smoke and old beer.", "Your door sticks.", "The same stool."],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    turn.play("I walk to the bar", life)
    before = store.live_facts("person:marisol")
    assert [f.value for f in before] == ["Marisol", "a swallow on her forearm"]

    turn.play("I walk home", life)
    assert store.entity(PLAYER_ID).location_id == "place:apartment"

    turn.play("I go back to the bar", life)

    assert store.entity(PLAYER_ID).location_id == "place:bar"
    # Same person, same details, not re-invented.
    assert store.live_facts("person:marisol") == before
    assert store.entity("person:marisol").location_id == "place:bar"


def test_coming_back_does_not_duplicate_the_place(store, config, call_log):
    llm, resolver, narrator, turn = build(
        store, config, call_log,
        [SPAWN, TO_THE_BAR, HOME_AGAIN, TO_THE_BAR],  # proposes the bar a second time
        ["Quiet.", "Smoke.", "Your door sticks.", "The same stool."],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    for said in ("I walk to the bar", "I walk home", "I go back to the bar"):
        turn.play(said, life)

    # The second proposal of place:bar and person:marisol is ignored, and the
    # facts are not written twice.
    assert len(store.history("person:marisol", "name")) == 1
    assert store.live_fact("person:marisol", "name").value == "Marisol"


def test_what_you_learned_about_her_stays_learned(store, config, call_log):
    llm, resolver, narrator, turn = build(
        store, config, call_log,
        [SPAWN, TO_THE_BAR, HOME_AGAIN],
        ["Quiet.", "Smoke.", "Your door sticks."],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    turn.play("I walk to the bar", life)
    turn.play("I walk home", life)

    known = {(k.fact.entity_id, k.fact.key) for k in store.known(PLAYER_ID)}
    assert ("person:marisol", "name") in known
    assert ("person:marisol", "tattoo") in known


# ---- items ----


def test_an_item_you_pick_up_shows_on_the_sheet_and_leaves_when_given(
    store, config, call_log
):
    pick_up = reply(
        {"moves": [{"entity_id": "thing:bottle", "location_id": "person:you"}]},
        intent="pick up the bottle",
        duration_minutes=1,
        player_location="place:apartment",
        summary="it's warm from the sun",
        new_entities=[
            {"id": "thing:bottle", "kind": "object", "name": "bottle of whiskey", "location_id": "place:apartment"}
        ],
    )
    give_away = reply(
        {"moves": [{"entity_id": "thing:bottle", "location_id": "person:neighbour"}]},
        intent="hand it over",
        duration_minutes=1,
        player_location="place:apartment",
        summary="he takes it",
        new_entities=[
            {"id": "person:neighbour", "kind": "person", "name": "Ray", "location_id": "place:apartment"}
        ],
    )
    llm, resolver, narrator, turn = build(
        store, config, call_log,
        [SPAWN, pick_up, give_away],
        ["Quiet.", "The glass is warm.", "He nods and shuts the door."],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    turn.play("I pick up the bottle", life)
    assert "bottle of whiskey" in character_sheet(store)

    turn.play("I hand it to Ray", life)
    assert "bottle of whiskey" not in character_sheet(store)
    assert store.entity("thing:bottle").location_id == "person:neighbour"


# ---- relaunch ----


def test_quitting_and_reopening_keeps_the_place_the_time_and_the_sheet(
    tmp_path, config, call_log
):
    db = tmp_path / "saves" / "relaunch" / "world.db"
    first = Store(db)
    llm, resolver, narrator, turn = build(
        first, config, call_log, [SPAWN, TO_THE_BAR], ["Quiet.", "Smoke and old beer."]
    )
    life, _ = begin_life(first, resolver, narrator, config.spawn_city, random.Random(7))
    turn.play("I walk to the bar", life)

    expected = {
        "where": first.entity(PLAYER_ID).location_id,
        "when": first.now(),
        "sheet": character_sheet(first),
        "time": local_time(first),
        "life": first.current_life().id,
    }
    last_narration = first.recent_turns(life.id, limit=1)[0]["narration"]
    first.close()

    # Relaunch: a new Store on the same file, no model calls at all.
    calls_before = len(call_log.records)
    again = Store(db)

    assert again.entity(PLAYER_ID).location_id == expected["where"]
    assert again.now() == expected["when"]
    assert character_sheet(again) == expected["sheet"]
    assert local_time(again) == expected["time"]
    assert again.current_life().id == expected["life"]
    assert again.recent_turns(expected["life"], limit=1)[0]["narration"] == last_narration
    assert len(call_log.records) == calls_before
    again.close()


def test_reopening_does_not_restart_the_clock(tmp_path, config, call_log):
    db = tmp_path / "saves" / "clock" / "world.db"
    first = Store(db)
    llm, resolver, narrator, turn = build(
        first, config, call_log, [SPAWN, TO_THE_BAR], ["Quiet.", "Smoke."]
    )
    life, _ = begin_life(first, resolver, narrator, config.spawn_city, random.Random(7))
    turn.play("I walk to the bar", life)
    moved_to = first.now()
    first.close()

    again = Store(db)
    assert again.now() == moved_to
    # And it still only goes forward from there.
    assert again.advance(5) > moved_to
    again.close()


def test_two_people_with_the_same_name_stay_separate_on_the_sheet(world):
    """The sheet grouped by name, so a world with two Garys merged them."""
    world.add_entity("person:gary-bar", "person", "Gary", location_id="place:bar")
    world.add_entity("person:gary-landlord", "person", "Gary", location_id="city:kcmo")
    bar = world.add_fact(
        "person:gary-bar", "job", "tends the bar", "invented", "obvious", "met him"
    )
    landlord = world.add_fact(
        "person:gary-landlord", "job", "owns the building", "invented", "obvious", "met him"
    )
    world.learn(PLAYER_ID, bar.id, "talked to him")
    world.learn(PLAYER_ID, landlord.id, "he knocked")

    sheet = character_sheet(world)

    assert "tends the bar" in sheet
    assert "owns the building" in sheet
