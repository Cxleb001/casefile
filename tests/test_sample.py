"""The bundled sample (sample/) must reproduce its recorded decisions exactly, with no keys and no Zetaris."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from casefile.agent.pipeline import load_alerts, run_case
from casefile.config import DEFAULT_TABLES, SAMPLE_DIR, get_settings
from casefile.data.local import LocalBackend
from casefile.evidence import EvidenceStore
from casefile.replay import ReplayLLM

pytestmark = pytest.mark.skipif(not (SAMPLE_DIR / "manifest.json").exists(), reason="no sample/ bundled yet")


def test_every_sample_case_replays_to_its_recorded_decision(tmp_path):
    manifest = json.loads((SAMPLE_DIR / "manifest.json").read_text(encoding="utf-8"))
    settings = replace(get_settings(), data_backend="local", tables=dict(DEFAULT_TABLES))
    backend = LocalBackend(settings, prepared_dir=SAMPLE_DIR / "data")
    store = EvidenceStore(tmp_path / "evidence.duckdb")
    for alert in load_alerts(SAMPLE_DIR / "alerts.csv"):
        for mode in ("rules", "replay"):
            llm = ReplayLLM(SAMPLE_DIR / "recordings", settings=settings) if mode == "replay" else None
            if mode == "replay" and not llm.case_id_for(alert["alert_id"]):
                continue
            report = run_case(alert, backend=backend, store=store, settings=settings, llm=llm,
                              max_steps=manifest["max_steps"], out_dir=tmp_path / "cases")["report"]
            got = report["disposition"] + (" (needs review)" if report.get("needs_review") else "")
            assert got == manifest["decisions"][f"{alert['alert_id']}/{mode}"], (alert["alert_id"], mode)
            if llm is not None:
                assert llm.mismatches == [], f"{alert['alert_id']}: replay drifted from the recording"
    store.close()
    backend.close()
