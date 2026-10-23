"""Turn IBM AMLworld files into Casefile's sources, alerts and hidden labels.

What it produces (all synthetic; no real people):

    data/prepared/ledger_a.parquet   Institution A's ledger   -> Postgres (Zetaris datasource)
    data/prepared/kyc_a.parquet      Institution A's KYC      -> Postgres
    data/prepared/ledger_b.parquet   Institution B's ledger   -> S3 Parquet
    data/prepared/kyc_b.parquet      Institution B's KYC      -> S3, shared only via a view without PII
    data/prepared/watchlist.csv      synthetic watchlist      -> S3 CSV
    data/prepared/manifest.json      counts, thresholds, settings
    eval/alerts.csv                  what the agent is given
    eval/labels.csv                  ground truth, used ONLY by the eval script

How it stays honest:
  * The Is Laundering flag never leaves this script except in eval/labels.csv.
  * Alerts come from simple monitoring rules applied the same way to every
    account; labels are used only to balance the eval set (half suspicious,
    half "hard negatives" that trip the same rules).
  * The data kept around each alert is chosen the same way for both classes
    (all of the alert account's transactions, then capped random samples of
    its counterparties' transactions), so how much data exists says nothing
    about the answer.
  * KYC attributes and planted watchlist hits are random and label-blind.

Usage:
    python scripts/prepare_data.py --trans data/raw/HI-Small_Trans.csv \
        --patterns data/raw/HI-Small_Patterns.txt --accounts data/raw/HI-Small_accounts.csv
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from faker import Faker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from casefile.data.schema import KYC_COLUMNS, LEDGER_COLUMNS, WATCHLIST_COLUMNS  # noqa: E402

TRANS_NAMES = ["ts_raw", "fb", "fa", "tb", "ta", "ar", "rc", "ap", "pc", "pf", "il"]
RULE_ORDER = ["FAN_OUT", "FAN_IN", "PASS_THROUGH", "FX_CHURN"]
QUANTILES = [0.999, 0.998, 0.995, 0.99, 0.98, 0.95, 0.90, 0.80]
PATTERN_RE = re.compile(r"BEGIN LAUNDERING ATTEMPT\s*-\s*([A-Z][A-Z\-]*)", re.IGNORECASE)

# Approximate 2022 USD values, only used to size declared turnover in KYC.
USD_RATE = {
    "US Dollar": 1.0, "Euro": 1.0, "UK Pound": 1.15, "Yuan": 0.145, "Yen": 0.0072, "Rupee": 0.0125,
    "Ruble": 0.016, "Canadian Dollar": 0.75, "Australian Dollar": 0.68, "Swiss Franc": 1.03,
    "Mexican Peso": 0.05, "Brazil Real": 0.19, "Shekel": 0.29, "Saudi Riyal": 0.27, "Bitcoin": 20000.0,
}
CURRENCY_COUNTRY = {
    "US Dollar": ["US"], "Euro": ["DE", "FR", "IT", "ES", "NL"], "UK Pound": ["GB"], "Yuan": ["CN"],
    "Yen": ["JP"], "Rupee": ["IN"], "Ruble": ["RU"], "Canadian Dollar": ["CA"], "Australian Dollar": ["AU"],
    "Swiss Franc": ["CH"], "Mexican Peso": ["MX"], "Brazil Real": ["BR"], "Shekel": ["IL"],
    "Saudi Riyal": ["SA"],
}
COUNTRY_LOCALE = {
    "US": "en_US", "GB": "en_GB", "DE": "de_DE", "FR": "fr_FR", "IT": "it_IT", "ES": "es_ES",
    "NL": "nl_NL", "IN": "en_IN", "CA": "en_CA", "AU": "en_AU", "CH": "de_CH", "MX": "es_MX",
    "BR": "pt_BR", "IE": "en_IE", "TR": "tr_TR",
}
FALLBACK_COUNTRIES = ["US", "GB", "DE", "FR", "IT", "ES", "NL", "CH", "IE", "TR", "AE", "SG", "HK", "PA", "CY"]
INDUSTRIES = [
    "Wholesale trade", "Import / export", "Construction", "Real estate", "Restaurants", "Car dealership",
    "Logistics", "Consulting", "Software", "Retail clothing", "Precious metals", "Money services",
    "Travel agency", "Healthcare clinic", "Agriculture", "Electronics distribution", "Shipping",
]
PROGRAMS = ["SYN-FINANCIAL-SANCTIONS", "SYN-COUNTER-NARCOTICS", "SYN-ANTI-CORRUPTION", "SYN-ORGANISED-CRIME"]
WATCHLIST_SOURCE = "SYNTHETIC watchlist generated for Casefile; not a real sanctions list"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def load_transactions(con: duckdb.DuckDBPyConnection, trans: Path) -> None:
    names = ", ".join(f"'{n}'" for n in TRANS_NAMES)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE tx AS
        SELECT
            'T' || substr(md5(concat_ws('|', ts_raw, fb, fa, tb, ta, ap, pc, ar, rc, pf, CAST(rn AS VARCHAR))), 1, 16) AS tx_id,
            strptime(trim(ts_raw), '%Y/%m/%d %H:%M') AS ts,
            CAST(trim(fb) AS BIGINT) AS from_bank,
            trim(fa) AS from_account,
            CAST(trim(tb) AS BIGINT) AS to_bank,
            trim(ta) AS to_account,
            CAST(ap AS DOUBLE) AS amount_paid,
            trim(pc) AS payment_currency,
            CAST(ar AS DOUBLE) AS amount_received,
            trim(rc) AS receiving_currency,
            trim(pf) AS payment_format,
            CAST(trim(il) AS INTEGER) AS is_laundering
        FROM (
            SELECT *, row_number() OVER () AS rn
            FROM read_csv('{sql_path(trans)}', header = true, all_varchar = true, names = [{names}])
        )
        """
    )


def parse_patterns(path: Path | None) -> pd.DataFrame:
    """Read laundering attempts from an AMLworld *_Patterns.txt file."""
    columns = ["attempt_id", "pattern_type", "from_bank", "from_account", "to_bank", "to_account"]
    if path is None:
        return pd.DataFrame(columns=columns)
    rows, attempt, kind = [], 0, None
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            match = PATTERN_RE.match(line)
            if match:
                attempt += 1
                kind = match.group(1).upper().rstrip("-")
                continue
            if line.upper().startswith("END LAUNDERING ATTEMPT"):
                kind = None
                continue
            if kind is None:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            try:
                rows.append((attempt, kind, int(parts[1]), parts[2], int(parts[3]), parts[4]))
            except ValueError:
                continue
    return pd.DataFrame(rows, columns=columns)


def load_accounts(con: duckdb.DuckDBPyConnection, path: Path | None) -> bool:
    if path is None:
        return False
    con.execute(
        f"""
        CREATE OR REPLACE TABLE account_owner AS
        SELECT DISTINCT
            CAST(trim("Bank ID") AS BIGINT) AS bank,
            trim("Account Number") AS account,
            trim("Entity ID") AS entity_id,
            trim("Entity Name") AS entity_name
        FROM read_csv('{sql_path(path)}', header = true, all_varchar = true)
        """
    )
    return True


# ---------------------------------------------------------------------------
# Institutions, account features, alerts
# ---------------------------------------------------------------------------

def assign_institutions(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Split banks into Institution A and B with roughly equal transaction volume."""
    volume = con.execute(
        """
        SELECT bank, count(*) AS n FROM (
            SELECT from_bank AS bank FROM tx UNION ALL SELECT to_bank FROM tx
        ) GROUP BY bank ORDER BY n DESC, bank
        """
    ).fetchdf()
    totals = {"A": 0, "B": 0}
    institution = []
    for n in volume["n"]:
        side = "A" if totals["A"] <= totals["B"] else "B"
        totals[side] += int(n)
        institution.append(side)
    volume["institution"] = institution
    return volume[["bank", "institution"]]


def build_account_features(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE OR REPLACE TABLE ends AS
        SELECT tx_id, ts, is_laundering, from_bank AS bank, from_account AS account, 'out' AS dir,
               to_bank AS cp_bank, to_account AS cp_account,
               payment_currency <> receiving_currency AS fx,
               amount_paid AS amount, payment_currency AS currency
        FROM tx
        UNION ALL
        SELECT tx_id, ts, is_laundering, to_bank, to_account, 'in', from_bank, from_account,
               payment_currency <> receiving_currency, amount_received, receiving_currency
        FROM tx
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE acct AS
        SELECT bank, account,
            count(DISTINCT CASE WHEN dir = 'out' AND NOT self THEN cp_account END) AS out_degree,
            count(DISTINCT CASE WHEN dir = 'in'  AND NOT self THEN cp_account END) AS in_degree,
            sum(CASE WHEN dir = 'out' AND NOT self THEN 1 ELSE 0 END) AS out_n,
            sum(CASE WHEN dir = 'in'  AND NOT self THEN 1 ELSE 0 END) AS in_n,
            sum(CASE WHEN fx AND NOT self THEN 1 ELSE 0 END) AS fx_n,
            sum(is_laundering) AS n_laund,
            min(ts) AS first_ts,
            max(ts) AS last_ts
        FROM (SELECT *, (bank = cp_bank AND account = cp_account) AS self FROM ends)
        GROUP BY bank, account
        """
    )


def account_labels(con: duckdb.DuckDBPyConnection, has_patterns: bool) -> None:
    if has_patterns:
        con.execute(
            """
            CREATE OR REPLACE TABLE acct_label AS
            SELECT a.bank, a.account, a.n_laund,
                   CASE WHEN p.account IS NOT NULL THEN 1 WHEN a.n_laund = 0 THEN 0 ELSE NULL END AS label,
                   p.pattern_types, p.attempt_ids
            FROM acct a
            LEFT JOIN (
                SELECT bank, account,
                       string_agg(DISTINCT pattern_type, '|' ORDER BY pattern_type) AS pattern_types,
                       string_agg(DISTINCT CAST(attempt_id AS VARCHAR), '|' ORDER BY CAST(attempt_id AS VARCHAR)) AS attempt_ids
                FROM pattern_accounts GROUP BY bank, account
            ) p USING (bank, account)
            """
        )
    else:
        con.execute(
            """
            CREATE OR REPLACE TABLE acct_label AS
            SELECT bank, account, n_laund,
                   CASE WHEN n_laund >= 2 THEN 1 WHEN n_laund = 0 THEN 0 ELSE NULL END AS label,
                   CASE WHEN n_laund >= 2 THEN 'UNKNOWN' END AS pattern_types,
                   NULL AS attempt_ids
            FROM acct
            """
        )


def rule_thresholds(features: pd.DataFrame, q: float) -> dict[str, int]:
    active = features[(features.out_n + features.in_n) > 0]
    pass_through = np.minimum(active.in_degree, active.out_degree)
    return {
        "FAN_OUT": max(3, int(np.ceil(active.out_degree.quantile(q)))),
        "FAN_IN": max(3, int(np.ceil(active.in_degree.quantile(q)))),
        "PASS_THROUGH": max(2, int(np.ceil(pass_through.quantile(q)))),
        "FX_CHURN": max(2, int(np.ceil(active.fx_n.quantile(q)))),
    }


def apply_rules(features: pd.DataFrame, thresholds: dict[str, int]) -> pd.Series:
    fired = pd.DataFrame(
        {
            "FAN_OUT": features.out_degree >= thresholds["FAN_OUT"],
            "FAN_IN": features.in_degree >= thresholds["FAN_IN"],
            "PASS_THROUGH": (features.in_degree >= thresholds["PASS_THROUGH"])
            & (features.out_degree >= thresholds["PASS_THROUGH"]),
            "FX_CHURN": features.fx_n >= thresholds["FX_CHURN"],
        },
        index=features.index,
    )
    return fired.apply(lambda row: [r for r in RULE_ORDER if row[r]], axis=1)


def trigger_text(row: pd.Series, thresholds: dict[str, int]) -> str:
    parts = []
    for rule in row["rules"]:
        if rule == "FAN_OUT":
            parts.append(f"sent funds to {int(row.out_degree)} distinct counterparties (rule: {thresholds[rule]}+)")
        elif rule == "FAN_IN":
            parts.append(f"received funds from {int(row.in_degree)} distinct counterparties (rule: {thresholds[rule]}+)")
        elif rule == "PASS_THROUGH":
            parts.append(
                f"pass-through activity: {int(row.in_degree)} senders in, {int(row.out_degree)} recipients out "
                f"(rule: {thresholds[rule]}+ each way)"
            )
        elif rule == "FX_CHURN":
            parts.append(f"{int(row.fx_n)} cross-currency transfers (rule: {thresholds[rule]}+)")
    return "; ".join(parts)


def select_alerts(con, n_alerts: int, rng: np.random.Generator):
    features = con.execute(
        """
        SELECT a.*, l.label, l.pattern_types, l.attempt_ids, i.institution
        FROM acct a
        JOIN acct_label l USING (bank, account)
        JOIN institutions i USING (bank)
        WHERE i.institution = 'A'
        ORDER BY a.bank, a.account
        """
    ).fetchdf()
    n_pos = n_alerts // 2
    n_neg = n_alerts - n_pos

    chosen_q, thresholds, pool = None, None, None
    for q in QUANTILES:
        thresholds = rule_thresholds(features, q)
        rules = apply_rules(features, thresholds)
        candidate = features.assign(rules=rules)[rules.map(len) > 0]
        positives = candidate[candidate.label == 1]
        negatives = candidate[candidate.label == 0]
        if len(positives) >= n_pos and len(negatives) >= n_neg:
            chosen_q, pool = q, candidate
            break
    if pool is None:
        raise SystemExit(
            "Not enough suspicious accounts at Institution A trip the monitoring rules. "
            "Use a larger dataset or lower --alerts."
        )

    pool = pool.copy()
    pool["primary_rule"] = pool["rules"].map(lambda r: r[0])

    # Positives: spread across laundering pattern types, one account per attempt where possible.
    positives = pool[pool.label == 1].copy()
    positives["first_type"] = positives["pattern_types"].fillna("UNKNOWN").str.split("|").str[0]
    positives = positives.iloc[rng.permutation(len(positives))]
    by_type = {t: g for t, g in positives.groupby("first_type", sort=True)}
    picked_pos, used_attempts = [], set()
    while len(picked_pos) < n_pos and any(len(g) for g in by_type.values()):
        for kind in sorted(by_type):
            group = by_type[kind]
            if len(picked_pos) >= n_pos or group.empty:
                continue
            fresh = group[group["attempt_ids"].fillna("").map(
                lambda ids: not (set(filter(None, ids.split("|"))) & used_attempts))]
            take = fresh.iloc[0] if not fresh.empty else group.iloc[0]
            picked_pos.append(take)
            used_attempts.update(filter(None, str(take["attempt_ids"] or "").split("|")))
            by_type[kind] = group.drop(index=take.name)

    # Negatives: same primary rule as a positive wherever possible ("hard negatives").
    negatives = pool[pool.label == 0]
    negatives = negatives.iloc[rng.permutation(len(negatives))]
    picked_neg, taken = [], set()
    for pos in picked_pos:
        same = negatives[(negatives.primary_rule == pos.primary_rule) & ~negatives.index.isin(taken)]
        if same.empty:
            same = negatives[~negatives.index.isin(taken)]
        if same.empty:
            break
        taken.add(same.index[0])
        picked_neg.append(same.iloc[0])
    while len(picked_neg) < n_neg:
        rest = negatives[~negatives.index.isin(taken)]
        if rest.empty:
            break
        taken.add(rest.index[0])
        picked_neg.append(rest.iloc[0])

    alerts = pd.DataFrame(picked_pos + picked_neg)
    alerts = alerts.iloc[rng.permutation(len(alerts))].reset_index(drop=True)
    alerts["alert_id"] = [f"ALR-{i + 1:03d}" for i in range(len(alerts))]
    alerts["trigger"] = alerts.apply(trigger_text, axis=1, thresholds=thresholds)
    return alerts, thresholds, chosen_q, len(pool)


# ---------------------------------------------------------------------------
# Neighbourhood subset
# ---------------------------------------------------------------------------

def build_subset(con, alerts: pd.DataFrame, hops: int, caps: list[int], background: int, seed: int) -> None:
    con.register("seeds_df", alerts[["bank", "account"]])
    con.execute("CREATE OR REPLACE TABLE seeds AS SELECT DISTINCT CAST(bank AS BIGINT) AS bank, account FROM seeds_df")
    con.execute("CREATE OR REPLACE TABLE seen AS SELECT bank, account FROM seeds")
    # Hop 0: every transaction of every alert account, uncapped.
    con.execute(
        "CREATE OR REPLACE TABLE sub AS SELECT DISTINCT e.tx_id FROM ends e JOIN seeds s USING (bank, account)"
    )
    frontier = "seeds"
    for hop in range(1, hops + 1):
        cap = caps[min(hop - 1, len(caps) - 1)]
        con.execute(
            f"""
            CREATE OR REPLACE TABLE frontier_next AS
            SELECT DISTINCT e.cp_bank AS bank, e.cp_account AS account
            FROM ends e JOIN {frontier} f USING (bank, account)
            JOIN sub USING (tx_id)
            ANTI JOIN seen s ON s.bank = e.cp_bank AND s.account = e.cp_account
            """
        )
        con.execute(
            f"""
            INSERT INTO sub
            SELECT DISTINCT tx_id FROM (
                SELECT e.tx_id,
                       row_number() OVER (PARTITION BY e.bank, e.account ORDER BY md5(e.tx_id || '{seed}')) AS rn
                FROM ends e JOIN frontier_next f USING (bank, account)
            ) WHERE rn <= {int(cap)}
            """
        )
        con.execute("CREATE OR REPLACE TABLE sub AS SELECT DISTINCT tx_id FROM sub")
        con.execute("INSERT INTO seen SELECT * FROM frontier_next")
        con.execute(f"CREATE OR REPLACE TABLE frontier_{hop} AS SELECT * FROM frontier_next")
        frontier = f"frontier_{hop}"
    if background > 0:
        con.execute(
            f"""
            INSERT INTO sub
            SELECT tx_id FROM tx ORDER BY md5(tx_id || 'bg{seed}') LIMIT {int(background)}
            """
        )
    con.execute("CREATE OR REPLACE TABLE sub AS SELECT DISTINCT tx_id FROM sub")


# ---------------------------------------------------------------------------
# KYC and watchlist (synthetic, label-blind)
# ---------------------------------------------------------------------------

class People:
    """Deterministic synthetic names per country."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._fakers: dict[str, Faker] = {}

    def faker(self, country: str) -> Faker:
        locale = COUNTRY_LOCALE.get(country, "en_US")
        if locale not in self._fakers:
            fake = Faker(locale)
            fake.seed_instance(f"{self.seed}-{locale}")
            self._fakers[locale] = fake
        return self._fakers[locale]

    def name(self, customer_type: str, country: str) -> str:
        fake = self.faker(country)
        if customer_type == "Individual":
            return fake.name()
        if customer_type == "Sole Proprietorship":
            return f"{fake.last_name()} {fake.random_element(['Trading', 'Services', 'Enterprises', 'Supplies'])}"
        if customer_type == "Partnership":
            return f"{fake.last_name()} & {fake.last_name()} {fake.random_element(['LLP', 'Partners', 'Associates'])}"
        return fake.company()


def entity_type_from_name(name: str | None, rng: np.random.Generator) -> str:
    if isinstance(name, str) and "#" in name:
        kind = name.split("#")[0].strip()
        if kind:
            return kind
    return str(rng.choice(["Corporation", "Sole Proprietorship", "Partnership", "Individual"],
                          p=[0.45, 0.25, 0.10, 0.20]))


def build_kyc(con, institutions: pd.DataFrame, has_owner: bool, rng, people: People, days: float) -> pd.DataFrame:
    owner_join = "LEFT JOIN account_owner o USING (bank, account)" if has_owner else ""
    owner_cols = "o.entity_id, o.entity_name" if has_owner else "NULL AS entity_id, NULL AS entity_name"
    rate_cases = " ".join(f"WHEN '{c}' THEN {r}" for c, r in USD_RATE.items())
    accounts = con.execute(
        f"""
        WITH sub_accounts AS (
            SELECT DISTINCT from_bank AS bank, from_account AS account FROM tx JOIN sub USING (tx_id)
            UNION
            SELECT DISTINCT to_bank, to_account FROM tx JOIN sub USING (tx_id)
        ),
        by_currency AS (
            SELECT bank, account, currency, count(*) AS n,
                   sum(amount * CASE currency {rate_cases} ELSE 1.0 END) AS usd
            FROM ends WHERE dir = 'out' GROUP BY bank, account, currency
        ),
        volume AS (
            SELECT bank, account,
                   round(sum(usd), 2) AS usd_volume,
                   first(currency ORDER BY n DESC, currency) AS main_currency
            FROM by_currency GROUP BY bank, account
        )
        SELECT s.bank, s.account, {owner_cols}, v.usd_volume, v.main_currency, i.institution
        FROM sub_accounts s
        LEFT JOIN volume v USING (bank, account)
        JOIN institutions i USING (bank)
        {owner_join}
        ORDER BY s.bank, s.account
        """
    ).fetchdf()

    records = []
    month_factor = 30.0 / max(days, 1.0)
    for row in accounts.itertuples(index=False):
        customer_type = entity_type_from_name(row.entity_name, rng)
        countries = CURRENCY_COUNTRY.get(row.main_currency) or FALLBACK_COUNTRIES
        country = str(rng.choice(countries))
        fake = people.faker(country)
        name = people.name(customer_type, country)
        if customer_type == "Individual":
            born = fake.date_of_birth(minimum_age=19, maximum_age=85)
            national_id = f"ID-{rng.integers(10**8, 10**9)}"
            occupation = fake.job()
        else:
            born = date(1970, 1, 1) + timedelta(days=int(rng.integers(0, 51 * 365)))
            national_id = f"REG-{rng.integers(10**7, 10**8)}"
            occupation = str(rng.choice(INDUSTRIES))
        opened = date(2005, 1, 1) + timedelta(days=int(rng.integers(0, 17 * 365 + 200)))
        reviewed = opened + timedelta(days=int(rng.integers(0, max(1, (date(2022, 8, 31) - opened).days))))
        observed = float(row.usd_volume or 0.0) * month_factor
        declared = max(100.0, observed * float(np.exp(rng.normal(0.0, 0.6))))
        declared = float(f"{declared:.2g}")
        records.append(
            {
                "bank_id": int(row.bank),
                "account": row.account,
                "customer_id": row.entity_id if isinstance(row.entity_id, str) and row.entity_id else f"C{row.account}",
                "customer_type": customer_type,
                "full_name": name,
                "dob_or_incorporation": born,
                "national_id": national_id,
                "country": country,
                "occupation_or_industry": occupation,
                "account_open_date": opened,
                "declared_monthly_turnover_usd": declared,
                "risk_rating": str(rng.choice(["Low", "Medium", "High"], p=[0.70, 0.25, 0.05])),
                "pep_flag": bool(rng.random() < 0.015),
                "last_review_date": reviewed,
                "institution": row.institution,
            }
        )
    return pd.DataFrame(records)


def name_variant(name: str, strength: str, rng: np.random.Generator) -> str:
    if strength != "fuzzy":
        return name
    parts = name.replace(",", "").split()
    choice = rng.integers(0, 3)
    if choice == 0 and len(parts) >= 2:
        return f"{parts[-1]}, {' '.join(parts[:-1])}"
    if choice == 1 and len(name) > 5:
        i = int(rng.integers(1, len(name) - 1))
        return name[:i] + name[i + 1 :]
    return name.upper().replace(".", "")


def build_watchlist(kyc: pd.DataFrame, alerts: pd.DataFrame, con, size: int, rng, people: People,
                    p_alert: float, p_counterparty: float):
    entries = []

    def entry(name, kind, born, country, aliases=""):
        return {
            "list_id": f"SYN-{len(entries) + 1:05d}",
            "entity_type": "Person" if kind == "Individual" else "Organization",
            "name": name,
            "aliases": aliases,
            "birth_or_incorporation": born.isoformat() if isinstance(born, date) else str(born),
            "country": country,
            "program": str(rng.choice(PROGRAMS)),
            "listed_on": (date(2010, 1, 1) + timedelta(days=int(rng.integers(0, 12 * 365)))).isoformat(),
            "source": WATCHLIST_SOURCE,
        }

    for _ in range(size):
        country = str(rng.choice(FALLBACK_COUNTRIES + ["RU", "IR", "VE", "KP", "SY", "BY"]))
        kind = "Individual" if rng.random() < 0.6 else "Corporation"
        fake = people.faker(country)
        born = fake.date_of_birth(minimum_age=25, maximum_age=80) if kind == "Individual" else \
            date(1975, 1, 1) + timedelta(days=int(rng.integers(0, 45 * 365)))
        name = people.name(kind, country)
        aliases = name_variant(name, "fuzzy", rng) if rng.random() < 0.3 else ""
        entries.append(entry(name, kind, born, country, aliases))

    kyc_a = kyc[kyc.institution == "A"].set_index(["bank_id", "account"])
    planted = {}

    def plant(bank: int, account: str) -> str | None:
        key = (int(bank), account)
        if key not in kyc_a.index:
            return None
        person = kyc_a.loc[key]
        strength = str(rng.choice(["exact", "name_only", "fuzzy"]))
        born = person.dob_or_incorporation
        country = person.country
        if strength == "name_only":
            born = born - timedelta(days=int(rng.integers(400, 9000)))
            country = str(rng.choice([c for c in FALLBACK_COUNTRIES if c != person.country]))
        entries.append(entry(name_variant(person.full_name, strength, rng), person.customer_type, born, country))
        return strength

    counterparty_rows = con.execute(
        """
        SELECT DISTINCT s.bank AS seed_bank, s.account AS seed_account, e.cp_bank, e.cp_account
        FROM ends e JOIN seeds s USING (bank, account)
        JOIN institutions i ON i.bank = e.cp_bank
        WHERE i.institution = 'A' AND NOT (e.cp_bank = s.bank AND e.cp_account = s.account)
        ORDER BY 1, 2, 3, 4
        """
    ).fetchdf()
    by_seed = {k: g for k, g in counterparty_rows.groupby(["seed_bank", "seed_account"])}

    for alert in alerts.itertuples(index=False):
        result = {"alert_match": "none", "counterparty_account": "", "counterparty_match": "none"}
        if rng.random() < p_alert:
            result["alert_match"] = plant(alert.bank, alert.account) or "none"
        group = by_seed.get((alert.bank, alert.account))
        if group is not None and len(group) and rng.random() < p_counterparty:
            cp = group.iloc[int(rng.integers(0, len(group)))]
            strength = plant(cp.cp_bank, cp.cp_account)
            if strength:
                result["counterparty_account"] = f"{int(cp.cp_bank)}:{cp.cp_account}"
                result["counterparty_match"] = strength
        planted[alert.alert_id] = result

    watchlist = pd.DataFrame(entries)
    watchlist = watchlist.iloc[rng.permutation(len(watchlist))].reset_index(drop=True)
    watchlist["list_id"] = [f"SYN-{i + 1:05d}" for i in range(len(watchlist))]
    return watchlist, planted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trans", type=Path, required=True, help="AMLworld *_Trans.csv")
    parser.add_argument("--patterns", type=Path, help="AMLworld *_Patterns.txt (recommended)")
    parser.add_argument("--accounts", type=Path, help="AMLworld *_accounts.csv (optional)")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "prepared")
    parser.add_argument("--eval-dir", type=Path, default=ROOT / "eval")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "data" / "work")
    parser.add_argument("--alerts", type=int, default=30, help="eval alerts, half suspicious")
    parser.add_argument("--hops", type=int, default=2, help="counterparty hops kept around each alert")
    parser.add_argument("--caps", default="150,40,8", help="max transactions kept per account at hop 1,2,3")
    parser.add_argument("--background", type=int, default=50_000, help="random extra transactions")
    parser.add_argument("--watchlist-size", type=int, default=1_500)
    parser.add_argument("--plant-alert", type=float, default=0.2, help="chance an alert's customer gets a watchlist entry")
    parser.add_argument("--plant-counterparty", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--memory-limit", default="2GB", help="DuckDB memory cap; keep low on an 8 GB machine")
    args = parser.parse_args()

    for path in (args.trans, args.patterns, args.accounts):
        if path is not None and not path.exists():
            raise SystemExit(f"File not found: {path}")
    if args.patterns is None:
        print("! No --patterns file: pattern types will be UNKNOWN and positives use the Is Laundering flag.")

    started = time.time()
    rng = np.random.default_rng(args.seed)
    caps = [int(c) for c in args.caps.split(",")]
    args.out.mkdir(parents=True, exist_ok=True)
    args.eval_dir.mkdir(parents=True, exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    work_db = args.work_dir / "prep.duckdb"
    if work_db.exists():
        work_db.unlink()

    con = duckdb.connect(str(work_db))
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    con.execute(f"SET temp_directory = '{sql_path(args.work_dir / 'tmp')}'")
    con.execute("SET preserve_insertion_order = false")

    print(f"1/7 Loading {args.trans.name} ...")
    load_transactions(con, args.trans)
    n_tx, n_laund, t_min, t_max = con.execute(
        "SELECT count(*), sum(is_laundering), min(ts), max(ts) FROM tx").fetchall()[0]
    days = max(1.0, (t_max - t_min).total_seconds() / 86400)
    print(f"    {n_tx:,} transactions, {n_laund:,} laundering, {days:.1f} days")

    print("2/7 Reading laundering patterns and account owners ...")
    pattern_tx = parse_patterns(args.patterns)
    con.register("pattern_tx_df", pattern_tx)
    con.execute(
        """
        CREATE OR REPLACE TABLE pattern_accounts AS
        SELECT DISTINCT attempt_id, pattern_type, bank, account FROM (
            SELECT attempt_id, pattern_type, CAST(from_bank AS BIGINT) AS bank, from_account AS account FROM pattern_tx_df
            UNION ALL
            SELECT attempt_id, pattern_type, CAST(to_bank AS BIGINT), to_account FROM pattern_tx_df
        )
        """
    )
    has_owner = load_accounts(con, args.accounts)
    print(f"    {pattern_tx.attempt_id.nunique() if len(pattern_tx) else 0:,} laundering attempts parsed")

    print("3/7 Splitting banks into Institution A and B ...")
    institutions = assign_institutions(con)
    con.register("institutions_df", institutions)
    con.execute("CREATE OR REPLACE TABLE institutions AS SELECT CAST(bank AS BIGINT) AS bank, institution FROM institutions_df")
    print(f"    {len(institutions):,} banks: {institutions.institution.value_counts().to_dict()}")

    print("4/7 Computing account activity and selecting alerts ...")
    build_account_features(con)
    account_labels(con, has_patterns=len(pattern_tx) > 0)
    alerts, thresholds, q, pool_size = select_alerts(con, args.alerts, rng)
    print(f"    rules at the {q:.1%} quantile flag {pool_size:,} Institution A accounts; "
          f"picked {int((alerts.label == 1).sum())} suspicious + {int((alerts.label == 0).sum())} hard negatives")

    print("5/7 Keeping the transaction neighbourhood around each alert ...")
    build_subset(con, alerts, args.hops, caps, args.background, args.seed)
    n_sub = con.execute("SELECT count(*) FROM sub").fetchall()[0][0]
    print(f"    {n_sub:,} transactions kept")

    print("6/7 Writing ledgers ...")
    ledger_cols = ", ".join(name for name, _ in LEDGER_COLUMNS)
    counts = {}
    for side in ("A", "B"):
        target = args.out / f"ledger_{side.lower()}.parquet"
        con.execute(
            f"""
            COPY (
                SELECT {ledger_cols} FROM tx JOIN sub USING (tx_id)
                WHERE from_bank IN (SELECT bank FROM institutions WHERE institution = '{side}')
                   OR to_bank   IN (SELECT bank FROM institutions WHERE institution = '{side}')
                ORDER BY ts, tx_id
            ) TO '{sql_path(target)}' (FORMAT PARQUET)
            """
        )
        counts[f"ledger_{side.lower()}"] = con.execute(f"SELECT count(*) FROM read_parquet('{sql_path(target)}')").fetchall()[0][0]
    counts["cross_institution"] = con.execute(
        """
        SELECT count(*) FROM tx JOIN sub USING (tx_id)
        JOIN institutions fi ON fi.bank = tx.from_bank
        JOIN institutions ti ON ti.bank = tx.to_bank
        WHERE fi.institution <> ti.institution
        """
    ).fetchall()[0][0]

    print("7/7 Generating KYC, watchlist, alerts and labels ...")
    people = People(args.seed)
    kyc = build_kyc(con, institutions, has_owner, rng, people, days)
    kyc_cols = [name for name, _ in KYC_COLUMNS]
    for side in ("A", "B"):
        part = kyc[kyc.institution == side][kyc_cols].reset_index(drop=True)
        part.to_parquet(args.out / f"kyc_{side.lower()}.parquet", index=False)
        counts[f"kyc_{side.lower()}"] = len(part)

    watchlist, planted = build_watchlist(
        kyc, alerts, con, args.watchlist_size, rng, people, args.plant_alert, args.plant_counterparty)
    watchlist[[name for name, _ in WATCHLIST_COLUMNS]].to_csv(args.out / "watchlist.csv", index=False)
    counts["watchlist"] = len(watchlist)
    institutions.rename(columns={"bank": "bank_id"}).to_csv(args.out / "institutions.csv", index=False)

    alerts_out = pd.DataFrame(
        {
            "alert_id": alerts.alert_id,
            "institution": "A",
            "bank_id": alerts.bank.astype("int64"),
            "account": alerts.account,
            "alert_ts": pd.to_datetime(alerts.last_ts).dt.strftime("%Y-%m-%d %H:%M"),
            "rules": alerts.rules.map("|".join),
            "trigger": alerts.trigger,
        }
    )
    alerts_out.to_csv(args.eval_dir / "alerts.csv", index=False)

    labels_out = pd.DataFrame(
        {
            "alert_id": alerts.alert_id,
            "is_suspicious": alerts.label.astype(int),
            "pattern_types": alerts.pattern_types.fillna(""),
            "attempt_ids": alerts.attempt_ids.fillna(""),
            "laundering_tx_count": alerts.n_laund.astype(int),
            "watchlist_alert_match": [planted[a]["alert_match"] for a in alerts.alert_id],
            "watchlist_counterparty": [planted[a]["counterparty_account"] for a in alerts.alert_id],
            "watchlist_counterparty_match": [planted[a]["counterparty_match"] for a in alerts.alert_id],
        }
    )
    labels_out.to_csv(args.eval_dir / "labels.csv", index=False)

    manifest = {
        "source_files": {k: str(v) for k, v in {"trans": args.trans, "patterns": args.patterns,
                                                 "accounts": args.accounts}.items() if v},
        "source_transactions": n_tx,
        "source_laundering_transactions": int(n_laund or 0),
        "days_spanned": round(days, 2),
        "rule_quantile": q,
        "rule_thresholds": thresholds,
        "alert_pool_size": int(pool_size),
        "alerts": len(alerts_out),
        "suspicious_alerts": int(labels_out.is_suspicious.sum()),
        "pattern_mix": labels_out[labels_out.is_suspicious == 1].pattern_types.str.split("|").str[0]
        .value_counts().to_dict(),
        "hops": args.hops,
        "caps": caps,
        "background": args.background,
        "kept_transactions": int(n_sub),
        "rows": counts,
        "seed": args.seed,
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    con.close()

    print("\nDone in {:.0f}s".format(time.time() - started))
    for key, value in counts.items():
        print(f"    {key:<18} {value:>10,}")
    print(f"    alerts -> {args.eval_dir / 'alerts.csv'}")
    print(f"    labels -> {args.eval_dir / 'labels.csv'}  (never give these to the agent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
