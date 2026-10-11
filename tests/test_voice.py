"""Speech out (spec 13.2). No test spawns `say`, so the suite stays silent.

The spawner is injected, so these tests assert the three things that are easy to
get wrong and invisible once it works: sentences come out in order, the model
stream is never made to wait for the speakers, and a broken synthesizer costs
the voice and not the turn.
"""

from __future__ import annotations

import threading
import time

import pytest

from worldsim.voice import Voice

TIMEOUT = 5.0  # generous: these are thread handoffs, not model calls


class FakePipe:
    def __init__(self, process: "FakeSay") -> None:
        self._process = process

    def write(self, data: bytes) -> int:
        if self._process.killed:
            raise BrokenPipeError("killed")
        self._process.text += data
        return len(data)

    def close(self) -> None:
        self._process.fed.set()


class FakeSay:
    """Stands in for one `say` process. `speaking_for` fakes playback."""

    def __init__(self, argv: list[str], speaking_for: float = 0.0, exits: int = 0) -> None:
        self.argv = argv
        self.text = b""
        self.killed = False
        self.returncode: int | None = None
        self.fed = threading.Event()
        self.stdin = FakePipe(self)
        self._speaking_for = speaking_for
        self._exits = exits

    @property
    def said(self) -> str:
        return self.text.decode("utf-8")

    def wait(self) -> int:
        if self.returncode is None:
            if self._speaking_for:
                time.sleep(self._speaking_for)
            self.returncode = -9 if self.killed else self._exits
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.fed.set()

    def poll(self) -> int | None:
        return self.returncode


class Recorder:
    """Every process the voice spawned, in order."""

    def __init__(self, speaking_for: float = 0.0, exits: int = 0, raises: bool = False) -> None:
        self.spawned: list[FakeSay] = []
        self._speaking_for = speaking_for
        self._exits = exits
        self._raises = raises

    def __call__(self, argv: list[str]) -> FakeSay:
        if self._raises:
            raise OSError("Operation not permitted")
        process = FakeSay(argv, self._speaking_for, self._exits)
        self.spawned.append(process)
        return process

    @property
    def fed(self) -> list[FakeSay]:
        return [p for p in self.spawned if p.text]

    @property
    def spoken(self) -> list[str]:
        return [p.said for p in self.fed]


def until(condition, timeout: float = TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def recorder():
    return Recorder()


@pytest.fixture
def voice(recorder):
    v = Voice(enabled=True, spawn=recorder)
    yield v
    v.close()


# ---- the sentences come out, in order ----


def test_sentences_are_spoken_in_the_order_they_cleared(voice, recorder):
    for line in ("The rain has not let up.", "A car slows at the curb.", "He does not get out."):
        voice.say(line)

    assert voice.wait(timeout=TIMEOUT), "the voice never went quiet"
    assert recorder.spoken == [
        "The rain has not let up.",
        "A car slows at the curb.",
        "He does not get out.",
    ]


def test_nothing_is_spoken_through_a_shell_or_through_argv(voice, recorder):
    """Narration is full of apostrophes. Nothing should have to be escaped."""
    line = "\"Don't,\" he says, and the screen door bangs; $HOME `whoami`."
    voice.say(line)

    assert voice.wait(timeout=TIMEOUT)
    assert recorder.spoken == [line]
    # The sentence reached it on stdin, not as an argument.
    argv = recorder.fed[0].argv
    assert argv[0] == "/usr/bin/say"
    assert argv[-2:] == ["-f", "-"]
    assert not any(line[:12] in arg for arg in argv)


def test_blank_sentences_are_not_spawned_for(voice, recorder):
    voice.say("   ")
    voice.say("")
    assert voice.wait(timeout=TIMEOUT)
    assert recorder.spawned == []


# ---- the model stream never waits for the speakers ----


def test_saying_a_sentence_does_not_wait_for_it_to_be_spoken():
    """The whole point. `say()` returns while the last one is still playing."""
    recorder = Recorder(speaking_for=0.3)
    voice = Voice(enabled=True, spawn=recorder)
    try:
        started = time.monotonic()
        for _ in range(4):
            voice.say("Four sentences, more than a second of speech.")
        enqueued = time.monotonic() - started

        assert enqueued < 0.1, f"handing over four sentences blocked for {enqueued:.2f}s"
        assert voice.wait(timeout=TIMEOUT)
        assert len(recorder.fed) == 4
    finally:
        voice.close()


def test_a_process_stands_by_so_no_sentence_pays_the_cold_cost(voice, recorder):
    """Spec 13.2 and the measurements in the milestone 7 brief.

    A cold `say` costs 1.35s before the first sound and a warm one 0.58s, so
    every sentence has to find a process already spawned and waiting.
    """
    voice.warm()
    assert len(recorder.spawned) == 1, "warm() did not put a process on standby"
    standby = recorder.spawned[0]
    assert standby.text == b"", "the standby process was fed before there was anything to say"

    voice.say("The rain has not let up.")
    assert until(lambda: standby.fed.is_set()), "the first sentence ignored the standby process"
    assert standby.said == "The rain has not let up."

    # And a replacement is spawned for the sentence after it, during playback.
    assert until(lambda: len(recorder.spawned) == 2), "no replacement was put on standby"
    assert voice.wait(timeout=TIMEOUT)


def test_a_second_sentence_finds_a_warm_process_too(recorder):
    """The replacement is spawned while the first sentence is still playing."""
    recorder = Recorder(speaking_for=0.2)
    voice = Voice(enabled=True, spawn=recorder)
    try:
        voice.warm()
        voice.say("One.")
        voice.say("Two.")
        assert voice.wait(timeout=TIMEOUT)
        # Two sentences, each fed to a process spawned before its text existed.
        assert len(recorder.fed) == 2
        assert recorder.spoken == ["One.", "Two."]
    finally:
        voice.close()


def test_waiting_kills_the_leftover_standby(voice, recorder):
    voice.warm()
    voice.say("Just the one.")
    assert voice.wait(timeout=TIMEOUT)

    # Nothing more is coming, so the standby would otherwise idle until the next
    # turn spawns a fresh one.
    assert all(p.returncode is not None for p in recorder.spawned if not p.text)


# ---- turn-taking ----


def test_waiting_returns_only_once_the_room_is_quiet():
    recorder = Recorder(speaking_for=0.25)
    voice = Voice(enabled=True, spawn=recorder)
    try:
        voice.say("One.")
        voice.say("Two.")
        started = time.monotonic()
        assert voice.wait(timeout=TIMEOUT)
        waited = time.monotonic() - started
        assert waited >= 0.4, f"wait() returned after {waited:.2f}s, before the voice stopped"
    finally:
        voice.close()


def test_silence_cuts_the_sentence_and_drops_the_rest():
    """Barge-in. The player gets the prompt back without hearing the rest."""
    recorder = Recorder(speaking_for=2.0)
    voice = Voice(enabled=True, spawn=recorder)
    try:
        for line in ("One.", "Two.", "Three."):
            voice.say(line)
        assert until(lambda: len(recorder.fed) == 1), "nothing started speaking"

        voice.silence()

        assert voice.wait(timeout=TIMEOUT)
        assert recorder.fed[0].killed, "the sentence in the air was not cut off"
        assert len(recorder.fed) == 1, "a dropped sentence was spoken anyway"
    finally:
        voice.close()


def test_silence_on_a_quiet_voice_is_harmless(voice):
    voice.silence()
    voice.silence()
    assert voice.wait(timeout=TIMEOUT)


# ---- the voice is an output device, never a reason to lose a turn ----


def test_a_voice_that_cannot_start_goes_quiet_instead_of_raising():
    recorder = Recorder(raises=True)
    voice = Voice(enabled=True, spawn=recorder)
    try:
        voice.warm()
        voice.say("The rain has not let up.")
        assert voice.wait(timeout=TIMEOUT)

        assert not voice.enabled, "a voice that cannot spawn kept trying"
        assert voice.failures, "the failure was not recorded anywhere"
    finally:
        voice.close()


def test_a_sentence_that_exits_badly_is_recorded_and_the_next_still_plays():
    recorder = Recorder(exits=1)
    voice = Voice(enabled=True, spawn=recorder)
    try:
        voice.say("One.")
        voice.say("Two.")
        assert voice.wait(timeout=TIMEOUT)

        assert recorder.spoken == ["One.", "Two."], "one bad exit stopped the voice"
        assert len(voice.failures) == 2
    finally:
        voice.close()


def test_failures_do_not_grow_without_bound():
    recorder = Recorder(exits=1)
    voice = Voice(enabled=True, spawn=recorder)
    try:
        for i in range(40):
            voice.say(f"Sentence {i}.")
        assert voice.wait(timeout=TIMEOUT)
        assert len(voice.failures) == 20
    finally:
        voice.close()


def test_a_disabled_voice_spawns_nothing_and_never_waits(recorder):
    voice = Voice(enabled=False, spawn=recorder)
    voice.warm()
    voice.say("The rain has not let up.")
    assert voice.wait(timeout=0.0) is True
    voice.silence()
    voice.close()
    assert recorder.spawned == []


def test_closing_twice_is_harmless(voice):
    voice.say("One.")
    voice.close()
    voice.close()


# ---- settings come from .env, never from code ----


def test_the_system_voice_is_the_default(recorder):
    voice = Voice(enabled=True, spawn=recorder)
    try:
        voice.warm()
        assert recorder.spawned[0].argv == ["/usr/bin/say", "-f", "-"]
    finally:
        voice.close()


def test_a_configured_voice_and_rate_reach_the_command(recorder):
    voice = Voice(enabled=True, voice="Daniel", rate=160, spawn=recorder)
    try:
        voice.warm()
        argv = recorder.spawned[0].argv
        assert argv[0] == "/usr/bin/say"
        assert argv[-2:] == ["-f", "-"]
        assert "-v" in argv and argv[argv.index("-v") + 1] == "Daniel"
        assert "-r" in argv and argv[argv.index("-r") + 1] == "160"
    finally:
        voice.close()
