# Input examples

What Casefile is given: one transaction-monitoring alert per case. Nothing else about the case is handed to the agent; everything it knows, it finds through its own queries.

`alerts.csv` is the 30-alert test set, exactly as the agent receives it (the same file as `eval/alerts.csv`; the labels are never in it):

| Column | Example (ALR-030) | Meaning |
| --- | --- | --- |
| `alert_id` | `ALR-030` | the case |
| `institution` | `A` | the bank that raised the alert (always the analyst's bank, A) |
| `bank_id`, `account` | `11`, `800924840` | the account under investigation |
| `alert_ts` | `2022-09-13 16:32` | when monitoring fired |
| `rules` | `PASS_THROUGH` | which monitoring rules tripped (fan-out, fan-in, pass-through, currency churn) |
| `trigger` | `pass-through activity: 15 senders in, 10 recipients out (rule: 5+ each way)` | the rule's own words |

## Run one

```bash
# No keys: the bundled sample replays the recorded run (sample/README.md)
SAMPLE_MODE=true python scripts/investigate.py ALR-030

# Live: Zetaris + Nightshift (see the main README's setup)
python scripts/investigate.py ALR-030
python scripts/investigate.py ALR-030 --rules     # no model: the deterministic baseline

# All 30, scored against the hidden labels
python scripts/run_eval.py --name my-run
```

In the app (`streamlit run app.py`), pick the alert from **Alert queue** and press **Investigate**.

The outputs these inputs produced are in [`../output_examples/`](../output_examples/README.md).
