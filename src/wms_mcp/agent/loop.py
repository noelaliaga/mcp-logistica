"""The agent loop: model -> tool calls over MCP -> results -> model, until it answers."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.types import CallToolResult, TextContent, Tool

from wms_mcp.agent.models import ChatModel, Message, ToolSpec


@dataclass
class ToolStep:
    tool: str
    arguments: dict[str, Any]
    is_error: bool
    outcome: str
    text: str
    latency_ms: float


@dataclass
class Trace:
    model: str
    prompt_version: str
    user: str
    steps: list[ToolStep] = field(default_factory=list)
    final_text: str = ""
    stopped: str = "answered"  # answered | max_steps

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def to_openai_tool(tool: Tool) -> ToolSpec:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }


def _result_text(result: CallToolResult) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


def _outcome(result: CallToolResult, text: str) -> str:
    if result.isError:
        return "error"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "ok"
    value = payload.get("outcome") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else "ok"


async def run_agent(
    model: ChatModel,
    session: ClientSession,
    user_message: str,
    *,
    system_prompt: str,
    prompt_version: str,
    max_steps: int = 6,
) -> Trace:
    tools = [to_openai_tool(t) for t in (await session.list_tools()).tools]
    messages: list[Message] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    trace = Trace(model=model.name, prompt_version=prompt_version, user=user_message)
    for _ in range(max_steps):
        turn = model.complete(messages, tools)
        messages.append(turn.as_message())
        if not turn.tool_calls:
            trace.final_text = turn.text or ""
            return trace
        for call in turn.tool_calls:
            started = time.perf_counter()
            result = await session.call_tool(call.name, call.arguments)
            text = _result_text(result)
            trace.steps.append(
                ToolStep(
                    tool=call.name,
                    arguments=call.arguments,
                    is_error=bool(result.isError),
                    outcome=_outcome(result, text),
                    text=text,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            )
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
    trace.stopped = "max_steps"
    return trace
