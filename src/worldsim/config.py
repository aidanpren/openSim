"""Configuration, read from .env. Model names never come from code.

Created by __main__.py at startup and passed to everything that needs it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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


@dataclass
class Corrections:
    """Which mechanisms that correct the model are switched on.

    There are two kinds of code in this engine and they are easy to confuse.

    One kind keeps the store coherent: the clock only moves forward, a fact
    needs an entity to live on, a quantity changes by a delta, the narrator's
    context is *built from* knowledge rows so a hidden fact has no path in.
    None of that is a judgement, none of it is here, and none of it can be
    turned off.

    The other kind second-guesses the model: did it narrate a thought the
    player never had, did it repeat itself, did it charge for a journey it
    never committed, did it agree with a player who wrote their own ending.
    Those are judgements, every one of them is an approximation, and every one
    of them needed live tuning — a stemmer, a three-condition guard, a
    one-shot stand-down. That is this.

    They are switchable because it is an open question whether they earn their
    keep. The model they correct is better at judgement than the regexes
    correcting it, and a session in Claude's chat window with none of this was
    more fun than a session with all of it. `Corrections.none()` is the other
    arm of that experiment: the store, the dice and rule 5 stay, and the
    second-guessing goes.

    Created by __main__.py and passed to the resolver, the narrator, and the
    turn.
    """

    # check_travel and check_not_a_yes_man: refusing an opening and spending
    # the retry. Both have cost a live turn by being wrong.
    refuse_turns: bool = True
    # narrated_the_player: blocking a sentence that invents a choice, a
    # thought, or speech the player never gave.
    gate_narration: bool = True
    # Heard: dropping a sentence that says what has already been said.
    drop_repeats: bool = True
    # not_a_fact and the transient reroute: moving a write the model put in
    # the wrong channel, or refusing it.
    route_writes: bool = True

    @classmethod
    def none(cls) -> Corrections:
        """Raw mode. The model is taken at its word."""
        return cls(False, False, False, False)

    @property
    def all_off(self) -> bool:
        return not (
            self.refuse_turns
            or self.gate_narration
            or self.drop_repeats
            or self.route_writes
        )


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
    # Spec 12. Every role runs on the background model, which is a third of the
    # price. There is no new model name here, and there could not be: the cheap
    # one is already configured as `BACKGROUND_MODEL`, so light mode is a remap
    # of roles onto models that `.env` already names.
    light: bool = False
    # Spec 14's raw arm. Which of the model-correcting mechanisms are on.
    corrections: Corrections = field(default_factory=Corrections)

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
        # spec 12. Off by default: the freedom test is why, and it is written
        # out in .env.example.
        light=_flag("LIGHT_MODE", default=False),
        # spec 14. Raw mode keeps the store, the dice and rule 5 and drops
        # everything that second-guesses the model.
        corrections=(
            Corrections.none() if _flag("RAW_MODE", default=False) else Corrections()
        ),
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
