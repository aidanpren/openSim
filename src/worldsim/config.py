"""Configuration, read from .env. Model names never come from code.

Created by __main__.py at startup and passed to everything that needs it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

ROLES = ("resolver", "narrator", "background")


@dataclass(frozen=True)
class ModelPrice:
    """Dollars per million tokens. Checked against OpenRouter on 2026-10-10."""

    input_per_million: float
    output_per_million: float

    def cents(self, input_tokens: int, output_tokens: int) -> float:
        dollars = (
            input_tokens / 1e6 * self.input_per_million
            + output_tokens / 1e6 * self.output_per_million
        )
        return dollars * 100


# Prices are not model names, so they live in code; the model ids they key on
# come from .env. An unlisted model costs 0 in the log rather than crashing.
PRICES = {
    "nousresearch/hermes-4-405b": ModelPrice(1.00, 3.00),
    "nousresearch/hermes-3-llama-3.1-70b": ModelPrice(0.70, 0.70),
}


class ConfigError(Exception):
    """Something required is missing from .env."""


@dataclass(frozen=True)
class Config:
    api_key: str
    models: dict[str, str]
    spawn_city: str
    saves_dir: Path
    logs_dir: Path
    token_budgets: dict[str, int]

    def model_for(self, role: str) -> str:
        if role not in self.models:
            raise ConfigError(f"no model configured for role {role!r}")
        return self.models[role]

    def price_for(self, model: str) -> ModelPrice | None:
        return PRICES.get(model)


def load_config(repo_root: Path) -> Config:
    """Read .env. Never logs or prints the key."""
    load_dotenv(repo_root / ".env")

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise ConfigError(
            "No OPENROUTER_API_KEY. Copy .env.example to .env and add your key."
        )

    models = {}
    for role in ROLES:
        var = f"{role.upper()}_MODEL"
        value = os.environ.get(var, "").strip()
        if not value:
            raise ConfigError(f"No {var} in .env.")
        models[role] = value

    return Config(
        api_key=api_key,
        models=models,
        spawn_city=os.environ.get("SPAWN_CITY", "Kansas City, Missouri").strip(),
        saves_dir=repo_root / "saves",
        logs_dir=repo_root / "logs",
        # spec 5.2
        token_budgets={"resolver": 5000, "narrator": 4000, "background": 4000},
    )
