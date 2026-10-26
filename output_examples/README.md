# Output examples

Everything here was produced by the scored runs of 26 October 2026: Casefile on Zetaris (both banks federated, or Bank A only), with NVIDIA Nemotron served through Meterless Nightshift. Nothing is edited. The inputs are in [`../input_examples/`](../input_examples/README.md).

## `runs/`: the four scored runs on the 30 test alerts

| Run | What | Folder |
| --- | --- | --- |
| Casefile, both banks | the full agent through Zetaris and Nightshift | `runs/final-ai-federated-v2/` |
| Casefile, Bank A only | the same, with Institution B's ledger and customer view hidden | `runs/final-ai-bank-a-v2/` |
| Rules only, both banks | the measured structure with no model | `runs/final-rules-federated-v2/` |
| Rules only, Bank A only | the same, Bank A only | `runs/final-rules-bank-a-v2/` |

Each folder has `run.log` (the console output: one line per alert, then the scores) and `results.json` (the scores, and per alert: truth, decision, typology, the model's own leaning, sanctions, claims and rejections, timing, and which models ran each step).

## `cases/`: case files, as an analyst would receive them

| File | Case |
| --- | --- |
| `ALR-030-…` | **Escalated, correct.** Gather-scatter: 12 one-off inbound ACH payments and 5 one-off outbound within three days, passed on. 13 tool calls by the investigator, 26 queries. The verifier (Nemotron Ultra) **rejected one claim** whose transaction counts were not in the cited rows; it stays in the report, struck through. |
| `ALR-012-…` | **Escalated, correct.** A four-hop ring that leaves Bank A, crosses into Bank B (`121:8000E1590`) and returns, with $18.2M out against $0.8M in. |
| `ALR-012-…-bank-a-only` | **The same alert with Bank B hidden.** The model concluded two legs of the ring did not exist and leaned CLOSE; the verifier rejected that claim, the measured ring still met the evidence standard, and the case was escalated **for review**. Compare it with the file above. |
| `ALR-022-…` | **Escalated, correct.** Gather-scatter, $112.9M to 16 counterparties in 10 days; all 8 claims verified. |
| `ALR-019-…` | **Closed, correct.** A hard negative: it trips the same monitoring rule, and Casefile explains why the activity is ordinary. |

Each case has two files:

- `.report.json`: the decision, typology, confidence, every claim with its evidence ids and verdict (which layer and which model checked it, and why), evidence against, what would change the decision, the sanctions referral and counterparty watchlist matches, limitations, the measured structure and decision basis, the full investigation trace (each Markovian step, its carryover state and prompt size, the engine summary and reflection), timing, and the data backend.
- `.sar.md`: the SAR draft. Findings with verdicts, then an evidence appendix with the SQL of every query, which backend ran it, how long it took, and the rows it returned.

## `model-calls/`: every model request and answer

- `all-calls.csv`: one row per model call in both AI runs (433 calls): run, case, step, provider (`meterless` = Nightshift), the model Nightshift reports as having served it, latency and tokens. Every call went through Nightshift; planning was served by `nvidia/nemotron-3.5-lightning-30b-a3b` (median 8.6 s), the report and the verifier by `nvidia/nemotron-3-ultra-550b-a55b` (median 43 s and 30 s). No call failed.
- `final-ai-federated-v2/<case>.jsonl`: the full record of each case's calls in the federated run: the messages sent, the options (temperature, token cap, thinking switch), the answer, the model's reasoning when it returned one, latency and tokens. `final-ai-bank-a-v2/` has ALR-012's calls from the Bank A only run.

These recordings are what sample mode replays: `sample/recordings/` holds seven of them.
