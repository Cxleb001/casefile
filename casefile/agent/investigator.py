"""The investigation loop, run on the Markovian engine (Meterless): bounded steps with a carried state.

After the standard workup, the planner model asks its own follow-up questions.
Each step is one Markovian chunk:

- step 1 sees the full case record (the heavy context enters once);
- every later step sees only a compact case brief, the carryover state the
  model wrote at the end of the previous step (COMPLETED / REMAINING /
  DECISIONS / CONTEXT), and the result of the tool it asked for.

So the prompt stays the same size however long the investigation runs, instead
of growing with every observation. The model replies in JSON (the spec's
documented JSON alternate); the host converts each reply to the engine's
bracket markers: a tool request becomes [NEEDS_TOOL ...], the state follows
[STATE_CHECKPOINT], and "finish" becomes [TASK_COMPLETE]. A malformed reply
gets one retry with the parse error; repeated calls are refused with a note.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..config import RUNS_DIR
from ..markovian import Markovian, needs_tool_marker
from .case import CaseState
from .prompts import investigator_system
from .tools import Toolbox

PLAN_STEP = "plan"
CHUNK_CONFIG = {"chunkSize": 4000, "carryoverTokens": 800}  # carryover budget in tokens (chars / 4)
TOOL_RESULT_CHARS = 3500
HISTORY_DIR = RUNS_DIR / "markovian"


def _alert_line(case: CaseState) -> str:
    alert = case.alert
    return (f"ALERT {alert.get('alert_id')}: account {case.subject} at Institution {alert.get('institution', 'A')}, "
            f"raised {alert.get('alert_ts')} by rules {alert.get('rules')}: {alert.get('trigger')}")


def _transcript(case: CaseState) -> str:
    body = "\n\n".join(f"#{o.step} " + o.for_model() for o in case.observations)
    limits = "\n".join(f"- {text}" for text in case.limitations) or "- none so far"
    return f"{_alert_line(case)}\n\nINVESTIGATION RECORD\n{body}\n\nLIMITATIONS SO FAR\n{limits}"


def _case_brief(case: CaseState) -> str:
    """A fixed-size reminder of the case for continuation steps: the alert, each workup summary, the signals."""
    summaries = [f"#{o.step} {o.tool}: {' '.join(o.summary.split())[:300]}" for o in case.observations if not o.error]
    signals = [f"- {s['name']} [{s['assessment']}, {s['strength']}/3]: {s['finding'][:220]}"
               for s in case.signals if s.get("strength", 0) > 0 or s["assessment"] != "context"]
    return "\n".join([_alert_line(case), "", "WORKUP SUMMARIES", *summaries, "", "SIGNALS", *(signals or ["- none"])])


def _signature(action: str, args: dict[str, Any]) -> str:
    return action + json.dumps(args, sort_keys=True, default=str)


def investigate(toolbox: Toolbox, llm, *, max_steps: int = 8, on_event: Callable | None = None) -> dict[str, Any]:
    """Run planner-chosen follow-ups on the Markovian engine.

    Returns {"steps": [...], "leaning": ..., "open_questions": [...], "engine": <run summary>}.
    """
    from ..llm import LLMError

    case = toolbox.case
    max_steps = max(1, min(int(max_steps), 32))
    system = investigator_system(toolbox.settings, max_steps)
    record, brief = _transcript(case), _case_brief(case)
    done = {_signature(o.tool, o.args) for o in case.observations}
    tool_calls: list[dict[str, Any]] = []

    def prompt_builder(*, step: int, carryover: str, step_input: str | None, **_: Any) -> tuple[str, str]:
        left = max_steps - step + 1
        if step == 1:
            return f"{record}\n\n({left} action(s) available.)", system
        parts = [f"CASE BRIEF\n{brief}", f"[CARRYOVER STATE]\n{carryover or '(none)'}"]
        if step_input:
            parts.append(f"[LATEST RESULT]\n{step_input}")
        parts.append(f"({left} action(s) left. Earlier results are not shown again: rely on your state.)")
        return "\n\n".join(parts), system

    def generator(prompt: str, attachments: list, system_prompt: str | None = None) -> dict[str, Any]:
        messages = [{"role": "system", "content": system_prompt or system}, {"role": "user", "content": prompt}]
        if system_prompt and system_prompt.startswith("You are the reflection pass"):
            result = llm.chat(PLAN_STEP, messages, max_tokens=600, temperature=0.0, case_id=case.case_id)
            return {"text": result.content}
        try:
            reply, result = llm.chat_json(PLAN_STEP, messages, max_tokens=2048, temperature=0.2, case_id=case.case_id)
        except LLMError as error:
            messages.append({"role": "user", "content": f"Your last reply was not one valid JSON object ({error}). "
                                                        "Reply again with exactly one JSON object."})
            reply, result = llm.chat_json(PLAN_STEP, messages, max_tokens=2048, temperature=0.0, case_id=case.case_id)
        if not isinstance(reply, dict):
            raise ValueError("planner reply was not a JSON object")
        action = str(reply.get("action", "")).strip()
        args = reply.get("args") if isinstance(reply.get("args"), dict) else {}
        thought = " ".join(str(reply.get("thought", "")).split())
        state = str(reply.get("state", "")).strip()
        meta = {"action": action or "finish", "args": args, "thought": thought,
                "model": getattr(result, "model", None), "latency_ms": getattr(result, "latency_ms", None),
                "measured": {"promptTokens": getattr(result, "prompt_tokens", None),
                             "completionTokens": getattr(result, "completion_tokens", None)}}
        if action in ("", "finish"):
            return {"text": f"{thought}\n[TASK_COMPLETE]", "metadata": meta}
        marker = needs_tool_marker(action, json.dumps(args, ensure_ascii=True, default=str))
        return {"text": f"{thought}\n{marker}\n[STATE_CHECKPOINT]\n{state}", "metadata": meta}

    def tool_handler(tool: str, tool_input: str) -> str:
        try:
            args = json.loads(tool_input or "{}")
        except json.JSONDecodeError as error:
            tool_calls.append({"error": f"arguments were not valid JSON: {error}"})
            return f"Your {tool} arguments were not valid JSON ({error}). Ask again or finish."
        args = args if isinstance(args, dict) else {}
        sig = _signature(tool, args)
        if sig in done:
            note = "That exact call already ran (see your state). Ask something new or finish."
            tool_calls.append({"skipped": note})
            return note
        done.add(sig)
        if on_event:
            thought = engine.progress.state.completed_chunks[-1].metadata.get("thought", "") \
                if engine.progress.state.completed_chunks else ""
            on_event({"type": "action", "thought": thought, "action": tool, "args": args})
        obs = toolbox.call(tool, args)
        tool_calls.append({"observation_step": obs.step, "error": obs.error})
        if on_event:
            on_event({"type": "observation", "observation": obs})
        return f"#{obs.step} " + obs.for_model(TOOL_RESULT_CHARS)

    def compressor(prompt: str, system_prompt: str) -> str:
        result = llm.chat(PLAN_STEP, [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
                          max_tokens=700, temperature=0.0, case_id=case.case_id)
        return result.content

    engine = Markovian({**CHUNK_CONFIG, "maxChunks": max_steps}, compressor=compressor, history_dir=HISTORY_DIR)
    run = engine.run(f"Investigate {_alert_line(case)}", mode="INVESTIGATE", generator=generator,
                     tool_handler=tool_handler, prompt_builder=prompt_builder)

    steps: list[dict[str, Any]] = []
    finish: dict[str, Any] = {}
    calls = iter(tool_calls)
    for chunk in run.chunks:
        meta = chunk.metadata
        step = {"step": chunk.id, "thought": meta.get("thought", chunk.content), "action": meta.get("action"),
                "args": meta.get("args", {}), "model": meta.get("model"), "latency_ms": meta.get("latency_ms"),
                "carryover_in": chunk.carryover, "carryover_level": meta.get("carryoverLevel"),
                "prompt_tokens": (meta.get("measured") or {}).get("promptTokens")}
        if meta.get("tool"):
            step.update(next(calls, {}))
        if meta.get("action") == "finish":
            finish = meta.get("args") or {}
            if on_event:
                on_event({"type": "finish", **{k: step[k] for k in ("step", "thought", "action", "args")}})
        steps.append(step)
    if run.status == "errored":
        steps.append({"step": len(run.chunks) + 1, "error": f"planner reply unusable: {run.error}"})

    prompt_sizes = [s["prompt_tokens"] for s in steps if s.get("prompt_tokens")]
    engine_summary = {
        "engine": "markovian", "run_id": run.id, "status": run.status, "chunks": len(run.chunks),
        "chunk_config": run.chunk_config.to_dict(), "token_label": run.token_label,
        "estimated_tokens_used": run.total_tokens_used, "estimated_tokens_saved": run.total_tokens_saved,
        "estimated_efficiency_percent": round(run.efficiency_percent, 1),
        "measured_prompt_tokens": prompt_sizes, "final_state": run.final_state, "reflection": run.reflection,
        "error": run.error,
    }
    return {"steps": steps, "leaning": finish.get("leaning"), "open_questions": finish.get("open_questions", []),
            "engine": engine_summary}
