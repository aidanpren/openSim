"""Narration starts before the resolver has finished writing.

The resolver emits a scene object then a ledger object. The scene commits and
narrates as soon as it closes, while the ledger is still arriving. That is the
whole latency fix, so it gets tests that would fail if the two ever became
sequential again.
"""

from __future__ import annotations

import json
import random

import pytest

from worldsim.llm import FakeLLM, LedgerFailed, ModelCallFailed, first_object_end
from worldsim.render import Narrator
from worldsim.resolve import PLAYER_ID, Resolver
from worldsim.turn import Turn, begin_life
from worldsim.__main__ import PROMPTS_DIR
from helpers import reply

SPAWN = json.dumps(
    {
        "new_entities": [
            {"id": "place:kitchen", "kind": "place", "name": "The line", "location_id": "place:spawn-city"}
        ],
        "new_facts": [
            {"entity_id": "person:you", "key": "cash_usd", "value": 20, "source": "invented", "visibility": "hidden"}
        ],
        "player_starts_in": "place:kitchen",
        "opening_situation": "rent is late",
    }
)


class OrderTrackingLLM(FakeLLM):
    """Records when chunks are produced relative to the narrator call."""

    def __init__(self, config, call_log, scripts):
        super().__init__(config, call_log, scripts)
        self.events: list[str] = []

    def _raw_stream(self, role, model, system, user):
        for chunk in super()._raw_stream(role, model, system, user):
            self.events.append("chunk")
            yield chunk

    def _raw(self, role, model, system, user):
        if role == "narrator":
            self.events.append("narrate")
        return super()._raw(role, model, system, user)


# ---- the splitter ----


@pytest.mark.parametrize(
    "text,first",
    [
        ('{"a":1}{"b":2}', '{"a":1}'),
        ('{"a":{"n":1}}{"b":2}', '{"a":{"n":1}}'),          # nested braces
        ('{"a":"}{"}{"b":2}', '{"a":"}{"}'),                 # braces inside a string
        (r'{"s":"he said \"hi\""}{"b":2}', r'{"s":"he said \"hi\""}'),  # escaped quotes
    ],
)
def test_the_first_object_is_found_exactly(text, first):
    assert text[: first_object_end(text)] == first


def test_an_unfinished_object_is_not_split():
    assert first_object_end('{"a":1') is None
    assert first_object_end('{"a":{"b":2}') is None


# ---- interleaving ----


def test_the_narrator_runs_before_the_stream_has_finished(store, config, call_log):
    turn_reply = reply(
        {"deltas": [{"entity_id": "person:you", "key": "cash_usd", "delta": -5}]},
        player_location="place:kitchen",
        summary="you pay",
    )
    llm = OrderTrackingLLM(
        config, call_log, {"resolver": [SPAWN, turn_reply], "narrator": ["Oil ticks.", "Coins on the counter."]}
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)

    llm.events.clear()
    turn.play("I pay for the beer", life)

    assert "narrate" in llm.events
    narrated_at = llm.events.index("narrate")
    chunks_after = llm.events[narrated_at:].count("chunk")
    assert chunks_after > 0, "the narrator waited for the whole stream"
    # And the ledger still landed.
    assert store.live_fact(PLAYER_ID, "cash_usd").value == 15.0


def test_time_to_scene_is_logged_separately_from_the_whole_call(store, config, call_log):
    llm = FakeLLM(
        config, call_log,
        {"resolver": [SPAWN, reply(player_location="place:kitchen")], "narrator": ["Oil ticks.", "Quiet."]},
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)

    turn.play("I wait", life)

    streamed = [r for r in call_log.records if r.scene_seconds is not None]
    assert streamed, "a streamed resolver call should record scene_seconds"
    assert streamed[-1].scene_seconds <= streamed[-1].seconds


# ---- failure modes ----


def test_a_bad_scene_is_retried_and_never_committed(store, config, call_log):
    nonsense = "not json at all{}"
    good = reply(player_location="place:kitchen", summary="fine")
    llm = FakeLLM(
        config, call_log,
        {"resolver": [SPAWN, nonsense, good], "narrator": ["Oil ticks.", "Quiet."]},
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)
    before = store.now()

    result = turn.play("I wait", life)

    assert result.summary == "fine"
    # The clock moved once, not twice: the rejected scene committed nothing.
    assert (store.now() - before).total_seconds() == 5 * 60


def test_a_reply_with_no_complete_object_fails_cleanly(store, config, call_log):
    """This crashed a live run with NameError instead of failing the turn."""
    truncated = '{"intent": "walk", "duration_minutes": 2, "player_loc'
    llm = FakeLLM(
        config, call_log,
        {"resolver": [SPAWN, truncated, truncated], "narrator": ["Oil ticks.", "x"]},
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)

    with pytest.raises(ModelCallFailed) as raised:
        turn.play("I walk out", life)
    assert "no complete JSON object" in str(raised.value)


def test_prose_instead_of_json_fails_cleanly(store, config, call_log):
    llm = FakeLLM(
        config, call_log,
        {"resolver": [SPAWN, "Sure! Here is what happens.", "Still no JSON."],
         "narrator": ["Oil ticks.", "x"]},
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)

    with pytest.raises(ModelCallFailed):
        turn.play("I walk out", life)


def test_a_bad_ledger_is_loud_because_the_turn_cannot_be_taken_back(
    store, config, call_log
):
    half_bad = reply(player_location="place:kitchen") .replace("{}", '{"deltas": "not a list"}')
    llm = FakeLLM(
        config, call_log,
        {"resolver": [SPAWN, half_bad], "narrator": ["Oil ticks.", "Quiet."]},
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city)

    # Distinct from ModelCallFailed: the scene is already true and already said.
    with pytest.raises(LedgerFailed):
        turn.play("I wait", life)
    assert issubclass(LedgerFailed, ModelCallFailed)
