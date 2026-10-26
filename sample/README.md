# Sample mode

This folder lets anyone run the whole of Casefile in about a minute, with **no Zetaris, no Nightshift and no keys**:

```bash
pip install -r requirements.txt -r requirements-ui.txt
SAMPLE_MODE=true streamlit run app.py                # the analyst view
SAMPLE_MODE=true python scripts/run_eval.py          # score the sample alerts
SAMPLE_MODE=true python scripts/investigate.py {{FIRST_ALERT}}  # one case in the terminal
```

On Windows PowerShell, set the variable first: `$env:SAMPLE_MODE="true"`, then run the command. You can also put `SAMPLE_MODE=true` in `.env`.

## What is in it

| Path | What |
| --- | --- |
| `alerts.csv` | {{ALERT_COUNT}} alerts from the 30-alert test set: {{ALERT_LIST}} |
| `labels.csv` | Their ground truth, read only by `run_eval.py` after each case |
| `data/` | The two banks' ledgers, customer records and the watchlist, cut down to the accounts these cases touch ({{ROW_COUNTS}}) |
| `recordings/` | Every model request and answer from the run `{{RUN_NAME}}`: Casefile on Zetaris, with NVIDIA Nemotron served through Meterless Nightshift |
| `manifest.json` | What was cut, the checks it passed, and each case's decision |
| `LICENSE-DATA.txt` | The data licence (CDLA-Sharing-1.0) |

## What runs, and what is replayed

Everything Casefile does runs for real: the standard workup, the Markovian investigation loop, every tool and SQL query (on DuckDB over the files in `data/`, with the same table names Zetaris uses and Institution B's customer records exposed only through the same view without personal data), the evidence store, the evidence and screening standards, and the deterministic verifier.

Only the model's answers are replayed. Each case's calls are answered from `recordings/` in the order the case asks them, and each answer still names the NVIDIA model that gave it. Because the data, the code and the case id are the same as in the recorded run, the agent asks the same questions in the same order, so the run plays back exactly: same follow-up queries, same cited evidence ids, same decision and verdicts. `run_eval.py` prints how many answers matched their request exactly; a drift shows up there, not as a silent difference.

The **Investigate** button and `--rules` work too: rules-only decisions need no recording at all.

## How it was made, and checked

`scripts/build_sample.py` ran every sample alert on the full test data (rules-only and the replayed model run), collected every account the queries named or returned, and kept every transaction touching those accounts, their customer records, and the whole synthetic watchlist. It then ran everything again on the slice and would have refused to write it unless:

- every query returned the same rows as on the full data ({{QUERY_COUNT}} queries, matched by evidence id);
- every case reached the same decision, typology, verdicts and screening result;
- the replay reached the decision recorded in the original run.

`tests/test_sample.py` repeats the decision check on every `pytest` run.

## Data notice

The transactions are a slice of IBM's AMLworld HI-Small dataset ([Kaggle](https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml); Altman et al., NeurIPS 2023), published by IBM under the **Community Data License Agreement – Sharing – Version 1.0**, and redistributed here under the same agreement (full text in `LICENSE-DATA.txt`).

**These files are changed from the original**: transactions were filtered to the accounts above, columns renamed and typed, transaction ids added, amounts kept as published, and the ledger split into two institutions by bank. Customer records and the watchlist are synthetic, generated for Casefile; no real person is described. See [`data/README.md`](../data/README.md) for how the full set is built.
