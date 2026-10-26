"""A recorded run plays back exactly, without a key: same tool calls, same evidence ids, same verdicts."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

from test_pipeline import ScriptedLLM

from casefile.agent.pipeline import run_case
from casefile.evidence import EvidenceStore
from casefile.llm import LLM
from casefile.replay import ReplayLLM, alert_of

MODELS = {"fast": "m-fast", "plan": "m-plan", "report": "m-report", "verify": "m-verify"}


def _recording_llm(world, record_dir):
    """The real LLM client, answering through a fake endpoint driven by the scripted model."""
    settings = replace(world["settings"], llm_provider="meterless", llm_api_key="test-token", models=MODELS)
    llm = LLM(settings)
    llm.record_dir = record_dir
    script, step_of = ScriptedLLM(), {model: step for step, model in MODELS.items()}

    def create(**kwargs):
        answer, _ = script.chat_json(step_of[kwargs["model"]], kwargs["messages"])
        message = SimpleNamespace(content=json.dumps(answer), tool_calls=None, reasoning_content=None, model_extra={})
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None,
                               model="nvidia/nemotron-3-ultra-550b-a55b")

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return llm


def _outcome(report):
    return {"disposition": report["disposition"], "typology": report["typology"],
            "claims": [(c["id"], c["evidence"], c["verdict"]["status"]) for c in report["claims"]],
            "steps": [(s.get("action"), json.dumps(s.get("args"), sort_keys=True)) for s in report["investigation"]["steps"]]}


def test_a_recorded_run_replays_exactly_without_a_key(world):
    record_dir = world["tmp"] / "llm"
    live = run_case(world["alert"], backend=world["backend"], store=world["store"], settings=world["settings"],
                    llm=_recording_llm(world, record_dir), max_steps=4, out_dir=world["tmp"] / "live")
    case_id = live["paths"]["case_id"]
    assert (record_dir / f"{case_id}.jsonl").exists()

    store = EvidenceStore(world["tmp"] / "replay.duckdb")  # a fresh evidence store, as on a judge's machine
    replay = ReplayLLM(record_dir, settings=world["settings"])
    assert replay.case_id_for(world["alert"]["alert_id"]) == case_id and alert_of(case_id) == "ALR-T01"
    again = run_case(world["alert"], backend=world["backend"], store=store, settings=world["settings"],
                     llm=replay, max_steps=4, out_dir=world["tmp"] / "replay")
    store.close()

    assert again["paths"]["case_id"] == case_id
    assert _outcome(again["report"]) == _outcome(live["report"])
    assert replay.mismatches == [] and replay.exact == len(json.loads("[" + ",".join(
        (record_dir / f"{case_id}.jsonl").read_text().splitlines()) + "]"))
    assert replay.models["verify"] == "nvidia/nemotron-3-ultra-550b-a55b"


def test_replaying_into_the_same_store_replaces_the_case(world):
    record_dir = world["tmp"] / "llm"
    run_case(world["alert"], backend=world["backend"], store=world["store"], settings=world["settings"],
             llm=_recording_llm(world, record_dir), max_steps=4, out_dir=world["tmp"] / "live")
    for _ in range(2):  # the same case id twice in one store: no duplicate-key error, no doubled claims
        result = run_case(world["alert"], backend=world["backend"], store=world["store"], settings=world["settings"],
                          llm=ReplayLLM(record_dir, settings=world["settings"]), max_steps=4,
                          out_dir=world["tmp"] / "replay")
    claims = world["store"].claims_for_case(result["paths"]["case_id"])
    assert len(claims) == len(result["report"]["claims"])
