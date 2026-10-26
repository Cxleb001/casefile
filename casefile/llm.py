"""One client for every model call, with the model chosen per step.

Both providers speak the OpenAI chat-completions API:

* ``nvidia``    -> https://integrate.api.nvidia.com/v1 with a key from build.nvidia.com
* ``meterless`` -> the Meterless Nightshift runtime (default http://127.0.0.1:8088/v1)

Steps: ``fast`` (extraction, summaries), ``plan`` (the investigation loop),
``verify`` (the verifier and the final report). Every call is timed and can be
logged, which is where the "which model ran which step" table comes from.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from openai import BadRequestError, OpenAI

from .config import RUNS_DIR, Settings, get_settings
from .replay import record_call

_THINK = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


@dataclass
class ChatResult:
    step: str
    provider: str
    model: str
    content: str
    reasoning: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    raw: Any = field(default=None, repr=False)


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(
        self,
        settings: Settings | None = None,
        on_call: Callable[[ChatResult, str | None], None] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.llm_api_key:
            variable = "NVIDIA_API_KEY" if self.settings.llm_provider == "nvidia" else "METERLESS_TOKEN"
            raise LLMError(f"Set {variable} in .env for CASEFILE_LLM_PROVIDER={self.settings.llm_provider}.")
        headers = {}
        if self.settings.llm_provider == "meterless":
            headers["meterless-app"] = "casefile"
        self.client = OpenAI(
            base_url=self.settings.llm_base_url,
            api_key=self.settings.llm_api_key,
            default_headers=headers or None,
            timeout=180,  # Meterless: a model load can take minutes
            max_retries=3,
        )
        self.on_call = on_call
        self._no_thinking_control: set[str] = set()  # models that rejected chat_template_kwargs
        # Every request and answer is kept in runs/llm/<case_id>.jsonl: the trace behind each decision,
        # and what sample mode replays without a key. CASEFILE_RECORD_LLM=false turns it off.
        recording = os.environ.get("CASEFILE_RECORD_LLM", "true").strip().lower() not in {"0", "false", "no", "off"}
        self.record_dir: Path | None = RUNS_DIR / "llm" if recording else None

    def model_for(self, step: str) -> str:
        try:
            return self.settings.models[step]
        except KeyError as error:
            raise LLMError(f"Unknown step {step!r}; use one of {sorted(self.settings.models)}") from error

    def _thinking_kwargs(self, step: str, model: str) -> dict[str, Any] | None:
        """Nemotron chat-template switches for this step's thinking level (see config.DEFAULT_THINKING)."""
        level = getattr(self.settings, "thinking", {}).get(step)
        # Applies to Nemotron models called directly on NVIDIA's API, or pinned by id through Nightshift
        # (e.g. nvidia/nemotron-3-ultra-550b-a55b); never to Nightshift routing modes (meterless/*).
        if (not level or self.settings.llm_provider not in ("nvidia", "meterless") or "nemotron" not in model.lower()
                or model.startswith("meterless/") or model in self._no_thinking_control):
            return None
        if level == "off":
            return {"enable_thinking": False}
        if level == "medium":
            return {"enable_thinking": True, "medium_effort": True}
        return {"enable_thinking": True}

    def list_models(self) -> list[str]:
        return sorted(m.id for m in self.client.models.list().data)

    def chat(
        self,
        step: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        extra_body: dict[str, Any] | None = None,
        case_id: str | None = None,
    ) -> ChatResult:
        model = self.model_for(step)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        body = dict(extra_body or {})
        template = self._thinking_kwargs(step, model)
        if template and "chat_template_kwargs" not in body:
            body["chat_template_kwargs"] = template
        if body:
            kwargs["extra_body"] = body
        if self.settings.llm_provider == "meterless":
            kwargs["extra_headers"] = {"meterless-scope": "user"}

        started = time.perf_counter()
        try:
            try:
                response = self.client.chat.completions.create(**kwargs)
            except BadRequestError:
                if not template:
                    raise
                # This model does not take thinking controls: remember, and ask again without them.
                self._no_thinking_control.add(model)
                body.pop("chat_template_kwargs", None)
                kwargs.pop("extra_body", None)
                if body:
                    kwargs["extra_body"] = body
                response = self.client.chat.completions.create(**kwargs)
        except Exception as error:  # surface the provider's own message
            message = f"{self.settings.llm_provider} call for step '{step}' ({model}) failed: {error}"
            if self.record_dir:
                record_call(self.record_dir, case_id, messages, None, {"temperature": temperature, "max_tokens": max_tokens},
                            step=step, provider=self.settings.llm_provider, model=model, error=message)
            raise LLMError(message) from error
        latency_ms = int((time.perf_counter() - started) * 1000)

        if not response.choices:
            raise LLMError(f"{model} returned no choices")
        message = response.choices[0].message
        content = message.content or ""
        if isinstance(content, list):  # some runtimes return content parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))

        reasoning = getattr(message, "reasoning_content", None)
        extra = getattr(message, "model_extra", None) or {}
        reasoning = reasoning or extra.get("reasoning_content") or extra.get("reasoning")
        thoughts = _THINK.findall(content)
        if thoughts:
            reasoning = (reasoning or "") + "\n".join(t.strip() for t in thoughts)
            content = _THINK.sub("", content)

        tool_calls = []
        for call in message.tool_calls or []:
            tool_calls.append(
                {
                    "id": call.id,
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                }
            )

        usage = response.usage
        result = ChatResult(
            step=step,
            provider=self.settings.llm_provider,
            model=getattr(response, "model", None) or model,
            content=content.strip(),
            reasoning=reasoning.strip() if isinstance(reasoning, str) and reasoning.strip() else None,
            tool_calls=tool_calls,
            latency_ms=latency_ms,
            prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            completion_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            raw=response,
        )
        if self.record_dir:
            record_call(self.record_dir, case_id, messages, result,
                        {"temperature": temperature, "max_tokens": max_tokens,
                         "chat_template_kwargs": body.get("chat_template_kwargs")})
        if self.on_call:
            self.on_call(result, case_id)
        return result

    def chat_json(self, step: str, messages: list[dict[str, Any]], **kwargs) -> tuple[Any, ChatResult]:
        """Ask for a JSON answer and parse it, tolerating code fences and stray prose."""
        result = self.chat(step, messages, **kwargs)
        return parse_json(result.content), result


def parse_json(text: str) -> Any:
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"Model did not return valid JSON: {text[:300]}")
