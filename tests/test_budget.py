"""Spec 5.2: every call's input stays within its budget, at turn 500 too.

Before this, nothing enforced the rule and `config.token_budgets` was read by
no code at all. The narrator was handed every fact the character had ever
learned and the resolver every entity nested in wherever the player stood, so
both prompts grew for the length of a campaign — money, latency, and the
salience of what the narration actually mentions.

Spec 14's "Budget" test lives here. The full priority-ordered assembly is
milestone 2; what these pin is that neither prompt grows without bound.
"""

from __future__ import annotations

import json
import random

from worldsim.budget import estimate_tokens, take_within
from worldsim.llm import FakeLLM
from worldsim.render import Narrator
from worldsim.resolve import PLAYER_ID, Resolver
from worldsim.turn import Turn, begin_life
from worldsim.__main__ import PROMPTS_DIR
from helpers import reply

SPAWN = json.dumps(
    {
        "new_entities": [
            {"id": "place:bar", "kind": "place", "name": "The Peanut", "location_id": "place:spawn-city"}
        ],
        "new_facts": [
            {"entity_id": "person:you", "key": "name", "value": "Dale Rusk", "source": "invented", "visibility": "obvious"}
        ],
        "player_starts_in": "place:bar",
        "opening_situation": "rent is late",
    }
)


def test_take_within_keeps_the_leading_items_that_fit():
    items = ["x" * 40] * 10  # ten items of roughly ten tokens each
    assert len(take_within(items, 100)) == 10
    assert len(take_within(items, 35)) == 3
    assert len(take_within(items, 0)) == 1, "one item always survives"


def test_a_long_game_does_not_grow_either_prompt(store, config, call_log):
    """500 turns of accumulating facts, with every prompt measured.

    The player stays in one place that keeps gaining facts, and keeps learning
    things about people elsewhere — the two things that used to grow the
    prompts turn on turn.
    """
    turns = 500
    script = [SPAWN] + [
        reply(
            intent=f"look around again, turn {n}",
            player_location="place:bar",
            summary=f"turn {n}",
            new_facts=[
                [f"place:bar", f"detail_{n}", f"something new about the room, number {n}"],
                [f"person:ghost-{n}", f"note_{n}", f"a thing you learned about them, number {n}"],
            ],
            new_entities=[[f"person:ghost-{n}", "person", f"Person {n}", "place:spawn-city"]],
            perceived=[[f"place:bar", f"detail_{n}"], [f"person:ghost-{n}", f"note_{n}"]],
        )
        for n in range(turns)
    ]
    llm = FakeLLM(
        config, call_log,
        {"resolver": script, "narrator": ["Quiet."] + [f"Line {n}." for n in range(turns)]},
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    for n in range(turns):
        turn.play(f"I look around, turn {n}", life)

    budgets = config.token_budgets
    over = [
        (role, estimate_tokens(system + user))
        for role, system, user in llm.prompts
        if estimate_tokens(system + user) > budgets[role]
    ]
    assert not over, f"{len(over)} calls went over budget, worst {max(t for _, t in over)}"

    # And the world really did keep growing underneath: the cap is doing work,
    # not passing because nothing accumulated.
    assert len(store.live_facts("place:bar")) > turns / 2
    assert len(store.known(PLAYER_ID)) > turns


def test_the_room_survives_the_trim_and_the_far_past_is_what_goes(store, config, call_log):
    """Priority order, not an arbitrary cut: what is here outlives what is not."""
    llm = FakeLLM(config, call_log, {"resolver": [SPAWN], "narrator": ["Quiet."]})
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    here = store.add_fact(
        "place:bar", "smell", "stale beer and fryer oil", "invented", "obvious", "t"
    )
    store.learn(PLAYER_ID, here.id, "you can smell it")
    store.add_entity("place:elsewhere", "place", "a motel in Topeka")
    for n in range(400):
        far = store.add_fact(
            "place:elsewhere", f"detail_{n}", f"something from months ago, {n}" * 4,
            "invented", "obvious", "t",
        )
        store.learn(PLAYER_ID, far.id, "you were there once")

    context = narrator.context("look around", [], [here.id])
    keys = [entry["key"] for entry in context["your_knowledge"]]

    assert "smell" in keys, "the room the character is standing in was dropped"
    assert len(keys) < 400, "nothing was trimmed at all"
    # The oldest memories of elsewhere go first, so the newest survive.
    assert "detail_399" in keys and "detail_0" not in keys
