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
    nightshift = _llm(llm_provider="meterless")
    assert nightshift._thinking_kwargs("plan", "meterless/text.fast") is None             # a routing mode
    assert nightshift._thinking_kwargs("plan", "nvidia/nemotron-3-ultra-550b-a55b") == {"enable_thinking": False}


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


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def get_query(self, qid):
        return {"purpose": "transactions", "columns": ["ts", "amount"], "row_count": len(self.rows), "rows": self.rows}


def _case(summary):
    obs = SimpleNamespace(tool="get_transactions", summary=summary, query_ids=["Q-0000000001"])
    return SimpleNamespace(observations=[obs], signals=[], all_query_ids=lambda: ["Q-0000000001"])


def test_verifier_reads_unicode_dashes_and_month_name_dates():
    from casefile.agent.verifier import deterministic_check

    store = _Store([["2022-09-01 05:40", 1984.37]] * 100)
    case = _case("34 transactions totalling $1,984.37 between 2022-09-01 and 2022-09-10.")
    ok = {"kind": "fact", "evidence": ["Q-0000000001"],
          "text": "On 2022‑09‑01 (1‑10 Sep 2022) the subject paid $1,984.37 in 34 transactions."}
    assert deterministic_check(ok, case, store)[0] is None
    wrong = {**ok, "text": "During Sep 1–10, 2022 the subject paid $2,500.00 in 34 transactions."}
    failure, _ = deterministic_check(wrong, case, store)
    assert failure and "2,500.00" in failure and "2022" not in failure


def test_verifier_shows_tool_summaries_before_raw_rows():
    from casefile.agent.verifier import evidence_text

    store = _Store([["2022-09-01 05:40", 1.0]] * 60)
    text = evidence_text(_case("34 Institution B counterparties could not be screened."), store, "Q-0000000001")
    assert text.index("could not be screened") < text.index("first rows")


def test_tools_accept_accounts_written_bank_colon_account(world):
    from casefile.agent.case import CaseState
    from casefile.agent.tools import Toolbox

    case = CaseState("T", world["alert"])
    obs = Toolbox(world["backend"], world["store"], world["settings"], case).call("get_transactions",
                                                                                 {"account": "2:Y", "bank_id": 1})
    assert not obs.error and "2:Y" in obs.summary


class MistakenLLM(OpinionatedLLM):
    """Makes one malformed tool call before finishing, like a fast model sometimes does."""

    def __init__(self) -> None:
        super().__init__("ESCALATE")
        self.turns = 0

    def chat_json(self, step, messages, **kwargs):
        if step == "plan":
            self.calls.append(step)
            self.turns += 1
            if self.turns == 1:
                return {"thought": "Look.", "action": "get_transactions", "args": {"account": "not an id!"}}, _Result("p")
            return {"thought": "Done.", "action": "finish", "args": {"leaning": "ESCALATE"}}, _Result("p")
        return super().chat_json(step, messages, **kwargs)


def test_planner_argument_mistakes_do_not_count_as_incomplete_data(world):
    report = _run(world, MistakenLLM())["report"]
    assert report["investigation"]["steps"][0]["error"]  # the mistake is visible in the trace
    assert "data_errors" not in report and not report.get("needs_review")
    assert not any("checks failed" in x for x in report["limitations"])


def test_counterparty_watchlist_match_is_listed_not_referred(world):
    from casefile.agent.case import CaseState
    from casefile.agent.report import apply_screening_standard
    from casefile.agent.tools import Toolbox

    # X ("Mary Stone") pays nobody suspicious; S, a counterparty of X, is a strong match ("PETROV, Ivan").
    case = CaseState("T", {**world["alert"], "account": "X"})
    toolbox = Toolbox(world["backend"], world["store"], world["settings"], case)
    toolbox.call("get_counterparties", {"direction": "in"})
    toolbox.call("screen_watchlist", {})
    report = apply_screening_standard({"sanctions_referral": {"needed": True, "reason": "counterparty S is listed"}}, case)
    assert report["model_sanctions_referral"] is True
    assert report["sanctions_referral"]["needed"] is False
    assert [m["account"] for m in report["counterparty_watchlist"]] == ["1:S"]


def test_subject_watchlist_match_is_referred_even_if_the_model_missed_it(world):
    from casefile.agent.case import CaseState
    from casefile.agent.report import apply_screening_standard
    from casefile.agent.tools import Toolbox

    case = CaseState("T", world["alert"])  # S is "Ivan Petrov", listed as "PETROV, Ivan" with the same DOB and country
    Toolbox(world["backend"], world["store"], world["settings"], case).call("screen_watchlist", {})
    report = apply_screening_standard({"sanctions_referral": {"needed": False}}, case)
    assert report["sanctions_referral"]["needed"] is True and "PETROV" in report["sanctions_referral"]["reason"]
