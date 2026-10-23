"""The model layer with a scripted stand-in model: no network, no key."""

from __future__ import annotations

import re
from dataclasses import dataclass

from casefile.agent.pipeline import run_case


@dataclass
class _Result:
    model: str
    latency_ms: int = 1


class ScriptedLLM:
    """Answers each step from the prompt it is given, like a well-behaved model would."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.planner_turns = 0

    def chat_json(self, step, messages, **kwargs):
        system, user = messages[0]["content"], messages[-1]["content"]
        self.calls.append(step)
        if step == "verify":
            return {"verdicts": [{"id": "C1", "supported": True, "reason": "ring rows show it"},
                                 {"id": "C3", "supported": False, "reason": "evidence does not show intent"}]}, _Result("fake-judge")
        if "investigating one transaction-monitoring alert" in system:
            self.planner_turns += 1
            if self.planner_turns == 1:
                return {"thought": "Check the exact transfers between the subject and Z.", "action": "get_transactions",
                        "args": {"counterparty": "Z", "order_by": "time"}}, _Result("fake-planner")
            if self.planner_turns == 2:  # a repeat: must be refused, not re-run
                return {"thought": "again", "action": "get_transactions",
                        "args": {"counterparty": "Z", "order_by": "time"}}, _Result("fake-planner")
            return {"thought": "Ring confirmed.", "action": "finish", "args": {"leaning": "ESCALATE"}}, _Result("fake-planner")
        # report writer: cite real evidence ids from the index it was given
        record = messages[1]["content"]
        cycle_line = next(line for line in record.splitlines() if line.startswith("- cycle"))
        cycle_ids = re.findall(r"Q-[0-9a-f]{10}", cycle_line)
        profile_id = re.search(r"(Q-[0-9a-f]{10}): customer record", record).group(1)
        return {
            "disposition": "ESCALATE", "typology": "CYCLE", "confidence": 0.8,
            "headline": "Funds circled back to the subject through Institution B.",
            "claims": [
                {"id": "C1", "kind": "fact", "text": "A 4-hop ring returned money to 1:S within a day.",
                 "evidence": cycle_ids},
                {"id": "C2", "kind": "fact", "text": "The subject moved $999,999 through the ring.",
                 "evidence": cycle_ids},
                {"id": "C3", "kind": "inference", "text": "The subject controls Y and Z.", "evidence": cycle_ids},
                {"id": "C4", "kind": "fact", "text": "The subject is Ivan Petrov.", "evidence": [profile_id]},
                {"id": "C5", "kind": "fact", "text": "Made-up evidence.", "evidence": ["Q-0000000000"]},
            ],
            "counter_evidence": [], "would_change_my_mind": ["Proof Y and Z are unrelated businesses."],
            "sanctions_referral": {"needed": True, "reason": "strong match", "evidence": []},
            "limitations": [], "narrative": "Test narrative.",
        }, _Result("fake-writer")


def test_full_case_with_model(world):
    llm = ScriptedLLM()
    events = []
    result = run_case(world["alert"], backend=world["backend"], store=world["store"], settings=world["settings"],
                      llm=llm, max_steps=4, on_event=events.append, out_dir=world["tmp"] / "cases")
    report = result["report"]
    claims = {c["id"]: c for c in report["claims"]}

    assert report["disposition"] == "ESCALATE" and report["typology"] == "CYCLE"
    assert claims["C1"]["verdict"]["status"] == "verified"                    # passed both layers
    assert claims["C2"]["verdict"]["status"] == "rejected"                    # invented number
    assert "999,999" in claims["C2"]["verdict"]["reason"]
    assert claims["C3"]["verdict"]["status"] == "rejected"                    # model said unsupported
    assert claims["C3"]["verdict"]["layer"] == "model"
    assert claims["C5"]["verdict"]["status"] == "rejected"                    # nonexistent evidence id
    assert report["verification"]["rejected"] == 3
    assert not report.get("needs_review")                                     # C1 and C4 still stand

    steps = report["investigation"]["steps"]
    assert steps[0]["action"] == "get_transactions" and not steps[0].get("error")
    assert steps[1].get("skipped")                                            # repeat refused
    assert report["investigation"]["leaning"] == "ESCALATE"
    assert any("withholds" in x or "could not be screened" in x for x in report["limitations"])

    case_dir = world["tmp"] / "cases"
    case_id = result["paths"]["case_id"]
    sar = (case_dir / f"{case_id}.sar.md").read_text()
    assert "~~The subject moved $999,999" in sar and "```sql" in sar
    assert {"plan", "verify"} <= set(llm.calls)
    assert any(e["type"] == "done" for e in events)


def test_rules_only_case_without_a_model(world):
    result = run_case(world["alert"], backend=world["backend"], store=world["store"], settings=world["settings"],
                      llm=None, out_dir=world["tmp"] / "cases")
    report = result["report"]
    assert report["disposition"] == "ESCALATE" and report["typology"] == "CYCLE"
    assert report["sanctions_referral"]["needed"] is True
    assert report["verification"]["rejected"] == 0
    assert report["author"]["model"] == "rules"
