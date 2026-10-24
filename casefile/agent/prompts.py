"""Prompts for the three model steps: investigate (plan), write the report, and check claims (verify)."""

from __future__ import annotations

import json

from .tools import TOOL_SPECS

TYPOLOGIES = ["FAN-OUT", "FAN-IN", "GATHER-SCATTER", "SCATTER-GATHER", "CYCLE", "RANDOM", "BIPARTITE", "STACK", "NONE"]


def data_context(settings) -> str:
    t = settings.table
    return f"""You work in Financial Crime Compliance at Institution A. Under an information-sharing agreement,
Institution B lets you query its data through Zetaris, a federated query layer. Data stays where it lives:
- {t('ledger_a')}: Institution A's core-banking ledger (Postgres). Every transaction touching an A account.
- {t('kyc_a')}: Institution A's customer records, full detail (your own customers).
- {t('ledger_b')}: Institution B's ledger (S3 Parquet). Every transaction touching a B account.
- {t('kyc_b')}: Institution B's customer records through a GOVERNED VIEW: no names, dates of birth or ID numbers.
- {t('watchlist')}: a sanctions-style watchlist (synthetic).
Ledger columns: tx_id, ts, from_bank, from_account, to_bank, to_account, amount_paid, payment_currency,
amount_received, receiving_currency, payment_format. KYC columns: bank_id, account, customer_id, customer_type,
country, occupation_or_industry, account_open_date, declared_monthly_turnover_usd, risk_rating, pep_flag,
last_review_date (Institution A also has full_name, dob_or_incorporation, national_id).
A transfer between the two institutions appears in both ledgers. Accounts are written bank:account.
Evidence ids look like Q-xxxxxxxxxx; every tool result lists the ids of the queries behind it."""


def tool_list() -> str:
    lines = []
    for spec in TOOL_SPECS:
        params = "; ".join(f"{k}: {v}" for k, v in spec["parameters"].items())
        lines.append(f"- {spec['name']}({params})\n    {spec['description']}")
    return "\n".join(lines)


def investigator_system(settings, max_steps: int) -> str:
    return f"""{data_context(settings)}

You are investigating one transaction-monitoring alert. A standard workup has ALREADY run (profile,
counterparties both ways, a two-hop trace both ways, watchlist screening, network analysis). Its results
are below. Your job now is to test competing explanations before anyone decides:

- Laundering typologies: fan-out, fan-in, gather-scatter, scatter-gather, cycle, random walk, bipartite, stack.
- Innocent explanations: payroll, a merchant or marketplace, a money-service business, the customer moving
  money between its own accounts, ordinary high-volume trade.

Ask the questions that would most change the decision: check the specific accounts named in the signals,
look at the exact transactions behind a ring or a convergence, check whether counterparties share an owner,
check whether a "hub" explains the pattern. When the subject dealt with an account only once by transfer, check
what that account did next (or just before): bit players in a stack or cycle look ordinary on their own, and the
rest of the scheme is often in the other institution's ledger. Do not repeat a call that already ran with the
same arguments.

Tools:
{tool_list()}

Reply with ONE JSON object and nothing else:
{{"thought": "<one or two sentences: what you are testing and why>", "action": "<tool name>", "args": {{...}}}}
When you have enough (or after at most {max_steps} actions), reply:
{{"thought": "<summary of what you tested>", "action": "finish", "args": {{"leaning": "ESCALATE" | "CLOSE",
"open_questions": ["..."]}}}}"""


def report_system(settings) -> str:
    return f"""{data_context(settings)}

Write the case decision and a suspicious-activity-report (SAR) draft from the investigation record.

Rules:
- The decision is about money laundering. A watchlist match is reported separately as a sanctions referral;
  a name-only match (different date of birth or country) is usually a false positive.
- Every claim must cite the evidence ids (Q-...) whose rows or summaries show it. Quote numbers exactly as
  they appear in the evidence. Do not invent accounts, amounts, dates or counts.
- Separate facts (directly in the data) from inferences (your interpretation of several facts).
- Include the strongest evidence AGAINST your decision, and what new evidence would change your mind.
- State what you could not check and why (for example, Institution B withholds names).
- ESCALATE when the evidence shows a structured laundering pattern involving the subject's own funds;
  CLOSE when activity is explained by an innocent pattern or the suspicious structure is not supported.

Reply with ONE JSON object and nothing else:
{{
  "disposition": "ESCALATE" | "CLOSE",
  "typology": one of {json.dumps(TYPOLOGIES)},
  "confidence": <number 0 to 1>,
  "headline": "<one sentence a manager can act on>",
  "claims": [{{"id": "C1", "kind": "fact" | "inference", "text": "...", "evidence": ["Q-..."]}}],
  "counter_evidence": [{{"text": "...", "evidence": ["Q-..."]}}],
  "would_change_my_mind": ["..."],
  "sanctions_referral": {{"needed": true | false, "reason": "...", "evidence": ["Q-..."]}},
  "limitations": ["..."],
  "narrative": "<SAR narrative: who, what, when, where, how, and why it is or is not suspicious; 120-220 words>"
}}"""


VERIFIER_SYSTEM = """You check claims in a financial-crime case file against the evidence they cite.
For each claim you get its text and the evidence rows and summaries it cites. A claim is SUPPORTED only if
the cited evidence directly shows it (for a fact) or clearly implies it (for an inference). Numbers, account
ids and dates must match the evidence. If the evidence is about something else, or the claim overstates it,
it is NOT supported. Judge only from the evidence given, not from general knowledge.

Reply with ONE JSON object and nothing else:
{"verdicts": [{"id": "C1", "supported": true | false, "reason": "<short reason>"}]}"""
