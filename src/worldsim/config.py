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
    # Spec 13.2. Defaulted so a config built without them still plays silently.
    # An empty voice name means the system voice: no voice name in code, for the
    # same reason model names are not in code.
    voice_enabled: bool = False
    voice_name: str = ""
    voice_rate: int | None = None

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
        # spec 13.2
        voice_enabled=_flag("VOICE_ENABLED", default=True),
        voice_name=os.environ.get("VOICE", "").strip(),
        voice_rate=_words_per_minute(os.environ.get("VOICE_RATE", "")),
    )


def _flag(var: str, default: bool) -> bool:
    value = os.environ.get(var, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _words_per_minute(value: str) -> int | None:
    """An unreadable rate is worth a refusal, not a silent fallback."""
    value = value.strip()
    if not value:
        return None
    try:
        rate = int(value)
    except ValueError:
        raise ConfigError(f"VOICE_RATE must be a whole number, not {value!r}.")
    if not 80 <= rate <= 400:
        raise ConfigError(f"VOICE_RATE must be between 80 and 400, not {rate}.")
    return rate
