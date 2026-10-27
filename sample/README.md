# Sample mode

This folder lets anyone run the whole of Casefile in about a minute, with **no Zetaris, no Nightshift and no keys**:

```bash
pip install -r requirements.txt -r requirements-ui.txt
SAMPLE_MODE=true streamlit run app.py                # the analyst view
SAMPLE_MODE=true python scripts/run_eval.py          # score the sample alerts
SAMPLE_MODE=true python scripts/investigate.py ALR-030  # one case in the terminal
```

On Windows PowerShell, set the variable first: `$env:SAMPLE_MODE="true"`, then run the command. You can also put `SAMPLE_MODE=true` in `.env`.

## What is in it

| Path | What |
| --- | --- |
| `alerts.csv` | 7 alerts from the 30-alert test set, chosen to show each kind of outcome (not a representative sample; the scores on all 30 are in the main README): four correct escalations (ALR-030 and ALR-022, gather-scatter; ALR-012, a four-hop ring that crosses into Bank B; ALR-004, a multi-pattern case), two hard negatives correctly closed (ALR-002, ALR-019), and one laundering case Casefile misses (ALR-007, scatter-gather) |
| `labels.csv` | Their ground truth, read only by `run_eval.py` after each case |
| `data/` | The two banks' ledgers, customer records and the watchlist, cut down to the accounts these cases touch (18,374 and 12,039 ledger rows, 155 and 141 customer records, 1,510 watchlist entries; 2.6 MB in all) |
| `recordings/` | Every model request and answer from the run `final-ai-federated-v2` (26 October 2026): Casefile on Zetaris, with NVIDIA Nemotron served through Meterless Nightshift |
| `manifest.json` | What was cut, the checks it passed, and each case's decision |
| `LICENSE-DATA.txt` | The data licence (CDLA-Sharing-1.0) |

## What runs, and what is replayed

Everything Casefile does runs for real: the standard workup, the Markovian investigation loop, every tool and SQL query (on DuckDB over the files in `data/`, with the same table names Zetaris uses and Institution B's customer records exposed only through the same view without personal data), the evidence store, the evidence and screening standards, and the deterministic verifier.

Only the model's answers are replayed. Each case's calls are answered from `recordings/` in the order the case asks them, and each answer still names the NVIDIA model that gave it. Because the data, the code and the case id are the same as in the recorded run, the agent asks the same questions in the same order, so the run plays back exactly: same follow-up queries, same cited evidence ids, same decision, claims and verdicts. `run_eval.py` prints how many answers matched their request exactly. For this sample it is 36 of 45; the other 9 match by order, because the recorded run read Zetaris, which prints timestamps as `2022-09-04 12:50:00.0` and can return tied rows in another order, while the sample reads DuckDB. The decisions are unaffected, and `tests/test_sample.py` fails if that count ever changes.

The **Investigate** button and `--rules` work too: rules-only decisions need no recording at all.

## How it was made, and checked

`scripts/build_sample.py` ran every sample alert on the full test data (rules-only and the replayed model run), collected every account the queries named or returned, and kept every transaction touching those accounts, their customer records, and the whole synthetic watchlist. It then ran everything again on the slice and would have refused to write it unless:

- every query returned the same rows as on the full data (242 queries, matched by evidence id);
- every case reached the same decision, typology, verdicts and screening result;
- the replay reached the decision recorded in the original run.

`tests/test_sample.py` repeats the decision check on every `pytest` run.

## Data notice

The transactions are a slice of IBM's AMLworld HI-Small dataset ([Kaggle](https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml); Altman et al., NeurIPS 2023), published by IBM under the **Community Data License Agreement – Sharing – Version 1.0**, and redistributed here under the same agreement (full text in `LICENSE-DATA.txt`).

**These files are changed from the original**: transactions were filtered to the accounts above, columns renamed and typed, transaction ids added, amounts kept as published, and the ledger split into two institutions by bank. Customer records and the watchlist are synthetic, generated for Casefile; no real person is described. See [`data/README.md`](../data/README.md) for how the full set is built.
