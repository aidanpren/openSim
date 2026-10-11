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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

from pydantic import BaseModel, ValidationError

from .config import OPENROUTER_BASE_URL, Config

T = TypeVar("T", bound=BaseModel)


class ModelCallFailed(Exception):
    """The model never returned usable output. The turn must not commit."""


class OutOfCredit(ModelCallFailed):
    """The account has no credit left. Retrying will not help."""


class ProviderBusy(ModelCallFailed):
    """The provider's in-flight budget is exhausted. It clears on its own."""


class StopNarration(Exception):
    """Raised by a stream_text consumer that has heard enough.

    The narrator is gated a sentence at a time, so a contract breach partway
    through is caught before that sentence is spoken. There is no taking back
    what has already gone out, so the rest of the generation is abandoned
    instead of being regenerated.
    """


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
    # Time to the first content token. Six streamed calls in logs/calls.jsonl
    # could not separate this from generation — the throughput of the same
    # model varied from 19.7 to 33.9 tok/s between calls — so which of the two
    # the player is actually waiting on was guesswork. It decides whether the
    # lever is fewer input tokens or fewer output tokens, so it gets measured.
    ttft_seconds: float | None = None
    # Wall time and output tokens at the close of each JSON object in a
    # multi-object stream, so the cost of each phase is readable afterwards.
    object_seconds: list[float] | None = None
    object_tokens: list[int] | None = None


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

# A validation error can run to hundreds of tokens of pydantic URLs, and the
# retry appends it to a prompt that was already budgeted. Cap it, and have the
# caller hold back this much room (spec 5.2).
MAX_ERROR_CHARS = 600
RETRY_ROOM = (len(RETRY_PREAMBLE) + MAX_ERROR_CHARS) // 4 + 1


@dataclass
class _Streamed:
    """How far one streamed attempt got. Owned by the caller of _stream_attempt.

    A non-empty `objects` means at least one object was valid and handed over,
    which is what tells the retry logic that the turn can no longer be replayed
    from the top: part of it is committed and the player has heard about it.
    """

    objects: list[Any] = field(default_factory=list)
    buffer: str = ""
    cursor: int = 0  # where in the buffer the next object starts
    seconds: float = 0.0
    ttft: float | None = None
    object_seconds: list[float] = field(default_factory=list)
    object_tokens: list[int] = field(default_factory=list)
    error: Exception | str = "the reply contained no complete JSON object"

    @property
    def handed_over(self) -> bool:
        return bool(self.objects)


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
        shapes: Sequence[type[BaseModel]],
        on_object: Callable[[int, Any], None],
        checks: Sequence[Callable[[Any], None] | None],
    ) -> None:
        """One pass over the stream, writing its progress into `streamed`.

        `streamed` is the caller's, not a return value, because an exception
        thrown after an object has been handed over still has to leave behind
        the fact that it was: by then part of the turn is committed and spoken
        for, and the call can no longer be replayed from the top.
        """
        scanner: ObjectScanner | None = None
        started = time.monotonic()
        for chunk in self._raw_stream(role, model, system, user):
            if streamed.ttft is None:
                streamed.ttft = time.monotonic() - started
            streamed.buffer += chunk
            streamed.seconds = time.monotonic() - started
            # Every object is away; the rest of the stream is just the usage
            # chunk the provider sends last.
            while len(streamed.objects) < len(shapes):
                if scanner is None:
                    start = json_start(streamed.buffer[streamed.cursor :])
                    if start is None:
                        break
                    streamed.cursor += start
                    scanner = ObjectScanner()
                end = scanner.feed(streamed.buffer[streamed.cursor :])
                if end is None:
                    break
                index = len(streamed.objects)
                body = streamed.buffer[streamed.cursor : streamed.cursor + end]
                try:
                    candidate = shapes[index].model_validate_json(body)
                    check = checks[index] if index < len(checks) else None
                    if check is not None:
                        check(candidate)
                except (ValidationError, ValueError) as exc:
                    streamed.error = exc
                    # Nothing handed over yet means nothing is committed, so the
                    # whole call can be retried (spec 3.3) with the reason
                    # attached. Otherwise the caller has to live with what it
                    # already committed and narrated.
                    return
                streamed.cursor += end
                scanner = None
                streamed.object_seconds.append(time.monotonic() - started)
                streamed.object_tokens.append(len(body) // 4)
                streamed.objects.append(candidate)
                on_object(index, candidate)
        streamed.seconds = time.monotonic() - started

    def stream_objects(
        self,
        role: str,
        system: str,
        user: str,
        shapes: Sequence[type[BaseModel]],
        on_object: Callable[[int, Any], None],
        checks: Sequence[Callable[[Any], None] | None] = (),
    ) -> list[Any]:
        """Stream one call that emits several JSON objects, back to back.

        `on_object(index, obj)` runs the moment each object is complete and
        valid, while the ones after it are still being generated. It is never
        called with unvalidated output, so it is safe for it to commit — which
        is the whole point: the first object is small, so narration can start
        on it seconds before the rest of the turn exists.

        Retries work on exactly one boundary. Until the first object is handed
        over nothing is committed, so a bad reply spends the spec 3.3 retry.
        After that there is no going back, and the caller is handed
        `LedgerFailed` with whatever it already committed left standing.
        """
        # A stream can end with no complete object at all — truncated output, or
        # prose instead of JSON — so this must be bound before the retry reads it.
        error: Exception | str = "the reply contained no complete JSON object"
        for attempt in (1, 2):
            if attempt == 2:
                user = user + "\n\n" + RETRY_PREAMBLE.format(
                    error=str(error)[:MAX_ERROR_CHARS]
                )
            model = self.config.model_for(role)
            streamed = _Streamed()

            for transport_try in (1, 2):
                started = time.monotonic()
                streamed = _Streamed()
                try:
                    self._stream_attempt(
                        streamed, role, model, system, user,
                        shapes, on_object, checks,
                    )
                except Exception as exc:
                    if streamed.handed_over:
                        self._log_stream(role, model, streamed, attempt)
                        raise LedgerFailed(
                            f"{role} died after {len(streamed.objects)} of "
                            f"{len(shapes)} objects were committed: {exc}"
                        ) from exc
                    self._transport_failed(
                        exc, role, model, started, attempt, transport_try
                    )
                    continue
                break

            # Every attempt that reached the provider is logged, whether or not
            # it survived validation (rule 9). A rejected one used to vanish,
            # which hid the retry and under-reported the session spend.
            self._log_stream(role, model, streamed, attempt)

            if not streamed.handed_over:
                error = streamed.error
                if attempt == 2:
                    raise ModelCallFailed(
                        f"{role} output was unusable twice: {error}"
                    )
                continue

            if len(streamed.objects) < len(shapes):
                # Everything up to here is true and spoken, so this cannot be
                # retried. Loud, because the missing tail carries the deltas.
                raise LedgerFailed(
                    f"{role} committed {len(streamed.objects)} of {len(shapes)} "
                    f"objects, then stopped: {streamed.error}"
                )
            return streamed.objects

        raise ModelCallFailed(f"{role} produced nothing usable")

    def _log_stream(
        self, role: str, model: str, streamed: _Streamed, attempt: int
    ) -> None:
        # An abandoned stream never reaches the provider's usage chunk, so a
        # rejected attempt logs zero tokens. The record still has to exist: it
        # is the only trace that the attempt happened and why it was dropped.
        usage = self._stream_usage if streamed.handed_over else (0, 0)
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
                ok=streamed.handed_over,
                error=None if streamed.handed_over else str(streamed.error),
                # The first object is what the player waits on, so it keeps the
                # name the dev view and the logs already use.
                scene_seconds=(
                    streamed.object_seconds[0] if streamed.object_seconds else None
                ),
                ttft_seconds=streamed.ttft,
                object_seconds=streamed.object_seconds or None,
                object_tokens=streamed.object_tokens or None,
            )
        )

    def stream_text(
        self, role: str, system: str, user: str, on_chunk: Callable[[str], None]
    ) -> str:
        """A prose call delivered as it is written. Returns the whole of it.

        The narrator used to block until its last token, which put its entire
        call between the player and the first word. `on_chunk` may raise
        `StopNarration` to abandon the rest.
        """
        model = self.config.model_for(role)
        for transport_try in (1, 2):
            started = time.monotonic()
            text = ""
            ttft = None
            self._stream_usage = (0, 0)
            try:
                for chunk in self._raw_stream(role, model, system, user):
                    if ttft is None:
                        ttft = time.monotonic() - started
                    text += chunk
                    on_chunk(chunk)
            except StopNarration:
                pass  # the caller has what it wants; the rest is not needed
            except Exception as exc:
                self._transport_failed(
                    exc, role, model, started, 1, transport_try
                )
                continue
            # A gated narration that stopped early never reaches the provider's
            # usage chunk, so the call would log zero tokens and vanish from the
            # session spend (rule 9). The text is in hand either way, so count
            # it the way the rest of the engine counts: four characters a token.
            usage = self._stream_usage
            if usage == (0, 0):
                usage = (len(system + user) // 4, len(text) // 4)
            price = self.config.price_for(model)
            self.call_log.write(
                CallRecord(
                    role=role,
                    model=model,
                    input_tokens=usage[0],
                    output_tokens=usage[1],
                    cost_cents=price.cents(*usage) if price else 0.0,
                    seconds=time.monotonic() - started,
                    attempt=1,
                    ok=True,
                    ttft_seconds=ttft,
                )
            )
            return text
        raise ModelCallFailed(f"{role} call failed")

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
            retry_user = user + "\n\n" + RETRY_PREAMBLE.format(
                error=str(first)[:MAX_ERROR_CHARS]
            )
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
