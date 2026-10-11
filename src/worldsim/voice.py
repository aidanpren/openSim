"""Speech out (spec 13.2). Built in `__main__.py`, fed by its `speak()` seam.

The narration already arrives one sentence at a time, the moment each sentence
clears the contract. This turns those sentences into sound without making the
model wait for the speakers: `say()` only enqueues, and a worker thread does
the talking.

Why `say` is driven this strangely, measured on 2026-10-11:

- It costs a fixed 1.35s before the first sound, whatever the sentence. Four
  sentences spawned one after another means 5.4s of silence inside a turn.
- That cost is voice loading, and it does not depend on the text. A process
  spawned 1.0s or more before the text is handed to it pays only 0.58s.
- It does not speak until stdin closes, so one process cannot be fed clause by
  clause. It is one process per sentence.

So a spawned, unfed process stands by, and its replacement is spawned the
instant the current one is fed. Every sentence then has the previous sentence's
playback to warm up in, which is always more than 1.0s. Every sentence pays
0.58s; none pays 1.35s.

Voice is an output device, never a reason to lose a turn. Anything that goes
wrong is recorded on `failures` and the game goes quiet, still printing text.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Protocol

SAY = Path("/usr/bin/say")

# Reading from stdin rather than argv: narration is full of apostrophes and
# quotes, and nothing should have to be escaped on the way to being spoken.
STDIN = "-f"

# The worker stops when it sees this, which a sentence can never be.
_STOP = object()


class Process(Protocol):
    """The part of `subprocess.Popen` this module uses."""

    stdin: object

    def wait(self) -> int: ...

    def kill(self) -> None: ...

    def poll(self) -> int | None: ...


Spawn = Callable[[list[str]], Process]


def spawn_say(args: list[str]) -> Process:
    """The real spawner. Absolute path, no shell, text on stdin."""
    return subprocess.Popen(args, stdin=subprocess.PIPE)


def available() -> bool:
    """Whether this machine can speak at all."""
    return sys.platform == "darwin" and SAY.is_file()


class Voice:
    """Speaks sentences in order, without ever blocking the caller.

    Not reusable across threads by more than one producer: the narration is a
    single stream, and sentences have to stay in order.
    """

    def __init__(
        self,
        enabled: bool = True,
        voice: str = "",
        rate: int | None = None,
        spawn: Spawn | None = None,
    ) -> None:
        # `spawn` is injected so tests never make noise. An explicit spawner
        # also means `enabled` can be true in a test without `say` existing.
        self.enabled = enabled and (spawn is not None or available())
        self.failures: list[str] = []
        self._argv = [str(SAY), STDIN, "-"]
        if voice:
            self._argv[1:1] = ["-v", voice]
        if rate:
            self._argv[1:1] = ["-r", str(rate)]
        self._spawn = spawn or spawn_say
        self._queue: queue.Queue = queue.Queue()
        self._warm: Process | None = None
        self._speaking: Process | None = None
        # Held while taking from the queue and while a sentence is in the air,
        # so `silence()` can tell what to kill and `wait()` can tell when the
        # room is actually quiet rather than just when the queue is empty.
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._worker: threading.Thread | None = None

    # ---- what the game calls ----

    def warm(self) -> None:
        """Put a fresh process on standby. Call before the resolver runs.

        The voice then loads during the seconds the model is thinking, which is
        the whole trick: that wait already exists.
        """
        if not self.enabled:
            return
        with self._lock:
            self._discard_warm()
            self._warm = self._try_spawn()

    def say(self, sentence: str) -> None:
        """Enqueue one sentence. Returns at once; never blocks the stream."""
        if not self.enabled or not sentence.strip():
            return
        self._ensure_worker()
        self._idle.clear()
        self._queue.put(sentence)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the room is quiet. The turn-taking boundary.

        Returns False if it gave up waiting, so a wedged synthesizer cannot
        hold the prompt hostage.
        """
        if not self.enabled:
            return True
        quiet = self._idle.wait(timeout)
        if quiet:
            # Nothing is coming, so a process on standby would only idle until
            # the next turn warms a fresh one.
            with self._lock:
                self._discard_warm()
        return quiet

    def silence(self) -> None:
        """Cut the current sentence and drop the rest. Barge-in."""
        if not self.enabled:
            return
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        with self._lock:
            self._kill(self._speaking)
            self._speaking = None
            self._discard_warm()
        self._idle.set()

    def close(self) -> None:
        """Stop talking and let the worker go. Safe to call twice."""
        if self._worker is None:
            with self._lock:
                self._discard_warm()
            return
        self.silence()
        self._queue.put(_STOP)
        self._worker.join(timeout=2.0)
        self._worker = None

    # ---- the worker ----

    def _ensure_worker(self) -> None:
        if self._worker is None:
            # Daemon: a stuck synthesizer must not keep the process alive after
            # the world is saved.
            self._worker = threading.Thread(
                target=self._run, name="voice", daemon=True
            )
            self._worker.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._queue.task_done()
                return
            try:
                self._utter(item)
            except Exception as exc:  # never kill the worker over one sentence
                self._fail(f"speaking {item[:40]!r}: {exc}")
            finally:
                self._queue.task_done()
                if self._queue.empty():
                    self._idle.set()

    def _utter(self, sentence: str) -> None:
        with self._lock:
            process = self._warm or self._try_spawn()
            self._warm = None
            if process is None:
                return
            self._speaking = process
            # Spawned before waiting, not after, so it warms up inside the 1.4s
            # or more that the sentence below takes to speak. Unconditionally,
            # even with an empty queue: the next sentence is usually still being
            # generated at this moment, and checking would miss it by a hair.
            # An unused standby process costs one kill in `wait()`.
            self._warm = self._try_spawn()
        lost = None
        try:
            process.stdin.write(sentence.encode("utf-8"))
            process.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            # Killed by `silence()`, or `say` died on its own. Either way this
            # sentence is lost and the next one is not.
            self._kill(process)
            lost = exc
        else:
            code = process.wait()
            if code not in (0, -9):  # -9 is `silence()` doing its job
                self._fail(f"say exited {code} on {sentence[:40]!r}")
        with self._lock:
            interrupted = self._speaking is not process  # `silence()` got here first
            if not interrupted:
                self._speaking = None
        if lost is not None and not interrupted:
            self._fail(f"could not hand over {sentence[:40]!r}: {lost}")

    # ---- processes ----

    def _try_spawn(self) -> Process | None:
        try:
            return self._spawn(list(self._argv))
        except OSError as exc:
            # No `say`, or the endpoint policy refused it. Go quiet for good
            # rather than retrying once a sentence for the rest of the session.
            self._fail(f"could not start the voice: {exc}")
            self.enabled = False
            return None

    def _discard_warm(self) -> None:
        """Caller holds the lock."""
        self._kill(self._warm)
        self._warm = None

    @staticmethod
    def _kill(process: Process | None) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            process.kill()
        except OSError:
            pass

    def _fail(self, message: str) -> None:
        # Capped: a broken synthesizer should not grow a list for 500 turns.
        if len(self.failures) < 20:
            self.failures.append(message)
