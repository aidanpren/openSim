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
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .config import OPENROUTER_BASE_URL, Config

T = TypeVar("T", bound=BaseModel)
S = TypeVar("S", bound=BaseModel)


class ModelCallFailed(Exception):
    """The model never returned usable output. The turn must not commit."""


class OutOfCredit(ModelCallFailed):
    """The account has no credit left. Retrying will not help."""


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

    def _call(self, role: str, system: str, user: str, attempt: int) -> str:
        model = self.config.model_for(role)
        for transport_try in (1, 2):
            started = time.monotonic()
            try:
                text, input_tokens, output_tokens = self._raw(role, model, system, user)
                break
            except Exception as exc:
                self.call_log.write(
                    CallRecord(role, model, 0, 0, 0.0, time.monotonic() - started,
                               attempt, ok=False, error=str(exc))
                )
                status = getattr(exc, "status_code", None)
                if status == 402:
                    raise OutOfCredit(
                        "The account is out of credit. Add some at "
                        "https://openrouter.ai/settings/credits"
                    ) from exc
                if status in TRANSIENT_STATUSES and transport_try == 1:
                    time.sleep(TRANSIENT_BACKOFF_SECONDS)
                    continue
                raise ModelCallFailed(f"{role} call failed: {exc}") from exc

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
            started = time.monotonic()
            buffer = ""
            first = None
            split_at = None

            for chunk in self._raw_stream(role, model, system, user):
                buffer += chunk
                if first is not None:
                    continue
                end = first_object_end(_strip_fences(buffer))
                if end is None:
                    continue
                cleaned = _strip_fences(buffer)
                try:
                    candidate = first_shape.model_validate_json(cleaned[:end])
                    if check is not None:
                        check(candidate)
                except (ValidationError, ValueError) as exc:
                    error = exc
                    first = None
                    split_at = None
                    break
                first, split_at = candidate, end
                scene_seconds = time.monotonic() - started
                on_first(first)

            if first is not None:
                usage = self._stream_usage
                self.call_log.write(
                    CallRecord(
                        role=role,
                        model=model,
                        input_tokens=usage[0],
                        output_tokens=usage[1],
                        cost_cents=(
                            price.cents(*usage)
                            if (price := self.config.price_for(model))
                            else 0.0
                        ),
                        seconds=time.monotonic() - started,
                        attempt=attempt,
                        ok=True,
                        scene_seconds=scene_seconds,
                    )
                )

            if first is None:
                if attempt == 2:
                    raise ModelCallFailed(f"{role} scene was unusable twice: {error}")
                continue

            rest = _strip_fences(buffer)[split_at:].strip()
            try:
                return first, second_shape.model_validate_json(rest or "{}")
            except (ValidationError, ValueError) as exc:
                raise LedgerFailed(
                    f"{role} scene committed but its ledger was unusable: {exc}"
                ) from exc

        raise ModelCallFailed(f"{role} produced no scene")

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


def first_object_end(text: str) -> int | None:
    """Index just past the first complete top-level JSON object, or None.

    This is what lets narration start before the resolver has finished writing:
    the scene object closes long before the ledger that follows it.
    """
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


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
