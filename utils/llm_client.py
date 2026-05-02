"""Unified LLM client — works with Anthropic API or OpenRouter.

Priority: OPENROUTER_API_KEY → ANTHROPIC_API_KEY

For OpenRouter, all Claude models are accessed as  "anthropic/<model-slug>".
Default OpenRouter model: anthropic/claude-3.5-sonnet (override via OPENROUTER_MODEL).

The client is a drop-in for the Anthropic SDK in our agents:
  client = build_client()
  response = client.messages.create(model=..., max_tokens=..., system=...,
                                    messages=..., tools=...)
  # response.content  → list of TextBlock / ToolUseBlock (same shape as Anthropic SDK)
  # response.stop_reason → "end_turn" | "tool_use"
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


# ── Fake Anthropic-shaped response objects ─────────────────────────────────

class _TextBlock:
    type = "text"
    def __init__(self, text: str) -> None:
        self.text = text

class _ToolUseBlock:
    type = "tool_use"
    def __init__(self, id: str, name: str, input: dict) -> None:
        self.id = id
        self.name = name
        self.input = input

class _Response:
    def __init__(self, content: list, stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


# ── Conversion helpers ─────────────────────────────────────────────────────

def _system_to_str(system) -> str:
    """Extract text from Anthropic system (str or list of blocks)."""
    if isinstance(system, str):
        return system
    parts = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block["text"])
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _anthropic_tools_to_openai(tools: list) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        }
        for t in (tools or [])
    ]


def _anthropic_messages_to_openai(messages: list) -> list:
    """Convert an Anthropic-format message history to OpenAI format."""
    out: list[dict] = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if role == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
            elif isinstance(content, list):
                # Could be tool results or regular content blocks
                if content and isinstance(content[0], dict) and content[0].get("type") == "tool_result":
                    for block in content:
                        result_text = block.get("content", "")
                        if isinstance(result_text, list):
                            result_text = " ".join(
                                b.get("text", "") for b in result_text if isinstance(b, dict)
                            )
                        out.append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": str(result_text),
                        })
                else:
                    out.append({"role": "user", "content": _user_content_to_openai(content)})

        elif role == "assistant":
            if isinstance(content, str):
                out.append({"role": "assistant", "content": content})
            elif isinstance(content, list):
                text_parts: list[str] = []
                tool_calls: list[dict] = []
                for block in content:
                    btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
                    if btype == "text":
                        text_parts.append(getattr(block, "text", None) or block.get("text", ""))
                    elif btype == "tool_use":
                        bid = getattr(block, "id", None) or block.get("id", "")
                        bname = getattr(block, "name", None) or block.get("name", "")
                        binput = getattr(block, "input", None) or block.get("input", {})
                        tool_calls.append({
                            "id": bid,
                            "type": "function",
                            "function": {"name": bname, "arguments": json.dumps(binput)},
                        })
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": " ".join(text_parts) if text_parts else None,
                }
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                out.append(entry)
    return out


def _user_content_to_openai(blocks: list) -> list:
    out = []
    for block in blocks:
        if isinstance(block, dict):
            t = block.get("type")
            if t == "text":
                out.append({"type": "text", "text": block["text"]})
            elif t == "image":
                src = block["source"]
                url = f"data:{src['media_type']};base64,{src['data']}"
                out.append({"type": "image_url", "image_url": {"url": url}})
        elif isinstance(block, str):
            out.append({"type": "text", "text": block})
    return out


def _openai_response_to_anthropic(response) -> _Response:
    choice = response.choices[0]
    msg = choice.message
    finish = choice.finish_reason

    content: list = []
    if msg.content:
        content.append(_TextBlock(msg.content))

    tool_calls = getattr(msg, "tool_calls", None) or []
    for tc in tool_calls:
        try:
            inp = json.loads(tc.function.arguments)
        except Exception:
            inp = {}
        content.append(_ToolUseBlock(id=tc.id, name=tc.function.name, input=inp))

    stop_reason = "tool_use" if finish == "tool_calls" else "end_turn"
    return _Response(content=content, stop_reason=stop_reason)


# ── Thin wrapper that mimics anthropic.Anthropic().messages ───────────────

class _Messages:
    def __init__(self, provider: str, client) -> None:
        self._provider = provider
        self._client = client

    def create(
        self,
        model: str,
        max_tokens: int,
        system=None,
        messages: list | None = None,
        tools: list | None = None,
    ) -> _Response:
        if self._provider == "anthropic":
            return self._anthropic_create(model, max_tokens, system, messages, tools)
        return self._openrouter_create(model, max_tokens, system, messages, tools)

    def _anthropic_create(self, model, max_tokens, system, messages, tools):
        # Use real Anthropic SDK — pass through as-is
        kwargs: dict[str, Any] = dict(model=model, max_tokens=max_tokens)
        if system is not None:
            kwargs["system"] = system
        if messages:
            kwargs["messages"] = messages
        if tools:
            kwargs["tools"] = tools
        return self._client.messages.create(**kwargs)

    def _openrouter_create(self, model, max_tokens, system, messages, tools):
        oai_messages: list[dict] = []
        if system:
            oai_messages.append({"role": "system", "content": _system_to_str(system)})
        oai_messages.extend(_anthropic_messages_to_openai(messages or []))

        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            messages=oai_messages,
        )
        if tools:
            kwargs["tools"] = _anthropic_tools_to_openai(tools)
            kwargs["tool_choice"] = "auto"

        raw = self._client.chat.completions.create(**kwargs)
        return _openai_response_to_anthropic(raw)


class UnifiedClient:
    """Drop-in replacement for anthropic.Anthropic() in our agents."""

    def __init__(self, provider: str, raw_client) -> None:
        self.messages = _Messages(provider, raw_client)


# ── Factory ────────────────────────────────────────────────────────────────

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_OLLAMA_BASE = "http://localhost:11434/v1"
_DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4-5"  # cost-effective default
_DEFAULT_OLLAMA_MODEL = "llama3.2:3b"

# Per-role model defaults (override via env vars)
# Analysis needs vision capability → sonnet
# Design is agentic code-writing → sonnet is sufficient, opus overkill
ROLE_DEFAULTS = {
    "analysis": "claude-sonnet-4-6",
    "design":   "claude-sonnet-4-6",
}


def _ollama_running() -> bool:
    try:
        import httpx
        r = httpx.get(f"{_OLLAMA_BASE}/models", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def build_client() -> UnifiedClient:
    """Return a UnifiedClient using whichever key/service is available.

    Priority: OPENROUTER_API_KEY → ANTHROPIC_API_KEY → Ollama (local)
    """
    or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    ant_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    use_ollama = os.environ.get("USE_OLLAMA", "").lower() in ("1", "true", "yes")

    if or_key and not use_ollama:
        from openai import OpenAI
        raw = OpenAI(base_url=_OPENROUTER_BASE, api_key=or_key)
        return UnifiedClient("openrouter", raw)

    if ant_key and not use_ollama:
        import anthropic
        raw = anthropic.Anthropic(api_key=ant_key)
        return UnifiedClient("anthropic", raw)

    # Ollama — free local fallback (or explicit USE_OLLAMA=1)
    if use_ollama or _ollama_running():
        from openai import OpenAI
        raw = OpenAI(base_url=_OLLAMA_BASE, api_key="ollama")
        return UnifiedClient("ollama", raw)

    raise EnvironmentError(
        "No LLM available. Set OPENROUTER_API_KEY, ANTHROPIC_API_KEY, "
        "or start Ollama (ollama serve) and set USE_OLLAMA=1 in .env"
    )


def resolve_model(requested: str | None, provider: str) -> str:
    """Map a model name to the correct format for the active provider."""
    if provider == "anthropic":
        return requested or "claude-opus-4-7"

    if provider == "ollama":
        return os.environ.get("OLLAMA_MODEL", _DEFAULT_OLLAMA_MODEL)

    # OpenRouter — use env override or map Anthropic names to OpenRouter slugs
    if os.environ.get("OPENROUTER_MODEL"):
        return os.environ["OPENROUTER_MODEL"]

    slug_map = {
        "claude-opus-4-7":   "anthropic/claude-opus-4-5",
        "claude-opus-4-5":   "anthropic/claude-opus-4-5",
        "claude-sonnet-4-6": "anthropic/claude-sonnet-4-5",
        "claude-sonnet-4-5": "anthropic/claude-sonnet-4-5",
        "claude-haiku-4-5":  "anthropic/claude-haiku-3-5",
    }
    return slug_map.get(requested or "", _DEFAULT_OPENROUTER_MODEL)
