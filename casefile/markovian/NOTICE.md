# Notice

`casefile/markovian` is a Python implementation of the **Markovian engine** specification by
[Meterless](https://github.com/meterless/meterless/tree/main/engines/markovian), licensed under the
Apache License, Version 2.0. It follows the spec's `AGENTS.md` contract and mirrors the logic of the
TypeScript reference implementation (config bounds and validation, marker protocol, compression cascade,
orchestrator loop, reflection, history with per-run config snapshots, and the efficiency model).

The conformance checks in `tests/test_markovian_conformance.py` are ported from the spec's
`conformance/runner.ts`, with the same check ids, inputs and expected values.

Changes from the reference: written in Python; the orchestrator fulfils `[NEEDS_TOOL]` pause markers
through a host tool handler and injects the result into the next chunk; chunks keep provider-reported
token usage under `measured`; the host may supply its own prompt builder (Casefile uses one for its
investigation steps).

A copy of the Apache License 2.0 is available at https://www.apache.org/licenses/LICENSE-2.0.
