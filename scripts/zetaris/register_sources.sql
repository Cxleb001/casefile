-- Register Casefile's sources in Zetaris.
-- Run ONE statement at a time in the Zetaris SQL Editor (it splits on semicolons).
-- `python scripts/publish_sources.py sql` prints this file with your bucket, prefix
-- and Postgres host filled in. Secrets stay as <PLACEHOLDERS>: type them in the
-- SQL Editor yourself, never into this file or a prompt.
--
-- Result: five tables the agent queries by these exact names
--   inst_a.ledger_a       Institution A core banking ledger   (Postgres)
--   inst_a.kyc_a          Institution A customer records      (Postgres)
--   inst_b.ledger_b       Institution B data lake ledger      (S3 Parquet)
--   shared.kyc_b          Institution B KYC without PII       (governed view over S3 Parquet)
--   compliance.watchlist  synthetic watchlist                 (S3 CSV)

-- 1. Institution A: Postgres ----------------------------------------------------

CREATE DATASOURCE inst_a DESCRIBE BY "Institution A core banking (Postgres)" OPTIONS (
  jdbcdriver "org.postgresql.Driver",
  jdbcurl "jdbc:postgresql://{{PG_HOST}}:{{PG_PORT}}/{{PG_DB}}?sslmode=require",
  username "{{PG_USER}}",
  password "<PG_PASSWORD>",
  fetchsize "5000"
);

REGISTER DATASOURCE TABLES ("ledger_a", "kyc_a") FROM inst_a;

-- Check: SELECT count(*) FROM inst_a.ledger_a;

-- 2. Institution B: S3 data lake ------------------------------------------------

CREATE LIGHTNING DATABASE inst_b DESCRIBE BY "Institution B data lake (S3 Parquet)";

CREATE LIGHTNING FILESTORE TABLE ledger_b FROM inst_b FORMAT PARQUET OPTIONS (
  path "s3a://{{S3_BUCKET}}/{{S3_PREFIX}}/inst_b/ledger_b/",
  awsAccessKeyId "<AWS_ACCESS_KEY_ID>",
  awsSecretAccessKey "<AWS_SECRET_ACCESS_KEY>",
  s3Endpoint "s3.{{AWS_REGION}}.amazonaws.com"
);

CREATE LIGHTNING FILESTORE TABLE kyc_b FROM inst_b FORMAT PARQUET OPTIONS (
  path "s3a://{{S3_BUCKET}}/{{S3_PREFIX}}/inst_b/kyc_b/",
  awsAccessKeyId "<AWS_ACCESS_KEY_ID>",
  awsSecretAccessKey "<AWS_SECRET_ACCESS_KEY>",
  s3Endpoint "s3.{{AWS_REGION}}.amazonaws.com"
);

-- Check: SELECT * FROM inst_b.ledger_b LIMIT 10;

-- 3. Compliance: synthetic watchlist (CSV) --------------------------------------

CREATE LIGHTNING DATABASE compliance DESCRIBE BY "Compliance file drop (S3 CSV)";

CREATE LIGHTNING FILESTORE TABLE watchlist FROM compliance FORMAT CSV OPTIONS (
  path "s3a://{{S3_BUCKET}}/{{S3_PREFIX}}/compliance/watchlist/",
  awsAccessKeyId "<AWS_ACCESS_KEY_ID>",
  awsSecretAccessKey "<AWS_SECRET_ACCESS_KEY>",
  s3Endpoint "s3.{{AWS_REGION}}.amazonaws.com",
  header "true",
  inferSchema "true"
);

-- 4. Governance: Institution B shares KYC with partners only without PII ----------

CREATE SCHEMASTORE CONTAINER shared;

CREATE SCHEMASTORE VIEW kyc_b WITH CONTAINER shared AS
SELECT bank_id, account, customer_id, customer_type, country, occupation_or_industry,
       account_open_date, declared_monthly_turnover_usd, risk_rating, pep_flag, last_review_date
FROM inst_b.kyc_b;

-- Check: SELECT * FROM shared.kyc_b LIMIT 10;   (no full_name, dob_or_incorporation, national_id)

-- 5. Optional but worth it for the demo: a restricted user for the agent --------
-- Create user casefile-agent in the Zetaris UI, run these as an administrator,
-- then create that user's API key and put it in .env as ZETARIS_API_KEY.
-- The agent can then read B's KYC only through the view; inst_b.kyc_b is refused.

GRANT SELECT ON DATASOURCE TABLE inst_a.ledger_a TO USER '{{AGENT_USER}}';
GRANT SELECT ON DATASOURCE TABLE inst_a.kyc_a TO USER '{{AGENT_USER}}';
GRANT SELECT ON LIGHTNING DATABASE TABLE inst_b.ledger_b TO USER '{{AGENT_USER}}';
GRANT SELECT ON LIGHTNING DATABASE TABLE compliance.watchlist TO USER '{{AGENT_USER}}';
GRANT SELECT ON VIEW TABLE shared.kyc_b TO USER '{{AGENT_USER}}';

-- Check, signed in as the agent user: SELECT full_name FROM inst_b.kyc_b LIMIT 1;  -> refused

-- 6. Speed (if federated queries are slow) ---------------------------------------
-- OFFSITE CACHE TABLE inst_b.ledger_b TO MEMORY WITH (expireInSec "-1", partcount "4");
