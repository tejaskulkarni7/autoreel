"""LLM providers used for highlight detection.

* ``anthropic`` - Claude via the official Anthropic SDK, with JSON-schema structured output.
* ``ollama`` - local models through Ollama's native API (lets us set ``num_ctx``; Ollama's
  default context window is small and silently truncates long prompts).
* ``openai_compatible`` - any OpenAI-compatible chat endpoint: LM Studio, vLLM, llama.cpp,
  or hosted ones (OpenAI, OpenRouter, Groq...).

Every client exposes ``context_tokens`` - how much prompt it can take - so the highlight
detector can split long transcripts into windows that actually fit.
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
    context_tokens: int
    output_reserve: int

    def complete_json(self, system: str, user: str, schema: dict) -> dict: ...


def estimate_tokens(text: str) -> int:
    """Rough token count (~3.5 characters per token for English prose)."""
    return int(len(text) / 3.5) + 1


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
    # Claude models have 200K-1M token windows; a 1-hour transcript is ~15K tokens.
    context_tokens = 150_000
    output_reserve = 0  # output has its own max_tokens budget

    def __init__(self, cfg: LLMConfig):
        import anthropic

        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key or None, max_retries=3)

    def _call(self, params: dict, use_fallbacks: bool):
        if use_fallbacks:
            ctx = self.client.beta.messages.stream(betas=[_FALLBACK_BETA], fallbacks="default", **params)
        else:
            ctx = self.client.messages.stream(**params)
        with ctx as stream:
            return stream.get_final_message()

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
        use_fallbacks = model in _FALLBACK_MODELS
        message = None
        # Up to 3 attempts, dropping optional features the account/model rejects.
        for _ in range(3):
            try:
                message = self._call(params, use_fallbacks)
                break
            except anthropic.BadRequestError as e:
                msg = str(e.message).lower()
                if use_fallbacks and "fallback" in msg:
                    log.warning("Refusal fallbacks rejected, retrying without them: %s", e.message)
                    use_fallbacks = False
                    continue
                if "effort" in msg and "effort" in params["output_config"]:
                    log.warning("Effort rejected, retrying without it: %s", e.message)
                    params["output_config"] = {k: v for k, v in params["output_config"].items() if k != "effort"}
                    continue
                if ("format" in msg or "schema" in msg) and "format" in params["output_config"]:
                    log.warning("Structured output rejected, retrying with plain JSON prompt: %s", e.message)
                    params["output_config"] = {k: v for k, v in params["output_config"].items() if k != "format"}
                    params["messages"] = [{"role": "user", "content": user + "\n\nReply with only a JSON object "
                                           "matching this JSON schema:\n" + json.dumps(schema)}]
                    continue
                raise LLMError(f"Anthropic rejected the request: {e.message}") from e
            except anthropic.AuthenticationError as e:
                raise LLMError("Anthropic authentication failed - set an API key in Settings or ANTHROPIC_API_KEY") from e
            except anthropic.PermissionDeniedError as e:
                raise LLMError(f"Anthropic API key lacks access: {e.message}") from e
            except anthropic.NotFoundError as e:
                raise LLMError(f"Anthropic model not found: {model}") from e
            except anthropic.RateLimitError as e:
                raise LLMError("Anthropic rate limit hit - try again shortly") from e
            except anthropic.APIStatusError as e:
                raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise LLMError(f"Could not reach the Anthropic API: {e}") from e
        if message is None:
            raise LLMError("Anthropic request failed after retries")
        if message.stop_reason == "refusal":
            raise LLMError("Claude declined to analyze this transcript")
        if message.stop_reason == "max_tokens":
            raise LLMError("Claude's answer was cut off (max_tokens); lower max clips")
        text = next((b.text for b in message.content if b.type == "text"), "")
        return extract_json(text)


class OllamaLLM:
    output_reserve = 3000

    def __init__(self, cfg: LLMConfig):
        import httpx

        self.cfg = cfg
        self.context_tokens = max(2048, cfg.ollama_num_ctx)
        self.base = cfg.ollama_base_url.rstrip("/").removesuffix("/v1")
        self.http = httpx.Client(timeout=httpx.Timeout(3600, connect=10))

    def list_models(self) -> list[str]:
        import httpx

        try:
            resp = self.http.get(f"{self.base}/api/tags")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"Could not reach Ollama at {self.base} - is it running? ({e})") from e
        return [m.get("name", "") for m in resp.json().get("models", [])]

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        import httpx

        body = {
            "model": self.cfg.ollama_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": schema,
            "options": {"num_ctx": self.context_tokens, "temperature": self.cfg.temperature},
        }
        try:
            resp = self.http.post(f"{self.base}/api/chat", json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"Could not reach Ollama at {self.base} - is it running? ({e})") from e
        if resp.status_code == 404:
            raise LLMError(f"Ollama model '{self.cfg.ollama_model}' not found - run: ollama pull {self.cfg.ollama_model}")
        if resp.status_code >= 400:
            raise LLMError(f"Ollama error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        used = data.get("prompt_eval_count") or 0
        if used and used >= self.context_tokens - 64:
            log.warning("Ollama prompt (%s tokens) filled the %s-token context; input may be truncated",
                        used, self.context_tokens)
        return extract_json((data.get("message") or {}).get("content", ""))


class OpenAICompatLLM:
    output_reserve = 3000

    def __init__(self, cfg: LLMConfig):
        from openai import OpenAI

        self.cfg = cfg
        self.context_tokens = max(2048, cfg.openai_context_tokens)
        self.client = OpenAI(
            base_url=cfg.openai_base_url or None,
            api_key=cfg.openai_api_key or os.environ.get("OPENAI_API_KEY") or "not-needed",
            timeout=3600,
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
            {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}},
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
    if cfg.provider == "ollama":
        return OllamaLLM(cfg)
    if cfg.provider == "openai_compatible":
        return OpenAICompatLLM(cfg)
    raise LLMError(f"Unknown LLM provider: {cfg.provider}")
