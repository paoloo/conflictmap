"""Thin Ollama client used by the local situational-awareness harness.

Talks to the local Ollama daemon over HTTP. No external dependencies beyond
`requests`. Supports JSON-schema-constrained completions and OpenAI-style
tool calling, which is all the ranking and agent layers need.

Default model: the one Akari (~/Workspace/SRC/tmp/b) uses for chat, Umi and Fuu,
a Qwen3-8B hybrid. qwen3:4b, the former default, is no longer installed. Reasoning
follows Akari's "off" policy: /no_think is appended to the system prompt and any
leading <think> block is stripped from the reply.
"""

from __future__ import annotations

import json
from typing import Any

import requests

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "huggingface.co/TeskesLab/Liberation-Qwen3-8B:Q4_K_M"
DEFAULT_TIMEOUT = 600


class LLMError(RuntimeError):
    """Raised when the local model cannot produce a usable response."""


class LLM:
    """Small wrapper around the Ollama REST API."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        timeout: int = DEFAULT_TIMEOUT,
        keep_alive: str = "30m",
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.keep_alive = keep_alive

    # -- low level ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        num_ctx: int = 16384,
        think: bool = False,
    ) -> dict[str, Any]:
        """Send a chat request; returns the full response object."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx,
            },
        }
        if schema is not None:
            payload["format"] = schema
        if tools:
            payload["tools"] = tools
        if think is not None and _model_supports_think(self.model):
            payload["think"] = think
        if not think:
            payload["messages"] = no_think(messages)
        response = requests.post(
            f"{self.host}/api/chat", json=payload, timeout=self.timeout
        )
        if response.status_code != 200:
            raise LLMError(
                f"ollama chat failed: HTTP {response.status_code}: {response.text[:400]}"
            )
        return response.json()

    def complete_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        *,
        temperature: float = 0.2,
        num_ctx: int = 16384,
    ) -> dict[str, Any]:
        """Constrained JSON completion validated against `schema`."""
        message = self.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            schema=schema,
            temperature=temperature,
            num_ctx=num_ctx,
        )
        content = strip_reasoning((message.get("message") or {}).get("content") or "")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned invalid JSON: {content[:400]}") from exc
        _validate(schema, parsed)
        return parsed

    # -- tool calling ------------------------------------------------------

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0.3,
        num_ctx: int = 16384,
    ) -> dict[str, Any]:
        """One model turn with tools available. Returns the raw message dict."""
        response = self.chat(
            messages,
            tools=tools,
            temperature=temperature,
            num_ctx=num_ctx,
        )
        return response.get("message") or {}


# -- schema validation (minimal, draft-style subset) -------------------------


def _validate(schema: dict[str, Any], value: Any, path: str = "$") -> None:
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    checks: dict[str, tuple[type, ...]] = {
        "object": (dict,),
        "array": (list,),
        "string": (str,),
        "number": (int, float),
        "integer": (int,),
        "boolean": (bool,),
    }
    if expected in checks and not isinstance(value, checks[expected]):
        # bool is a subclass of int; reject bools where ints are expected
        if expected == "integer" and isinstance(value, bool):
            raise LLMError(f"{path}: expected integer, got boolean")
        raise LLMError(f"{path}: expected {expected}, got {type(value).__name__}")
    if expected == "object" and isinstance(value, dict):
        for key in schema.get("required", []) or []:
            if key not in value:
                raise LLMError(f"{path}: missing required key '{key}'")
        props = schema.get("properties", {}) or {}
        for key, sub in props.items():
            if key in value:
                _validate(sub, value[key], f"{path}.{key}")
    if expected == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate(item_schema, item, f"{path}[{index}]")
    if "enum" in schema and value not in schema["enum"]:
        raise LLMError(f"{path}: value {value!r} not in enum {schema['enum']}")
    if "minimum" in schema and isinstance(value, (int, float)):
        if value < schema["minimum"]:
            raise LLMError(f"{path}: value {value} below minimum {schema['minimum']}")
    if "maximum" in schema and isinstance(value, (int, float)):
        if value > schema["maximum"]:
            raise LLMError(f"{path}: value {value} above maximum {schema['maximum']}")


_THINK_MODELS = ("qwen3", "deepseek-r1", "qwq", "gpt-oss")


def _model_supports_think(model: str) -> bool:
    return any(tag in model.lower() for tag in _THINK_MODELS)


def no_think(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ask Qwen3-style hybrids not to reason (Akari's model_reasoning = "off")."""
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") == "system":
            if "/no_think" not in str(m.get("content", "")):
                m["content"] = f"{m.get('content', '')}\n/no_think"
            return out
    return [{"role": "system", "content": "/no_think"}, *out]


def strip_reasoning(text: str) -> str:
    """Drop a leading reasoning block (`<think>...</think>` or a bare `...</think>`)."""
    head, marker, tail = text.partition("</think>")
    if marker and (not head.strip() or head.lstrip().startswith("<think>") or len(head) < 4000):
        return tail.lstrip()
    return text
