# Data

Casefile runs on **IBM AMLworld**: synthetic bank transactions with complete laundering labels. Customer records and the watchlist are generated on top. There are no real people anywhere in this project.

## Source

| | |
| --- | --- |
| Dataset | IBM Transactions for Anti Money Laundering (AML), "AMLworld" |
| Where | Kaggle: <https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml> |
| Paper | Altman et al., *Realistic Synthetic Financial Transactions for Anti-Money Laundering Models*, NeurIPS 2023 Datasets and Benchmarks ([arXiv 2306.16424](https://arxiv.org/abs/2306.16424)) |
| Licence | Community Data License Agreement – Sharing – Version 1.0 (CDLA-Sharing-1.0). The full text is in [`sample/LICENSE-DATA.txt`](../sample/LICENSE-DATA.txt). |
| Subset used | **HI-Small** (the higher illicit ratio, small size): `HI-Small_Trans.csv`, `HI-Small_Patterns.txt`, and `HI-Small_accounts.csv` where Kaggle lists it |
| Size of the source | 5,078,345 transactions over 17.7 days, of which 5,177 are flagged as laundering |

The raw files are **not** in this repository; `scripts/codespace/get_data.sh` downloads them (you need a free Kaggle token). The only AMLworld data committed here is the small slice in [`sample/`](../sample/README.md), redistributed under the same licence.

## What Casefile builds from it

`scripts/prepare_data.py` turns the raw files into two banks' systems, an alert queue and hidden labels. It is deterministic: the same files and `--seed` give identical output.

1. **Two institutions.** The banks in AMLworld are split into Institution A and Institution B with roughly equal transaction volume. A transaction between them appears in both ledgers, as it would in real life.
2. **Alerts.** Four simple monitoring rules (fan-out, fan-in, pass-through, currency churn) are applied the same way to every Institution A account, with thresholds at the 99.9th percentile (fan-out 16 counterparties, fan-in 37, pass-through 5 each way, currency churn 2). That gives a pool of 893 alerts.
3. **A balanced test set.** 30 alerts are drawn from the pool: 15 laundering accounts and 15 "hard negatives" that trip the same rules but are innocent. Labels decide only which alerts are drawn, never what data is kept.
4. **The data around each alert.** All of the alert account's transactions, then capped random samples of its counterparties' transactions two hops out (at most 150, 40 and 8 per account at each hop), plus 50,000 random background transactions. The same procedure runs for both classes, so how much data exists says nothing about the answer.
5. **Customer records (KYC).** Generated with Faker: name, date of birth or incorporation, national ID, country, occupation or industry, opening date, declared turnover (sized from real volume), risk rating, PEP flag. They are random and label-blind.
6. **Watchlist.** 1,510 synthetic entries, each marked `SYNTHETIC watchlist generated for Casefile; not a real sanctions list`. About 20% of alert customers and 20% of counterparties get a planted exact or fuzzy match, chosen without looking at labels; some innocent customers have name-only matches, as in real screening.

| File (in `data/prepared/`) | Rows (test set, seed 42) | What it stands for |
| --- | --- | --- |
| `ledger_a.parquet` | 91,630 | Institution A's core-banking ledger |
| `kyc_a.parquet` | 38,161 | Institution A's customer records, complete |
| `ledger_b.parquet` | 90,468 | Institution B's ledger (56,606 transactions cross between the banks) |
| `kyc_b.parquet` | 40,878 | Institution B's customer records; partners only ever see a view without names, birth dates or ID numbers |
| `watchlist.csv` | 1,510 | Synthetic compliance watchlist |
| `../eval/alerts.csv` | 30 | What the agent is given |
| `../eval/labels.csv` | 30 | Ground truth: **never** committed, read only by `scripts/run_eval.py` after each case |

A second, separate batch was used for tuning: 80 alerts with `--alerts 80 --seed 7`, of which 16 share an account with the test set and are skipped (`run_eval.py --skip-accounts-in eval/alerts.csv`), leaving 64. The test set was not used to choose any threshold or prompt.

## How it is registered in Zetaris

The prepared files are loaded into one small Postgres container with three databases (`bank_a`, `bank_b`, `compliance`), standing in for the two banks' own systems and a compliance store. Zetaris then federates over them; Casefile reads nothing directly.

| Zetaris name | Datasource | What the agent sees |
| --- | --- | --- |
| `inst_a.ledger_a` | `inst_a` → Postgres `bank_a` | A's ledger |
| `inst_a.kyc_a` | `inst_a` → Postgres `bank_a` | A's full customer records |
| `inst_b.ledger_b` | `inst_b` → Postgres `bank_b` | B's ledger |
| `inst_b.kyc_b_shared` | a view **inside B's database** | B's customer records without `full_name`, `dob_or_incorporation`, `national_id` |
| `compliance.watchlist` | `compliance` → Postgres `compliance` | the synthetic watchlist |

Setup is scripted, with no manual SQL:

```bash
bash scripts/codespace/connect_sources.sh   # start the sources container on Zetaris's network
python scripts/publish_local.py load --yes  # load data/prepared into bank_a, bank_b, compliance
python scripts/zetaris_apply.py             # create the datasources, register the tables, check them
```

`python scripts/publish_local.py sql` prints the same registration statements (`scripts/zetaris/register_local.sql`) if you prefer to run them in the Zetaris SQL Editor. A cloud variant (Neon Postgres + S3 Parquet, `scripts/publish_sources.py`, `scripts/zetaris/register_sources.sql`) uses the same table names.

## Regenerate it

```bash
bash scripts/codespace/get_data.sh   # download HI-Small from Kaggle into data/raw
python scripts/prepare_data.py --trans data/raw/HI-Small_Trans.csv --patterns data/raw/HI-Small_Patterns.txt \
    --accounts data/raw/HI-Small_accounts.csv --memory-limit 6GB          # test set: 30 alerts, seed 42
python scripts/prepare_data.py --trans data/raw/HI-Small_Trans.csv --patterns data/raw/HI-Small_Patterns.txt \
    --accounts data/raw/HI-Small_accounts.csv --alerts 80 --seed 7 \
    --out data/work/dev/prepared --eval-dir data/work/dev/eval             # tuning batch
python scripts/check_setup.py --data
```

On an 8 GB machine keep the default `--memory-limit 2GB`; it is slower but fits.

## The bundled sample

[`sample/`](../sample/README.md) holds a slice of the test data around a few showcase alerts, plus the model answers recorded from a real run through Meterless Nightshift, so the full agent runs with `SAMPLE_MODE=true` and no keys. `scripts/build_sample.py` cuts it and refuses to write it unless every query returns the same rows as on the full data and every case reaches the same decision.

## Limitations

- **Synthetic transactions.** AMLworld is generated by a simulator of people, companies and banks. Its laundering typologies are realistic, but real payment mixes differ, and thresholds tuned here would need recalibrating on a real bank's data.
- **Synthetic customers and watchlist.** Declared turnover, risk ratings and watchlist hits carry no laundering signal by design, so the agent cannot "cheat" from them, and they cannot help it either.
- **A sample of the world.** Each alert keeps its own account's full history but only capped samples of counterparties' activity, so a ring that runs through a high-volume account can be partly missing, exactly as it would be for a bank that sees only part of the network.
- **Two institutions.** Real investigations span many banks and payment rails; this models the simplest case where federation matters.
- **Small test set.** 30 alerts give wide confidence intervals: one case moves accuracy by 3.3 points.
