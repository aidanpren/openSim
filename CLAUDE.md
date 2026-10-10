# World Sim

A voice-played simulation of real life on Earth. Each turn, one model call resolves what's true and a second narrates only what the player's character perceives.

Vision: @docs/vision.md

- How it works: `docs/spec.md`. Read the relevant section before changing a system.
- Current work: the lowest-numbered brief in `docs/milestones/` that still has unchecked boxes. Tick each box as you finish it.

## Stack

- Python 3.12, managed with uv
- OpenRouter through the `openai` SDK (`base_url="https://openrouter.ai/api/v1"`)
- SQLite (stdlib `sqlite3`), pydantic, pytest

## Commands

- `uv sync`: install dependencies
- `uv run python -m worldsim`: play (starts a new world or resumes the last one)
- `uv run pytest`: run tests (offline, with `FakeLLM`)
- `uv run python scripts/freedom_test.py`: run the model freedom test

## Rules that don't bend

Each one gets a test when its system is built. Never weaken a test to make it pass. If a rule looks wrong, stop and ask.

1. The world store is the only source of truth. Models propose; code validates and commits.
2. Every turn is resolve, commit, render. The narrator never creates or changes facts.
3. Facts are never edited or deleted. A change is a new fact that supersedes the old one and cites its cause.
4. Quantities like cash and counts change by deltas applied in code. Models never write totals.
5. The narrator's context holds only what the player character knows or perceives. Hidden facts, other people's minds, the timeline, and the director's plans never reach it.
6. Randomness comes from code (`random.Random`, seedable). The resolver writes both outcomes before code rolls.
7. The world clock never moves backward, and it moves only by in-world durations.
8. Ending a life never changes world facts. A sandbox never writes to the world it was copied from.
9. Every model call logs its role, model, tokens, cost, and latency to `logs/`.

## Narration (spec section 4)

Two to four spoken sentences, second person, present tense. Never narrate the player's choices, words, thoughts, or feelings. No game language: dice, odds, quests, objectives, stats. No lists or markdown. No closing prompt like "What do you do?" Narrate outcomes, not procedures.

## Code conventions

- `__main__.py` builds every component and passes dependencies in explicitly. No globals or singletons.
- Don't add a class or function that nothing calls.
- When you add a module, say in your summary what creates it and what calls it.
- Prompt text lives in `src/worldsim/prompts/*.md`, not in Python strings.
- Model output that feeds code is JSON validated with pydantic. Invalid output gets one retry with the error attached; if that fails, the turn doesn't commit.
- Model names come from `.env`, never from code.
- Unit tests never touch the network.
- `.env`, `saves/`, and `logs/` are gitignored. Never print or log API keys.

## Working rhythm

- One milestone at a time. Check every "done when" item before calling it finished.
- If the spec and the code disagree, or a request conflicts with the spec, ask before changing either.
- Prefer the smallest change that meets the milestone.
