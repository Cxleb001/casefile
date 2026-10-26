# How Casefile uses the sponsor technology

Casefile is an anti-money-laundering investigator. It takes an alert from Institution A, follows the money into Institution B, and writes a suspicious-activity-report draft in which every claim cites the query and rows behind it, and a verifier checks every claim before an analyst reads it. Each sponsor does load-bearing work in that path; none is decorative.

| | What it does in Casefile | Where |
| --- | --- | --- |
| **Zetaris** | The only way the agent reads data: one federated SQL layer over both banks and a compliance store, with Institution B's personal data held back by a governed view | `casefile/data/zetaris.py`, `casefile/agent/tools.py`, `scripts/zetaris_apply.py` |
| **Meterless Markovian engine** | Runs the investigation: bounded steps with a compact carryover state instead of a growing transcript | `casefile/markovian/`, `casefile/agent/investigator.py` |
| **Meterless Nightshift** | The primary AI provider: every model call goes through it | `casefile/llm.py`, `scripts/check_nightshift.py` |
| **NVIDIA Nemotron** | The models, reached through Nightshift: a fast model plans each step, a large model writes and verifies the decision | `.env` model ids, `casefile/config.py` |

---

## Zetaris: federated, governed data

**The problem it solves.** Laundering crosses banks on purpose. Bank A sees money leave to an account at Bank B and nothing after that. Copying both banks' data into one place is exactly what privacy rules forbid. Casefile asks Zetaris instead.

**How it is wired.**

- Zetaris runs in Docker (`scripts/codespace/start_zetaris.sh`). The two banks' systems and a compliance store are three Postgres databases (`bank_a`, `bank_b`, `compliance`) in a sources container on Zetaris's network.
- `scripts/zetaris_apply.py` creates three datasources (`inst_a`, `inst_b`, `compliance`), registers five tables, and checks them through Zetaris's API, so setup needs no hand-typed SQL. Data loading is `scripts/publish_local.py load --yes`.
- Institution B shares customer records only as `inst_b.kyc_b_shared`, a view that runs **inside B's database** and drops `full_name`, `dob_or_incorporation` and `national_id`. Casefile never reads around it. When the view blocks a check (B's customers cannot be screened by name), the report says so under "What I could not check".
- `casefile/data/zetaris.py` sends each query to `POST /api/proxy/sql-editor/sqls/run-query` (the route from Zetaris's getting-started guides), with the login's bearer token and the organisation id. A Zetaris login lasts about 15 minutes, so the client reads the token's expiry, signs in again a minute early, and retries once if a session is dropped mid-run (`tests/test_zetaris_auth.py`).

**What the agent asks it.** Every tool is federated Spark SQL. `get_counterparties`, for example, joins A's ledger, B's ledger, A's customer records and B's governed view in one statement, so a counterparty at Bank B comes back with its type, country and risk rating but no name. `follow_money` traces two hops out and two hops back across both banks; `find_related_accounts` and the ring-closing query look for payments that bring money back to where it started.

**Why it matters, measured.** Every query is stored with its SQL, the rows it returned and its timing (`casefile/evidence.py`). That is what makes each claim clickable in the app and checkable by the verifier. The same agent with Institution B hidden (`run_eval.py --single-bank`) shows what Bank A could conclude alone; see the results table in the README.

**How to check it.** `python scripts/check_setup.py` runs a three-way federated query (A's ledger × B's ledger × B's governed view) and prints row counts for all five tables.

---

## Meterless Markovian engine: bounded investigation

**Which engine.** The Markovian engine from [meterless/meterless](https://github.com/meterless/meterless/tree/main/engines/markovian) (Apache-2.0), implemented in Python in `casefile/markovian/` following the spec's `AGENTS.md` contract (see `casefile/markovian/NOTICE.md`).

**What it does here.** An investigation is a sequence of follow-up questions: is this a payroll account? where did the money go next? did it come back? A naive agent appends every tool result to one growing transcript, so the prompt grows with each step and cost and latency grow with it. On the Markovian engine each step is a bounded chunk (`chunkSize` 4,000 tokens, `carryoverTokens` 800): the model sees the case brief, a compact carryover state (`COMPLETED / REMAINING / DECISIONS / CONTEXT`) and only the latest result, then either asks for a tool with a `[NEEDS_TOOL]` marker or finishes with `[TASK_COMPLETE]`. If the state outgrows its budget, the engine's compression cascade (marker → model → heuristic → truncation) shrinks it. A reflection pass at the end records what the run learned, and every run is saved with a snapshot of its configuration.

**The test that shows it works.**

- **Meterless's own conformance suite passes against our engine: 8 of 8 checks.** `bash scripts/conformance/run_meterless_conformance.sh` fetches the spec's `conformance/runner.ts` and runs it against Casefile's Python engine through a small bridge (`scripts/conformance/markovian_bridge.ts` → `markovian_bridge.py`).
- The same checks, ported with the same ids, inputs and expected values, run in `pytest` (`tests/test_markovian_conformance.py`: eff-001, snap-001, snap-002, ovl-001, lab-001, ref-001, mrk-001, cfg-001), plus 6 engine tests (`tests/test_markovian_engine.py`).
- In the scored run the prompt stays bounded: the first step carries the full investigation record (median 10,700 tokens, system prompt included); every later step, up to the eighth, stays under 8,600 tokens (medians 5,800 to 8,300) however many results came before, because each step sees only the brief, the carryover and the latest result. The app's **Trace** tab charts the prompt size per step and shows each step's carryover.

---

## Meterless Nightshift: the primary AI provider

**How Casefile connects.** `casefile/llm.py` follows Meterless's provider contract:

- `CASEFILE_LLM_PROVIDER=meterless`, base URL `http://127.0.0.1:8088/v1` (any pasted form is normalised to `<host>/v1`), and the bearer token from Nightshift's `proxy-token` file in `METERLESS_TOKEN`.
- Every request sends `meterless-app: casefile`; chat requests add `meterless-scope: user`.
- `python scripts/check_nightshift.py` auto-detects the runtime (ports 8088, 8090, 8787, 11434), decides the status from `GET /v1/models` (online, unauthorized or offline), lists the routing modes and models, and sends one test chat, printing which model served it.
- In a Codespace, a one-line SSH tunnel from the laptop (`gh codespace ssh -c <name> -- -N -R 8088:127.0.0.1:8088`) makes the laptop's Nightshift appear at `127.0.0.1:8088`; see [NIGHTSHIFT.md](NIGHTSHIFT.md).

**Routing.** Casefile pins each step to a model id that Nightshift serves (below) rather than a routing mode, because a step's job decides the model: many short planning steps need speed, the decision and its check need depth. Nightshift's routing modes (`meterless/auto`, `meterless/text.deep` …) also work by changing one line in `.env`.

**Logs.** Every call is logged with the step, the provider and the model Nightshift reports as having served it, latency and tokens: in the evidence store (`llm_calls`), in each run's `results.json`, and as a full request-and-answer record in `runs/eval/<run>/llm/<case>.jsonl`. Those recordings are also what sample mode replays without a key.

---

## NVIDIA: Nemotron, through Nightshift

The NVIDIA key lives **in Nightshift**, not in this project. Casefile asks Nightshift for NVIDIA models by id:

| Step | Model | Thinking | Why |
| --- | --- | --- | --- |
| `plan`: each Markovian investigation step | `nvidia/nemotron-3.5-lightning-30b-a3b` | off | many short tool choices; speed matters |
| `report`: decision and SAR draft | `nvidia/nemotron-3-ultra-550b-a55b` | medium | weighs the evidence and writes claims with citations |
| `verify`: checks every claim against its cited rows | `nvidia/nemotron-3-ultra-550b-a55b` | medium | the strictest reader |

Nemotron's reasoning switch is set per step through `chat_template_kwargs` (`enable_thinking`, `medium_effort`), so thinking tokens are spent only where they help; a model or gateway that rejects the switch is asked again without it (`tests/test_guardrails.py`).

**Where NVIDIA contributes.** The planner decides which innocent explanation to test next and asks its own federated queries. The report model writes the decision with a claim-by-claim evidence list. The verifier model reads each surviving claim next to its evidence and rejects any it does not support. In the scored run on 30 held-out alerts, 433 calls went through Nightshift, all served by the NVIDIA model each step asked for, with none failing; the verifier struck out 18 of 239 claims, and with both banks the model's own judgement agreed with the measured evidence on every case. Full results and the honest caveats are in the README.

---

## What is unique

- **Claims you can check, not prose you have to trust.** Every factual claim cites evidence ids; a deterministic check rejects any account, date or number not in the cited rows, and then a Nemotron verifier reads the claim against the evidence. Rejected claims stay visible, struck through.
- **The decision follows the evidence.** The structure Casefile measures in the money (rings, layering, scatter-gather, pass-through) sets the disposition. If the model leans the other way, the case is marked NEEDS REVIEW rather than silently overruled either way.
- **Federation with governance built in.** Two banks' data meet only in Zetaris queries, and Institution B's personal data never leaves B's view. The report states what governance prevented it from checking.
- **Replayable.** Every model call is recorded per case, and evidence ids are repeatable, so a recorded run plays back exactly: the bundled sample runs the full agent with no keys.
