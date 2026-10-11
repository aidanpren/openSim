"""The rules the store enforces (spec 2, spec 14)."""

from __future__ import annotations

import pytest

from worldsim.store import ClockWentBackward, FactConflict, StoreError


# ---- the clock (rule 7) ----


def test_clock_never_moves_backward(store):
    before = store.now()
    with pytest.raises(ClockWentBackward):
        store.advance(-30)
    assert store.now() == before


def test_clock_moves_by_the_duration_it_is_given(store):
    before = store.now()
    after = store.advance(90)
    assert (after - before).total_seconds() == 90 * 60
    assert store.now() == after


# ---- facts are history, not state (rule 3) ----


def test_a_second_live_fact_for_a_key_is_rejected(world):
    world.add_fact("person:bartender", "name", "Marisol", "invented", "obvious", "turn 1")
    with pytest.raises(FactConflict):
        world.add_fact("person:bartender", "name", "Someone Else", "invented", "obvious", "turn 2")
    assert world.live_fact("person:bartender", "name").value == "Marisol"


def test_superseding_keeps_the_old_fact_as_history(world):
    old = world.add_fact(
        "place:bar", "hours", "open until 2am", "world-knowledge", "obvious", "turn 1"
    )
    new = world.supersede(
        "place:bar", "hours", "closed for repairs", "invented", "obvious", "turn 9: fire"
    )

    assert world.live_fact("place:bar", "hours").value == "closed for repairs"
    assert world.fact(old.id).superseded_by == new.id
    assert [f.value for f in world.history("place:bar", "hours")] == [
        "open until 2am",
        "closed for repairs",
    ]
    assert world.fact(old.id).value == "open until 2am"


def test_a_fact_must_cite_a_cause(world):
    with pytest.raises(StoreError):
        world.add_fact("place:bar", "smell", "stale beer", "invented", "obvious", "")


def test_superseding_a_fact_that_does_not_exist_is_refused(world):
    with pytest.raises(StoreError):
        world.supersede("place:bar", "hours", "closed", "invented", "obvious", "turn 2")


def test_unknown_source_and_visibility_are_refused(world):
    with pytest.raises(StoreError):
        world.add_fact("place:bar", "a", "x", "made-up-source", "obvious", "turn 1")
    with pytest.raises(StoreError):
        world.add_fact("place:bar", "b", "x", "invented", "sort-of-visible", "turn 1")


# ---- quantities change only by delta (rule 4) ----


def test_cash_changes_by_exactly_the_delta(world):
    world.add_fact("person:you", "cash_usd", 1200, "invented", "hidden", "spawn")

    assert world.apply_delta("person:you", "cash_usd", -37.5, "turn 4: bought a round") == 1162.5
    assert world.apply_delta("person:you", "cash_usd", 500, "turn 7: won a hand") == 1662.5
    assert world.live_fact("person:you", "cash_usd").value == 1662.5


def test_a_quantity_cannot_be_superseded_with_a_written_total(world):
    world.add_fact("person:you", "cash_usd", 1200, "invented", "hidden", "spawn")
    with pytest.raises(StoreError, match="apply_delta"):
        world.supersede("person:you", "cash_usd", "9999", "invented", "hidden", "turn 2")
    assert world.live_fact("person:you", "cash_usd").value == 1200


def test_a_delta_on_a_text_fact_is_refused(world):
    world.add_fact("person:you", "job", "line cook", "invented", "obvious", "spawn")
    with pytest.raises(StoreError):
        world.apply_delta("person:you", "job", 1, "turn 2")


def test_every_quantity_change_leaves_a_trail(world):
    world.add_fact("person:you", "cash_usd", 100, "invented", "hidden", "spawn")
    world.apply_delta("person:you", "cash_usd", -40, "turn 2: whiskey")
    causes = [f.cause for f in world.history("person:you", "cash_usd")]
    assert causes == ["spawn", "turn 2: whiskey"]


# ---- revisit: the world remembers (milestone 1 done-when) ----


def test_a_place_is_the_same_after_leaving_and_coming_back(world):
    world.add_fact("person:bartender", "name", "Marisol", "invented", "obvious", "turn 1")
    world.add_fact(
        "person:bartender", "detail", "forearm tattoo of a swallow", "invented", "noticeable", "turn 1"
    )
    before = world.live_facts("person:bartender")

    world.move("person:you", "city:kcmo")
    world.advance(240)
    world.move("person:you", "place:bar")

    assert world.live_facts("person:bartender") == before
    assert world.entity("person:bartender").location_id == "place:bar"


# ---- items (milestone 1 done-when) ----


def test_putting_an_entity_in_a_place_that_does_not_exist_is_a_store_error(world):
    # A real run crashed here: sqlite raised IntegrityError, which callers
    # draining model output were not catching.
    with pytest.raises(StoreError):
        world.add_entity("place:ghost", "place", "Nowhere", location_id="place:nope")
    assert world.entity("place:ghost") is None


def test_moving_into_a_place_that_does_not_exist_is_refused(world):
    with pytest.raises(StoreError):
        world.move("person:you", "place:nope")
    assert world.entity("person:you").location_id == "place:bar"


def test_an_entity_cannot_be_inside_itself(world):
    with pytest.raises(StoreError):
        world.move("place:bar", "place:bar")


def test_an_item_picked_up_is_on_you_and_gone_when_given_away(world):
    world.add_entity("thing:bottle", "object", "bottle of whiskey", location_id="place:bar")

    world.move("thing:bottle", "person:you")
    assert "thing:bottle" in [e.id for e in world.contents("person:you")]

    world.move("thing:bottle", "person:bartender")
    assert "thing:bottle" not in [e.id for e in world.contents("person:you")]
    assert "thing:bottle" in [e.id for e in world.contents("person:bartender")]


# ---- knowledge and lies (spec 2.3) ----


def test_a_mind_knows_only_what_it_learned(world):
    secret = world.add_fact(
        "person:bartender", "has_a_gun_under_the_bar", "yes", "invented", "hidden", "turn 1"
    )
    seen = world.add_fact(
        "person:bartender", "name", "Marisol", "invented", "obvious", "turn 1"
    )
    world.learn("person:you", seen.id, "saw her nametag")

    known_ids = [k.fact.id for k in world.known("person:you")]
    assert seen.id in known_ids
    assert secret.id not in known_ids


def test_a_belief_can_differ_from_the_fact(world):
    truth = world.add_fact("person:bartender", "name", "Marisol", "invented", "obvious", "turn 1")
    world.learn("person:you", truth.id, "she told me", belief="Sam")

    held = world.known("person:you")[0]
    assert held.value == "Sam"
    assert held.fact.value == "Marisol"


# ---- lives are separate from the world (rule 8) ----


def test_ending_a_life_leaves_every_world_fact_unchanged(world):
    world.add_fact("place:bar", "hours", "open until 2am", "world-knowledge", "obvious", "turn 1")
    world.add_fact("person:you", "cash_usd", 1200, "invented", "hidden", "spawn")
    life = world.start_life("person:you")

    before = {
        entity: world.live_facts(entity)
        for entity in ("place:bar", "person:you", "person:bartender")
    }

    world.end_life(life.id, "The cold comes up through the floor, and then nothing.")

    after = {
        entity: world.live_facts(entity)
        for entity in ("place:bar", "person:you", "person:bartender")
    }
    assert after == before
    assert world.life(life.id).ended_at is not None
    assert world.current_life() is None


# ---- resuming (build step 9) ----


def test_recent_turns_come_back_oldest_first(world):
    life = world.start_life("person:you")
    for n in range(5):
        world.advance(10)
        world.log_turn(life.id, f"turn {n}", f"intent {n}", None, f"narration {n}")

    recent = world.recent_turns(life.id, limit=3)
    assert [row["player_said"] for row in recent] == ["turn 2", "turn 3", "turn 4"]
