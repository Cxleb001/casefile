"""The agent's tools. Each one runs federated SQL, records it as evidence, and returns an observation.

Every query names tables through ``settings.table(key)``, so the same SQL runs
on the local DuckDB stand-in and on Zetaris. A transaction between the two
institutions appears in both ledgers; tools union both and de-duplicate by
``tx_id``, and the case remembers which ledgers each transaction was seen in.

Institution B shares customer records only through the governed view
(``shared.kyc_b``, no names, dates of birth or ID numbers). Tools never try
to read around it; where that stops a check, they record a limitation that
ends up in the report.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import timedelta
from typing import Any, Callable

from ..data.base import QueryResult
from .case import CaseState, fmt_usd, key, split_key
from .fx import to_usd, usd_sql
from .sqlutil import (
    account_literal,
    bank_literal,
    check_readonly,
    in_list,
    to_datetime,
    to_iso,
)

LEDGER_COLS = [
    "tx_id", "ts", "from_bank", "from_account", "to_bank", "to_account",
    "amount_paid", "payment_currency", "amount_received", "receiving_currency", "payment_format",
]
KYC_PUBLIC_COLS = [
    "customer_id", "customer_type", "country", "occupation_or_industry", "account_open_date",
    "declared_monthly_turnover_usd", "risk_rating", "pep_flag", "last_review_date",
]
B_WITHHELD = ["full_name", "dob_or_incorporation", "national_id"]
MAX_IN_LIST = 200


class ToolError(RuntimeError):
    pass


def _norm_name(text: Any) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _token_sort_ratio(a: str, b: str) -> float:
    """0-100 similarity of two names regardless of word order (stdlib, no extra dependency)."""
    from difflib import SequenceMatcher

    left, right = " ".join(sorted(a.split())), " ".join(sorted(b.split()))
    if not left or not right:
        return 0.0
    return 100.0 * SequenceMatcher(None, left, right).ratio()


def _date10(value: Any) -> str | None:
    iso = to_iso(value)
    return iso[:10] if iso else None


class Toolbox:
    """Bound to one case. ``call(name, args)`` is what the agent loop uses."""

    def __init__(self, backend, store, settings, case: CaseState) -> None:
        self.backend = backend
        self.store = store
        self.settings = settings
        self.case = case
        self._watchlist: list[dict[str, Any]] | None = None
        self._watchlist_qid: str | None = None
        self.tools: dict[str, Callable[..., Any]] = {
            "profile_account": self.profile_account,
            "get_counterparties": self.get_counterparties,
            "follow_money": self.follow_money,
            "get_transactions": self.get_transactions,
            "find_related_accounts": self.find_related_accounts,
            "screen_watchlist": self.screen_watchlist,
            "analyze_network": self.analyze_network,
            "run_sql": self.run_sql,
        }

    # ------------------------------------------------------------------ plumbing

    def t(self, logical: str) -> str:
        return self.settings.table(logical)

    def _run(self, purpose: str, sql: str, columns: list[str] | None) -> tuple[QueryResult, str]:
        try:
            result = self.backend.query(sql, columns=columns)
        except Exception as error:  # record failed queries too: they are part of the audit trail
            qid = self.store.record_query(self.case.case_id, sql=sql, purpose=purpose,
                                          backend=self.backend.name, error=str(error)[:2000])
            raise ToolError(f"query {qid} failed: {str(error)[:500]}") from error
        qid = self.store.record_query(self.case.case_id, result, purpose=purpose)
        return result, qid

    def call(self, name: str, args: dict[str, Any] | None = None):
        args = dict(args or {})
        fn = self.tools.get(name)
        if fn is None:
            return self.case.add_observation(name, args, "", {}, [], error=f"Unknown tool {name!r}; "
                                             f"available: {', '.join(self.tools)}")
        try:
            return fn(**args)
        except TypeError as error:
            return self.case.add_observation(name, args, "", {}, [], error=f"Bad arguments: {error}")
        except (ToolError, ValueError) as error:
            return self.case.add_observation(name, args, "", {}, [], error=str(error))

    def _subject(self, bank_id: Any, account: Any) -> tuple[int, str]:
        if account is None:
            return split_key(self.case.subject)
        text = str(account).strip()
        if ":" in text:  # written bank:account, the way the case record shows accounts
            bank_text, acct_text = text.split(":", 1)
            if bank_text.strip().isdigit():
                return int(bank_text), acct_text.strip()
        if bank_id is None:
            located = self._locate(str(account))
            if located is None:
                raise ToolError(f"Account {account} is not in either institution's customer records")
            return located
        return int(bank_id), str(account).strip()

    def _locate(self, account: str) -> tuple[int, str] | None:
        for known in self.case.accounts:
            bank, acct = split_key(known)
            if acct == account:
                return bank, acct
        acct = account_literal(account)
        sql = (f"SELECT bank_id AS bank_id, account AS account FROM {self.t('kyc_a')} WHERE account = {acct} "
               f"UNION ALL SELECT bank_id AS bank_id, account AS account FROM {self.t('kyc_b')} WHERE account = {acct}")
        result, _ = self._run(f"locate account {account}", sql, ["bank_id", "account"])
        if not result.rows:
            return None
        return int(result.rows[0][0]), str(result.rows[0][1])

    def _subject_tx_sql(self, bank: int, account: str, extra_where: str = "") -> str:
        """Every transaction touching one account, from both ledgers, tagged with where it was seen."""
        b, a = bank_literal(bank), account_literal(account)
        cond = f"((from_bank = {b} AND from_account = {a}) OR (to_bank = {b} AND to_account = {a}))"
        if extra_where:
            cond += f" AND ({extra_where})"
        cols = ", ".join(LEDGER_COLS)
        return (f"SELECT {cols}, 'A' AS seen_in FROM {self.t('ledger_a')} WHERE {cond} "
                f"UNION ALL SELECT {cols}, 'B' AS seen_in FROM {self.t('ledger_b')} WHERE {cond}")

    def _lookup_kyc(self, account_keys: list[str], purpose: str) -> str | None:
        """Batch-fetch the shareable customer fields for accounts not yet known; returns the query id."""
        missing = [k for k in account_keys if "institution" not in self.case.accounts.get(k, {})]
        if not missing:
            return None
        qids = []
        for start in range(0, len(missing), MAX_IN_LIST):
            chunk = missing[start : start + MAX_IN_LIST]
            accounts = in_list(split_key(k)[1] for k in chunk)
            cols = ", ".join(KYC_PUBLIC_COLS)
            sql = (f"SELECT 'A' AS institution, bank_id, account, {cols} FROM {self.t('kyc_a')} "
                   f"WHERE account IN {accounts} "
                   f"UNION ALL SELECT 'B' AS institution, bank_id, account, {cols} FROM {self.t('kyc_b')} "
                   f"WHERE account IN {accounts}")
            columns = ["institution", "bank_id", "account", *KYC_PUBLIC_COLS]
            result, qid = self._run(purpose, sql, columns)
            qids.append(qid)
            wanted = set(chunk)
            for rec in result.records():
                k = key(rec["bank_id"], rec["account"])
                if k in wanted:
                    rec = {c: (_date10(v) if c.endswith("_date") else v) for c, v in rec.items()}
                    rec["kyc_query_id"] = qid
                    self.case.add_account(k, rec)
        return qids[0] if qids else None

    # --------------------------------------------------------------------- tools

    def profile_account(self, bank_id: Any = None, account: Any = None):
        """Customer record plus a summary of activity across both institutions' ledgers."""
        bank, acct = self._subject(bank_id, account)
        k = key(bank, acct)
        b, a = bank_literal(bank), account_literal(acct)
        cols = ", ".join(KYC_PUBLIC_COLS)
        kyc_sql = (
            f"SELECT 'A' AS institution, full_name, {cols} FROM {self.t('kyc_a')} "
            f"WHERE bank_id = {b} AND account = {a} "
            f"UNION ALL SELECT 'B' AS institution, CAST(NULL AS STRING) AS full_name, {cols} "
            f"FROM {self.t('kyc_b')} WHERE bank_id = {b} AND account = {a}"
        )
        kyc_res, kyc_qid = self._run(f"customer record for {k}", kyc_sql, ["institution", "full_name", *KYC_PUBLIC_COLS])
        record = kyc_res.records()[0] if kyc_res.rows else {}
        record = {c: (_date10(v) if c.endswith("_date") else v) for c, v in record.items()}
        if record:
            record["kyc_query_id"] = kyc_qid
            self.case.add_account(k, record)

        usd = usd_sql("amount_paid", "payment_currency")
        activity_sql = f"""
SELECT direction, count(*) AS n_tx, count(DISTINCT cp_account) AS n_counterparties,
       round(sum(usd), 2) AS usd_total, min(ts) AS first_ts, max(ts) AS last_ts,
       sum(CASE WHEN payment_currency <> receiving_currency THEN 1 ELSE 0 END) AS n_cross_currency,
       sum(CASE WHEN payment_format IN ('ACH', 'Wire') THEN 1 ELSE 0 END) AS n_ach_wire,
       sum(CASE WHEN payment_format IN ('Cash', 'Bitcoin') THEN 1 ELSE 0 END) AS n_cash_bitcoin
FROM (
    SELECT CASE WHEN from_bank = {b} AND from_account = {a} THEN 'out' ELSE 'in' END AS direction,
           CASE WHEN from_bank = {b} AND from_account = {a} THEN to_account ELSE from_account END AS cp_account,
           {usd} AS usd, ts, payment_currency, receiving_currency, payment_format
    FROM (SELECT DISTINCT {", ".join(LEDGER_COLS)} FROM ({self._subject_tx_sql(bank, acct)}) u) d
    WHERE NOT (from_bank = to_bank AND from_account = to_account)
) t
GROUP BY direction
ORDER BY direction
""".strip()
        act_cols = ["direction", "n_tx", "n_counterparties", "usd_total", "first_ts", "last_ts",
                    "n_cross_currency", "n_ach_wire", "n_cash_bitcoin"]
        act_res, act_qid = self._run(f"activity summary for {k}", activity_sql, act_cols)
        activity = {}
        for rec in act_res.records():
            rec["first_ts"], rec["last_ts"] = to_iso(rec["first_ts"]), to_iso(rec["last_ts"])
            activity[rec.pop("direction")] = rec

        institution = record.get("institution")
        lines = []
        if record:
            name = record.get("full_name") or "(name withheld by Institution B)"
            lines.append(
                f"{k} is an Institution {institution} customer: {record.get('customer_type')}, {name}, "
                f"{record.get('country')}, {record.get('occupation_or_industry')}; risk {record.get('risk_rating')}, "
                f"PEP {record.get('pep_flag')}, declared monthly turnover {fmt_usd(float(record.get('declared_monthly_turnover_usd') or 0))}."
            )
            if institution == "B":
                self.case.limit(f"Institution B withholds {', '.join(B_WITHHELD)} for {k} (governed view {self.t('kyc_b')}).")
        else:
            lines.append(f"{k} is not in either institution's customer records.")
        for direction in ("in", "out"):
            act = activity.get(direction)
            if act:
                lines.append(
                    f"{direction.upper()}: {act['n_tx']} transactions with {act['n_counterparties']} counterparties, "
                    f"about {fmt_usd(float(act['usd_total'] or 0))}, {act['first_ts']} to {act['last_ts']}; "
                    f"{act['n_cross_currency']} cross-currency, {act['n_ach_wire']} ACH/wire, {act['n_cash_bitcoin']} cash/bitcoin."
                )
        observed = sum(float(activity.get(d, {}).get("usd_total") or 0) for d in ("out",))
        days = self._span_days(activity)
        if record.get("declared_monthly_turnover_usd") and days:
            monthly = observed * 30.0 / days
            ratio = monthly / float(record["declared_monthly_turnover_usd"])
            lines.append(f"Outflows run at about {fmt_usd(monthly)} a month, {ratio:.1f}x the declared turnover.")
        data = {"account": k, "customer": record, "activity": activity}
        return self.case.add_observation("profile_account", {"bank_id": bank, "account": acct},
                                         "\n".join(lines), data, [kyc_qid, act_qid])

    @staticmethod
    def _span_days(activity: dict[str, dict[str, Any]]) -> float:
        stamps = [to_datetime(v.get(f)) for v in activity.values() for f in ("first_ts", "last_ts")]
        stamps = [s for s in stamps if s]
        if len(stamps) < 2:
            return 0.0
        return max(1.0, (max(stamps) - min(stamps)).total_seconds() / 86400)

    def get_counterparties(self, bank_id: Any = None, account: Any = None, direction: str = "out",
                           order_by: str = "usd", limit: int = 15):
        """Who the account paid (or was paid by), joined to both institutions' customer records."""
        if direction not in {"in", "out"}:
            raise ToolError("direction must be 'in' or 'out'")
        bank, acct = self._subject(bank_id, account)
        k = key(bank, acct)
        b, a = bank_literal(bank), account_literal(acct)
        limit = max(1, min(int(limit), 50))
        order = {"usd": "usd_total DESC", "count": "n_tx DESC", "recent": "last_ts DESC"}.get(order_by, "usd_total DESC")
        if direction == "out":
            me, cp_bank, cp_acct = f"from_bank = {b} AND from_account = {a}", "to_bank", "to_account"
        else:
            me, cp_bank, cp_acct = f"to_bank = {b} AND to_account = {a}", "from_bank", "from_account"
        usd = usd_sql("amount_paid", "payment_currency")
        cols = ", ".join(LEDGER_COLS)
        sql = f"""
SELECT c.cp_bank, c.cp_account, c.n_tx, c.usd_total, c.first_ts, c.last_ts, c.n_formats, c.top_format,
       CASE WHEN ka.account IS NOT NULL THEN 'A' WHEN kb.account IS NOT NULL THEN 'B' ELSE 'unknown' END AS institution,
       COALESCE(ka.customer_type, kb.customer_type) AS customer_type,
       COALESCE(ka.country, kb.country) AS country,
       COALESCE(ka.occupation_or_industry, kb.occupation_or_industry) AS occupation_or_industry,
       COALESCE(ka.risk_rating, kb.risk_rating) AS risk_rating,
       c.n_all
FROM (
    SELECT {cp_bank} AS cp_bank, {cp_acct} AS cp_account, count(*) AS n_tx, round(sum({usd}), 2) AS usd_total,
           min(ts) AS first_ts, max(ts) AS last_ts, count(DISTINCT payment_format) AS n_formats,
           max(payment_format) AS top_format, count(*) OVER () AS n_all
    FROM (SELECT DISTINCT {cols} FROM ({self._subject_tx_sql(bank, acct)}) u) d
    WHERE {me} AND NOT (from_bank = to_bank AND from_account = to_account)
    GROUP BY {cp_bank}, {cp_acct}
) c
LEFT JOIN {self.t('kyc_a')} ka ON ka.bank_id = c.cp_bank AND ka.account = c.cp_account
LEFT JOIN {self.t('kyc_b')} kb ON kb.bank_id = c.cp_bank AND kb.account = c.cp_account
ORDER BY {order}, c.cp_account
LIMIT {limit}
""".strip()
        columns = ["cp_bank", "cp_account", "n_tx", "usd_total", "first_ts", "last_ts", "n_formats", "top_format",
                   "institution", "customer_type", "country", "occupation_or_industry", "risk_rating", "n_all"]
        result, qid = self._run(f"{direction} counterparties of {k}", sql, columns)
        rows = []
        n_all = 0
        by_inst: dict[str, int] = {}
        for rec in result.records():
            n_all = int(rec.pop("n_all") or 0)
            ck = key(rec["cp_bank"], rec["cp_account"])
            rec["first_ts"], rec["last_ts"] = to_iso(rec["first_ts"]), to_iso(rec["last_ts"])
            self.case.add_account(ck, {"institution": rec["institution"] if rec["institution"] != "unknown" else None,
                                       "customer_type": rec["customer_type"], "country": rec["country"],
                                       "occupation_or_industry": rec["occupation_or_industry"],
                                       "risk_rating": rec["risk_rating"]})
            by_inst[rec["institution"]] = by_inst.get(rec["institution"], 0) + 1
            rows.append({"account": ck, **{c: rec[c] for c in ("n_tx", "usd_total", "first_ts", "last_ts", "top_format",
                                                               "institution", "customer_type", "country",
                                                               "occupation_or_industry", "risk_rating")}})
        word = "paid" if direction == "out" else "was paid by"
        summary = (f"{k} {word} {n_all} distinct counterparties; showing top {len(rows)} by {order_by}. "
                   f"Of those shown: " + ", ".join(f"{n} at Institution {i}" for i, n in sorted(by_inst.items())) + ".")
        if rows:
            top = rows[0]
            summary += (f" Largest: {top['account']} ({top['institution']}, {top['customer_type']}, {top['country']}) "
                        f"{top['n_tx']} tx, {fmt_usd(float(top['usd_total'] or 0))}.")
        return self.case.add_observation("get_counterparties",
                                         {"bank_id": bank, "account": acct, "direction": direction,
                                          "order_by": order_by, "limit": limit},
                                         summary, {"total_counterparties": n_all, "counterparties": rows}, [qid])

    def follow_money(self, bank_id: Any = None, account: Any = None, direction: str = "out",
                     hops: int = 2, window_hours: int = 72, max_frontier: int = 30):
        """Trace funds hop by hop across both institutions, respecting time order.

        Out: what each recipient did with the money within ``window_hours`` of receiving it.
        In: where each sender got the money within ``window_hours`` before sending it.
        """
        if direction not in {"in", "out"}:
            raise ToolError("direction must be 'in' or 'out'")
        hops = max(1, min(int(hops), 4))
        window = timedelta(hours=max(1, min(int(window_hours), 24 * 14)))
        max_frontier = max(1, min(int(max_frontier), 60))
        bank, acct = self._subject(bank_id, account)
        origin = key(bank, acct)
        qids: list[str] = []

        # Hop 0: the origin's own transactions in this direction.
        side = "from" if direction == "out" else "to"
        b, a = bank_literal(bank), account_literal(acct)
        sql0 = self._subject_tx_sql(bank, acct, f"{side}_bank = {b} AND {side}_account = {a}")
        res0, q0 = self._run(f"follow {direction}: hop 1 from {origin}", sql0, [*LEDGER_COLS, "seen_in"])
        qids.append(q0)
        self.case.add_transactions(res0.records(), q0)

        def hop_edges(tx_ids: set[str]):
            return [self.case.edges[t] for t in tx_ids if t in self.case.edges]

        first = {str(r["tx_id"]) for r in res0.records()}
        layers: list[list] = [[e for e in hop_edges(first) if not e.is_self]]
        visited = {origin}
        # frontier account -> the time money reached it (out) or left it (in)
        frontier: dict[str, Any] = {}
        weight: dict[str, float] = {}
        for e in layers[0]:
            nxt = e.dst if direction == "out" else e.src
            if nxt == origin:
                continue
            t0 = frontier.get(nxt)
            if direction == "out":
                frontier[nxt] = e.ts if t0 is None else min(t0, e.ts)
            else:
                frontier[nxt] = e.ts if t0 is None else max(t0, e.ts)
            weight[nxt] = weight.get(nxt, 0.0) + e.usd

        skipped_hubs = 0
        for hop in range(2, hops + 1):
            candidates = [k for k in frontier if k not in visited]
            if not candidates:
                break
            # Follow specific accounts, not hubs: a big merchant's onward payments say nothing
            # about this money and would swamp the trace.
            qids.extend(self._account_degrees(sorted(candidates, key=lambda k: (-weight.get(k, 0.0), k))[:400],
                                              f"counterparty counts before hop {hop} from {origin}"))
            cut = self._hub_cut(candidates)
            hubs = [k for k in candidates if (self.case.accounts.get(k, {}).get("degree") or 0) >= cut]
            skipped_hubs += len(hubs)
            candidates = [k for k in candidates if k not in set(hubs)]
            if not candidates:
                break
            candidates.sort(key=lambda k: (-weight.get(k, 0.0), k))
            chosen = candidates[:max_frontier]
            visited.update(chosen)
            accounts = in_list(split_key(k)[1] for k in chosen)
            times = [frontier[k] for k in chosen]
            if direction == "out":
                bound = f"ts >= TIMESTAMP '{min(times):%Y-%m-%d %H:%M:%S}' AND ts <= TIMESTAMP '{max(times) + window:%Y-%m-%d %H:%M:%S}'"
                where = f"from_account IN {accounts} AND {bound}"
            else:
                bound = f"ts >= TIMESTAMP '{min(times) - window:%Y-%m-%d %H:%M:%S}' AND ts <= TIMESTAMP '{max(times):%Y-%m-%d %H:%M:%S}'"
                where = f"to_account IN {accounts} AND {bound}"
            cols = ", ".join(LEDGER_COLS)
            sql = (f"SELECT {cols}, 'A' AS seen_in FROM {self.t('ledger_a')} WHERE {where} "
                   f"UNION ALL SELECT {cols}, 'B' AS seen_in FROM {self.t('ledger_b')} WHERE {where}")
            res, qid = self._run(f"follow {direction}: hop {hop} ({len(chosen)} accounts)", sql, [*LEDGER_COLS, "seen_in"])
            qids.append(qid)
            chosen_set = set(chosen)
            keep = []
            for rec in res.records():
                src, dst = key(rec["from_bank"], rec["from_account"]), key(rec["to_bank"], rec["to_account"])
                ts = to_datetime(rec["ts"])
                if ts is None or src == dst:
                    continue
                anchor = src if direction == "out" else dst
                if anchor not in chosen_set:
                    continue
                t_ref = frontier[anchor]
                if direction == "out" and not (t_ref <= ts <= t_ref + window):
                    continue
                if direction == "in" and not (t_ref - window <= ts <= t_ref):
                    continue
                keep.append(rec)
            self.case.add_transactions(keep, qid)
            layer = [self.case.edges[str(r["tx_id"])] for r in keep if str(r["tx_id"]) in self.case.edges]
            layers.append(layer)
            new_frontier: dict[str, Any] = {}
            new_weight: dict[str, float] = {}
            for e in layer:
                nxt = e.dst if direction == "out" else e.src
                t0 = new_frontier.get(nxt)
                if direction == "out":
                    new_frontier[nxt] = e.ts if t0 is None else min(t0, e.ts)
                else:
                    new_frontier[nxt] = e.ts if t0 is None else max(t0, e.ts)
                new_weight[nxt] = new_weight.get(nxt, 0.0) + e.usd
            frontier, weight = new_frontier, new_weight

        # Which institution each reached account belongs to (governed fields only).
        reached = sorted({(e.dst if direction == "out" else e.src) for layer in layers for e in layer})
        kyc_qid = self._lookup_kyc(reached[:600], f"customer records for accounts reached from {origin}")
        if kyc_qid:
            qids.append(kyc_qid)

        # What the trace shows.
        hop_rows = []
        for i, layer in enumerate(layers, start=1):
            ends = {(e.dst if direction == "out" else e.src) for e in layer}
            inst = {}
            for k in sorted(ends):  # sorted: a set's order changes between processes, and prompts should not
                label = self.case.institution_of(k) or "unknown"
                inst[label] = inst.get(label, 0) + 1
            hop_rows.append({"hop": i, "transactions": len(layer), "accounts": len(ends),
                             "usd": round(sum(e.usd for e in layer), 2), "accounts_by_institution": inst})
        returned = [e for layer in layers[1:] for e in layer
                    if (e.dst if direction == "out" else e.src) == origin]
        convergence = []
        if len(layers) >= 2:
            feeders: dict[str, set[str]] = {}
            for e in layers[1]:
                end, via = (e.dst, e.src) if direction == "out" else (e.src, e.dst)
                if end != origin:
                    feeders.setdefault(end, set()).add(via)
            convergence = sorted(((k, len(v)) for k, v in feeders.items() if len(v) >= 2), key=lambda x: (-x[1], x[0]))[:10]

        word = "sent" if direction == "out" else "received"
        lines = [f"Traced money {word} by {origin} over {len(layers)} hop(s), window {int(window.total_seconds() // 3600)}h:"]
        for row in hop_rows:
            inst = ", ".join(f"{n} at {i}" for i, n in sorted(row["accounts_by_institution"].items()))
            lines.append(f"  hop {row['hop']}: {row['transactions']} tx, {row['accounts']} accounts ({inst}), about {fmt_usd(row['usd'])}")
        if returned:
            lines.append(f"  Money came back to {origin} in {len(returned)} transaction(s) "
                         f"(e.g. {returned[0].tx_id} on {returned[0].ts:%Y-%m-%d %H:%M}): a cycle.")
        if convergence:
            top = convergence[0]
            role = "received from" if direction == "out" else "sent to"
            lines.append(f"  {len(convergence)} account(s) at hop 2 {role} 2+ hop-1 accounts; "
                         f"most: {top[0]} via {top[1]} accounts (scatter-gather shape).")
        cross = sum(1 for layer in layers for e in layer if len(e.seen_in) == 2)
        lines.append(f"  {cross} of the traced transactions cross between Institution A and B.")
        if skipped_hubs:
            lines.append(f"  Not followed: {skipped_hubs} hub account(s) with very many counterparties "
                         f"(merchants or processors), whose onward payments say nothing about this money.")
        data = {"origin": origin, "direction": direction, "hops": hop_rows,
                "returned_to_origin": [e.to_dict() for e in returned[:5]],
                "convergence": [{"account": k, "fed_by": n, "institution": self.case.institution_of(k)}
                                for k, n in convergence],
                "cross_institution_transactions": cross}
        return self.case.add_observation("follow_money",
                                         {"bank_id": bank, "account": acct, "direction": direction, "hops": hops,
                                          "window_hours": int(window.total_seconds() // 3600),
                                          "max_frontier": max_frontier},
                                         "\n".join(lines), data, qids)

    def get_transactions(self, bank_id: Any = None, account: Any = None, direction: str = "both",
                         counterparty: str | None = None, start: str | None = None, end: str | None = None,
                         order_by: str = "usd", limit: int = 20):
        """Individual transactions of an account, optionally with one counterparty or in a time range."""
        bank, acct = self._subject(bank_id, account)
        k = key(bank, acct)
        b, a = bank_literal(bank), account_literal(acct)
        conds = []
        if direction == "out":
            conds.append(f"from_bank = {b} AND from_account = {a}")
        elif direction == "in":
            conds.append(f"to_bank = {b} AND to_account = {a}")
        if counterparty:
            cp = str(counterparty).split(":")[-1]
            c = account_literal(cp)
            conds.append(f"(from_account = {c} OR to_account = {c})")
        if start:
            conds.append(f"ts >= {self._ts(start)}")
        if end:
            conds.append(f"ts <= {self._ts(end)}")
        limit = max(1, min(int(limit), 100))
        usd = usd_sql("amount_paid", "payment_currency")
        order = {"usd": "usd DESC", "time": "ts ASC", "recent": "ts DESC"}.get(order_by, "usd DESC")
        cols = ", ".join(LEDGER_COLS)
        sql = (f"SELECT {cols}, round({usd}, 2) AS usd FROM (SELECT DISTINCT {cols} FROM "
               f"({self._subject_tx_sql(bank, acct, ' AND '.join(conds))}) u) d ORDER BY {order}, tx_id LIMIT {limit}")
        result, qid = self._run(f"transactions of {k}", sql, [*LEDGER_COLS, "usd"])
        records = result.records()
        self.case.add_transactions([{**r, "seen_in": ""} for r in records], qid)
        rows = [{"tx_id": r["tx_id"], "ts": to_iso(r["ts"]),
                 "from": key(r["from_bank"], r["from_account"]), "to": key(r["to_bank"], r["to_account"]),
                 "amount_paid": r["amount_paid"], "payment_currency": r["payment_currency"],
                 "amount_received": r["amount_received"], "receiving_currency": r["receiving_currency"],
                 "payment_format": r["payment_format"], "usd": r["usd"]} for r in records]
        summary = f"{len(rows)} transaction(s) of {k}" + (f" with {counterparty}" if counterparty else "") + \
                  f", ordered by {order_by}."
        if rows:
            summary += f" Largest about {fmt_usd(max(float(r['usd'] or 0) for r in rows))}."
        return self.case.add_observation("get_transactions",
                                         {"bank_id": bank, "account": acct, "direction": direction,
                                          "counterparty": counterparty, "start": start, "end": end,
                                          "order_by": order_by, "limit": limit},
                                         summary, {"transactions": rows}, [qid])

    @staticmethod
    def _ts(value: Any) -> str:
        from .sqlutil import ts_literal

        return ts_literal(value)

    def find_related_accounts(self, bank_id: Any = None, account: Any = None):
        """Other accounts held by the same customer, at either institution, and money moved between them."""
        bank, acct = self._subject(bank_id, account)
        k = key(bank, acct)
        if "customer_id" not in self.case.accounts.get(k, {}):
            self.profile_account(bank, acct)
        customer_id = self.case.accounts.get(k, {}).get("customer_id")
        if not customer_id:
            return self.case.add_observation("find_related_accounts", {"bank_id": bank, "account": acct},
                                             f"No customer id on record for {k}.", {}, [])
        cid = account_literal(customer_id)
        sql = (f"SELECT 'A' AS institution, bank_id, account FROM {self.t('kyc_a')} WHERE customer_id = {cid} "
               f"UNION ALL SELECT 'B' AS institution, bank_id, account FROM {self.t('kyc_b')} WHERE customer_id = {cid}")
        result, qid = self._run(f"accounts of customer {customer_id}", sql, ["institution", "bank_id", "account"])
        related = [{"account": key(r["bank_id"], r["account"]), "institution": r["institution"]}
                   for r in result.records() if key(r["bank_id"], r["account"]) != k]
        own = {k} | {r["account"] for r in related}
        transfers = [e for e in self.case.edges.values() if e.src in own and e.dst in own and not e.is_self]
        summary = (f"Customer {customer_id} holds {len(related)} other account(s)"
                   + (": " + ", ".join(f"{r['account']} ({r['institution']})" for r in related) if related else "")
                   + f". {len(transfers)} known transfer(s) between them.")
        return self.case.add_observation("find_related_accounts", {"bank_id": bank, "account": acct}, summary,
                                         {"customer_id": customer_id, "related": related,
                                          "transfers_between": [e.to_dict() for e in transfers[:20]]}, [qid])

    def _load_watchlist(self) -> tuple[list[dict[str, Any]], str]:
        if self._watchlist is None:
            sql = (f"SELECT list_id, entity_type, name, aliases, birth_or_incorporation, country, program "
                   f"FROM {self.t('watchlist')}")
            result, qid = self._run("load watchlist for screening", sql,
                                    ["list_id", "entity_type", "name", "aliases", "birth_or_incorporation",
                                     "country", "program"])
            entries = []
            for rec in result.records():
                names = [rec["name"]] + [a for a in str(rec.get("aliases") or "").split("|") if a and a != "None"]
                rec["_norms"] = [_norm_name(n) for n in names if n and str(n) != "nan"]
                rec["birth_or_incorporation"] = _date10(rec.get("birth_or_incorporation"))
                entries.append(rec)
            self._watchlist, self._watchlist_qid = entries, qid
            # Word index: only names sharing a word (3+ letters) get the slower similarity check.
            self._watchlist_index: dict[str, set[int]] = {}
            for i, rec in enumerate(entries):
                for norm in rec["_norms"]:
                    for word in norm.split():
                        if len(word) >= 3:
                            self._watchlist_index.setdefault(word, set()).add(i)
        return self._watchlist, self._watchlist_qid  # type: ignore[return-value]

    def screen_watchlist(self, bank_id: Any = None, account: Any = None, include_counterparties: bool = True,
                         threshold: int = 88):
        """Screen the account's owner (and known counterparties) against the watchlist.

        Only Institution A's own customers can be screened by name; B's names
        are withheld by its governed view, which becomes a stated limitation.
        """
        bank, acct = self._subject(bank_id, account)
        k = key(bank, acct)
        targets = [k]
        if include_counterparties:
            targets += [a for a in self.case.accounts if a != k]
        a_side = [t for t in targets if self.case.institution_of(t) in (None, "A")]
        b_side = [t for t in targets if self.case.institution_of(t) == "B"]

        watchlist, wl_qid = self._load_watchlist()
        qids = [wl_qid]
        people: dict[str, dict[str, Any]] = {}
        for start in range(0, len(a_side), MAX_IN_LIST):
            chunk = a_side[start : start + MAX_IN_LIST]
            accounts = in_list(split_key(t)[1] for t in chunk)
            sql = (f"SELECT bank_id, account, full_name, dob_or_incorporation, country, customer_type "
                   f"FROM {self.t('kyc_a')} WHERE account IN {accounts}")
            res, qid = self._run(f"names to screen ({len(chunk)} Institution A customers)", sql,
                                 ["bank_id", "account", "full_name", "dob_or_incorporation", "country", "customer_type"])
            qids.append(qid)
            wanted = set(chunk)
            for rec in res.records():
                ak = key(rec["bank_id"], rec["account"])
                if ak in wanted:
                    rec["dob_or_incorporation"] = _date10(rec["dob_or_incorporation"])
                    people[ak] = rec

        hits = []
        for ak, person in people.items():
            norm = _norm_name(person["full_name"])
            if not norm:
                continue
            candidates: set[int] = set()
            for word in norm.split():
                candidates |= self._watchlist_index.get(word, set())
            for entry in (watchlist[i] for i in sorted(candidates)):
                score = max((_token_sort_ratio(norm, n) for n in entry["_norms"]), default=0)
                if score < threshold:
                    continue
                same_dob = bool(person["dob_or_incorporation"]) and person["dob_or_incorporation"] == entry["birth_or_incorporation"]
                same_country = str(person["country"]) == str(entry["country"])
                strength = "strong" if (same_dob and same_country) else ("partial" if (same_dob or same_country) else "name only")
                hits.append({
                    "account": ak, "is_subject": ak == k, "customer_name": person["full_name"],
                    "customer_dob": person["dob_or_incorporation"], "customer_country": person["country"],
                    "list_id": entry["list_id"], "listed_name": entry["name"], "listed_dob": entry["birth_or_incorporation"],
                    "listed_country": entry["country"], "program": entry["program"], "name_score": round(float(score), 1),
                    "strength": strength,
                })
        hits.sort(key=lambda h: (not h["is_subject"], {"strong": 0, "partial": 1, "name only": 2}[h["strength"]], -h["name_score"]))
        if b_side:
            self.case.limit(f"{len(b_side)} counterparties at Institution B could not be screened by name: "
                            f"B's governed view ({self.t('kyc_b')}) withholds names and dates of birth.")
        subject_hits = [h for h in hits if h["is_subject"]]
        lines = [f"Screened {len(people)} Institution A customer(s) against {len(watchlist)} watchlist entries."]
        if subject_hits:
            h = subject_hits[0]
            lines.append(f"SUBJECT {k} ({h['customer_name']}) matches {h['list_id']} '{h['listed_name']}' "
                         f"({h['program']}), name score {h['name_score']}, {h['strength']} match "
                         f"(listed DOB {h['listed_dob']} vs {h['customer_dob']}; country {h['listed_country']} vs {h['customer_country']}).")
        else:
            lines.append(f"No watchlist match for the subject {k}.")
        cp_hits = [h for h in hits if not h["is_subject"]]
        if cp_hits:
            lines.append(f"{len(cp_hits)} counterparty match(es), best: {cp_hits[0]['account']} "
                         f"'{cp_hits[0]['customer_name']}' ~ '{cp_hits[0]['listed_name']}' ({cp_hits[0]['strength']}).")
        if b_side:
            lines.append(f"{len(b_side)} Institution B counterparties not screenable: names withheld by B.")
        return self.case.add_observation("screen_watchlist",
                                         {"bank_id": bank, "account": acct,
                                          "include_counterparties": include_counterparties},
                                         "\n".join(lines),
                                         {"screened": len(people), "hits": hits[:15],
                                          "unscreenable_institution_b": len(b_side)}, qids)

    def _hub_cut(self, account_keys) -> float:
        """Degree at or above which an account counts as a hub: 3x the median of these accounts, at least 50."""
        from statistics import median

        degrees = [self.case.accounts.get(k, {}).get("degree") for k in account_keys]
        degrees = [d for d in degrees if d is not None]
        return max(50.0, 3.0 * median(degrees)) if degrees else 50.0

    def _close_rings(self, subject: str) -> list[str]:
        """Fetch payments from accounts the money reached to accounts the money came from.

        Each such "bridge" payment can close a ring (subject -> ... -> bridge -> ... -> subject)
        that neither trace alone can see, because a ring's time order may start at any member.
        """
        cut = self._hub_cut({e.dst for e in self.case.subject_edges("out")} | {e.src for e in self.case.subject_edges("in")})

        def ok(k: str) -> bool:
            return k != subject and (self.case.accounts.get(k, {}).get("degree") or 0) < cut

        out1 = {e.dst for e in self.case.subject_edges("out") if ok(e.dst)}
        out2 = {e.dst for e in self.case.edges.values() if e.src in out1 and ok(e.dst)} - out1
        in1 = {e.src for e in self.case.subject_edges("in") if ok(e.src)}
        in2 = {e.src for e in self.case.edges.values() if e.dst in in1 and ok(e.src)} - in1
        reached = (sorted(out1) + sorted(out2))[:300]
        sources = (sorted(in1) + sorted(in2))[:300]
        if not reached or not sources:
            return []
        stamps = [e.ts for e in self.case.edges.values()]
        lo, hi = min(stamps) - timedelta(days=10), max(stamps) + timedelta(days=10)
        to_list = in_list(split_key(k)[1] for k in sources)
        cols = ", ".join(LEDGER_COLS)
        qids = []
        for start in range(0, len(reached), MAX_IN_LIST):
            from_list = in_list(split_key(k)[1] for k in reached[start : start + MAX_IN_LIST])
            where = (f"from_account IN {from_list} AND to_account IN {to_list} "
                     f"AND ts >= TIMESTAMP '{lo:%Y-%m-%d %H:%M:%S}' AND ts <= TIMESTAMP '{hi:%Y-%m-%d %H:%M:%S}'")
            sql = (f"SELECT {cols}, 'A' AS seen_in FROM {self.t('ledger_a')} WHERE {where} "
                   f"UNION ALL SELECT {cols}, 'B' AS seen_in FROM {self.t('ledger_b')} WHERE {where}")
            res, qid = self._run(f"payments closing rings around {subject}", sql, [*LEDGER_COLS, "seen_in"])
            qids.append(qid)
            wanted_src, wanted_dst = set(reached), set(sources)
            keep = [r for r in res.records()
                    if key(r["from_bank"], r["from_account"]) in wanted_src and key(r["to_bank"], r["to_account"]) in wanted_dst]
            self.case.add_transactions(keep, qid)
        return qids

    def _account_degrees(self, account_keys: list[str], purpose: str) -> list[str]:
        """How many distinct counterparties each account has across both ledgers (hub detection)."""
        missing = [k for k in account_keys if "degree" not in self.case.accounts.get(k, {})]
        qids = []
        for start in range(0, len(missing), MAX_IN_LIST):
            chunk = missing[start : start + MAX_IN_LIST]
            accounts = in_list(split_key(k)[1] for k in chunk)
            parts = []
            for ledger in ("ledger_a", "ledger_b"):
                t = self.t(ledger)
                parts.append(f"SELECT from_bank AS bank, from_account AS acct, to_account AS cp FROM {t} "
                             f"WHERE from_account IN {accounts}")
                parts.append(f"SELECT to_bank AS bank, to_account AS acct, from_account AS cp FROM {t} "
                             f"WHERE to_account IN {accounts}")
            sql = (f"SELECT bank, acct, count(DISTINCT cp) AS degree FROM ({' UNION ALL '.join(parts)}) x "
                   f"WHERE acct <> cp GROUP BY bank, acct")
            result, qid = self._run(purpose, sql, ["bank", "acct", "degree"])
            qids.append(qid)
            wanted = set(chunk)
            for rec in result.records():
                k = key(rec["bank"], rec["acct"])
                if k in wanted:
                    self.case.add_account(k, {"degree": int(rec["degree"] or 0), "degree_query_id": qid})
        return qids

    def analyze_network(self, bank_id: Any = None, account: Any = None):
        """Derive laundering-typology signals from everything traced so far for this case."""
        from .analysis import compute_signals

        bank, acct = self._subject(bank_id, account)
        k = key(bank, acct)
        if not self.case.subject_edges("out") and not self.case.subject_edges("in"):
            self.follow_money(bank, acct, "out", hops=2)
            self.follow_money(bank, acct, "in", hops=2)

        # Degrees for the accounts that matter most: direct counterparties first, then the
        # most connected accounts in the traced graph. Hubs (big merchants, processors) make
        # cycles and convergence appear by chance, so the analysis discounts them.
        direct = {e.dst for e in self.case.subject_edges("out")} | {e.src for e in self.case.subject_edges("in")}
        links: dict[str, int] = {}
        for e in self.case.edges.values():
            for node in (e.src, e.dst):
                if node != k:
                    links[node] = links.get(node, 0) + 1
        ranked = sorted(direct, key=lambda n: (-links.get(n, 0), n)) + \
            sorted((n for n in links if n not in direct and links[n] >= 2), key=lambda n: (-links[n], n))
        degree_qids = self._account_degrees([k] + ranked[:399], f"counterparty counts for accounts around {k}")
        degree_qids += self._close_rings(k)

        signals = compute_signals(self.case, k)
        self.case.signals = signals
        lines = [f"{len(signals)} signals for {k}:"]
        for s in signals:
            lines.append(f"  - {s['name']} [{s['assessment']}, {s['strength']}/3]: {s['finding']}")
        qids = sorted({q for s in signals for q in s.get("query_ids", [])} | set(degree_qids))
        return self.case.add_observation("analyze_network", {"bank_id": bank, "account": acct},
                                         "\n".join(lines), {"signals": signals}, qids)

    def run_sql(self, sql: str, purpose: str = "agent query"):
        """A read-only SELECT over the registered tables, capped at 200 rows."""
        text = check_readonly(sql)
        allowed = {self.t(x).lower() for x in ("ledger_a", "kyc_a", "ledger_b", "kyc_b", "watchlist")}
        named = {m.lower() for m in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\b", text)}
        sources = {n for n in named if n.split(".")[0] in {a.split(".")[0] for a in allowed}}
        unknown = {n for n in sources if n not in allowed}
        if unknown:
            raise ToolError(f"Only registered tables may be queried: {', '.join(sorted(allowed))}; "
                            f"not {', '.join(sorted(unknown))}")
        wrapped = f"SELECT * FROM ({text}) q LIMIT 200"
        result, qid = self._run(f"agent SQL: {purpose}"[:200], wrapped, None)
        rows = [{c: (to_iso(v) if hasattr(v, "strftime") else v) for c, v in r.items()} for r in result.records()[:50]]
        summary = f"{result.row_count} row(s); columns: {', '.join(result.columns)}."
        return self.case.add_observation("run_sql", {"sql": text, "purpose": purpose}, summary,
                                         {"columns": result.columns, "rows": rows}, [qid])


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "profile_account",
        "description": "Customer record (Institution A: full; Institution B: governed, no names) and a summary of the "
                       "account's activity across both institutions' ledgers, including turnover vs declared.",
        "parameters": {"bank_id": "integer, optional (defaults to the alerted account)",
                       "account": "string account id, optional"},
    },
    {
        "name": "get_counterparties",
        "description": "Who the account paid (direction='out') or was paid by ('in'), aggregated per counterparty and "
                       "joined to both institutions' customer records. order_by: 'usd' | 'count' | 'recent'.",
        "parameters": {"bank_id": "integer, optional", "account": "string, optional", "direction": "'out' | 'in'",
                       "order_by": "'usd' | 'count' | 'recent'", "limit": "integer <= 50"},
    },
    {
        "name": "follow_money",
        "description": "Trace funds hop by hop across both institutions in time order. 'out' follows what recipients did "
                       "with the money next; 'in' follows where senders got it. Reports cycles back to the origin and "
                       "accounts where several flows converge.",
        "parameters": {"bank_id": "integer, optional", "account": "string, optional", "direction": "'out' | 'in'",
                       "hops": "integer 1-4 (default 2)", "window_hours": "integer (default 72)",
                       "max_frontier": "integer accounts followed per hop (default 30)"},
    },
    {
        "name": "get_transactions",
        "description": "Individual transactions of an account, optionally only with one counterparty or within a time "
                       "range. Use to quote exact amounts and times as evidence.",
        "parameters": {"bank_id": "integer, optional", "account": "string, optional",
                       "direction": "'in' | 'out' | 'both'", "counterparty": "account id, optional",
                       "start": "'YYYY-MM-DD HH:MM', optional", "end": "'YYYY-MM-DD HH:MM', optional",
                       "order_by": "'usd' | 'time' | 'recent'", "limit": "integer <= 100"},
    },
    {
        "name": "find_related_accounts",
        "description": "Other accounts the same customer holds at either institution, and transfers between them "
                       "(tells own-account sweeps apart from third-party layering).",
        "parameters": {"bank_id": "integer, optional", "account": "string, optional"},
    },
    {
        "name": "screen_watchlist",
        "description": "Screen the subject and known counterparties against the sanctions-style watchlist by name, "
                       "date of birth and country. Institution B customers cannot be screened (names withheld).",
        "parameters": {"bank_id": "integer, optional", "account": "string, optional",
                       "include_counterparties": "boolean (default true)"},
    },
    {
        "name": "analyze_network",
        "description": "Compute typology signals from everything traced so far: rapid pass-through, cycles, "
                       "scatter-gather, bursts, amount uniformity, counterparty interconnection, cross-institution "
                       "share, turnover vs declared.",
        "parameters": {"bank_id": "integer, optional", "account": "string, optional"},
    },
    {
        "name": "run_sql",
        "description": "One read-only SELECT over the registered tables (names are given in the system prompt). "
                       "Capped at 200 rows. Use only when no other tool answers the question.",
        "parameters": {"sql": "string, a single SELECT", "purpose": "string, why you are running it"},
    },
]
