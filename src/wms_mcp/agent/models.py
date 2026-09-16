"""Chat-model adapters behind one small protocol.

* ScriptedModel replays hand-written turns. It ignores what the tools return,
  so a scripted scenario tests the server, the loop and the graders, not a model.
* LiteLLMModel sends the conversation to any provider LiteLLM supports
  (OpenAI, Anthropic, Gemini/Vertex...). It is only used by ``wms-eval --model``;
  LiteLLM is an optional dependency and is never imported by the test suite.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

Message = dict[str, Any]
ToolSpec = dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AssistantTurn:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)

    def as_message(self) -> Message:
        message: Message = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in self.tool_calls
            ]
        return message


class ChatModel(Protocol):
    name: str

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> AssistantTurn:
        """Return the next assistant turn: text, tool calls, or both."""
        ...


class ScriptExhaustedError(RuntimeError):
    pass


class ScriptedModel:
    """Replays a fixed list of turns, one per call."""

    name = "scripted"

    def __init__(self, turns: Sequence[AssistantTurn]) -> None:
        self._turns = list(turns)
        self._next = 0

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> AssistantTurn:
        if self._next >= len(self._turns):
            raise ScriptExhaustedError("the scripted model has no turns left")
        turn = self._turns[self._next]
        self._next += 1
        return turn


Completion = Callable[..., Any]


def _field(obj: Any, key: str) -> Any:
    """LiteLLM returns pydantic-like objects; tests pass dicts. Read either."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


class LiteLLMModel:
    """OpenAI-format tool calling through ``litellm.completion``.

    ``completion`` can be injected so the response parsing is testable without
    LiteLLM or network access.
    """

    def __init__(
        self, model: str, *, completion: Completion | None = None, temperature: float = 0.0
    ) -> None:
        self.name = model
        self.temperature = temperature
        if completion is None:
            litellm = importlib.import_module("litellm")  # optional dependency
            completion = litellm.completion
        self._completion = completion

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> AssistantTurn:
        response = self._completion(
            model=self.name,
            messages=list(messages),
            tools=list(tools),
            tool_choice="auto",
            temperature=self.temperature,
        )
        message = _field(_field(response, "choices")[0], "message")
        calls = []
        for raw in _field(message, "tool_calls") or []:
            function = _field(raw, "function")
            arguments = _field(function, "arguments") or "{}"
            parsed = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
            calls.append(ToolCall(str(_field(raw, "id")), str(_field(function, "name")), parsed))
        return AssistantTurn(text=_field(message, "content"), tool_calls=calls)
