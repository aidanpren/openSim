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

from worldsim.llm import (
    FakeLLM,
    LedgerFailed,
    ModelCallFailed,
    ObjectScanner,
    first_object_end,
    json_start,
)
from worldsim.render import Narrator
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
            {"entity_id": "person:you", "key": "cash_usd", "value": 20, "source": "invented", "visibility": "noticeable"}
        ],
        "player_starts_in": "place:kitchen",
        "secrets": IN_MOTION_SECRETS,
        "schedule": IN_MOTION_SCHEDULE,
        "opening_situation": "rent is late",
    }
)


class OrderTrackingLLM(FakeLLM):
    """Records when chunks are produced relative to the narrator call."""

    def __init__(self, config, call_log, scripts):
        super().__init__(config, call_log, scripts)
        self.events: list[str] = []

    def _raw_stream(self, role, model, system, user):
        # The narrator streams now too, so which role a chunk belongs to is
        # what distinguishes "the resolver is still writing" from "the voice
        # has started".
        if role == "narrator":
            self.events.append("narrate")
        for chunk in super()._raw_stream(role, model, system, user):
            if role == "resolver":
                self.events.append("chunk")
            yield chunk


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
    # The opening has to settle something the character notices, or there is no
    # first beat to fire mid-stream — a turn that perceives nothing falls back
    # to one narration after the whole call, which is what this guards against.
    turn_reply = reply(
        {"deltas": [{"entity_id": "person:you", "key": "cash_usd", "delta": -5}]},
        player_location="place:kitchen",
        summary="you pay",
        new_facts=[["place:kitchen", "ticket_rail", "three tickets deep"]],
    )
    llm = OrderTrackingLLM(
        config, call_log, scripts([SPAWN, turn_reply], ["Oil ticks.", "Coins on the counter."])
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

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
        scripts([SPAWN, reply(player_location="place:kitchen")], ["Oil ticks.", "Quiet."]),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

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
        scripts([SPAWN, nonsense, good], ["Oil ticks.", "Quiet."]),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
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
        scripts([SPAWN, truncated, truncated], ["Oil ticks.", "x"]),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    with pytest.raises(ModelCallFailed) as raised:
        turn.play("I walk out", life)
    assert "no complete JSON object" in str(raised.value)


def test_prose_instead_of_json_fails_cleanly(store, config, call_log):
    llm = FakeLLM(
        config, call_log,
        scripts([SPAWN, "Sure! Here is what happens.", "Still no JSON."], ["Oil ticks.", "x"]),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    with pytest.raises(ModelCallFailed):
        turn.play("I walk out", life)


def test_the_llm_layer_raises_ledger_failed_distinctly(store, config, call_log):
    """stream_objects cannot retry a bad ledger: the scene is committed.

    turn.play catches this and keeps the narration — see
    test_an_unusable_ledger_still_delivers_the_narration — but the distinction
    has to exist at this layer so the caller can tell the two apart.
    """
    half_bad = reply(player_location="place:kitchen").replace(
        "{}", '{"deltas": "not a list"}'
    )
    llm = FakeLLM(config, call_log, scripts([half_bad], []))
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    store.add_entity("place:kitchen", "place", "The line")

    with pytest.raises(LedgerFailed):
        resolver.resolve("I wait", [], lambda scene: None)
    assert issubclass(LedgerFailed, ModelCallFailed)


# ---- transport failures on the streaming path ----
#
# A live 402 crashed uncaught here: all the retry and classification logic lived
# in _call, which the streaming path never touches.


class StatusError(Exception):
    def __init__(self, status_code, message="boom"):
        super().__init__(f"Error code: {status_code} - {message}")
        self.status_code = status_code


class FlakyStreamLLM(FakeLLM):
    """Raises on the first n attempts to open the resolver stream."""

    def __init__(self, config, call_log, scripts, make_error, failures):
        super().__init__(config, call_log, scripts)
        self.make_error = make_error
        self.failures = failures
        self.opens = 0

    def _raw_stream(self, role, model, system, user):
        if role == "resolver":
            self.opens += 1
            if self.opens <= self.failures:
                raise self.make_error()
        for chunk in super()._raw_stream(role, model, system, user):
            yield chunk


def drive(store, config, call_log, make_error, failures):
    llm = FlakyStreamLLM(
        config,
        call_log,
        scripts(
            [SPAWN, reply(player_location="place:kitchen")],
            ["Oil ticks.", "You lean over the pit.", "The pit is backed up."],
        ),
        make_error,
        failures,
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))
    return llm, turn, life


def test_a_transient_status_reopens_the_stream(store, config, call_log, monkeypatch):
    monkeypatch.setattr("worldsim.llm.TRANSIENT_BACKOFF_SECONDS", 0)
    llm, turn, life = drive(store, config, call_log, lambda: StatusError(503), 1)

    result = turn.play("I look at the pit", life)

    assert result.narration == "You lean over the pit. The pit is backed up."
    assert llm.opens == 2


def test_a_key_that_cannot_afford_the_request_is_not_retried(store, config, call_log):
    from worldsim.llm import OutOfCredit

    def too_many_tokens():
        return StatusError(
            402, "You requested up to 1200 tokens, but can only afford 1132"
        )

    llm, turn, life = drive(store, config, call_log, too_many_tokens, 1)

    with pytest.raises(OutOfCredit) as raised:
        turn.play("I look at the pit", life)
    assert llm.opens == 1
    assert "max_tokens" in str(raised.value)


def test_an_in_flight_budget_on_the_stream_is_retried(store, config, call_log, monkeypatch):
    monkeypatch.setattr("worldsim.llm.IN_FLIGHT_BACKOFF_SECONDS", 0)

    def in_flight():
        return StatusError(402, "{'reason': 'in_flight_budget_exhausted'}")

    llm, turn, life = drive(store, config, call_log, in_flight, 1)

    assert turn.play("I look at the pit", life).narration == (
        "You lean over the pit. The pit is backed up."
    )
    assert llm.opens == 2


def test_a_failed_stream_is_logged_and_commits_nothing(store, config, call_log, monkeypatch):
    monkeypatch.setattr("worldsim.llm.TRANSIENT_BACKOFF_SECONDS", 0)
    llm, turn, life = drive(store, config, call_log, lambda: StatusError(500), 2)
    before = store.now()
    call_log.records.clear()

    with pytest.raises(ModelCallFailed):
        turn.play("I look at the pit", life)

    assert store.now() == before
    # The attempt is spoken before the resolver is called, so it is on the
    # record and it succeeded; both resolver attempts then failed, and nothing
    # the turn would have committed was committed.
    assert [r.ok for r in call_log.records] == [True, False, False]


def test_a_number_where_a_superseded_value_should_be_text_is_accepted(store, config, call_log):
    """A live turn was lost because the model sent a bare number here."""
    from worldsim.resolve import Supersede

    change = Supersede(
        entity_id="person:you", key="age", value=29,
        source="invented", visibility="obvious",
    )
    assert change.value == "29"


def test_an_unusable_ledger_still_delivers_the_narration(store, config, call_log):
    """The player heard nothing at all when this happened in a live run."""
    half_bad = reply(player_location="place:kitchen").replace(
        "{}", '{"deltas": "not a list"}'
    )
    llm = FakeLLM(
        config, call_log,
        scripts(
            [SPAWN, half_bad],
            ["Oil ticks.", "You lean over the pit.", "The pit is backed up."],
        ),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    result = turn.play("I look at the pit", life)

    assert result.narration == "You lean over the pit. The pit is backed up."
    assert any("ledger unusable" in item for item in result.dropped)
    # The turn is still on the record, so the next turn has continuity.
    assert store.recent_turns(life.id, limit=1)[0]["narration"] == result.narration


# ---- the compact wire format ----
#
# Facts are most of what the player waits for, so they go on the wire as short
# arrays. Both forms are accepted: a format slip costs a whole retry, which is
# worth far more than the tokens saved.


def test_a_fact_can_be_written_as_an_array():
    from worldsim.resolve import NewFact

    f = NewFact.model_validate(["person:gary", "beard", "graying", "w", "n"])
    assert (f.entity_id, f.key, f.value) == ("person:gary", "beard", "graying")
    assert f.source == "world-knowledge"
    assert f.visibility == "noticeable"


def test_the_letters_may_be_left_off_and_default_sensibly():
    from worldsim.resolve import NewFact

    f = NewFact.model_validate(["place:bar", "smell", "stale beer"])
    assert f.source == "invented"
    assert f.visibility == "obvious"


def test_the_spelled_out_form_still_works():
    from worldsim.resolve import NewFact

    f = NewFact.model_validate(
        {"entity_id": "place:bar", "key": "smell", "value": "stale beer",
         "source": "world-knowledge", "visibility": "hidden"}
    )
    assert f.source == "world-knowledge" and f.visibility == "hidden"


def test_entities_and_perceived_and_superseded_take_arrays_too():
    from worldsim.resolve import NewEntity, Perceived, Supersede

    e = NewEntity.model_validate(["place:bar", "place", "The Peanut", "place:spawn-city"])
    assert (e.id, e.kind, e.name, e.location_id) == (
        "place:bar", "place", "The Peanut", "place:spawn-city",
    )
    assert NewEntity.model_validate(["place:void", "place", "Nowhere"]).location_id is None

    p = Perceived.model_validate(["place:bar", "smell"])
    assert (p.entity_id, p.key) == ("place:bar", "smell")

    s = Supersede.model_validate(["place:bar", "hours", "closed", "i", "o"])
    assert s.value == "closed" and s.source == "invented"


def test_a_quantity_in_the_compact_form_is_still_forced_numeric():
    from worldsim.resolve import NewFact

    assert NewFact.model_validate(["person:you", "cash_usd", "27", "i", "h"]).value == 27.0


def test_the_compact_form_is_actually_shorter():
    """The whole point. If this stops being true the change is pointless."""
    verbose = json.dumps(
        {"entity_id": "person:gary", "key": "beard", "value": "graying",
         "source": "invented", "visibility": "noticeable"}
    )
    compact = json.dumps(["person:gary", "beard", "graying", "i", "n"])
    assert len(compact) < len(verbose) / 2


def test_a_scene_of_compact_facts_commits_normally(store, config, call_log):
    compact_turn = (
        json.dumps({
            "intent": "walk into the tavern",
            "duration_minutes": 9,
            "player_location": "place:tavern",
            "summary": "you take a stool",
            "new_entities": [["place:tavern", "place", "The Double Luck", "place:spawn-city"]],
            "new_facts": [
                ["place:tavern", "smell", "stale beer"],
                ["person:gary", "beard", "graying", "i", "n"],
            ],
            "perceived": [["place:tavern", "smell"]],
        })
        + "{}"
    )
    # person:gary has no entity, so that fact is dropped, not crashed on.
    llm = FakeLLM(
        config, call_log,
        scripts(
            [SPAWN, compact_turn],
            ["Oil ticks.", "You push the door open.", "Stale beer."],
        ),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    result = turn.play("I walk into the tavern", life)

    assert store.entity("place:tavern").name == "The Double Luck"
    assert store.live_fact("place:tavern", "smell").value == "stale beer"
    assert store.entity(PLAYER_ID).location_id == "place:tavern"
    assert result.narration == "You push the door open. Stale beer."


# ---- the splitter reads a growing buffer once ----
#
# The scanner keeps its brace depth between chunks instead of rescanning from
# the top, which took the splitter off a quadratic. It is on the one path the
# player waits on, so it gets tests that pin the incremental behaviour.


@pytest.mark.parametrize(
    "text, first",
    [
        ('{"a": 1}{"b": 2}', 8),
        ('{"a": "}"}{"b": 2}', 10),
        ('{"a": "\\\\"}{"b": 2}', 11),
        ('{"a": {"b": 1}}{}', 15),
    ],
)
def test_feeding_in_chunks_finds_the_same_split_as_one_pass(text, first):
    # A brace or a quote can land on a chunk boundary, so the carried state has
    # to survive it. Any chunking must agree with reading the whole string.
    for size in (1, 2, 3, 7):
        scanner = ObjectScanner()
        found = None
        for cut in range(size, len(text) + size, size):
            found = scanner.feed(text[:cut])
            if found is not None:
                break
        assert found == first, f"chunk size {size}"
        assert found == first_object_end(text)


def test_json_start_waits_until_the_offset_cannot_change():
    # A fence whose header has not closed yet has no knowable start, because
    # answering early would shift every index the scanner has already read.
    assert json_start("") is None
    assert json_start("  ") is None
    assert json_start("```js") is None
    assert json_start("```json\n{") == 8
    assert json_start('{"a": 1}') == 0
    assert json_start('\n\n{"a": 1}') == 2


def test_a_ledger_followed_by_a_closing_fence_is_still_read(store, config, call_log):
    """The ledger is located the same way the scene is, not parsed whole.

    A lost ledger means a lost delta, which means wrong money, so trailing
    prose or a closing fence must not cost the turn.
    """
    fenced = (
        "```json\n"
        + reply(
            {"deltas": [{"entity_id": "person:you", "key": "cash_usd", "delta": -5}]},
            player_location="place:kitchen",
        )
        + "\n```\nHope that helps!"
    )
    llm = FakeLLM(config, call_log, scripts([SPAWN, fenced], ["Oil ticks.", "Change."]))
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    result = turn.play("I buy a coffee", life)

    assert result.dropped == []
    assert store.live_fact(PLAYER_ID, "cash_usd").value == 15


# ---- rule 9: every call that reached the provider is logged ----


def test_a_rejected_scene_is_still_logged_as_a_call(store, config, call_log):
    """A scene that failed validation was billed and used to vanish.

    Two calls went out and one record was written, which hid the retry and
    under-reported the session spend — the measurement milestone 1 still owes.
    """
    unusable = '{"intent": "no duration, no location"}{}'
    good = reply(player_location="place:kitchen")
    llm = FakeLLM(
        config, call_log,
        scripts([SPAWN, unusable, good], ["Oil ticks.", "You are there."]),
    )
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random(7))
    life, _ = begin_life(store, resolver, narrator, config.spawn_city, random.Random(7))

    before = len(call_log.records)
    turn.play("I wait", life)

    resolver_calls = [r for r in call_log.records[before:] if r.role == "resolver"]
    assert len(resolver_calls) == 2, "the rejected attempt has no record"
    rejected, accepted = resolver_calls
    assert rejected.ok is False and rejected.attempt == 1
    assert "duration_minutes" in rejected.error
    assert accepted.ok is True and accepted.attempt == 2
