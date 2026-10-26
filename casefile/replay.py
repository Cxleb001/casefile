"""Record model calls, and replay them later without a key.

Every live call is appended to ``runs/llm/<case_id>.jsonl``: the messages sent, the step, the
model that answered (as Nightshift reports it), the answer, its reasoning, latency and tokens.

``ReplayLLM`` answers from those files in the order the case asked. With the same data, the same
code and the same case id, the agent asks the same questions in the same order, so a recorded run
plays back exactly: same tool calls, same evidence ids, same report and verdicts. This is what
sample mode runs on (see casefile/sample.py), so the full agent can be shown without Zetaris,
Nightshift or any key.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_CASE_SUFFIX = re.compile(r"-\d{8}-\d{6}$")


def request_hash(messages: list[dict[str, Any]]) -> str:
    """A fingerprint of what was asked, to tell an exact replay from one matched by order."""
    text = json.dumps(messages, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def alert_of(case_id: str) -> str:
    """ALR-004-20261026-121500 -> ALR-004."""
    return _CASE_SUFFIX.sub("", case_id or "")


def record_call(directory: Path, case_id: str | None, messages: list[dict[str, Any]], result=None,
                options: dict[str, Any] | None = None, *, step: str = "", provider: str = "", model: str = "",
                error: str | None = None) -> None:
    """Append one call to <directory>/<case_id>.jsonl. Never lets a disk problem break a run.

    A failed call is recorded too (with its error), so a replay fails at the same point and the
    calls after it still line up.
    """
    entry = {
        "case_id": case_id,
        "step": result.step if result else step,
        "provider": result.provider if result else provider,
        "model": result.model if result else model,
        "request_hash": request_hash(messages),
        "messages": messages,
        "options": options or {},
        "content": result.content if result else "",
        "reasoning": result.reasoning if result else None,
        "tool_calls": result.tool_calls if result else [],
        "latency_ms": result.latency_ms if result else 0,
        "prompt_tokens": result.prompt_tokens if result else None,
        "completion_tokens": result.completion_tokens if result else None,
        "error": error,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", case_id or "no-case")
        with open(directory / f"{name}.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def load_recordings(directory: Path) -> dict[str, list[dict[str, Any]]]:
    """case_id -> its calls in order, for every *.jsonl in the directory."""
    cases: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(Path(directory).glob("*.jsonl")):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    entry = json.loads(line)
                    cases.setdefault(entry["case_id"] or path.stem, []).append(entry)
    return cases


class ReplayLLM:
    """Stands in for casefile.llm.LLM and answers every call from a recording.

    Calls are matched per case in order. A call whose request differs from the recording (changed
    code or data) still gets the next recorded answer for the same step, and is counted in
    ``mismatches`` so a drifted replay is visible instead of silent.
    """

    def __init__(self, directory: Path, settings=None,
                 on_call: Callable[[Any, str | None], None] | None = None) -> None:
        from .config import get_settings

        self.settings = settings or get_settings()
        self.directory = Path(directory)
        self.recordings = load_recordings(self.directory)
        if not self.recordings:
            from .llm import LLMError

            raise LLMError(f"No recorded model calls in {self.directory}")
        self.on_call = on_call
        self._position: dict[str, int] = {}
        self.exact = 0
        self.mismatches: list[dict[str, Any]] = []

    # The recorded case id is reused, so evidence ids come out the same as in the recording.
    def case_id_for(self, alert_id: str) -> str | None:
        matches = sorted(c for c in self.recordings if alert_of(c) == alert_id)
        return matches[-1] if matches else None

    @property
    def models(self) -> dict[str, str]:
        """Which model answered each step in the recording."""
        seen: dict[str, str] = {}
        for calls in self.recordings.values():
            for call in calls:
                seen.setdefault(call["step"], call["model"])
        return seen

    def model_for(self, step: str) -> str:
        return self.models.get(step, "recorded")

    def list_models(self) -> list[str]:
        return sorted(set(self.models.values()))

    def chat(self, step: str, messages: list[dict[str, Any]], *, case_id: str | None = None,
             **_ignored: Any):
        from .llm import ChatResult, LLMError

        calls = self.recordings.get(case_id or "", [])
        position = self._position.get(case_id or "", 0)
        index = next((i for i in range(position, len(calls)) if calls[i]["step"] == step), None)
        if index is None:
            raise LLMError(f"replay: no recorded '{step}' answer left for case {case_id}")
        call = calls[index]
        self._position[case_id or ""] = index + 1
        if call["request_hash"] == request_hash(messages) and index == position:
            self.exact += 1
        else:
            self.mismatches.append({"case_id": case_id, "step": step, "index": index})
        if call.get("error"):
            raise LLMError(call["error"])
        result = ChatResult(
            step=step,
            provider=f"replay of {call.get('provider', '?')}",
            model=call["model"],
            content=call["content"],
            reasoning=call.get("reasoning"),
            tool_calls=call.get("tool_calls") or [],
            latency_ms=call.get("latency_ms") or 0,
            prompt_tokens=call.get("prompt_tokens"),
            completion_tokens=call.get("completion_tokens"),
        )
        if self.on_call:
            self.on_call(result, case_id)
        return result

    def chat_json(self, step: str, messages: list[dict[str, Any]], **kwargs):
        from .llm import parse_json

        result = self.chat(step, messages, **kwargs)
        return parse_json(result.content), result
