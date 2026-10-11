# World Sim

A voice-played simulation of real life on Earth. Each turn, one model call resolves what's true and a second narrates only what the player's character perceives.

Vision: @docs/vision.md

- How it works: `docs/spec.md`. Read the relevant section before changing a system.
- Current work: the lowest-numbered brief in `docs/milestones/` that still has unchecked boxes. Tick each box as you finish it.

## Stack

- Python 3.12 is the target, managed with uv. **Neither is available on this
  machine** — see "Dev environment here" below before running anything.
- OpenRouter through the `openai` SDK (`base_url="https://openrouter.ai/api/v1"`)
- SQLite (stdlib `sqlite3`), pydantic, pytest

## Dev environment here

This Mac cannot run uv. The installer works, but macOS refuses to exec the
binary (exit 126), and even a copied `/bin/echo` is killed (exit 137) — an
endpoint policy, with Jamf tooling in `/usr/local/bin`. Python extension
modules load fine; only standalone executables are blocked. The Xcode license
has also not been accepted, which is what breaks `/usr/bin/python3`, `pip3`,
and `git`.

So the venv is built from Xcode's bundled Python 3.9.6 at
`/Applications/Xcode.app/Contents/Developer/usr/bin/python3`, and 3.9 needs
`eval_type_backport` — pydantic 2 otherwise raises `TypeError` on `X | None`
annotations at class-definition time, which would make the spec 3.1 resolver
contract unwritable. Write code for 3.12 anyway: modern annotations are fine,
but avoid `match`, `X | Y` in `isinstance`, and 3.10+ stdlib additions.

`git` does not run here either. Commit through GitKraken, or accept the Xcode
license to get the CLI back.

To rebuild the environment from scratch:

```
/Applications/Xcode.app/Contents/Developer/usr/bin/python3 -m venv .venv
./.venv/bin/pip install openai python-dotenv pydantic pytest eval_type_backport
```

`pip install -e .` does **not** work, because `pyproject.toml` honestly declares
`requires-python = ">=3.12"`. `src/` reaches the interpreter through
`PYTHONPATH` instead, and through pytest's `pythonpath` setting for tests.

## Commands

- `./.venv/bin/python -m pytest`: run tests (offline, with `FakeLLM`)
- `PYTHONPATH=src ./.venv/bin/python -m worldsim`: play (new world, or resumes
  the last one under `saves/`)
- `./.venv/bin/python scripts/freedom_test.py`: run the model freedom test

Once a real 3.12 is installed, these become `uv sync`, `uv run python -m
worldsim`, `uv run pytest`, and `uv run python scripts/freedom_test.py`, and
`eval_type_backport` can be dropped.

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
