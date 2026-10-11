"""The terminal loop (spec 13.1). Builds every component and wires them up.

    PYTHONPATH=src python -m worldsim

No globals and no singletons: everything below is constructed here and passed
in explicitly.
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import ConfigError, load_config
from .llm import CallLog, LedgerFailed, ModelCallFailed, OpenRouterLLM, OutOfCredit
from .render import Narrator
from .resolve import PLAYER_ID, SPAWN_PLACE_ID, Resolver
from .sheet import character_sheet
from .store import Store, StoreError
from .turn import Turn, TurnResult, begin_life

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

COULD_NOT_RESOLVE = "Sorry, that didn't come through. Say it again?"


def latest_world(saves_dir: Path) -> Path | None:
    """The most recently played world, for resuming (build step 9)."""
    if not saves_dir.is_dir():
        return None
    worlds = [p for p in saves_dir.iterdir() if (p / "world.db").is_file()]
    if not worlds:
        return None
    return max(worlds, key=lambda p: (p / "world.db").stat().st_mtime)


def local_time(store: Store) -> str:
    """The clock in the local time of wherever the character is (spec 6.1)."""
    now = store.now()
    zone_fact = store.live_fact(SPAWN_PLACE_ID, "timezone")
    if zone_fact:
        try:
            local = now.astimezone(ZoneInfo(str(zone_fact.value)))
            return local.strftime("%A, %B %-d, %Y, %-I:%M %p ") + str(zone_fact.value)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return now.strftime("%A, %B %-d, %Y, %-I:%M %p UTC")


def log_failure(config, player_said: str, exc: Exception) -> None:
    """Why a turn was refused, so a refusal can be diagnosed later."""
    path = config.logs_dir / "failures.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}\t{player_said}\t{exc}\n")


def dev_view(store: Store, resolver: Resolver, result: TurnResult | None, call_log: CallLog) -> str:
    """Hidden during normal play (spec 13.5)."""
    lines = ["", "--- dev ---", f"world time: {store.now().isoformat()}"]
    if result:
        lines.append(f"intent: {result.intent}  ({result.duration_minutes} min)")
        if result.roll:
            r = result.roll
            lines.append(
                f"roll: p={r.p_success:.2f} rolled={r.rolled:.3f} -> "
                f"{'success' if r.succeeded else 'failure'} ({r.why})"
            )
            lines.append(f"  on success: {r.on_success}")
            lines.append(f"  on failure: {r.on_failure}")
        else:
            lines.append("roll: none, the outcome was not in doubt")
        lines.append(f"perceived fact ids: {result.noticed_fact_ids}")
        if result.dropped:
            lines.append("dropped by validation:")
            lines.extend(f"  {item}" for item in result.dropped)
    lines.append("truth here:")
    lines.append(json.dumps(resolver.truth_here(), indent=2))
    for record in call_log.records[-3:]:
        timing = f"{record.seconds:.1f}s"
        if record.scene_seconds is not None:
            # What the player actually waited for, before the voice could start.
            timing = f"{record.scene_seconds:.1f}s to scene, {record.seconds:.1f}s total"
        lines.append(
            f"call {record.role}/{record.model}: {record.input_tokens}+"
            f"{record.output_tokens} tok, {record.cost_cents:.3f}c, {timing}"
        )
    lines.append(f"session so far: {call_log.total_cents():.2f}c")
    lines.append("--- end dev ---")
    return "\n".join(lines)


def main() -> int:
    try:
        config = load_config(REPO_ROOT)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    call_log = CallLog(config.logs_dir / "calls.jsonl")

    resuming = latest_world(config.saves_dir)
    if resuming:
        world_dir = resuming
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        world_dir = config.saves_dir / f"world-{stamp}"

    store = Store(world_dir / "world.db")
    llm = OpenRouterLLM(config, call_log)
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random())

    life = store.current_life()
    if life is None:
        print(f"A new life in {config.spawn_city}.\n")
        try:
            life, opening = begin_life(store, resolver, narrator, config.spawn_city)
        except (ModelCallFailed, StoreError) as exc:
            print(f"The world could not be started: {exc}", file=sys.stderr)
            store.close()
            return 1
        print(opening)
    else:
        past = store.recent_turns(life.id, limit=1)
        print(f"Back in it. {local_time(store)}.\n")
        if past and past[0]["narration"]:
            print(past[0]["narration"])

    dev = False
    last: TurnResult | None = None
    print()

    while True:
        try:
            said = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not said:
            continue

        if said.startswith("/"):
            command = said.split()[0].lower()
            if command == "/quit":
                break
            elif command == "/sheet":
                print("\n" + character_sheet(store) + "\n")
            elif command == "/time":
                print(f"\n{local_time(store)}\n")
            elif command == "/cost":
                print(
                    f"\n{len(call_log.records)} calls, "
                    f"{call_log.total_cents():.2f} cents this session\n"
                )
            elif command == "/dev":
                dev = not dev
                print(f"\ndev view {'on' if dev else 'off'}\n")
            else:
                print(f"\nNo such command. Try /sheet, /time, /cost, /dev, /quit.\n")
            continue

        try:
            last = turn.play(said, life)
        except LedgerFailed as exc:
            # The scene is already true and already narrated, so this is not a
            # "say it again" — part of the turn happened and some consequence
            # did not. Loud, because a missing delta means wrong money.
            log_failure(config, said, exc)
            print("\nSomething in that didn't settle right. Check /dev.\n")
            if dev:
                print(f"--- dev --- ledger lost: {exc}")
            continue
        except OutOfCredit as exc:
            # Not the player's fault and not worth "say it again".
            log_failure(config, said, exc)
            print(f"\n{exc}\nYour world is saved. Add credit and relaunch.\n")
            break
        except ModelCallFailed as exc:
            # The reason only lived in the retry prompt, which made a refused
            # turn impossible to diagnose after the fact.
            log_failure(config, said, exc)
            print(f"\n{COULD_NOT_RESOLVE}\n")
            if dev:
                print(f"--- dev --- turn refused: {exc}")
            continue
        except StoreError as exc:
            # The turn's proposals are drained one by one and each is caught, so
            # reaching here means a write the store refuses outright. Say the
            # out-of-character line rather than lose the session, and log it.
            print(f"\n{COULD_NOT_RESOLVE}\n")
            if dev:
                print(f"--- dev --- store refused the turn: {exc}")
            continue

        print(f"\n{last.narration}\n")

        if dev:
            print(dev_view(store, resolver, last, call_log))

        if last.ends_life:
            store.end_life(life.id, last.summary)
            print("\nThat life is over.\n")
            break

    print(f"Saved to {world_dir}. {call_log.total_cents():.2f} cents this session.")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
