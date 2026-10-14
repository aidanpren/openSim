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
from typing import Callable

from .config import ConfigError, load_config
from .llm import (
    CallLog,
    LedgerFailed,
    ModelCallFailed,
    OpenRouterLLM,
    OutOfCredit,
    ProviderBusy,
)
from .render import Narrator
from .resolve import PLAYER_ID, SPAWN_PLACE_ID, Resolver
from .sheet import character_sheet, local_time
from .store import Store, StoreError
from .turn import Turn, TurnResult, begin_life
from .voice import Voice

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

COULD_NOT_RESOLVE = "Sorry, that didn't come through. Say it again?"

USAGE = """\
    PYTHONPATH=src python -m worldsim [--new]

      --new   start a new life in a new world, instead of resuming the most
              recently played one under saves/

Environment (see .env.example): LIGHT_MODE, RAW_MODE, VOICE_ENABLED."""

COMMANDS = """\
/help   this list
/sheet  who you are, what you have, what is true right now
/time   the local date and time where you are
/cost   this session's spend
/dice   show the roll inline, off by default
/light  run every role on the cheap model
/raw    take the model at its word: no gate, no filter, no refusals
/dev    the dev view: the truth here, rolls, the timeline, dropped writes
/quit   save and exit

Anything else is said or done in the world. You can also ask the narrator
about yourself — "what am I doing?", "where am I?", "what's in my pocket?" —
which costs no time and settles nothing."""

# How long the prompt will wait on the voice before deciding it is wedged, in
# 0.2s polls. Short polls rather than one long block so Ctrl-C lands promptly.
VOICE_PATIENCE = 450


def make_speak(voice: Voice) -> Callable[[str], None]:
    """Deliver one sentence the moment it clears the contract.

    Both at once, on purpose: the screen keeps the transcript that playtests are
    reviewed against, and the speakers get the sentence while the one after it
    is still being written. `Voice.say` only enqueues, so the model stream never
    waits for the synthesizer.
    """

    def speak(sentence: str) -> None:
        print(sentence, end=" ", flush=True)
        voice.say(sentence)

    return speak


def wait_for_quiet(voice: Voice) -> None:
    """Hold the prompt until the narration has finished being spoken.

    This is turn-taking, not politeness: a prompt that appears over the voice
    invites the player to talk across the world. Ctrl-C cuts the rest short.
    """
    try:
        for _ in range(VOICE_PATIENCE):
            if voice.wait(timeout=0.2):
                return
    except KeyboardInterrupt:
        print()
    # Barge-in, or a synthesizer that stopped answering. Either way the player
    # gets the prompt back.
    voice.silence()


def latest_world(saves_dir: Path) -> Path | None:
    """The most recently played world, for resuming (build step 9)."""
    if not saves_dir.is_dir():
        return None
    worlds = [p for p in saves_dir.iterdir() if (p / "world.db").is_file()]
    if not worlds:
        return None
    return max(worlds, key=lambda p: (p / "world.db").stat().st_mtime)


def log_failure(config, player_said: str, exc: Exception) -> None:
    """Why a turn was refused, so a refusal can be diagnosed later."""
    path = config.logs_dir / "failures.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}\t{player_said}\t{exc}\n")


def _state_now(store: Store) -> dict:
    """Transient state for the player and whoever is in the room (spec 2.1)."""
    player = store.entity(PLAYER_ID)
    here = player.location_id if player else None
    nearby = [e.id for e in store.contents(here)] if here else []
    out = {}
    for entity_id in [PLAYER_ID] + nearby:
        held = {s.key: s.value for s in store.state(entity_id)}
        if held:
            out[entity_id] = held
    return out


def dev_view(
    store: Store,
    resolver: Resolver,
    result: TurnResult | None,
    call_log: CallLog,
    voice: Voice | None = None,
) -> str:
    """Hidden during normal play (spec 13.5)."""
    lines = ["", "--- dev ---", f"world time: {store.now().isoformat()}"]
    if voice is not None and voice.failures:
        # The voice falls silent rather than losing a turn, which means a broken
        # synthesizer is otherwise indistinguishable from a quiet room.
        lines.append(f"voice: {len(voice.failures)} failures")
        lines.extend(f"  {item}" for item in voice.failures[-3:])
    if result:
        lines.append(
            f"intent: {result.intent}  ({result.duration_minutes} min, "
            f"read as {result.mode})"
        )
        if result.roll:
            r = result.roll
            lines.append(
                f"roll: p={r.p_success:.2f} rolled={r.rolled:.3f} -> "
                f"{'success' if r.succeeded else 'failure'} ({r.why})"
            )
            lines.append(f"  margin: {r.band}  {r.as_dice()}")
            lines.append(f"  on success: {r.on_success}")
            lines.append(f"  on failure: {r.on_failure}")
        else:
            lines.append("roll: none, the outcome was not in doubt")
        lines.append(f"perceived fact ids: {result.noticed_fact_ids}")
        if result.fired:
            # Transient state keeps no history, so this and the turn log are
            # the only places a change to it is readable (spec 2.1).
            lines.append("the clock fired:")
            lines.extend(f"  {item}" for item in result.fired)
        if result.changed_state:
            lines.append("transient state changed:")
            lines.extend(f"  {item}" for item in result.changed_state)
        if result.dropped:
            lines.append("dropped by validation:")
            lines.extend(f"  {item}" for item in result.dropped)
    lines.append("state now:")
    lines.append(json.dumps(_state_now(store), indent=2))
    ahead = store.pending()
    if ahead:
        lines.append("timeline ahead:")
        lines.extend(
            f"  {e.at.isoformat()} {e.what}"
            f"{' (reaches you)' if e.reaches_player else ''}"
            for e in ahead
        )
    lines.append("truth here:")
    lines.append(json.dumps(resolver.truth_here(), indent=2))
    for record in call_log.records[-3:]:
        timing = f"{record.seconds:.1f}s"
        if record.object_seconds:
            # What the player actually waited for, phase by phase. The first
            # number is the one that matters: nothing is spoken before it.
            phases = ", ".join(f"{s:.1f}s" for s in record.object_seconds)
            timing = f"objects at {phases}; {record.seconds:.1f}s total"
        elif record.scene_seconds is not None:
            timing = f"{record.scene_seconds:.1f}s to scene, {record.seconds:.1f}s total"
        if record.ttft_seconds is not None:
            # Prefill against generation. Six streamed calls could not separate
            # these, which is why the lever was guesswork.
            timing += f" (first token {record.ttft_seconds:.1f}s)"
        lines.append(
            f"call {record.role}/{record.model}: {record.input_tokens}+"
            f"{record.output_tokens} tok, {record.cost_cents:.3f}c, {timing}"
        )
    lines.append(f"session so far: {call_log.total_cents():.2f}c")
    lines.append("--- end dev ---")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0
    start_new = "--new" in argv
    unknown = [a for a in argv if a not in ("--new",)]
    if unknown:
        print(f"Unknown option {unknown[0]!r}.\n\n{USAGE}", file=sys.stderr)
        return 2

    try:
        config = load_config(REPO_ROOT)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    call_log = CallLog(config.logs_dir / "calls.jsonl")

    # Resuming is the default because picking up where you stopped is the
    # point of a persistent world. There was no way to say otherwise, though,
    # so a new life meant moving save directories around by hand — and the
    # newest world is whatever was touched last, which after a playtest is not
    # the one anybody meant.
    resuming = None if start_new else latest_world(config.saves_dir)
    if resuming:
        world_dir = resuming
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        world_dir = config.saves_dir / f"world-{stamp}"

    store = Store(world_dir / "world.db")
    llm = OpenRouterLLM(config, call_log)
    # One object, built here and passed in, so which arm of the experiment a
    # session ran on is a single readable thing rather than four scattered ifs.
    corrections = config.corrections
    resolver = Resolver(llm, store, config, PROMPTS_DIR, corrections)
    narrator = Narrator(llm, store, config, PROMPTS_DIR, corrections)
    # One generator for the whole session: it draws the station a life starts
    # at and then every roll in it (rule 6).
    rng = random.Random()
    turn = Turn(store, resolver, narrator, rng, corrections)
    voice = Voice(
        enabled=config.voice_enabled,
        voice=config.voice_name,
        rate=config.voice_rate,
    )
    speak = make_speak(voice)

    if corrections.all_off:
        # Loud, because a raw world carries writes that cannot be corrected
        # later: a posture written as a fact is frozen by rule 3 forever.
        print(
            "Raw mode: the model is taken at its word. The store, the dice and\n"
            "rule 5 still hold; nothing else does. Do not play a world you want\n"
            "to keep.\n"
        )
    if llm.light:
        # Never a surprise which model is talking. A session that reads oddly
        # should be traceable to this line rather than guessed at.
        print(f"Light mode: every role on {llm.model_for('resolver')}.\n")

    life = store.current_life()
    if life is None:
        print(f"A new life in {config.spawn_city}.\n")
        voice.warm()
        try:
            life, opening = begin_life(
                store, resolver, narrator, config.spawn_city, rng, on_text=speak,
                premises_path=config.logs_dir / "premises.txt",
                corrections=corrections,
            )
        except (ModelCallFailed, StoreError) as exc:
            print(f"The world could not be started: {exc}", file=sys.stderr)
            voice.close()
            store.close()
            return 1
        print()
    else:
        past = store.recent_turns(life.id, limit=1)
        # Which world, by name. Without it a resume is indistinguishable from
        # a spawn until the narration contradicts what you expected, and after
        # a playtest the newest save is rarely the one anybody meant.
        print(
            f"Back in it: {world_dir.name}. {local_time(store)}."
            "  (--new for a new life)\n"
        )
        if past and past[0]["narration"]:
            print(past[0]["narration"])

    dev = False
    # Off by default, and text only. The player was curious about seeing the
    # roll and unsure whether he wanted it, so it is a toggle rather than a
    # decision: the narration never mentions it either way (spec 3.2).
    dice = False
    last: TurnResult | None = None
    print()

    while True:
        wait_for_quiet(voice)
        try:
            said = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not said:
            continue

        if said.startswith("/"):
            command = said.split()[0].lower()
            if len(said.split()) > 1:
                # `/dev I fly to mexico` toggled the view and silently ate the
                # action, which reads as the world ignoring you.
                print("\nCommands take no arguments; the rest was ignored.")
            if command in ("/help", "/h", "/?"):
                print("\n" + COMMANDS + "\n")
            elif command == "/quit":
                break
            elif command == "/sheet":
                print("\n" + character_sheet(store) + "\n")
            elif command == "/time":
                print(f"\n{local_time(store)}\n")
            elif command == "/cost":
                turns = max(len(store.recent_turns(life.id, limit=1000)) - 1, 1)
                print(
                    f"\n{len(call_log.records)} calls, "
                    f"{call_log.total_cents():.2f} cents this session, "
                    f"{call_log.total_cents() / turns:.2f} a turn"
                    f"{' (light)' if llm.light else ''}\n"
                )
            elif command == "/dev":
                dev = not dev
                print(f"\ndev view {'on' if dev else 'off'}\n")
            elif command == "/dice":
                dice = not dice
                print(f"\ndice {'shown' if dice else 'hidden'}\n")
            elif command == "/raw":
                # One object shared by the resolver, the narrator and the turn,
                # so flipping it here flips it everywhere.
                # Currently raw means switch them back on, and the other way
                # round. Written the other way first, which made `/raw` set
                # True when they were already True: a no-op that reported
                # success.
                on = corrections.all_off
                corrections.refuse_turns = on
                corrections.gate_narration = on
                corrections.drop_repeats = on
                corrections.route_writes = on
                print(f"\nraw mode {'on' if corrections.all_off else 'off'}\n")
                if corrections.all_off:
                    print(
                        "The store, the dice and rule 5 still hold. Nothing is"
                        " second-guessing the model now, and a write it puts in"
                        " the wrong place stays there.\n"
                    )
            elif command == "/light":
                # Takes effect on the next call, so the turn in flight is not
                # half one model and half another.
                llm.light = not llm.light
                print(
                    f"\nlight mode {'on' if llm.light else 'off'}: "
                    f"{llm.model_for('resolver')}\n"
                )
                if llm.light:
                    print(
                        "The cheap model is looser with the narration contract."
                        " A blocked sentence costs a regeneration, so a bad run"
                        " can end up no cheaper. Watch /cost.\n"
                    )
            else:
                print(f"\nNo such command.\n\n{COMMANDS}\n")
            continue

        # The voice loads while the resolver thinks, so the first sentence is
        # spoken 0.58s after it clears rather than 1.35s.
        voice.warm()

        try:
            last = turn.play(said, life, on_text=speak)
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
        except ProviderBusy as exc:
            # Keeps the session alive: this clears on its own.
            log_failure(config, said, exc)
            print(f"\n{exc}\n")
            continue
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

        print()  # the narration already went out, sentence by sentence

        if dice and last.roll is not None:
            # Printed, never spoken: the voice is the fiction and this isn't.
            print(last.roll.as_dice())

        if dev:
            print(dev_view(store, resolver, last, call_log, voice))

        if last.ends_life:
            store.end_life(life.id, last.summary)
            print("\nThat life is over.\n")
            break

    wait_for_quiet(voice)
    voice.close()
    print(f"Saved to {world_dir}. {call_log.total_cents():.2f} cents this session.")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
