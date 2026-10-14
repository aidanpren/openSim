"""Drive a life through scripted turns and write the transcript (spec 14).

    PYTHONPATH=src ./.venv/bin/python scripts/playtest.py <name> [turns.txt]

This is the harness the 30-turn run used, made repeatable. It builds the real
components against the real models — so it costs money and needs the network —
and writes a reviewable transcript to `logs/playtests/`, with every dropped
write, every roll, and the latency and cost of every call.

Player lines come from a file, one per line, or from stdin. Blank lines and
lines starting with `#` are skipped. A line of `---` pauses and prints what the
world has said so far, which is how a batch gets answered against what actually
happened rather than against what was expected.

Nothing here is imported by the game. It exists so a playtest is a command
rather than a procedure.
"""

from __future__ import annotations

import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from worldsim.config import load_config  # noqa: E402
from worldsim.llm import CallLog, ModelCallFailed, OpenRouterLLM  # noqa: E402
from worldsim.render import Narrator, sentences_in  # noqa: E402
from worldsim.resolve import Resolver  # noqa: E402
from worldsim.sheet import character_sheet, local_time  # noqa: E402
from worldsim.store import Store  # noqa: E402
from worldsim.turn import Turn, begin_life  # noqa: E402
from worldsim.__main__ import PROMPTS_DIR  # noqa: E402


def read_lines(path: str | None) -> list[str]:
    text = Path(path).read_text() if path else sys.stdin.read()
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    name = argv[0]
    said_lines = read_lines(argv[1] if len(argv) > 1 else None)

    config = load_config(REPO_ROOT)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = config.logs_dir / "playtests" / f"{stamp}-{name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    call_log = CallLog(config.logs_dir / "calls.jsonl")
    store = Store(config.saves_dir / f"playtest-{name}" / "world.db")
    llm = OpenRouterLLM(config, call_log)
    resolver = Resolver(llm, store, config, PROMPTS_DIR)
    narrator = Narrator(llm, store, config, PROMPTS_DIR)
    turn = Turn(store, resolver, narrator, random.Random())

    lines: list[str] = [f"# Playtest: {name}, {stamp}", ""]
    firsts: list[float] = []

    def note(text: str = "") -> None:
        lines.append(text)
        # The transcript is the point of the run and it is written at the end,
        # so a closed stdout — piping this to `head` — must not take the run
        # down with it. One live run was lost exactly that way.
        try:
            print(text, flush=True)
        except (BrokenPipeError, ValueError):
            pass

    # Resuming matters for a playtest specifically: writing player lines that
    # answer what the world actually said means spawning, reading the opening,
    # and coming back with the next batch.
    life = store.current_life()
    if life is None:
        started = time.monotonic()
        life, opening = begin_life(
            store, resolver, narrator, config.spawn_city, random.Random(),
            premises_path=config.logs_dir / "premises.txt",
        )
        note(f"Spawned in {time.monotonic() - started:.1f}s. {local_time(store)}.")
        note()
        note(opening)
    else:
        past = store.recent_turns(life.id, limit=1)
        note(f"Resumed. {local_time(store)}.")
        note()
        if past and past[0]["narration"]:
            note(past[0]["narration"])
    note()

    for said in said_lines:
        if said == "---":
            note("<!-- batch boundary -->")
            continue
        # Time to the first narrated word, which is the only latency target
        # worth optimising (spec 12).
        first: list[float] = []
        began = time.monotonic()

        def spoken(sentence: str) -> None:
            if not first:
                first.append(time.monotonic() - began)

        note(f"> {said}")
        try:
            result = turn.play(said, life, on_text=spoken)
        except ModelCallFailed as exc:
            note(f"  [REFUSED: {exc}]")
            note()
            continue
        if first:
            firsts.append(first[0])
        note(result.narration)
        note(
            f"  [{result.mode}, {result.duration_minutes} min, "
            f"first word {first[0] if first else float('nan'):.1f}s, "
            f"{len(sentences_in(result.narration))} sentences]"
        )
        if result.roll:
            note(f"  [ROLL {result.roll.as_dice()} {result.roll.band}]")
        for item in result.fired:
            note(f"  [CLOCK: {item}]")
        for item in result.changed_state:
            note(f"  [STATE: {item}]")
        for item in result.dropped:
            note(f"  [DROPPED: {item}]")
        if result.ends_life:
            note("  [THAT LIFE IS OVER]")
            break
        note()

    note("## Sheet at the end")
    note()
    note("```")
    note(character_sheet(store))
    note("```")
    note()
    note("## Measured")
    note()
    turns = max(len(firsts), 1)
    note(f"- turns played: {len(firsts)}")
    if firsts:
        ordered = sorted(firsts)
        note(f"- first narrated word, p50: {ordered[len(ordered) // 2]:.1f}s")
        note(f"- first narrated word, worst: {ordered[-1]:.1f}s")
    note(f"- cost: {call_log.total_cents():.2f}c total, "
         f"{call_log.total_cents() / turns:.2f}c a turn")
    note(f"- calls: {len(call_log.records)}, "
         f"{len(call_log.records) / turns:.1f} a turn")
    note(f"- contract breaches that survived a regeneration: {narrator.breaches}")
    note()
    note("## Timeline still ahead")
    note()
    for event in store.pending():
        note(f"- {event.at.isoformat()} {event.what}"
             f"{' (reaches you)' if event.reaches_player else ''}")

    out.write_text("\n".join(lines) + "\n")
    store.close()
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
