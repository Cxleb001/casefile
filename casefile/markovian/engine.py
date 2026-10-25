"""The Markovian orchestrator and engine facade (Markovian spec sections 4.5, 7, 11, 12, 14).

One loop runs the chain: build the chunk prompt, call the injected generator,
clean markers, account tokens, run the compression cascade, persist the chunk,
then either finish ([TASK_COMPLETE]), hand a pause marker to the host
([NEEDS_TOOL] is fulfilled by ``tool_handler`` and its result becomes the next
chunk's step input), or continue. Reflection runs after a completed chain and
can never fail the run. Abort is checked before each chunk and before
compression.

Rulings this implementation makes where the spec leaves room:
- "Exactly one marker per chunk" counts the state/final markers; a chunk may
  also carry one pause marker ([NEEDS_TOOL]), because a tool request and the
  state to resume from belong to the same step.
- Chunk tokens are estimated from the cleaned output, ceil(chars/4), and the
  run is labelled "estimated". Provider-reported usage, when the generator
  returns it, is kept per chunk under ``measured`` and labelled as such.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .carryover import Compressor, CarryoverService
from .config import ChunkConfig, estimate_tokens, resolve_config, run_efficiency
from .history import HistoryService
from .markers import clean_markers, parse_markers
from .progress import ProgressService
from .prompts import build_prompt, overlap_slice

# generator(prompt, attachments, system_prompt) -> str | {"text": str, "metadata": {...}}
Generator = Callable[[str, list, "str | None"], Any]
ToolHandler = Callable[[str, str], str]
PromptBuilder = Callable[..., "str | tuple[str, str]"]


@dataclass
class ChunkInfo:
    id: int                 # 1-based step index
    tokens: int             # estimated from the cleaned output, ceil(chars/4)
    carryover: str          # the state passed INTO this step
    content: str            # display-safe output
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tokens": self.tokens, "carryover": self.carryover, "content": self.content,
                **({"metadata": self.metadata} if self.metadata else {})}


@dataclass
class MarkovianRun:
    id: str
    timestamp: float
    prompt: str
    mode: str
    chunk_config: ChunkConfig          # mandatory per-run snapshot
    chunks: list[ChunkInfo] = field(default_factory=list)
    total_tokens_used: int = 0
    total_tokens_saved: int = 0
    efficiency_percent: float = 0.0
    duration_ms: int = 0
    status: str = "completed"          # completed | max-chunks | errored | aborted
    token_label: str = "estimated"     # estimated | measured
    output: str = ""
    final_state: str = ""
    reflection: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "timestamp": self.timestamp, "prompt": self.prompt, "mode": self.mode,
                "chunkConfig": self.chunk_config.to_dict(), "chunks": [c.to_dict() for c in self.chunks],
                "totalTokensUsed": self.total_tokens_used, "totalTokensSaved": self.total_tokens_saved,
                "efficiencyPercent": round(self.efficiency_percent, 2), "durationMs": self.duration_ms,
                "status": self.status, "tokenLabel": self.token_label, "output": self.output,
                "finalState": self.final_state, "reflection": self.reflection, "error": self.error}


def _as_text(result: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(result, dict):
        return str(result.get("text", "")), dict(result.get("metadata") or {})
    return str(result or ""), {}


def _aborted(abort: Any) -> bool:
    if abort is None:
        return False
    if callable(abort):
        return bool(abort())
    is_set = getattr(abort, "is_set", None)
    return bool(is_set()) if callable(is_set) else bool(abort)


class Markovian:
    """Runs long work as bounded chunks with a compressed carryover state."""

    def __init__(self, chunk_config: dict[str, Any] | ChunkConfig | None = None, *, generator: Generator | None = None,
                 compressor: Compressor | None = None, clock: Callable[[], float] | None = None,
                 history_dir: str | Path | None = None) -> None:
        self.config = resolve_config(chunk_config)
        self.clock = clock or (lambda: time.time() * 1000)
        self.history = HistoryService(history_dir)
        self.progress = ProgressService(self.clock, self.config.max_chunks)
        self.carryover_service = CarryoverService(compressor)
        self.default_generator = generator

    def on_progress(self, fn: Callable) -> Callable[[], None]:
        return self.progress.subscribe(fn)

    def run(self, goal: str | None = None, *, prompt: str | None = None, mode: str = "ARCHITECT",
            generator: Generator | None = None, step_fn: Callable[..., Any] | None = None,
            stop_when: Callable[..., bool] | None = None, memory_context: str | None = None,
            attachments: list[dict[str, Any]] | None = None, abort: Any = None, reflect: bool = True,
            tool_handler: ToolHandler | None = None, prompt_builder: PromptBuilder | None = None,
            system_prompt: str | None = None) -> MarkovianRun:
        goal = goal or prompt
        if not goal:
            raise ValueError("Markovian: run needs a goal or prompt")
        config = self.config
        gen = self._generator(generator, step_fn, goal)
        start = self.clock()
        run = MarkovianRun(id=f"run-{uuid.uuid4().hex[:12]}", timestamp=start, prompt=goal[:500], mode=mode,
                           chunk_config=config)
        attachments = attachments or []
        cleaned_parts: list[str] = []
        carryover, previous_cleaned, step_input = "", "", None
        done = False
        self.progress.phase("initializing", f"{mode} chain, maxChunks={config.max_chunks}")

        try:
            while len(run.chunks) < config.max_chunks and not done:
                if _aborted(abort):
                    run.status = "aborted"
                    break
                step = len(run.chunks) + 1
                self.progress.phase("generating", f"chunk {step}")
                builder_args = dict(mode=mode, goal=goal, step=step, carryover=carryover,
                                    overlap_text=overlap_slice(previous_cleaned, config), config=config,
                                    memory_context=memory_context, attachments=attachments if step == 1 else [],
                                    step_input=step_input)
                built = (prompt_builder or build_prompt)(**builder_args)
                chunk_prompt, chunk_system = built if isinstance(built, tuple) else (built, system_prompt)
                text, meta = _as_text(gen(chunk_prompt, attachments if step == 1 else [], chunk_system))
                parsed = parse_markers(text)
                content = parsed.cleaned
                chunk = ChunkInfo(id=step, tokens=estimate_tokens(content), carryover=carryover, content=content)
                if meta:
                    chunk.metadata.update(meta)
                # Persist first: the chunk is paid for, so an abort below still keeps it (section 11).
                run.chunks.append(chunk)
                cleaned_parts.append(content)
                previous_cleaned = content
                self.progress.chunk_done(chunk)

                if _aborted(abort):
                    run.status = "aborted"
                    break
                self.progress.phase("compressing", f"chunk {step}")
                nxt = self.carryover_service.next(marker_state=parsed.state, previous_carryover=carryover,
                                                  chunk_text=text, config=config)
                carryover = nxt.carryover
                chunk.metadata["carryoverLevel"] = nxt.level
                eff = run_efficiency(run.chunks, config)
                self.progress.update(tokens_used=eff["totalTokensUsed"], tokens_saved=eff["tokensSaved"],
                                     carryover_size=estimate_tokens(carryover))

                step_input = None
                if parsed.done:
                    done = True
                elif parsed.needs_tool:
                    tool, tool_input = parsed.needs_tool["tool"], parsed.needs_tool["input"].replace("\\u0027", "'")
                    chunk.metadata.update({"tool": tool, "toolInput": tool_input})
                    if tool_handler is None:
                        raise RuntimeError(f"chunk {step} asked for tool {tool!r} but the host has no tool handler")
                    try:
                        step_input = tool_handler(tool, tool_input)
                    except Exception as error:  # noqa: BLE001  a failed tool is reported to the next chunk
                        step_input = f"Tool {tool} failed: {error}"
                    chunk.metadata["toolResultChars"] = len(step_input or "")
                elif parsed.needs_clarification:
                    chunk.metadata["needsClarification"] = parsed.needs_clarification
                    done = True  # no human in this loop: stop and surface the question
                if not done and stop_when is not None and stop_when(step=step, carryover=carryover):
                    done = True

            if not done and run.status == "completed" and len(run.chunks) >= config.max_chunks:
                run.status = "max-chunks"
        except Exception as error:  # noqa: BLE001
            run.status = "errored"
            run.error = str(error)
            self.progress.phase("error", run.error)

        run.output = "\n\n".join(cleaned_parts)
        run.final_state = carryover
        eff = run_efficiency(run.chunks, run.chunk_config)
        run.total_tokens_used = int(eff["totalTokensUsed"])
        run.total_tokens_saved = int(eff["tokensSaved"])
        run.efficiency_percent = float(eff["efficiencyPercent"])

        if reflect and run.status == "completed" and run.chunks:
            self.progress.phase("reflecting")
            try:
                reflection_prompt = (f"Reflect on this completed run.\nGoal: {goal[:200]}\nFinal state: {carryover}\n"
                                     f"Output (truncated): {run.output[:4000]}\n"
                                     "Verdict: ratify if the goal is met, refine with concrete gaps otherwise.")
                text, _ = _as_text(gen(reflection_prompt, [], "You are the reflection pass. Be brief and concrete."))
                run.reflection = clean_markers(text)
            except Exception as error:  # noqa: BLE001  reflection never fails the run (section 7)
                run.reflection = None
                run.error = f"reflection failed (run preserved): {error}"

        run.duration_ms = int(self.clock() - start)
        self.progress.phase("error" if run.status == "errored" else "complete", run.status)
        self.history.record(run.to_dict())
        return run

    def _generator(self, generator: Generator | None, step_fn: Callable | None, goal: str) -> Generator:
        if generator is not None:
            return generator
        if step_fn is not None:
            counter = {"step": 0}

            def adapted(prompt: str, attachments: list, system_prompt: str | None = None) -> dict[str, Any]:
                marker = "[CARRYOVER STATE]\n"
                carry = prompt.split(marker, 1)[1].split("\n\n", 1)[0].strip() if marker in prompt else ""
                result = step_fn(goal=goal, carryover=carry, step=counter["step"], prompt=prompt)
                counter["step"] += 1
                return {"text": result["content"] if isinstance(result, dict) else str(result)}

            return adapted
        if self.default_generator is not None:
            return self.default_generator
        raise ValueError("Markovian: provide a generator or step_fn")


__all__ = ["Markovian", "MarkovianRun", "ChunkInfo", "Generator", "ToolHandler"]
