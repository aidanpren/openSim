"""Fixtures. Nothing here touches the network."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from worldsim.config import Config
from worldsim.llm import CallLog
from worldsim.store import Store

# A fixed start so clock assertions are exact: 11 p.m. on a Friday, UTC.
WORLD_START = datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc)


@pytest.fixture
def config(tmp_path):
    return Config(
        api_key="not-a-real-key",
        models={
            "resolver": "nousresearch/hermes-4-405b",
            "narrator": "nousresearch/hermes-4-405b",
            "background": "nousresearch/hermes-3-llama-3.1-70b",
        },
        spawn_city="Kansas City, Missouri",
        saves_dir=tmp_path / "saves",
        logs_dir=tmp_path / "logs",
        token_budgets={"resolver": 5000, "narrator": 4000, "background": 4000},
    )


@pytest.fixture
def call_log(tmp_path):
    return CallLog(tmp_path / "logs" / "calls.jsonl")


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "saves" / "test-world" / "world.db", starts_at=WORLD_START)
    yield store
    store.close()


@pytest.fixture
def world(store):
    """A player in a bar, which is the smallest world worth testing."""
    store.add_entity("city:kcmo", "place", "Kansas City, Missouri")
    store.add_entity("place:bar", "place", "The Peanut", location_id="city:kcmo")
    store.add_entity("person:you", "person", "Dale Rusk", location_id="place:bar")
    store.add_entity("person:bartender", "person", "Marisol", location_id="place:bar")
    return store
