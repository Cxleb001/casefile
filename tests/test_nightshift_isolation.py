"""Each Nightshift call gets its own conversation id, so no request can see another's content."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from casefile.config import get_settings
from casefile.llm import LLM


def _client(seen):
    def create(**kwargs):
        seen.append(kwargs.get("extra_headers", {}))
        message = SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None, model_extra={})
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None, model=kwargs["model"])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _llm(seen):
    llm = LLM(replace(get_settings(), llm_provider="meterless", llm_api_key="t"))
    llm.client = _client(seen)
    return llm


def test_every_call_of_every_case_gets_its_own_app_id(monkeypatch):
    monkeypatch.delenv("CASEFILE_NIGHTSHIFT_ISOLATION", raising=False)
    seen = []
    llm = _llm(seen)
    for case in ("ALR-001-20261027-100000", "ALR-002-20261027-100500"):
        for _ in range(2):
            llm.chat("plan", [{"role": "user", "content": "hi"}], case_id=case)
    apps = [h["meterless-app"] for h in seen]
    assert apps == ["casefile-alr-001-20261027-100000-1", "casefile-alr-001-20261027-100000-2",
                    "casefile-alr-002-20261027-100500-1", "casefile-alr-002-20261027-100500-2"]
    assert all(h["meterless-scope"] == "user" for h in seen)


def test_isolation_can_be_per_case_or_off(monkeypatch):
    seen = []
    llm = _llm(seen)
    monkeypatch.setenv("CASEFILE_NIGHTSHIFT_ISOLATION", "case")
    llm.chat("plan", [{"role": "user", "content": "hi"}], case_id="ALR-001-x")
    llm.chat("verify", [{"role": "user", "content": "hi"}], case_id="ALR-001-x")
    monkeypatch.setenv("CASEFILE_NIGHTSHIFT_ISOLATION", "app")
    llm.chat("plan", [{"role": "user", "content": "hi"}], case_id="ALR-001-x")
    llm.chat("plan", [{"role": "user", "content": "hi"}])  # no case: the app's own id
    assert [h["meterless-app"] for h in seen] == ["casefile-alr-001-x", "casefile-alr-001-x", "casefile", "casefile"]
