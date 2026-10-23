# Casefile

An agent that investigates money-laundering alerts across two institutions' data through Zetaris, and writes an evidence-linked report an analyst can file. Open Agent Hackathon 2026, Track 2: The Agent That Can Explain Why.

> Status: Day 1 scaffold. Data prep, the data layer (Zetaris and a local stand-in), the model client and the evidence store work. The agent loop, the verifier, the UI and the eval come next.

## How it fits together

| Source | What it is | Where it lives | Zetaris name |
| --- | --- | --- | --- |
| Institution A ledger | Every transaction touching A's accounts | Postgres | `inst_a.ledger_a` |
| Institution A KYC | A's customer records, full detail | Postgres | `inst_a.kyc_a` |
| Institution B ledger | Every transaction touching B's accounts | S3 Parquet | `inst_b.ledger_b` |
| Institution B KYC | Shared **without PII** through a governed view | S3 Parquet + view | `shared.kyc_b` |
| Watchlist | Synthetic sanctions-style list | S3 CSV | `compliance.watchlist` |

The analyst works at Institution A. Laundering flows cross into Institution B, so following the money takes a federated query, and B's customer names stay hidden. The agent reports that as a limit on what it could check.

Data comes from IBM's AMLworld (synthetic transactions with complete laundering labels, [paper](https://arxiv.org/abs/2306.16424)). KYC records and the watchlist are generated, so there are no real people anywhere.

```
casefile/
  casefile/
    config.py         settings from .env
    llm.py            one client for NVIDIA's API or Meterless, model chosen per step
    evidence.py       every query, model call and claim, in runs/casefile.duckdb
    data/
      base.py         QueryResult and get_backend()
      zetaris.py      SQL over Zetaris REST
      local.py        DuckDB stand-in with the same table names
      schema.py       column layouts shared by everything
  scripts/
    make_sample_data.py   small fake AMLworld-format data, for testing
    prepare_data.py       AMLworld -> sources, alerts, hidden labels
    publish_sources.py    load Postgres, upload to S3, print the Zetaris SQL
    check_setup.py        today's gate: one federated query + one Nemotron reply
    zetaris/register_sources.sql
  eval/
    alerts.csv            what the agent gets (generated)
    labels.csv            ground truth, eval only (generated, gitignored)
```

## Setup

Python 3.10 or later. From the project folder:

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux:    source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
```

Test the pipeline on fake data first (a minute or two):

```bash
python scripts/make_sample_data.py --out data/raw/sample
python scripts/prepare_data.py --trans data/raw/sample/SAMPLE_Trans.csv --patterns data/raw/sample/SAMPLE_Patterns.txt --accounts data/raw/sample/SAMPLE_accounts.csv
python scripts/check_setup.py --data
```

## Today: one federated query and one Nemotron reply

Your machine has 8 GB of RAM. Zetaris recommends giving Docker 8 GB for a local install (its preflight fails below 6 GB), so Zetaris runs as a small cluster in your AWS account instead.

1. **Nemotron key (5 min).** Create a key at [build.nvidia.com](https://build.nvidia.com/settings/api-keys) and put it in `.env` as `NVIDIA_API_KEY`. Then:
   ```bash
   python scripts/check_setup.py --models   # Nemotron ids your key can call
   python scripts/check_setup.py --model    # one reply
   ```
2. **Real data (10 min).** From the [IBM AML dataset on Kaggle](https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml), download `HI-Small_Trans.csv`, `HI-Small_Patterns.txt` and, if it's listed, `HI-Small_accounts.csv` into `data/raw/`. Then:
   ```bash
   python scripts/prepare_data.py --trans data/raw/HI-Small_Trans.csv --patterns data/raw/HI-Small_Patterns.txt --accounts data/raw/HI-Small_accounts.csv
   ```
3. **Zetaris on AWS (about 1 hour, mostly waiting).** Follow Zetaris's [AWS Free Tier checklist](https://zetaris.github.io/getting-started/docs/install/aws-free-tier-checklist.html). A new AWS account gets up to $200 in credits. Then follow the [managed cluster guide](https://zetaris.github.io/getting-started/docs/install/managed-aws-cluster-guide.html). Zetaris measured a Mini cluster running non-stop for a week at about $80, so suspend it when you stop for the night. In the cluster's UI:
   - Copy the REST API base (ends in `/api/v1.0`) into `ZETARIS_REST_URL`.
   - Copy **Management → Organisation → ID** into `ZETARIS_ORG_ID`.
   - Create a key under **Management → API Tokens** and put it in `ZETARIS_API_KEY`.
4. **Institution A's Postgres (5 min).** A free [Neon](https://neon.tech) database works. Put its connection string in `PG_URL`.
5. **Institution B's bucket (15 min).** Create an S3 bucket in the cluster's region and put its name in `S3_BUCKET`. For Zetaris to read it, create an IAM user with read-only access to that bucket; its keys go into the registration SQL, not into `.env`.
6. **Publish and register.**
   ```bash
   python scripts/publish_sources.py postgres --yes
   python scripts/publish_sources.py s3 --yes
   python scripts/publish_sources.py sql > register.sql
   ```
   Run `register.sql` one statement at a time in the Zetaris SQL Editor, typing the `<PLACEHOLDER>` secrets there. Then set `CASEFILE_DATA_BACKEND=zetaris` and run:
   ```bash
   python scripts/check_setup.py
   ```
   It passes when the federated query (A's ledger × B's ledger × B's KYC view) returns rows and Nemotron replies.

Until step 6 is done, keep `CASEFILE_DATA_BACKEND=local` and build against the stand-in; the agent code doesn't change.

## How the eval stays honest

- The laundering flag never reaches a source the agent can read. It exists only in `eval/labels.csv`.
- Alerts come from four simple monitoring rules (fan-out, fan-in, pass-through, currency churn), applied the same way to every account. Half the alerts are laundering accounts and half are "hard negatives" that trip the same rules.
- The transactions kept around each alert are chosen the same way for both kinds of alert, so data volume gives nothing away.
- KYC attributes and planted watchlist hits are random and label-blind. Some innocent customers have name-only watchlist matches, as in real screening.
- `prepare_data.py` is deterministic: the same inputs and `--seed` give identical files.

## Models

| Step | Used for | Default (NVIDIA API) |
| --- | --- | --- |
| `fast` | Extraction, row summaries | `nvidia/nvidia-nemotron-nano-9b-v2` |
| `plan` | The investigation loop | `nvidia/nemotron-3-super-120b-a12b` |
| `verify` | Verifier and final report | `nvidia/nemotron-3-super-120b-a12b` |

Override any of them in `.env`. To route through Meterless instead, set `CASEFILE_LLM_PROVIDER=meterless` and `METERLESS_TOKEN`; its model ids are routing modes such as `meterless/text.deep`.
