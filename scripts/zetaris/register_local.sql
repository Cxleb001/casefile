-- Register Casefile's LOCAL sources in a local Zetaris install.
-- Run ONE statement at a time in the Zetaris SQL Editor (http://localhost:3000).
-- `python scripts/publish_local.py sql` prints this file with your settings filled in.
-- The password stays as <LOCAL_PG_PASSWORD>: type it in the SQL Editor yourself.
--
-- Zetaris runs in Docker, so it reaches the sources container through
-- host.docker.internal (Docker Desktop on Windows and macOS provides this name).
-- Descriptions avoid parentheses, slashes and colons: Zetaris rejects them.
--
-- Result: five tables the agent queries by these names (set the same in .env)
--   inst_a.ledger_a         Institution A ledger
--   inst_a.kyc_a            Institution A customer records, full
--   inst_b.ledger_b         Institution B ledger
--   inst_b.kyc_b_shared     Institution B customer records WITHOUT names, birth dates, ID numbers
--   compliance.watchlist    synthetic watchlist

-- 1. Institution A ---------------------------------------------------------------

CREATE DATASOURCE inst_a DESCRIBE BY "Institution A core banking - Postgres" OPTIONS (
  jdbcdriver "org.postgresql.Driver",
  jdbcurl "jdbc:postgresql://{{ZETARIS_PG_HOST}}:{{LOCAL_PG_PORT}}/bank_a",
  username "{{LOCAL_PG_USER}}",
  password "<LOCAL_PG_PASSWORD>",
  fetchsize "5000"
);

REGISTER DATASOURCE TABLES ("ledger_a", "kyc_a") FROM inst_a;

-- Check: SELECT count(*) FROM inst_a.ledger_a;

-- 2. Institution B ---------------------------------------------------------------

CREATE DATASOURCE inst_b DESCRIBE BY "Institution B systems - Postgres" OPTIONS (
  jdbcdriver "org.postgresql.Driver",
  jdbcurl "jdbc:postgresql://{{ZETARIS_PG_HOST}}:{{LOCAL_PG_PORT}}/bank_b",
  username "{{LOCAL_PG_USER}}",
  password "<LOCAL_PG_PASSWORD>",
  fetchsize "5000"
);

REGISTER DATASOURCE TABLES ("ledger_b") FROM inst_b;

-- Governance: Institution B shares customer records only without PII. The view
-- runs inside B's database, so names, birth dates and ID numbers never leave it.
CREATE DATASOURCE VIEW kyc_b_shared FROM inst_b AS
SELECT bank_id, account, customer_id, customer_type, country, occupation_or_industry,
       account_open_date, declared_monthly_turnover_usd, risk_rating, pep_flag, last_review_date
FROM kyc_b;

-- Check: SELECT * FROM inst_b.kyc_b_shared LIMIT 5;   (no full_name, dob_or_incorporation, national_id)

-- 3. Compliance watchlist -----------------------------------------------------------

CREATE DATASOURCE compliance DESCRIBE BY "Compliance watchlist - Postgres" OPTIONS (
  jdbcdriver "org.postgresql.Driver",
  jdbcurl "jdbc:postgresql://{{ZETARIS_PG_HOST}}:{{LOCAL_PG_PORT}}/compliance",
  username "{{LOCAL_PG_USER}}",
  password "<LOCAL_PG_PASSWORD>",
  fetchsize "5000"
);

REGISTER DATASOURCE TABLES ("watchlist") FROM compliance;

-- 4. The federated check Casefile's setup test runs (three tables, two databases) ----
-- SELECT a.from_account, b.to_account, k.country
-- FROM inst_a.ledger_a a
-- JOIN inst_b.ledger_b b ON b.from_bank = a.to_bank AND b.from_account = a.to_account
-- JOIN inst_b.kyc_b_shared k ON k.bank_id = a.to_bank AND k.account = a.to_account
-- LIMIT 5;
