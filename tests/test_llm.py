"""Model-call plumbing: one retry, and every call logged (spec 3.3, rule 9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from worldsim.config import ModelPrice
from worldsim.llm import FakeLLM, ModelCallFailed, OutOfCredit, ProviderBusy

VALID = '{"intent": "pocket the whiskey", "duration_minutes": 1}'
MISSING_FIELD = '{"intent": "pocket the whiskey"}'
NOT_JSON = "Sure! Here's what happens next:"


class Shape(BaseModel):
    intent: str
    duration_minutes: int


def test_valid_json_is_returned_on_the_first_call(config, call_log):
    llm = FakeLLM(config, call_log, {"resolver": [VALID]})

    result = llm.json("resolver", "system", "user", Shape)

    assert result.intent == "pocket the whiskey"
    assert len(call_log.records) == 1
    assert call_log.records[0].attempt == 1


def test_invalid_json_gets_exactly_one_retry(config, call_log):
    llm = FakeLLM(config, call_log, {"resolver": [NOT_JSON, VALID]})

    result = llm.json("resolver", "system", "user", Shape)

    assert result.duration_minutes == 1
    assert [r.attempt for r in call_log.records] == [1, 2]


def test_a_second_failure_raises_so_nothing_commits(config, call_log):
    llm = FakeLLM(config, call_log, {"resolver": [NOT_JSON, MISSING_FIELD]})

    with pytest.raises(ModelCallFailed):
        llm.json("resolver", "system", "user", Shape)

    assert len(call_log.records) == 2


def test_the_retry_tells_the_model_what_was_wrong(config, call_log):
    llm = FakeLLM(config, call_log, {"resolver": [MISSING_FIELD, VALID]})

    llm.json("resolver", "system", "user", Shape)

    first_prompt, retry_prompt = llm.prompts[0][2], llm.prompts[1][2]
    assert "duration_minutes" not in first_prompt
    assert "duration_minutes" in retry_prompt


def test_json_wrapped_in_a_code_fence_is_still_read(config, call_log):
    llm = FakeLLM(config, call_log, {"resolver": [f"```json\n{VALID}\n```"]})

    assert llm.json("resolver", "system", "user", Shape).duration_minutes == 1


def test_every_call_is_logged_to_disk_with_role_model_cost_and_latency(config, call_log):
    llm = FakeLLM(config, call_log, {"narrator": ["You push the door open."]})

    llm.text("narrator", "system", "user")

    written = [json.loads(line) for line in call_log.path.read_text().splitlines()]
    assert len(written) == 1
    record = written[0]
    assert record["role"] == "narrator"
    assert record["model"] == "nousresearch/hermes-4-405b"
    assert record["input_tokens"] > 0
    assert record["output_tokens"] > 0
    assert record["cost_cents"] > 0
    assert record["seconds"] >= 0
    assert record["ok"] is True


def test_a_failed_call_is_logged_as_not_ok(config, call_log):
    llm = FakeLLM(config, call_log, {"resolver": []})

    with pytest.raises(ModelCallFailed):
        llm.text("resolver", "system", "user")

    assert call_log.records[0].ok is False
    assert call_log.records[0].error


class Boom(Exception):
    """Stands in for an openai APIStatusError, which carries .status_code."""

    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FlakyLLM(FakeLLM):
    """Fails with a given status the first n times, then answers."""

    def __init__(self, config, call_log, status, failures):
        super().__init__(config, call_log, {"resolver": [VALID]})
        self.status = status
        self.failures = failures
        self.attempts = 0

    def _raw(self, role, model, system, user):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise Boom(self.status)
        return super()._raw(role, model, system, user)


def test_a_transient_error_is_retried_quietly(config, call_log, monkeypatch):
    monkeypatch.setattr("worldsim.llm.TRANSIENT_BACKOFF_SECONDS", 0)
    llm = FlakyLLM(config, call_log, status=429, failures=1)

    assert llm.json("resolver", "s", "u", Shape).duration_minutes == 1
    assert llm.attempts == 2
    assert [r.ok for r in call_log.records] == [False, True]


def test_a_transient_error_twice_gives_up(config, call_log, monkeypatch):
    monkeypatch.setattr("worldsim.llm.TRANSIENT_BACKOFF_SECONDS", 0)
    llm = FlakyLLM(config, call_log, status=503, failures=2)

    with pytest.raises(ModelCallFailed):
        llm.json("resolver", "s", "u", Shape)
    assert llm.attempts == 2


def test_running_out_of_credit_is_not_retried(config, call_log):
    llm = FlakyLLM(config, call_log, status=402, failures=1)

    with pytest.raises(OutOfCredit):
        llm.json("resolver", "s", "u", Shape)
    assert llm.attempts == 1


class BusyLLM(FlakyLLM):
    """A 402 that is an exhausted in-flight budget, not an empty account."""

    def _raw(self, role, model, system, user):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise Boom402InFlight()
        return FakeLLM._raw(self, role, model, system, user)


class Boom402InFlight(Exception):
    def __init__(self):
        super().__init__(
            "Error code: 402 - {'error': {'metadata': "
            "{'reason': 'in_flight_budget_exhausted'}}}"
        )
        self.status_code = 402


def test_an_exhausted_in_flight_budget_is_retried_not_called_out_of_credit(
    config, call_log, monkeypatch
):
    """A live run told the player to add credit while they had $9.82."""
    monkeypatch.setattr("worldsim.llm.IN_FLIGHT_BACKOFF_SECONDS", 0)
    llm = BusyLLM(config, call_log, status=402, failures=1)

    assert llm.json("resolver", "s", "u", Shape).duration_minutes == 1
    assert llm.attempts == 2


def test_a_provider_that_stays_busy_says_so_without_mentioning_credit(
    config, call_log, monkeypatch
):
    monkeypatch.setattr("worldsim.llm.IN_FLIGHT_BACKOFF_SECONDS", 0)
    llm = BusyLLM(config, call_log, status=402, failures=2)

    with pytest.raises(ProviderBusy) as raised:
        llm.json("resolver", "s", "u", Shape)
    assert "credit" not in str(raised.value).lower()
    assert not isinstance(raised.value, OutOfCredit)


def test_a_plain_error_is_not_retried(config, call_log):
    llm = FlakyLLM(config, call_log, status=400, failures=1)

    with pytest.raises(ModelCallFailed):
        llm.json("resolver", "s", "u", Shape)
    assert llm.attempts == 1


def test_cost_comes_from_tokens_and_the_model_price(config):
    # The spec's own estimate for a resolver turn: ~3,000 in, ~600 out, ~0.48 cents.
    assert ModelPrice(1.00, 3.00).cents(3000, 600) == pytest.approx(0.48)
    assert ModelPrice(0.70, 0.70).cents(3000, 600) == pytest.approx(0.252)


def test_an_unpriced_model_logs_zero_rather_than_crashing(config, call_log):
    config.models["resolver"] = "someone/a-model-we-have-no-price-for"
    llm = FakeLLM(config, call_log, {"resolver": [VALID]})

    llm.json("resolver", "system", "user", Shape)

    assert call_log.records[0].cost_cents == 0.0


def test_the_session_total_adds_up(config, call_log):
    llm = FakeLLM(config, call_log, {"narrator": ["one.", "two.", "three."]})

    for _ in range(3):
        llm.text("narrator", "system", "user")

    assert call_log.total_cents() == pytest.approx(
        sum(r.cost_cents for r in call_log.records)
    )
    assert call_log.total_cents() > 0


# ---- light mode (spec 12) ----


def test_light_mode_sends_every_role_to_the_background_model(config, call_log):
    """A remap of roles, not a model name in code (CLAUDE.md's rule)."""
    from dataclasses import replace

    llm = FakeLLM(config, call_log, {"resolver": ["{}"], "narrator": ["x"]})
    assert llm.model_for("resolver") == config.models["resolver"]
    assert llm.model_for("narrator") == config.models["narrator"]

    llm.light = True
    cheap = config.models["background"]
    for role in ("resolver", "narrator", "background"):
        assert llm.model_for(role) == cheap, role

    # And it can be the default, from .env rather than from a toggle.
    lit = FakeLLM(replace(config, light=True), call_log, {})
    assert lit.light
    assert lit.model_for("resolver") == cheap


def test_the_log_records_the_model_the_call_actually_ran_on(config, call_log):
    """Rule 9: the log, the cost, and the request can never disagree."""
    llm = FakeLLM(config, call_log, {"narrator": ["The fryer ticks."]})
    llm.light = True

    llm.text("narrator", "system", "user")

    assert call_log.records[-1].model == config.models["background"]


def test_no_model_name_is_written_in_code():
    """Model names come from .env, so light mode may not smuggle one in."""
    import worldsim

    source = Path(worldsim.__file__).parent
    offenders = [
        path.name
        for path in source.glob("*.py")
        # config.py holds the price table, which is keyed on model ids and is
        # not a choice of model (its own comment says so).
        if path.name != "config.py" and "hermes" in path.read_text().lower()
    ]
    assert not offenders, offenders
