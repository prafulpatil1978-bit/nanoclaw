"""Unified LLM client — works with Anthropic API, OpenRouter, or Ollama (local).

Priority: OPENROUTER_API_KEY → ANTHROPIC_API_KEY → Ollama (local)

Automatic fallback: if the primary cloud provider returns a rate-limit or
network error, the client transparently retries via Ollama (if it is running).

Complexity-based routing: pass complexity="low"|"medium"|"high" (or "analysis")
to resolve_model() and it will choose the cheapest model that can handle the
task. "auto" as a model name triggers this routing automatically.

The client is a drop-in for the Anthropic SDK in our agents:
  client = build_client()
  response = client.messages.create(model=..., max_tokens=..., system=...,
                                    messages=..., tools=...)
  # response.content  → list of TextBlock / ToolUseBlock (same shape as Anthropic SDK)
  # response.stop_reason → "end_turn" | "tool_use"
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

_log = logging.getLogger(__name__)


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
        # openrouter and ollama both use the OpenAI-compatible path
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

    def __init__(self, provider: str, raw_client, fallback_messages=None) -> None:
        primary = _Messages(provider, raw_client)
        if fallback_messages is not None:
            self.messages = _SmartMessages(primary, fallback_messages)
        else:
            self.messages = primary


def _is_provider_error(exc: Exception) -> bool:
    """True for retriable errors (rate limit, network) — not for auth failures."""
    msg = str(exc).lower()
    # Auth failures should surface to the user; Ollama can't fix them
    if any(w in msg for w in ("401", "403", "unauthorized", "forbidden", "user not found")):
        return False
    return any(w in msg for w in (
        "429", "500", "502", "503", "504",
        "rate limit", "timeout", "connection", "server error", "overloaded",
    ))


class _SmartMessages:
    """Primary provider with transparent Ollama fallback on retriable errors."""

    def __init__(self, primary: _Messages, fallback: _Messages) -> None:
        self._primary = primary
        self._fallback = fallback
        # Expose _provider so resolve_model() continues to work correctly
        self._provider = primary._provider

    def create(
        self,
        model: str,
        max_tokens: int,
        system=None,
        messages: list | None = None,
        tools: list | None = None,
    ) -> _Response:
        try:
            return self._primary.create(model, max_tokens, system, messages, tools)
        except Exception as exc:
            if _is_provider_error(exc):
                ollama_model = os.environ.get("OLLAMA_MODEL", _DEFAULT_OLLAMA_MODEL)
                _log.warning(
                    "Primary provider error (%s) — falling back to Ollama (%s)",
                    exc, ollama_model,
                )
                return self._fallback.create(ollama_model, max_tokens, system, messages, tools)
            raise


# ── Factory ────────────────────────────────────────────────────────────────

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_OLLAMA_BASE = "http://localhost:11434/v1"
_DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4-5"  # cost-effective default
_DEFAULT_OLLAMA_MODEL = "llama3.2:3b"

# Complexity → cheapest model that can reliably handle the task
# "analysis" = lightweight JSON extraction → always Haiku
# "low"      = simple geometry (≤2 parts) → Haiku
# "medium"   = moderate (3–5 parts) → Sonnet
# "high"     = complex assemblies (6+ parts) → Sonnet (Opus not auto-selected; costs 5×)
_COMPLEXITY_MODEL: dict[str, dict[str, str]] = {
    "openrouter": {
        "analysis": "anthropic/claude-3-5-haiku",
        "low":      "anthropic/claude-3-5-haiku",
        "medium":   "anthropic/claude-sonnet-4-5",
        "high":     "anthropic/claude-sonnet-4-5",
    },
    "anthropic": {
        "analysis": "claude-haiku-4-5-20251001",
        "low":      "claude-haiku-4-5-20251001",
        "medium":   "claude-sonnet-4-6",
        "high":     "claude-sonnet-4-6",
    },
}

_SLUG_MAP = {
    "claude-opus-4-7":   "anthropic/claude-opus-4-5",
    "claude-opus-4-5":   "anthropic/claude-opus-4-5",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4-5",
    "claude-sonnet-4-5": "anthropic/claude-sonnet-4-5",
    "claude-haiku-4-5":  "anthropic/claude-3-5-haiku",
}


def _ollama_running() -> bool:
    try:
        import httpx
        r = httpx.get(f"{_OLLAMA_BASE}/models", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _build_ollama_messages() -> _Messages | None:
    """Return an Ollama _Messages if the daemon is reachable, else None."""
    if _ollama_running():
        from openai import OpenAI
        return _Messages("ollama", OpenAI(base_url=_OLLAMA_BASE, api_key="ollama"))
    return None


def build_client() -> UnifiedClient:
    """Return a UnifiedClient using whichever key/service is available.

    Priority: OPENROUTER_API_KEY → ANTHROPIC_API_KEY → Ollama (local)

    When a cloud provider is primary, Ollama is wired as a silent fallback:
    rate-limit / network errors automatically retry via Ollama.
    """
    or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    ant_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    use_ollama = os.environ.get("USE_OLLAMA", "").lower() in ("1", "true", "yes")

    if or_key and not use_ollama:
        from openai import OpenAI
        raw = OpenAI(base_url=_OPENROUTER_BASE, api_key=or_key)
        fallback = _build_ollama_messages()
        return UnifiedClient("openrouter", raw, fallback_messages=fallback)

    if ant_key and not use_ollama:
        import anthropic
        raw = anthropic.Anthropic(api_key=ant_key)
        fallback = _build_ollama_messages()
        return UnifiedClient("anthropic", raw, fallback_messages=fallback)

    # Ollama — free local (explicit USE_OLLAMA=1 or auto-detected)
    if use_ollama or _ollama_running():
        from openai import OpenAI
        raw = OpenAI(base_url=_OLLAMA_BASE, api_key="ollama")
        return UnifiedClient("ollama", raw)

    raise EnvironmentError(
        "No LLM available. Set OPENROUTER_API_KEY, ANTHROPIC_API_KEY, "
        "or start Ollama (ollama serve) and set USE_OLLAMA=1 in .env"
    )


def score_complexity(obj_desc) -> str:
    """Estimate design complexity from an ObjectDescription.

    Returns "low", "medium", or "high". Used to select the cheapest model
    that can reliably handle the design task.
    """
    num_parts  = len(getattr(obj_desc, "suggested_parts", []))
    desc_words = len((getattr(obj_desc, "description", "") or "").split())
    features   = len(getattr(obj_desc, "features", []))
    score = num_parts * 3 + (desc_words // 20) + (features // 3)
    if score >= 10:
        return "high"
    if score >= 5:
        return "medium"
    return "low"


def resolve_model(requested: str | None, provider: str, complexity: str = "medium") -> str:
    """Map a model name/complexity hint to the correct string for the active provider.

    Special values for ``requested``:
      - None / "" / "auto"  → complexity-based routing (cheapest capable model)
      - any explicit model  → mapped to provider format and used as-is
    """
    # Ollama always uses whatever model is configured locally
    if provider == "ollama":
        return os.environ.get("OLLAMA_MODEL", _DEFAULT_OLLAMA_MODEL)

    # Auto or unset → pick by complexity
    is_auto = not requested or requested == "auto"

    if provider == "anthropic":
        if is_auto:
            return _COMPLEXITY_MODEL["anthropic"].get(complexity, "claude-sonnet-4-6")
        return requested  # pass explicit model through unchanged

    # OpenRouter
    if os.environ.get("OPENROUTER_MODEL") and is_auto:
        # Env-level override takes priority over auto-routing
        return os.environ["OPENROUTER_MODEL"]

    if is_auto:
        return _COMPLEXITY_MODEL["openrouter"].get(complexity, _DEFAULT_OPENROUTER_MODEL)

    # Explicit model name — map Anthropic slugs to OpenRouter format
    return _SLUG_MAP.get(requested, requested)  # pass unknown names through unchanged
