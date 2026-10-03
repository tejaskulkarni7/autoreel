"""LLM providers used for highlight detection.

* ``anthropic`` - Claude via the official Anthropic SDK, with JSON-schema structured output.
* ``openai_compatible`` - any OpenAI-compatible chat endpoint: Ollama / LM Studio / vLLM /
  llama.cpp for fully local runs, or hosted ones (OpenAI, OpenRouter, Groq...).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Protocol

from ..config import LLMConfig

log = logging.getLogger(__name__)

# Models that accept the server-side refusal fallback ("fallbacks": "default").
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def complete_json(self, system: str, user: str, schema: dict) -> dict: ...


def extract_json(text: str) -> dict:
    """Parse a JSON object out of a model reply (tolerates code fences / chatter)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"Model did not return JSON: {text[:300]!r}")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise LLMError(f"Model returned invalid JSON: {e}") from e
    if isinstance(data, list):
        data = {"clips": data}
    if not isinstance(data, dict):
        raise LLMError("Model JSON was not an object")
    return data


def _supports_effort(model: str) -> bool:
    return not re.search(r"haiku|claude-3|sonnet-4-5|sonnet-4-0|sonnet-4-2|opus-4-0|opus-4-1|sonnet-4$|opus-4$", model)


class AnthropicLLM:
    def __init__(self, cfg: LLMConfig):
        import anthropic

        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key or None, max_retries=3)

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        import anthropic

        model = self.cfg.anthropic_model
        output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
        if _supports_effort(model):
            output_config["effort"] = self.cfg.anthropic_effort
        params = dict(
            model=model,
            max_tokens=32000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
        try:
            if model in _FALLBACK_MODELS:
                stream_ctx = self.client.beta.messages.stream(betas=[_FALLBACK_BETA], fallbacks="default", **params)
            else:
                stream_ctx = self.client.messages.stream(**params)
            with stream_ctx as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic authentication failed - set an API key in Settings or ANTHROPIC_API_KEY") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Anthropic model not found: {model}") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Anthropic rate limit hit - try again shortly") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Could not reach the Anthropic API: {e}") from e

        if message.stop_reason == "refusal":
            raise LLMError("Claude declined to analyze this transcript")
        if message.stop_reason == "max_tokens":
            raise LLMError("Claude's answer was cut off (max_tokens); lower max clips or enable chunking")
        text = next((b.text for b in message.content if b.type == "text"), "")
        return extract_json(text)


class OpenAICompatLLM:
    def __init__(self, cfg: LLMConfig):
        from openai import OpenAI

        self.cfg = cfg
        self.client = OpenAI(
            base_url=cfg.openai_base_url or None,
            api_key=cfg.openai_api_key or os.environ.get("OPENAI_API_KEY") or "not-needed",
            timeout=1800,
            max_retries=2,
        )

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        import openai

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user + "\n\nReply with a single JSON object matching this JSON schema:\n"
             + json.dumps(schema)},
        ]
        formats = [
            {"type": "json_schema", "json_schema": {"name": "clips", "schema": schema}},
            {"type": "json_object"},
            None,
        ]
        last_err: Exception | None = None
        for fmt in formats:
            kwargs = dict(model=self.cfg.openai_model, messages=messages, temperature=self.cfg.temperature)
            if fmt is not None:
                kwargs["response_format"] = fmt
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except openai.BadRequestError as e:  # server doesn't support this response_format
                last_err = e
                log.info("response_format %s rejected, falling back: %s", fmt and fmt["type"], e)
                continue
            except openai.APIConnectionError as e:
                raise LLMError(f"Could not reach LLM server at {self.cfg.openai_base_url}: {e}") from e
            except openai.APIStatusError as e:
                raise LLMError(f"LLM server error {e.status_code}: {e.message}") from e
            content = resp.choices[0].message.content or ""
            return extract_json(content)
        raise LLMError(f"LLM request failed: {last_err}")


def get_llm(cfg: LLMConfig) -> LLMClient:
    if cfg.provider == "anthropic":
        return AnthropicLLM(cfg)
    if cfg.provider == "openai_compatible":
        return OpenAICompatLLM(cfg)
    raise LLMError(f"Unknown LLM provider: {cfg.provider}")
