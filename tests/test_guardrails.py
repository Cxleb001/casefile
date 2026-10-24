"""Guardrails around the model: the evidence standard, the report fallback, and thinking controls."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from types import SimpleNamespace

import httpx
import openai
import pytest

from casefile.agent.pipeline import run_case
from casefile.config import get_settings
from casefile.llm import LLM, LLMError


@dataclass
class _Result:
    model: str
    latency_ms: int = 1


class OpinionatedLLM:
    """Finishes the investigation at once, then argues for a fixed disposition (or replies with broken JSON)."""

    def __init__(self, disposition: str, report_ok: bool = True) -> None:
        self.disposition = disposition
        self.report_ok = report_ok
        self.calls: list[str] = []

    def chat_json(self, step, messages, **kwargs):
        self.calls.append(step)
        if step == "verify":
            return {"verdicts": []}, _Result("fake-judge")
        if "investigating one transaction-monitoring alert" in messages[0]["content"]:
            return {"thought": "Enough.", "action": "finish", "args": {"leaning": self.disposition}}, _Result("fake-planner")
        if not self.report_ok:
            raise LLMError('Model did not return valid JSON: {"disposition": "ESCA')
        record = messages[1]["content"]
        assert "MEASURED STRUCTURE" in record
        profile_id = re.search(r"(Q-[0-9a-f]{10}): customer record", record).group(1)
        return {
            "disposition": self.disposition, "typology": "GATHER-SCATTER", "confidence": 0.9,
            "headline": "The model's view.",
            "claims": [{"id": "C1", "kind": "fact", "text": "The subject is a customer of Institution A.",
                        "evidence": [profile_id]}],
            "counter_evidence": [], "would_change_my_mind": [], "limitations": [], "narrative": "Test.",
            "sanctions_referral": {"needed": False, "reason": "", "evidence": []},
        }, _Result("fake-writer")


def _run(world, llm, account="S"):
    alert = {**world["alert"], "account": account}
    return run_case(alert, backend=world["backend"], store=world["store"], settings=world["settings"], llm=llm,
                    max_steps=2, out_dir=world["tmp"] / "cases")


def test_model_escalation_without_measured_structure_goes_to_review_closed(world):
    # R05 is an ordinary shopper: one card payment to a big merchant, no structure at all.
    result = _run(world, OpinionatedLLM("ESCALATE"), account="R05")
    report = result["report"]
    assert report["model_disposition"] == "ESCALATE"
    assert report["disposition"] == "CLOSE" and report["typology"] == "NONE"
    assert report["needs_review"] is True and report["confidence"] <= 0.5
    assert "leaned ESCALATE" in report["decision_basis"]
    sar = (world["tmp"] / "cases" / f"{result['paths']['case_id']}.sar.md").read_text()
    assert "leaned ESCALATE" in sar


def test_model_close_against_measured_structure_goes_to_review_escalated(world):
    report = _run(world, OpinionatedLLM("CLOSE"))["report"]  # S sits on a 4-hop ring
    assert report["model_disposition"] == "CLOSE"
    assert report["disposition"] == "ESCALATE" and report["typology"] == "CYCLE"
    assert report["needs_review"] is True
    assert report["measured"]["disposition"] == "ESCALATE" and any(s.startswith("cycle") for s in report["measured"]["signals"])


def test_agreement_keeps_the_model_report(world):
    llm = OpinionatedLLM("ESCALATE")
    report = _run(world, llm)["report"]
    assert report["disposition"] == "ESCALATE" and report["typology"] == "GATHER-SCATTER"
    assert not report.get("needs_review")
    assert "agree" in report["decision_basis"]
    assert {"plan", "report", "verify"} <= set(llm.calls)


def test_unusable_report_falls_back_to_the_measured_decision(world):
    report = _run(world, OpinionatedLLM("ESCALATE", report_ok=False))["report"]
    assert report["author"]["model"].startswith("rules")
    assert report["disposition"] == "ESCALATE" and report["needs_review"] is True
    assert any("could not be used" in x for x in report["limitations"])


def _llm(**changes):
    values = {"llm_provider": "nvidia", "llm_api_key": "test-key",
              "thinking": {"plan": "off", "report": "medium", "verify": "on"}, **changes}
    settings = replace(get_settings(), **values)
    return LLM(settings)


def test_thinking_controls_per_step():
    llm = _llm()
    assert llm._thinking_kwargs("plan", "nvidia/nemotron-3-ultra-550b-a55b") == {"enable_thinking": False}
    assert llm._thinking_kwargs("report", "nvidia/nemotron-3-ultra-550b-a55b") == {"enable_thinking": True,
                                                                                  "medium_effort": True}
    assert llm._thinking_kwargs("verify", "nvidia/nemotron-3-ultra-550b-a55b") == {"enable_thinking": True}
    assert llm._thinking_kwargs("plan", "meta/llama-3.1-8b-instruct") is None
    assert _llm(llm_provider="meterless")._thinking_kwargs("plan", "nemotron-x") is None


def test_model_that_rejects_thinking_controls_is_asked_again_without_them():
    llm = _llm(models={"plan": "nvidia/nemotron-new"})
    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        if "chat_template_kwargs" in kwargs.get("extra_body", {}):
            response = httpx.Response(400, request=httpx.Request("POST", "https://example.test/v1/chat/completions"))
            raise openai.BadRequestError("unknown field chat_template_kwargs", response=response, body=None)
        message = SimpleNamespace(content='{"ok": true}', tool_calls=None, model_extra={})
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None, model="nvidia/nemotron-new")

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    parsed, _ = llm.chat_json("plan", [{"role": "user", "content": "hi"}])
    assert parsed == {"ok": True}
    assert len(sent) == 2 and "extra_body" not in sent[1]
    llm.chat_json("plan", [{"role": "user", "content": "again"}])
    assert len(sent) == 3 and "extra_body" not in sent[2]  # remembered: no second rejected call


def test_other_bad_requests_still_fail_loudly():
    llm = _llm(models={"plan": "meta/llama-3.1-8b-instruct"})

    def create(**kwargs):
        response = httpx.Response(400, request=httpx.Request("POST", "https://example.test/v1/chat/completions"))
        raise openai.BadRequestError("context too long", response=response, body=None)

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(LLMError, match="context too long"):
        llm.chat("plan", [{"role": "user", "content": "hi"}])
