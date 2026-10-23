"""The investigation loop: after the standard workup, the planner model asks its own follow-up questions.

The protocol is plain JSON actions rather than native tool calling, so it works
the same through NVIDIA's API and through Meterless. A malformed reply gets
one retry with the parse error; repeated calls are refused with a note.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .case import CaseState
from .prompts import investigator_system
from .tools import Toolbox

PLAN_STEP = "plan"


def _transcript(case: CaseState) -> str:
    alert = case.alert
    head = (f"ALERT {alert.get('alert_id')}: account {case.subject} at Institution {alert.get('institution', 'A')}, "
            f"raised {alert.get('alert_ts')} by rules {alert.get('rules')}: {alert.get('trigger')}")
    body = "\n\n".join(f"#{o.step} " + o.for_model() for o in case.observations)
    limits = "\n".join(f"- {text}" for text in case.limitations) or "- none so far"
    return f"{head}\n\nINVESTIGATION RECORD\n{body}\n\nLIMITATIONS SO FAR\n{limits}"


def _signature(action: str, args: dict[str, Any]) -> str:
    return action + json.dumps(args, sort_keys=True, default=str)


def investigate(toolbox: Toolbox, llm, *, max_steps: int = 8, on_event: Callable | None = None) -> dict[str, Any]:
    """Run planner-chosen follow-ups. Returns {"steps": [...], "leaning": ..., "open_questions": [...]}."""
    from ..llm import LLMError

    case = toolbox.case
    system = investigator_system(toolbox.settings, max_steps)
    done = {_signature(o.tool, o.args) for o in case.observations}
    steps: list[dict[str, Any]] = []
    finish: dict[str, Any] = {}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": _transcript(case)}]

    for i in range(max_steps):
        try:
            reply, result = llm.chat_json(PLAN_STEP, messages, max_tokens=4096, temperature=0.2, case_id=case.case_id)
        except LLMError as error:
            messages.append({"role": "user", "content": f"Your last reply was not one valid JSON object ({error}). "
                                                        "Reply again with exactly one JSON object."})
            try:
                reply, result = llm.chat_json(PLAN_STEP, messages, max_tokens=4096, temperature=0.0,
                                              case_id=case.case_id)
            except LLMError as again:
                steps.append({"step": i + 1, "error": f"planner reply unusable: {again}"})
                break
        if not isinstance(reply, dict):
            steps.append({"step": i + 1, "error": "planner reply was not a JSON object"})
            break
        action = str(reply.get("action", "")).strip()
        args = reply.get("args") or {}
        thought = str(reply.get("thought", "")).strip()
        messages.append({"role": "assistant", "content": json.dumps(reply, ensure_ascii=False)})
        step = {"step": i + 1, "thought": thought, "action": action, "args": args, "model": result.model,
                "latency_ms": result.latency_ms}
        if action == "finish" or not action:
            finish = args if isinstance(args, dict) else {}
            steps.append(step)
            if on_event:
                on_event({"type": "finish", **step})
            break
        sig = _signature(action, args if isinstance(args, dict) else {})
        if sig in done:
            note = f"That exact call already ran (see the record). Ask something new or finish."
            messages.append({"role": "user", "content": note})
            step["skipped"] = note
            steps.append(step)
            continue
        done.add(sig)
        if on_event:
            on_event({"type": "action", **step})
        obs = toolbox.call(action, args if isinstance(args, dict) else {})
        step["observation_step"] = obs.step
        step["error"] = obs.error
        steps.append(step)
        if on_event:
            on_event({"type": "observation", "step": i + 1, "observation": obs})
        messages.append({"role": "user", "content": f"#{obs.step} " + obs.for_model() +
                         f"\n\n({max_steps - i - 1} action(s) left.)"})
    return {"steps": steps, "leaning": finish.get("leaning"), "open_questions": finish.get("open_questions", [])}
