"""Typology signals computed from the money-flow graph a case has traced.

These are deterministic and cite the queries their edges came from, so the
model reasons over measured facts instead of eyeballing raw rows. Each signal
says whether it points towards laundering ("suspicious"), towards an innocent
explanation ("benign"), or is background ("context"), with a strength 0-3.
Thresholds are deliberately simple and stated in the finding text so a
reviewer can see why a signal fired.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from statistics import median
from typing import Any

from .case import CaseState, Edge, fmt_usd

LAUNDERING_FORMATS = {"ACH", "Wire", "Cash", "Bitcoin"}
# Account-to-account transfer rails, as opposed to card or cheque purchases at merchants.
TRANSFER_FORMATS = {"ACH", "Wire", "Bitcoin"}
ONE_OFF_DAYS = 3
ONE_OFF_MEDIUM = 4
ONE_OFF_STRONG = 8
ONE_OFF_BOTH = 4


def _signal(name: str, finding: str, value: Any, assessment: str, strength: int, edges: list[Edge] | None = None,
            extra_qids: list[str] | None = None) -> dict[str, Any]:
    ordered: list[str] = []
    for e in sorted(edges or [], key=lambda e: e.ts):
        ordered.extend(sorted(e.query_ids))
    ordered.extend(extra_qids or [])
    qids = list(dict.fromkeys(q for q in ordered if q))
    return {
        "name": name,
        "finding": finding,
        "value": value,
        "assessment": assessment,
        "strength": int(strength),
        "tx_ids": sorted({e.tx_id for e in edges or []})[:40],
        "query_ids": qids,
    }


def _neighbours(case: CaseState, subject: str) -> tuple[list[Edge], list[Edge]]:
    return case.subject_edges("out"), case.subject_edges("in")


def hub_threshold(case: CaseState, subject: str) -> float:
    """Accounts with this many distinct counterparties or more count as hubs (big merchants, processors).

    Relative to the subject's own counterparties so it adapts to each dataset,
    with a floor of 50.
    """
    out_edges, in_edges = _neighbours(case, subject)
    direct = {e.dst for e in out_edges} | {e.src for e in in_edges}
    degrees = [case.accounts.get(c, {}).get("degree") for c in direct]
    degrees = [d for d in degrees if d is not None]
    return max(50.0, 3.0 * median(degrees)) if degrees else 50.0


def is_hub(case: CaseState, account_key: str, threshold: float) -> bool:
    degree = case.accounts.get(account_key, {}).get("degree")
    return degree is not None and degree >= threshold


def _degree_qids(case: CaseState, accounts) -> list[str]:
    return sorted({case.accounts.get(a, {}).get("degree_query_id") for a in accounts} - {None})


def passes_on(incoming: Edge, outgoing: Edge, tolerance: float = 2.5) -> bool:
    """Did the middle account pass on a similar amount?

    Compares what it received (``amount_received``, in its own currency) with
    what it then paid (``amount_paid``, also in its own currency), so no
    exchange rate is involved.
    """
    received, paid = incoming.amount_received, outgoing.amount_paid
    return received > 0 and paid > 0 and received / tolerance <= paid <= received * tolerance


def ring_is_plausible(path: list[Edge]) -> bool:
    """A laundering ring moves a similar amount around in one pass, within days.

    Every hop passes on a similar amount (see ``passes_on``), the whole ring
    spans at most 10 days, and read cyclically the timestamps rise with at most
    one drop (the ring may have started at any member, not necessarily the subject).
    """
    n = len(path)
    if not all(passes_on(path[i], path[(i + 1) % n]) for i in range(n)):
        return False
    times = [e.ts for e in path]
    if max(times) - min(times) > timedelta(days=10):
        return False
    drops = sum(1 for i in range(n) if times[i] > times[(i + 1) % n])
    return drops <= 1


def cycle_signal(case: CaseState, subject: str) -> dict[str, Any] | None:
    """Money that goes round a ring of non-hub accounts and back to the subject."""
    hub = hub_threshold(case, subject)
    by_src: dict[str, list[Edge]] = defaultdict(list)
    for e in case.edges.values():
        if not e.is_self:
            by_src[e.src].append(e)
    found: list[list[Edge]] = []
    budget = 60_000  # expansions; keeps a busy account from exploding the search
    stack: list[tuple[str, list[Edge]]] = []
    for e in by_src.get(subject, []):
        if not is_hub(case, e.dst, hub) and e.amount_paid > 0:
            stack.append((e.dst, [e]))
    while stack and len(found) < 8 and budget > 0:
        budget -= 1
        node, path = stack.pop()
        if len(path) >= 6:
            continue
        on_path = {x.src for x in path}
        for e in by_src.get(node, []):
            if not passes_on(path[-1], e):
                continue
            if e.dst == subject:
                ring = path + [e]
                if ring_is_plausible(ring) and not any({x.tx_id for x in ring} == {y.tx_id for y in f} for f in found):
                    found.append(ring)
                continue
            if e.dst in on_path or is_hub(case, e.dst, hub):
                continue
            stack.append((e.dst, path + [e]))
    if not found:
        return _signal("cycle", "No ring of similar-sized payments through non-hub accounts returns to the subject.",
                       0, "context", 0)
    best = min(found, key=len)
    hops = len(best)
    route = " -> ".join([best[0].src] + [e.dst for e in best])
    span = max(e.ts for e in best) - min(e.ts for e in best)
    strength = 3 if hops >= 3 else 2
    sent = f"{best[0].amount_paid:,.2f} {best[0].payment_currency}"
    back = f"{best[-1].amount_received:,.2f} {best[-1].receiving_currency}"
    return _signal(
        "cycle",
        f"A {hops}-hop ring returns money to the subject ({route}): it sent {sent} and got back {back}; every hop "
        f"passed on a similar amount, all within {span.days}d {span.seconds // 3600}h.",
        {"rings_found": len(found), "hops": hops, "route": route},
        "suspicious", strength, [e for ring in found for e in ring],
        _degree_qids(case, {e.dst for ring in found for e in ring}),
    )


def convergence_signal(case: CaseState, subject: str) -> dict[str, Any] | None:
    """Several of the subject's recipients paying the same next account (scatter-gather),
    or several of its senders funded by the same earlier account (gather-scatter)."""
    out_edges, in_edges = _neighbours(case, subject)
    hub = hub_threshold(case, subject)
    paid: dict[str, list[Edge]] = defaultdict(list)
    for e in out_edges:
        paid[e.dst].append(e)
    got: dict[str, list[Edge]] = defaultdict(list)
    for e in in_edges:
        got[e.src].append(e)

    recipients = {a for a in paid if not is_hub(case, a, hub)}
    senders = {a for a in got if not is_hub(case, a, hub)}
    sinks: dict[str, list[Edge]] = defaultdict(list)
    for e in case.edges.values():
        if (e.src in recipients and e.dst not in (subject, e.src) and not is_hub(case, e.dst, hub)
                and any(p.ts <= e.ts and passes_on(p, e) for p in paid[e.src])):
            sinks[e.dst].append(e)
    sources: dict[str, list[Edge]] = defaultdict(list)
    for e in case.edges.values():
        if (e.dst in senders and e.src not in (subject, e.dst) and not is_hub(case, e.src, hub)
                and any(e.ts <= g.ts and passes_on(e, g) for g in got[e.dst])):
            sources[e.src].append(e)
    best_sink = max(sinks.items(), key=lambda kv: len({e.src for e in kv[1]}), default=None)
    best_src = max(sources.items(), key=lambda kv: len({e.dst for e in kv[1]}), default=None)
    n_sink = len({e.src for e in best_sink[1]}) if best_sink else 0
    n_src = len({e.dst for e in best_src[1]}) if best_src else 0
    if max(n_sink, n_src) < 2:
        return _signal("convergence", "No non-hub account collects from (or funds) several of the subject's "
                                      "counterparties.", 0, "context", 0)
    if n_sink >= n_src:
        feeders = [e for e in out_edges if e.dst in {x.src for x in best_sink[1]}]
        return _signal(
            "convergence",
            f"{n_sink} of the subject's recipients each paid the same account {best_sink[0]} "
            f"with a similar amount (scatter-gather shape; {fmt_usd(sum(e.usd for e in best_sink[1]))} collected).",
            {"shape": "scatter-gather", "account": best_sink[0], "via": n_sink},
            "suspicious", 3 if n_sink >= 3 else 2, feeders + best_sink[1],
            _degree_qids(case, {best_sink[0]} | {e.src for e in best_sink[1]}),
        )
    fed = [e for e in in_edges if e.src in {x.dst for x in best_src[1]}]
    return _signal(
        "convergence",
        f"{n_src} of the subject's senders were each funded with a similar amount by the same account {best_src[0]} "
        f"shortly before paying the subject (gather-scatter shape).",
        {"shape": "gather-scatter", "account": best_src[0], "via": n_src},
        "suspicious", 3 if n_src >= 3 else 2, fed + best_src[1],
        _degree_qids(case, {best_src[0]} | {e.dst for e in best_src[1]}),
    )


def interconnection_signal(case: CaseState, subject: str) -> dict[str, Any] | None:
    """Counterparties of the subject that also pay each other: a closed group rather than a customer base."""
    out_edges, in_edges = _neighbours(case, subject)
    hub = hub_threshold(case, subject)
    cps = {a for a in ({e.dst for e in out_edges} | {e.src for e in in_edges}) if not is_hub(case, a, hub)}
    if len(cps) < 3:
        return None
    links = [e for e in case.edges.values() if e.src in cps and e.dst in cps and not e.is_self]
    pairs = {(e.src, e.dst) for e in links}
    involved = {a for p in pairs for a in p}
    share = len(involved) / len(cps)
    if len(pairs) < 2:
        return _signal("interconnection",
                       f"Only {len(pairs)} payment(s) seen between the subject's {len(cps)} counterparties.",
                       {"links": len(pairs), "counterparties": len(cps)}, "context", 0, links)
    strength = 3 if len(pairs) >= 6 else 2 if len(pairs) >= 3 else 1
    return _signal(
        "interconnection",
        f"{len(pairs)} payment links among the subject's own counterparties ({len(involved)} of {len(cps)} involved, "
        f"{share:.0%}, hubs excluded): they trade with each other, not just with the subject.",
        {"links": len(pairs), "involved": len(involved), "counterparties": len(cps), "share": round(share, 3)},
        "suspicious", strength, links, _degree_qids(case, involved),
    )


def pass_through_signal(case: CaseState, subject: str) -> dict[str, Any] | None:
    """How much of what came in left again quickly (layering) rather than staying."""
    out_edges, in_edges = _neighbours(case, subject)
    if not out_edges or not in_edges:
        return None
    in_usd = sum(e.usd for e in in_edges)
    out_usd = sum(e.usd for e in out_edges)
    window = timedelta(hours=72)
    ins = sorted(in_edges, key=lambda e: e.ts)
    quick = []
    for o in out_edges:
        if any(i.ts <= o.ts <= i.ts + window for i in ins):
            quick.append(o)
    quick_usd = sum(e.usd for e in quick)
    ratio = out_usd / in_usd if in_usd else 0.0
    share = quick_usd / in_usd if in_usd else 0.0
    strength = 2 if (0.6 <= ratio <= 1.1 and share >= 0.6) else 1 if share >= 0.4 else 0
    return _signal(
        "pass_through",
        f"Out/in ratio {ratio:.2f} ({fmt_usd(out_usd)} out vs {fmt_usd(in_usd)} in); "
        f"{fmt_usd(quick_usd)} ({share:.0%} of inflows) left within 72h of an inflow.",
        {"out_in_ratio": round(ratio, 3), "quick_share": round(share, 3)},
        "suspicious" if strength else "context", strength, quick[:30] + ins[:10],
    )


def uniform_series_signal(case: CaseState, subject: str) -> dict[str, Any] | None:
    """A run of similar-sized payments to (or from) different accounts: structured, not organic."""
    best: tuple[int, list[Edge], str] = (0, [], "")
    hub = hub_threshold(case, subject)
    for direction in ("out", "in"):
        edges = sorted(case.subject_edges(direction), key=lambda e: e.ts)
        if len(edges) < 5:
            continue
        cut = sorted(e.usd for e in edges)[int(0.75 * (len(edges) - 1))]
        big = [e for e in edges if e.usd >= cut and e.usd > 0
               and not is_hub(case, e.dst if direction == "out" else e.src, hub)]
        for i, anchor in enumerate(big):
            window = [e for e in big[i:] if e.ts <= anchor.ts + timedelta(days=7)]
            if len(window) < 5:
                continue
            window.sort(key=lambda e: e.usd)
            for lo in range(len(window)):
                band = [e for e in window[lo:] if e.usd <= window[lo].usd * 1.5]
                distinct = {(e.dst if direction == "out" else e.src) for e in band}
                if len(distinct) > best[0]:
                    best = (len(distinct), band, direction)
    n, band, direction = best
    if n < 5:
        return _signal("uniform_amounts", "No run of 5+ similar-sized large payments to different non-hub accounts.",
                       n, "context", 0)
    lo, hi = min(e.usd for e in band), max(e.usd for e in band)
    strength = 2 if n >= 8 else 1
    word = "to" if direction == "out" else "from"
    return _signal(
        "uniform_amounts",
        f"{n} payments {word} different accounts within 7 days, all between {fmt_usd(lo)} and {fmt_usd(hi)} "
        f"in the subject's top quarter by size.",
        {"count": n, "direction": direction, "min_usd": round(lo, 2), "max_usd": round(hi, 2)},
        "suspicious", strength, band,
    )


def burst_signal(case: CaseState, subject: str) -> dict[str, Any] | None:
    edges = sorted(case.subject_edges("out") + case.subject_edges("in"), key=lambda e: e.ts)
    if len(edges) < 5:
        return None
    best_n, best_window = 0, []
    j = 0
    for i, e in enumerate(edges):
        while edges[j].ts < e.ts - timedelta(hours=24):
            j += 1
        window = edges[j : i + 1]
        cps = {(x.dst if x.src == subject else x.src) for x in window}
        if len(cps) > best_n:
            best_n, best_window = len(cps), window
    span_days = max(1.0, (edges[-1].ts - edges[0].ts).total_seconds() / 86400)
    all_cps = {(x.dst if x.src == subject else x.src) for x in edges}
    per_day = len(all_cps) / span_days
    ratio = best_n / per_day if per_day else 0.0
    strength = 1 if ratio >= 3 and best_n >= 6 else 0
    return _signal(
        "burst",
        f"Busiest 24 hours: {best_n} distinct counterparties (average {per_day:.1f} a day, {ratio:.1f}x).",
        {"max_24h": best_n, "avg_per_day": round(per_day, 2)},
        "suspicious" if strength else "context", strength, best_window[:30],
    )


def _densest(edges: list[Edge], days: float) -> list[Edge]:
    """The largest set of these edges that fits in one window of ``days``."""
    edges = sorted(edges, key=lambda e: e.ts)
    best: list[Edge] = []
    for i, first in enumerate(edges):
        window = [e for e in edges[i:] if e.ts <= first.ts + timedelta(days=days)]
        if len(window) > len(best):
            best = window
    return best


def one_off_transfer_signal(case: CaseState, subject: str) -> dict[str, Any] | None:
    """Bank transfers to (or from) many different accounts that the subject deals with only once.

    Ordinary customers pay the same people and merchants again and again; mule
    networks move money in single transfers between accounts with no other
    relationship (fan-out, fan-in, or both through one account: gather-scatter).
    """
    out_edges, in_edges = _neighbours(case, subject)
    hub = hub_threshold(case, subject)

    def one_offs(edges: list[Edge], side: str) -> list[Edge]:
        by_party: dict[str, list[Edge]] = defaultdict(list)
        for e in edges:
            by_party[getattr(e, side)].append(e)
        return [v[0] for party, v in by_party.items()
                if len(v) == 1 and v[0].payment_format in TRANSFER_FORMATS and not is_hub(case, party, hub)]

    sent = _densest(one_offs(out_edges, "dst"), ONE_OFF_DAYS)
    got = _densest(one_offs(in_edges, "src"), ONE_OFF_DAYS)
    n_out, n_in = len(sent), len(got)
    if max(n_out, n_in) < 2:
        return _signal("one_off_transfers", "No burst of one-off bank transfers: the subject's transfer "
                                            "counterparties recur.", {"out": n_out, "in": n_in}, "context", 0)
    if min(n_out, n_in) >= 2 and 4 * min(n_out, n_in) >= max(n_out, n_in):
        shape = "gather-scatter"
    else:
        shape = "fan-out" if n_out >= n_in else "fan-in"
    big, small = max(n_out, n_in), min(n_out, n_in)
    strength = 3 if (big >= ONE_OFF_STRONG or small >= ONE_OFF_BOTH) else 2 if big >= ONE_OFF_MEDIUM or small >= 2 else 1
    parts = []
    if n_out >= 2 or shape == "gather-scatter":
        parts.append(f"paid {n_out} different accounts one transfer each")
    if n_in >= 2 or shape == "gather-scatter":
        parts.append(f"received one transfer each from {n_in} different accounts")
    formats = sorted({e.payment_format for e in sent + got})
    return _signal(
        "one_off_transfers",
        f"Within {ONE_OFF_DAYS} days the subject {' and '.join(parts)} ({'/'.join(formats)}), with no other "
        f"payments to or from them in the traced data ({shape} shape; hubs excluded).",
        {"shape": shape, "out": n_out, "in": n_in, "days": ONE_OFF_DAYS},
        "suspicious", strength, sent + got,
    )


def layered_transfer_signal(case: CaseState, subject: str) -> dict[str, Any] | None:
    """One-off transfers where the money keeps moving by transfer: the recipient passes it on within
    72 hours, or the sender had just been funded by transfer (layering through several accounts)."""
    out_edges, in_edges = _neighbours(case, subject)
    hub = hub_threshold(case, subject)
    window = timedelta(hours=72)
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for e in out_edges:
        counts[("out", e.dst)] += 1
    for e in in_edges:
        counts[("in", e.src)] += 1
    chains: list[tuple[Edge, Edge]] = []
    others = [x for x in case.edges.values() if not x.is_self and x.payment_format in TRANSFER_FORMATS]
    for e in out_edges:
        if counts[("out", e.dst)] != 1 or e.payment_format not in TRANSFER_FORMATS or is_hub(case, e.dst, hub):
            continue
        onward = [f for f in others if f.src == e.dst and f.dst != subject and e.ts <= f.ts <= e.ts + window]
        if onward:
            chains.append((e, min(onward, key=lambda f: f.ts)))
    for e in in_edges:
        if counts[("in", e.src)] != 1 or e.payment_format not in TRANSFER_FORMATS or is_hub(case, e.src, hub):
            continue
        prior = [f for f in others if f.dst == e.src and f.src != subject and e.ts - window <= f.ts <= e.ts]
        if prior:
            chains.append((max(prior, key=lambda f: f.ts), e))
    if not chains:
        return _signal("layered_transfers", "None of the subject's one-off transfers is passed on (or was funded) "
                                            "by another transfer within 72 hours.", 0, "context", 0)
    middles = {(a.dst) for a, _ in chains}
    strength = 2 if len(middles) >= 2 else 1
    return _signal(
        "layered_transfers",
        f"{len(middles)} account(s) in the subject's one-off transfers moved the money on by transfer within 72 hours "
        f"(received and paid onward, or funded just before paying the subject): a layering chain.",
        {"chains": len(middles)}, "suspicious", strength, [x for pair in chains for x in pair],
        _degree_qids(case, middles),
    )


def cross_institution_signal(case: CaseState, subject: str) -> dict[str, Any] | None:
    out_edges, in_edges = _neighbours(case, subject)
    cps = {e.dst for e in out_edges} | {e.src for e in in_edges}
    if not cps:
        return None
    labelled = {c: case.institution_of(c) for c in cps}
    at_b = [c for c, i in labelled.items() if i == "B"]
    cross = [e for e in out_edges + in_edges if len(e.seen_in) == 2]
    share = len(at_b) / len(cps)
    return _signal(
        "cross_institution",
        f"{len(at_b)} of {len(cps)} counterparties ({share:.0%}) bank at Institution B; {len(cross)} of the subject's "
        f"transactions appear in both ledgers. Institution A alone would not see where that money went next.",
        {"counterparties_at_b": len(at_b), "counterparties": len(cps), "share": round(share, 3)},
        "context", 0, cross[:20],
    )


def hub_signal(case: CaseState, subject: str) -> dict[str, Any] | None:
    out_edges, in_edges = _neighbours(case, subject)
    direct = {e.dst for e in out_edges} | {e.src for e in in_edges}
    if not direct:
        return None
    hub = hub_threshold(case, subject)
    hubs = [a for a in direct if is_hub(case, a, hub)]
    hub_usd = sum(e.usd for e in out_edges + in_edges if (e.dst in hubs or e.src in hubs))
    total = sum(e.usd for e in out_edges + in_edges) or 1.0
    own = case.accounts.get(subject, {}).get("degree")
    return _signal(
        "hubs",
        f"{len(hubs)} of {len(direct)} counterparties are high-volume hubs ({int(hub)}+ counterparties each), "
        f"carrying {hub_usd / total:.0%} of the subject's value. Subject itself has "
        f"{own if own is not None else 'unknown'} counterparties.",
        {"hubs": len(hubs), "threshold": int(hub), "hub_value_share": round(hub_usd / total, 3)},
        "context", 0, [], _degree_qids(case, set(hubs) | {subject}),
    )


def channel_signal(case: CaseState, subject: str) -> dict[str, Any] | None:
    edges = case.subject_edges("out") + case.subject_edges("in")
    if not edges:
        return None
    by_format: dict[str, int] = defaultdict(int)
    fx = 0
    for e in edges:
        by_format[e.payment_format] += 1
        fx += e.payment_currency != e.receiving_currency
    risky = sum(n for f, n in by_format.items() if f in LAUNDERING_FORMATS)
    mix = ", ".join(f"{f} {n}" for f, n in sorted(by_format.items(), key=lambda kv: (-kv[1], kv[0])))
    return _signal(
        "channels",
        f"Payment formats: {mix}. {risky / len(edges):.0%} ACH/wire/cash/bitcoin; {fx / len(edges):.0%} change currency.",
        {"formats": dict(by_format), "cross_currency_share": round(fx / len(edges), 3)},
        "context", 0, [],
    )


def turnover_signal(case: CaseState, subject: str) -> dict[str, Any] | None:
    info = case.accounts.get(subject, {})
    declared = float(info.get("declared_monthly_turnover_usd") or 0)
    out_edges = case.subject_edges("out")
    if not declared or not out_edges:
        return None
    span = max(1.0, (max(e.ts for e in out_edges) - min(e.ts for e in out_edges)).total_seconds() / 86400)
    monthly = sum(e.usd for e in out_edges) * 30.0 / span
    ratio = monthly / declared
    strength = 1 if ratio >= 3 else 0
    return _signal(
        "turnover_vs_declared",
        f"Traced outflows run at about {fmt_usd(monthly)} a month against {fmt_usd(declared)} declared ({ratio:.1f}x).",
        {"monthly_usd": round(monthly, 2), "declared_usd": declared, "ratio": round(ratio, 2)},
        "suspicious" if strength else "context", strength, [], [info.get("kyc_query_id")] if info.get("kyc_query_id") else [],
    )


def counterparty_mix_signal(case: CaseState, subject: str) -> dict[str, Any] | None:
    """Who the counterparties are; e.g. a company paying many individuals regularly looks like payroll."""
    out_edges, in_edges = _neighbours(case, subject)
    cps = {e.dst for e in out_edges} | {e.src for e in in_edges}
    types: dict[str, int] = defaultdict(int)
    for c in cps:
        types[str(case.accounts.get(c, {}).get("customer_type") or "unknown")] += 1
    if not cps:
        return None
    mix = ", ".join(f"{t} {n}" for t, n in sorted(types.items(), key=lambda kv: (-kv[1], kv[0])))  # ties: by name
    subject_type = case.accounts.get(subject, {}).get("customer_type")
    individuals = types.get("Individual", 0) / len(cps)
    payroll_like = subject_type in {"Corporation", "Partnership"} and individuals >= 0.7 and len(out_edges) >= 5
    return _signal(
        "counterparty_mix",
        f"Counterparty types: {mix}." + (" A company paying mostly individuals: consistent with payroll." if payroll_like else ""),
        {"types": dict(types)},
        "benign" if payroll_like else "context", 1 if payroll_like else 0, [],
    )


def compute_signals(case: CaseState, subject: str | None = None) -> list[dict[str, Any]]:
    subject = subject or case.subject
    makers = [cycle_signal, one_off_transfer_signal, layered_transfer_signal, convergence_signal, interconnection_signal, pass_through_signal, uniform_series_signal,
              burst_signal, turnover_signal, cross_institution_signal, hub_signal, channel_signal,
              counterparty_mix_signal]
    signals = []
    for make in makers:
        try:
            sig = make(case, subject)
        except Exception as error:  # a broken heuristic must not sink the case
            sig = _signal(make.__name__.replace("_signal", ""), f"Could not compute: {error}", None, "context", 0)
        if sig:
            signals.append(sig)
    order = {"suspicious": 0, "benign": 1, "context": 2}
    signals.sort(key=lambda s: (order[s["assessment"]], -s["strength"]))
    return signals


def suspicion_score(signals: list[dict[str, Any]]) -> int:
    """Sum of suspicious strengths minus benign strengths; a crude prior, not the decision."""
    return sum(s["strength"] for s in signals if s["assessment"] == "suspicious") - \
        sum(s["strength"] for s in signals if s["assessment"] == "benign")
