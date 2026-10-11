"""Model calls through OpenRouter, and a FakeLLM for tests.

Both log every call to logs/calls.jsonl with role, model, tokens, cost, and
latency (rule 9). JSON calls that fail validation get exactly one retry with the
error attached (spec 3.3); a second failure raises, so nothing commits.

Created by __main__.py, which picks OpenRouterLLM or FakeLLM, and called by
resolve.py, render.py, and the background jobs.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .config import OPENROUTER_BASE_URL, Config

T = TypeVar("T", bound=BaseModel)
S = TypeVar("S", bound=BaseModel)


class ModelCallFailed(Exception):
    """The model never returned usable output. The turn must not commit."""


class OutOfCredit(ModelCallFailed):
    """The account has no credit left. Retrying will not help."""


class ProviderBusy(ModelCallFailed):
    """The provider's in-flight budget is exhausted. It clears on its own."""


class LedgerFailed(ModelCallFailed):
    """The scene committed and was narrated, but its ledger was unusable.

    Distinct because the turn cannot be retried: part of it is already true and
    the player has already heard about it. Deltas may be missing, so this is
    loud rather than silent.
    """


# A real playtest lost four turns to OpenRouter 402s that the engine reported to
# the player as "say it again". These are worth one quiet retry instead.
TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
TRANSIENT_BACKOFF_SECONDS = 2.0
# OpenRouter hints Retry-After: 120 for an exhausted in-flight budget, but it
# usually clears as soon as the earlier requests settle, so wait less and let
# the player retry rather than freezing the turn for two minutes.
IN_FLIGHT_BACKOFF_SECONDS = 8.0


@dataclass
class CallRecord:
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_cents: float
    seconds: float
    attempt: int
    ok: bool
    error: str | None = None
    # How long until the narrator could start. This, not `seconds`, is what the
    # player actually waits for on a streamed resolver call.
    scene_seconds: float | None = None


class CallLog:
    """Appends one JSON object per model call. Created by __main__.py."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: list[CallRecord] = []

    def write(self, record: CallRecord) -> None:
        self.records.append(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(record)) + "\n")

    def total_cents(self) -> float:
        return sum(r.cost_cents for r in self.records)


RETRY_PREAMBLE = (
    "Your previous reply was not valid for this task and was rejected.\n"
    "Error:\n{error}\n\n"
    "Reply again with only valid JSON matching the required shape. No prose."
)


@dataclass
class _Streamed:
    """How far one streamed attempt got. Owned by the caller of _stream_attempt.

    `first` being set means the scene was valid and `on_first` has already run,
    which is what tells the retry logic that the turn can no longer be replayed
    from the top.
    """

    first: Any = None
    buffer: str = ""
    json_start: int = 0
    split_at: int = 0
    seconds: float = 0.0
    scene_seconds: float | None = None
    error: Exception | str = "the reply contained no complete JSON object"


class _BaseLLM:
    """Shared retry-and-validate logic. Subclasses only implement _raw."""

    def __init__(self, config: Config, call_log: CallLog) -> None:
        self.config = config
        self.call_log = call_log
        # (input_tokens, output_tokens) from the most recent stream, which the
        # provider only reports once generation has finished.
        self._stream_usage: tuple[int, int] = (0, 0)

    def _raw(self, role: str, model: str, system: str, user: str) -> tuple[str, int, int]:
        """Return (text, input_tokens, output_tokens)."""
        raise NotImplementedError

    def _transport_failed(
        self, exc: Exception, role: str, model: str, started: float,
        attempt: int, transport_try: int,
    ) -> None:
        """Log a failed call and classify it. Returns only if worth retrying.

        Shared by the plain and streaming paths. It used to live only in
        `_call`, which left the streaming path — now the main one — with no
        error handling at all.
        """
        self.call_log.write(
            CallRecord(role, model, 0, 0, 0.0, time.monotonic() - started,
                       attempt, ok=False, error=str(exc))
        )
        status = getattr(exc, "status_code", None)
        message = str(exc).lower()

        if status == 402:
            # Several different problems share this status.
            if "in_flight" in message:
                # Clears on its own once earlier requests settle.
                if transport_try == 1:
                    time.sleep(IN_FLIGHT_BACKOFF_SECONDS)
                    return
                raise ProviderBusy(
                    "The model provider is still working through earlier "
                    "requests. Wait a moment and say it again."
                ) from exc
            raise OutOfCredit(
                "This key cannot afford the request. Either its weekly limit is "
                "spent, the account is empty, or max_tokens reserves more than "
                "is left. OpenRouter said: " + str(exc)
            ) from exc

        if status in TRANSIENT_STATUSES and transport_try == 1:
            time.sleep(TRANSIENT_BACKOFF_SECONDS)
            return
        raise ModelCallFailed(f"{role} call failed: {exc}") from exc

    def _call(self, role: str, system: str, user: str, attempt: int) -> str:
        model = self.config.model_for(role)
        for transport_try in (1, 2):
            started = time.monotonic()
            try:
                text, input_tokens, output_tokens = self._raw(role, model, system, user)
                break
            except Exception as exc:
                self._transport_failed(
                    exc, role, model, started, attempt, transport_try
                )
                continue

        price = self.config.price_for(model)
        self.call_log.write(
            CallRecord(
                role=role,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_cents=price.cents(input_tokens, output_tokens) if price else 0.0,
                seconds=time.monotonic() - started,
                attempt=attempt,
                ok=True,
            )
        )
        return text

    def text(self, role: str, system: str, user: str) -> str:
        """A call whose output is prose, not data."""
        return self._call(role, system, user, attempt=1)

    def _raw_stream(self, role: str, model: str, system: str, user: str):
        """Yield output in chunks. Sets self._stream_usage when finished."""
        raise NotImplementedError

    def _stream_attempt(
        self,
        streamed: _Streamed,
        role: str,
        model: str,
        system: str,
        user: str,
        first_shape: type[T],
        on_first: Callable[[T], None],
        check: Callable[[T], None] | None,
    ) -> None:
        """One pass over the stream, writing its progress into `streamed`.

        `streamed` is the caller's, not a return value, because an exception
        thrown after `on_first` has fired still has to leave behind the fact
        that it fired: by then the scene is committed and spoken for.
        """
        scanner: ObjectScanner | None = None
        started = time.monotonic()
        for chunk in self._raw_stream(role, model, system, user):
            streamed.buffer += chunk
            streamed.seconds = time.monotonic() - started
            if streamed.first is not None:
                continue  # the scene is away; just collect the ledger
            if scanner is None:
                start = json_start(streamed.buffer)
                if start is None:
                    continue
                streamed.json_start = start
                scanner = ObjectScanner(start)
            end = scanner.feed(streamed.buffer)
            if end is None:
                continue
            body = streamed.buffer[streamed.json_start : end]
            try:
                candidate = first_shape.model_validate_json(body)
                if check is not None:
                    check(candidate)
            except (ValidationError, ValueError) as exc:
                # Nothing is committed yet, so abandon the rest of the stream
                # and spend the retry (spec 3.3) with the reason attached.
                streamed.error = exc
                return
            streamed.first, streamed.split_at = candidate, end
            streamed.scene_seconds = time.monotonic() - started
            on_first(candidate)
        streamed.seconds = time.monotonic() - started

    def stream_pair(
        self,
        role: str,
        system: str,
        user: str,
        first_shape: type[T],
        second_shape: type[S],
        on_first: Callable[[T], None],
        check: Callable[[T], None] | None = None,
    ) -> tuple[T, S]:
        """Stream one call that emits two JSON objects, back to back.

        `on_first` runs the moment the first object is complete and valid,
        while the second is still being generated. It is called at most once
        and never with unvalidated output, so it is safe for it to commit.

        If the *second* object is invalid there is no going back — the first
        has already been committed and narrated — so the caller is handed the
        error rather than the whole turn being retried and double-committed.
        """
        # A stream can end with no complete object at all — truncated output, or
        # prose instead of JSON — so this must be bound before the retry reads it.
        error: Exception | str = "the reply contained no complete JSON object"
        for attempt in (1, 2):
            if attempt == 2:
                user = user + "\n\n" + RETRY_PREAMBLE.format(error=str(error))
            model = self.config.model_for(role)
            streamed = _Streamed()

            for transport_try in (1, 2):
                started = time.monotonic()
                streamed = _Streamed()
                try:
                    self._stream_attempt(
                        streamed, role, model, system, user,
                        first_shape, on_first, check,
                    )
                except Exception as exc:
                    if streamed.first is not None:
                        # The scene is already committed and narrated, so the
                        # call cannot be replayed from the top.
                        self._log_stream(role, model, streamed, attempt)
                        raise LedgerFailed(
                            f"{role} died after its scene was committed: {exc}"
                        ) from exc
                    self._transport_failed(
                        exc, role, model, started, attempt, transport_try
                    )
                    continue
                break

            # Every attempt that reached the provider is logged, whether or not
            # its scene survived validation (rule 9). A rejected one used to
            # vanish, which hid the retry and under-reported the session spend.
            self._log_stream(role, model, streamed, attempt)

            if streamed.first is None:
                error = streamed.error
                if attempt == 2:
                    raise ModelCallFailed(f"{role} scene was unusable twice: {error}")
                continue

            try:
                return streamed.first, self._second_object(streamed, second_shape)
            except (ValidationError, ValueError) as exc:
                raise LedgerFailed(
                    f"{role} scene committed but its ledger was unusable: {exc}"
                ) from exc

        raise ModelCallFailed(f"{role} produced no scene")

    @staticmethod
    def _second_object(streamed: _Streamed, shape: type[S]) -> S:
        """The ledger, found the same way the scene was.

        Brace-scanning rather than parsing the whole remainder, because models
        add a closing fence or a line of prose after the second object often
        enough, and a lost ledger means wrong money.
        """
        rest = streamed.buffer[streamed.split_at :]
        start = json_start(rest)
        if start is None:
            return shape.model_validate_json("{}")  # nothing followed the scene
        end = ObjectScanner(start).feed(rest)
        if end is None:
            # Truncated, so there is no object to read. Let the shape decide
            # whether an empty ledger is acceptable.
            return shape.model_validate_json(rest[start:].strip() or "{}")
        return shape.model_validate_json(rest[start:end])

    def _log_stream(
        self, role: str, model: str, streamed: _Streamed, attempt: int
    ) -> None:
        # An abandoned stream never reaches the provider's usage chunk, so a
        # rejected scene logs zero tokens. The record still has to exist: it is
        # the only trace that the attempt happened and why it was thrown away.
        usage = self._stream_usage if streamed.first is not None else (0, 0)
        price = self.config.price_for(model)
        self.call_log.write(
            CallRecord(
                role=role,
                model=model,
                input_tokens=usage[0],
                output_tokens=usage[1],
                cost_cents=price.cents(*usage) if price else 0.0,
                seconds=streamed.seconds,
                attempt=attempt,
                ok=streamed.first is not None,
                error=None if streamed.first is not None else str(streamed.error),
                scene_seconds=streamed.scene_seconds,
            )
        )

    def json(
        self,
        role: str,
        system: str,
        user: str,
        shape: type[T],
        check: Callable[[T], None] | None = None,
    ) -> T:
        """A call whose output feeds code. One retry on invalid output.

        `check` raises ValueError for output that parses but doesn't make sense
        against the store, like moving the player into a place that doesn't
        exist. Those get the same single retry as a schema error, so semantic
        problems reach the model with an explanation instead of being dropped.
        """

        def parse(raw: str) -> T:
            parsed = shape.model_validate_json(_strip_fences(raw))
            if check is not None:
                check(parsed)
            return parsed

        raw = self._call(role, system, user, attempt=1)
        try:
            return parse(raw)
        except (ValidationError, ValueError) as first:
            retry_user = user + "\n\n" + RETRY_PREAMBLE.format(error=str(first))
            raw = self._call(role, system, retry_user, attempt=2)
            try:
                return parse(raw)
            except (ValidationError, ValueError) as second:
                raise ModelCallFailed(
                    f"{role} returned unusable output twice: {second}"
                ) from second


class ObjectScanner:
    """Finds where a top-level JSON object closes, one chunk at a time.

    This is what lets narration start before the resolver has finished writing:
    the scene object closes long before the ledger that follows it. It keeps its
    brace depth between calls, so a growing buffer is read once rather than
    rescanned from the top on every chunk — the splitter sits on the one path
    the player is actually waiting on, and rescanning made it quadratic in
    output length.
    """

    __slots__ = ("_depth", "_in_string", "_escaped", "_read", "_end")

    def __init__(self, start: int = 0) -> None:
        self._depth = 0
        self._in_string = False
        self._escaped = False
        self._read = start
        self._end: int | None = None

    def feed(self, text: str) -> int | None:
        """Index into `text` just past the object's closing brace, or None.

        `text` must be the same buffer each time, only longer.
        """
        if self._end is not None:
            return self._end
        for index in range(self._read, len(text)):
            char = text[index]
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif char == "\\":
                    self._escaped = True
                elif char == '"':
                    self._in_string = False
                continue
            if char == '"':
                self._in_string = True
            elif char == "{":
                self._depth += 1
            elif char == "}":
                self._depth -= 1
                if self._depth == 0:
                    self._end = index + 1
                    return self._end
        self._read = len(text)
        return None


def first_object_end(text: str) -> int | None:
    """Index just past the first complete top-level JSON object, or None."""
    return ObjectScanner().feed(text)


def json_start(text: str) -> int | None:
    """Where the JSON begins in a partial reply, or None if not yet knowable.

    Models wrap output in ```json fences even when told not to. A streamed
    reply has to be sliced at a stable offset, because the scanner remembers
    how far it has read, so this waits until the answer cannot change: either a
    non-fence character has arrived, or the fence's header line has closed.
    """
    index = 0
    while index < len(text) and text[index].isspace():
        index += 1
    if index == len(text):
        return None  # nothing but whitespace so far; a fence could still start
    if text[index] == "`":
        newline = text.find("\n", index)
        if newline == -1:
            return None  # still inside the fence header
        return newline + 1
    return index


def _strip_fences(text: str) -> str:
    """Models like to wrap JSON in ```json fences even when told not to."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        if cleaned.endswith("```"):
            cleaned = cleaned[: -len("```")]
    return cleaned.strip()


class OpenRouterLLM(_BaseLLM):
    """The real client. Created by __main__.py when a key is present."""

    def __init__(self, config: Config, call_log: CallLog, max_tokens: int = 1200) -> None:
        super().__init__(config, call_log)
        from openai import OpenAI

        self.max_tokens = max_tokens
        self._client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=config.api_key)

    def _raw(self, role: str, model: str, system: str, user: str) -> tuple[str, int, int]:
        completion = self._client.chat.completions.create(
            model=model,
            max_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        usage = completion.usage
        return (
            (completion.choices[0].message.content or "").strip(),
            usage.prompt_tokens if usage else 0,
            usage.completion_tokens if usage else 0,
        )

    def _raw_stream(self, role: str, model: str, system: str, user: str):
        self._stream_usage = (0, 0)
        stream = self._client.chat.completions.create(
            model=model,
            max_tokens=self.max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        for chunk in stream:
            if getattr(chunk, "usage", None):
                self._stream_usage = (
                    chunk.usage.prompt_tokens,
                    chunk.usage.completion_tokens,
                )
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


class FakeLLM(_BaseLLM):
    """Scripted responses for tests. Never touches the network.

    Queue one string per expected call, per role:
        FakeLLM(config, log, {"resolver": ['{"intent": "..."}']})
    """

    def __init__(
        self, config: Config, call_log: CallLog, scripts: dict[str, list[str]]
    ) -> None:
        super().__init__(config, call_log)
        self.scripts = {role: list(replies) for role, replies in scripts.items()}
        self.prompts: list[tuple[str, str, str]] = []

    def _raw(self, role: str, model: str, system: str, user: str) -> tuple[str, int, int]:
        self.prompts.append((role, system, user))
        queue = self.scripts.get(role)
        if not queue:
            raise AssertionError(f"FakeLLM has no scripted reply left for {role!r}")
        reply = queue.pop(0)
        # Rough token counts so cost and budget tests have something to read.
        return reply, len(system + user) // 4, len(reply) // 4

    def _raw_stream(self, role: str, model: str, system: str, user: str):
        reply, input_tokens, output_tokens = self._raw(role, model, system, user)
        self._stream_usage = (input_tokens, output_tokens)
        # Deliberately chunked small, so tests exercise the mid-stream split
        # rather than seeing one complete blob.
        size = max(1, len(reply) // 7)
        for start in range(0, len(reply), size):
            yield reply[start : start + size]
