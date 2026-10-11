"""Rule 4 at the model boundary (spec 2.1, 3.1).

A real run against hermes-4-405b wrote cash as the string "27", which landed in
the store as a text fact. Text facts can be superseded, so the model could then
write a new total directly, straight through rule 4. These tests pin that shut.
"""

from __future__ import annotations

import json
import random

import pytest
from pydantic import ValidationError

from worldsim.llm import FakeLLM, ModelCallFailed
from worldsim.render import Narrator
from worldsim.resolve import NewFact, Resolver, Supersede, is_quantity
from worldsim.turn import Turn, begin_life
from worldsim.__main__ import PROMPTS_DIR
from helpers import reply


def a_fact(key, value):
    return NewFact(
        entity_id="person:you", key=key, value=value,
        source="invented", visibility="hidden",
    )


def test_a_quantity_written_as_a_string_becomes_a_number():
    assert a_fact("cash_usd", "27").value == 27.0
    assert a_fact("cash_usd", 27).value == 27.0
    assert a_fact("smokes_count", "3").value == 3.0


def test_money_shaped_strings_are_still_read():
    assert a_fact("cash_usd", "$1,200").value == 1200.0
    assert a_fact("cash_usd", " 18.50 ").value == 18.5


def test_a_quantity_that_is_not_a_number_at_all_is_refused():
    with pytest.raises(ValidationError):
        a_fact("cash_usd", "a couple hundred")


def test_text_facts_are_left_alone():
    assert a_fact("job", "line cook").value == "line cook"
    assert a_fact("history", "2019").value == "2019"


def test_only_quantity_keys_are_treated_as_quantities():
    assert is_quantity("cash_usd") and is_quantity("beers_count")
    assert not is_quantity("job") and not is_quantity("age")


def test_a_quantity_cannot_be_superseded_with_a_total():
    with pytest.raises(ValidationError, match="delta"):
        Supersede(
            entity_id="person:you", key="cash_usd", value="18.50",
            source="invented", visibility="hidden",
        )


# ---- end to end: the bug the real run exposed ----

SPAWN_WITH_STRING_CASH = json.dumps(
    {
        "new_entities": [
            {"id": "place:diner", "kind": "place", "name": "Milt's", "location_id": "place:spawn-city"}
        ],
        "new_facts": [
            {"entity_id": "person:you", "key": "name", "value": "Ethan Carter", "source": "invented", "visibility": "obvious"},
            {"entity_id": "person:you", "key": "cash_usd", "value": "27", "source": "invented", "visibility": "hidden"},
        ],
        "player_starts_in": "place:diner",
        "opening_situation": "rent is late",
    }
)

BUY_CIGARETTES = reply(
    {"deltas": [{"entity_id": "person:you", "key": "cash_usd", "delta": -8.5}]},
    intent="buy a pack of cigarettes",
    duration_minutes=3,
    player_location="place:diner",
    summary="you pay and pocket them",
)


def build(store, config, call_log, resolver_script, narrator_script):
    llm = FakeLLM(config, call_log, {"resolver": resolver_script, "narrator": narrator_script})
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, PROMPTS_DIR)
    return resolver, narrator, Turn(store, resolver, narrator, random.Random(7))


def test_a_purchase_changes_cash_by_exactly_the_delta(store, config, call_log):
    resolver, narrator, turn = build(
        store, config, call_log,
        [SPAWN_WITH_STRING_CASH, BUY_CIGARETTES],
        ["The fryer ticks as it cools.", "The clerk slides them across."],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)

    # Stored as a number despite the model sending a string.
    assert store.live_fact("person:you", "cash_usd").value == 27.0

    result = turn.play("I buy a pack of cigarettes", life)

    assert store.live_fact("person:you", "cash_usd").value == 18.5
    assert result.dropped == []


def test_the_player_starts_in_a_specific_place_not_the_city(store, config, call_log):
    resolver, narrator, turn = build(
        store, config, call_log, [SPAWN_WITH_STRING_CASH], ["The fryer ticks."]
    )
    begin_life(store, resolver, narrator, config.spawn_city)

    assert store.entity("person:you").location_id == "place:diner"


def test_a_model_that_insists_on_superseding_cash_fails_the_turn(store, config, call_log):
    bad = reply(
        {
            "superseded": [
                {"entity_id": "person:you", "key": "cash_usd", "value": "18.50",
                 "source": "invented", "visibility": "hidden"}
            ]
        },
        intent="count what's left",
        duration_minutes=1,
        player_location="place:diner",
        summary="you count it",
    )
    resolver, narrator, turn = build(
        store, config, call_log,
        [SPAWN_WITH_STRING_CASH, bad, bad],
        ["The fryer ticks.", "unused"],
    )
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)

    # Rejected, retried once, rejected again, so nothing commits (spec 3.3).
    with pytest.raises(ModelCallFailed):
        turn.play("I count what's left", life)

    assert store.live_fact("person:you", "cash_usd").value == 27.0
