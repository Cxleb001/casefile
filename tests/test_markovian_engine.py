"""The Markovian engine beyond the conformance suite: tool pauses, the carryover cascade, history,
and the investigation loop's bounded prompts."""

from __future__ import annotations

import json
from dataclasses import dataclass

from casefile.agent.case import CaseState
from casefile.agent.investigator import investigate
from casefile.agent.tools import Toolbox
from casefile.agent.workup import standard_workup
from casefile.markovian import CarryoverService, Markovian, needs_tool_marker, parse_markers, resolve_config

STATE = "COMPLETED: profiled the subject\nREMAINING: check the ring\nDECISIONS: none yet\nCONTEXT: subject 1:S"


def test_needs_tool_pauses_the_run_and_the_result_resumes_it():
    prompts, asked = [], []

    def generator(prompt, attachments, system_prompt=None):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {"text": "Look at S.\n" + needs_tool_marker("lookup", '{"who": "O\'Brien"}') + "\n[STATE_CHECKPOINT]\n" + STATE}
        return {"text": "Seen enough. [TASK_COMPLETE]"}

    def tool_handler(tool, tool_input):
        asked.append((tool, json.loads(tool_input)))
        return "lookup result: 3 rows"

    run = Markovian({"chunkSize": 4000}).run("tool pause", generator=generator, tool_handler=tool_handler, reflect=False)
    assert asked == [("lookup", {"who": "O'Brien"})]          # quotes survive the marker round trip
    assert "[STEP INPUT]\nlookup result: 3 rows" in prompts[1]
    assert "[CARRYOVER STATE]\n" + STATE in prompts[1]
    assert run.chunks[0].metadata["tool"] == "lookup" and run.chunks[0].metadata["carryoverLevel"] == "marker"
    assert run.status == "completed" and "NEEDS_TOOL" not in run.output


def test_a_tool_request_without_a_handler_is_an_error_not_a_silent_skip():
    run = Markovian().run("no handler", generator=lambda p, a, s=None: needs_tool_marker("x", "{}"), reflect=False)
    assert run.status == "errored" and "no tool handler" in run.error


def test_carryover_cascade_falls_through_in_order():
    config = resolve_config({"carryoverTokens": 128})
    service = CarryoverService()
    assert service.next(marker_state=STATE, previous_carryover="", chunk_text="", config=config).level == "marker"
    with_llm = CarryoverService(lambda prompt, system: STATE)
    assert with_llm.next(marker_state="too short", previous_carryover="", chunk_text="x", config=config).level == "llm"
    broken_llm = CarryoverService(lambda prompt, system: (_ for _ in ()).throw(RuntimeError("down")))
    text = "We looked around. Status: ring confirmed through Y and Z. Next: check the hub."
    assert broken_llm.next(marker_state=None, previous_carryover="", chunk_text=text, config=config).level == "heuristic"
    tail = service.next(marker_state=None, previous_carryover="", chunk_text="word " * 2000, config=config)
    assert tail.level == "truncation" and len(tail.carryover) <= 128 * 4


def test_runs_persist_with_their_config_snapshot(tmp_path):
    engine = Markovian({"chunkSize": 4000, "carryoverTokens": 640}, history_dir=tmp_path)
    engine.run("one", generator=lambda p, a, s=None: "done [TASK_COMPLETE]", reflect=False)
    saved = json.loads((tmp_path / "history.json").read_text())
    assert saved[0]["chunkConfig"]["carryoverTokens"] == 640 and saved[0]["status"] == "completed"
    assert Markovian(history_dir=tmp_path).history.cumulative_stats()["totalRuns"] == 1


def test_markers_parse_the_json_alternate_conversion():
    parsed = parse_markers("Check Z.\n" + needs_tool_marker("get_transactions", '{"counterparty": "2:Z"}') +
                           "\n[STATE_CHECKPOINT]\n" + STATE)
    assert parsed.needs_tool == {"tool": "get_transactions", "input": '{"counterparty": "2:Z"}'}
    assert parsed.state == STATE and parsed.cleaned == "Check Z." and not parsed.done


@dataclass
class _Result:
    model: str = "fake-planner"
    latency_ms: int = 1
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    content: str = "ratified"


class RecordingPlanner:
    """Asks three different questions, then finishes; records every prompt it is shown."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.questions = [("get_transactions", {"counterparty": "Z", "order_by": "time"}),
                          ("get_counterparties", {"bank_id": 2, "account": "Z", "direction": "out"}),
                          ("profile_account", {"bank_id": 2, "account": "Y"})]

    def chat_json(self, step, messages, **kwargs):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        turn = len(self.prompts)
        if turn <= len(self.questions):
            action, args = self.questions[turn - 1]
            state = (f"COMPLETED: {turn} check(s), latest {action}\nREMAINING: the rest\n"
                     f"DECISIONS: ring through 2:Y and 2:Z looks real\nCONTEXT: subject 1:S; turn {turn}")
            return {"thought": f"Step {turn}: {action}.", "action": action, "args": args, "state": state}, \
                _Result(prompt_tokens=len(prompt) // 4)
        return {"thought": "Ring confirmed.", "action": "finish", "args": {"leaning": "ESCALATE"}}, _Result()

    def chat(self, step, messages, **kwargs):
        return _Result()


def test_investigation_steps_see_the_state_not_the_growing_record(world):
    case = CaseState("T", world["alert"])
    toolbox = Toolbox(world["backend"], world["store"], world["settings"], case)
    standard_workup(toolbox)
    planner = RecordingPlanner()
    result = investigate(toolbox, planner, max_steps=6)

    first, later = planner.prompts[0], planner.prompts[1:]
    assert "INVESTIGATION RECORD" in first
    assert all("INVESTIGATION RECORD" not in p and "[CARRYOVER STATE]" in p for p in later)
    assert "CONTEXT: subject 1:S; turn 2" in planner.prompts[2]           # the state written one step earlier
    assert "turn 1" not in planner.prompts[2]                              # ...and not the one before that
    # Flat cost: later prompts stay well below the full record however many observations pile up.
    assert max(len(p) for p in later) < len(first)
    assert [s["action"] for s in result["steps"]] == ["get_transactions", "get_counterparties", "profile_account",
                                                       "finish"]
    assert result["leaning"] == "ESCALATE"
    engine = result["engine"]
    assert engine["engine"] == "markovian" and engine["status"] == "completed" and engine["chunks"] == 4
    assert engine["chunk_config"]["carryoverTokens"] == 800 and engine["measured_prompt_tokens"]
