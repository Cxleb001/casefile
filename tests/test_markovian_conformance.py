"""The Meterless Markovian conformance suite, ported check for check to Python.

Source: meterless/meterless engines/markovian/conformance/runner.ts. Same check
ids, same inputs, same expected numbers. The upstream runner imports a
TypeScript module; Casefile's engine is Python, so the checks run here against
casefile.markovian. A green run is the spec's definition of done.
"""

from __future__ import annotations

import pytest

from casefile.markovian import ConfigValidationError, Markovian, model_totals, run_efficiency

T0 = 1_750_000_000_000


def step_gen(total: int):
    calls = {"n": 0}

    def generator(prompt, attachments, system_prompt=None):
        calls["n"] += 1
        done = calls["n"] >= total
        body = f"Output for step {calls['n']} with enough text to register in the accounting math. " * 10
        marker = ("[TASK_COMPLETE]" if done else
                  f"[STATE_CHECKPOINT]\nCOMPLETED: steps 1-{calls['n']}\nREMAINING: {total - calls['n']}\n"
                  "DECISIONS: none\nCONTEXT: conformance")
        return {"text": body + "\n" + marker}

    return generator


def engine(**config):
    return Markovian({"chunkSize": 4000, "carryoverTokens": 640, **config}, clock=lambda: T0)


def test_eff_001_efficiency_model_reproduces_the_documented_worked_example():
    totals = model_totals(20)
    assert totals["naiveTotal"] == 246_000
    assert totals["markovianTotal"] == 34_000
    assert abs(totals["efficiency"] * 100 - 86.18) < 0.5


def test_snap_001_run_records_carry_a_mandatory_chunk_config_snapshot():
    run = engine().run("snapshot check", generator=step_gen(3), reflect=False)
    assert run.chunk_config is not None and run.chunk_config.carryover_tokens == 640
    assert run.to_dict()["chunkConfig"]["carryoverTokens"] == 640


def test_snap_002_historical_efficiency_uses_the_runs_own_snapshot():
    chunks = [{"id": i, "tokens": 1000, "carryover": "", "content": ""} for i in (1, 2, 3, 4)]
    a = run_efficiency(chunks, {"chunkSize": 8000, "maxChunks": 24, "carryoverTokens": 512, "overlapTokens": 0})
    b = run_efficiency(chunks, {"chunkSize": 8000, "maxChunks": 24, "carryoverTokens": 2048, "overlapTokens": 0})
    assert a["standardCost"] == 10_000  # prefix-sum formula
    assert a["markovianCost"] == 6_048  # 4*512 + 4000
    assert a["tokensSaved"] != b["tokensSaved"]


def test_ovl_001_overlap_is_implemented_never_accepted_and_ignored():
    prompts: list[str] = []

    def generator(prompt, attachments, system_prompt=None):
        prompts.append(prompt)
        done = len(prompts) >= 3
        return {"text": f"chunk {len(prompts)} content " * 20 +
                ("[TASK_COMPLETE]" if done else "[STATE_CHECKPOINT]\nCOMPLETED: chunk recorded with carried state")}

    engine(overlapTokens=64).run("overlap conformance", generator=generator, reflect=False)
    assert any("[OVERLAP]" in p for p in prompts[1:])


def test_lab_001_token_numbers_are_labelled():
    run = engine().run("label check", generator=step_gen(2), reflect=False)
    assert run.token_label in ("estimated", "measured")


def test_ref_001_reflection_failure_does_not_fail_the_run():
    calls = {"n": 0}

    def generator(prompt, attachments, system_prompt=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("provider exploded in reflection")
        return {"text": "done in one chunk [TASK_COMPLETE]"}

    run = engine().run("reflection failure", generator=generator)
    assert run.status == "completed"


def test_mrk_001_markers_are_cleaned_from_display_content_and_drive_completion():
    run = engine().run("marker cleaning", generator=step_gen(3), reflect=False)
    assert run.status == "completed"
    assert "STATE_CHECKPOINT" not in run.output and "TASK_COMPLETE" not in run.output
    assert len(run.chunks) == 3


def test_cfg_001_out_of_range_config_is_rejected_with_a_typed_error():
    with pytest.raises(ConfigValidationError):
        Markovian({"chunkSize": 128, "carryoverTokens": 640}, clock=lambda: T0)
