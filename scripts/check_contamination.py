"""Did any model reply see content from outside its own request? Reads a run's recorded calls.

Two tell-tales, both measured against what Casefile actually sent (the recording keeps every message):
- an account id in a reply that never appears in that case's own prompts, but does in another case's;
- a planner reply that talks about verifying claims (C1-C8, X1-X3, verdicts): that is the verifier's
  task, which the planner is never given.

    python scripts/check_contamination.py runs/eval/<name> [runs/eval/<other> ...]
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

ACCOUNT = re.compile(r"\b\d{1,6}:[0-9A-F]{8,9}\b")
OFF_TASK = re.compile(r"verdicts?\b|C1-C\d|X1-X\d|evaluate (each|the remaining|all) claims?", re.IGNORECASE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", type=Path)
    args = parser.parse_args()
    for run in args.runs:
        calls = []
        for path in sorted((run / "llm").glob("*.jsonl")):
            with open(path, encoding="utf-8") as handle:
                calls += [json.loads(line) for line in handle if line.strip()]
        own: dict[str, set[str]] = collections.defaultdict(set)
        for call in calls:
            own[call["case_id"]] |= set(ACCOUNT.findall(json.dumps(call["messages"])))
        owners: dict[str, set[str]] = collections.defaultdict(set)
        for case_id, accounts in own.items():
            for account in accounts:
                owners[account].add(case_id)
        plan = [c for c in calls if c["step"] == "plan" and "investigating one transaction-monitoring alert"
                in c["messages"][0]["content"]]
        off_task = [c for c in plan if OFF_TASK.search(c["content"] or "")]
        foreign = []
        for call in calls:
            if call["step"] == "verify":
                continue
            stray = [a for a in set(ACCOUNT.findall(call["content"] or "")) - own[call["case_id"]] if owners.get(a)]
            if stray:
                foreign.append((call, stray))
        statuses = collections.Counter()
        for path in (run / "cases").glob("*.report.json"):
            engine = json.loads(path.read_text(encoding="utf-8")).get("investigation", {}).get("engine") or {}
            statuses[engine.get("status", "none")] += 1
        print(f"## {run.name}: {len(calls)} model calls, {len(plan)} planner steps")
        print(f"  planner replies about verifying claims (another request's task): {len(off_task)} of {len(plan)}")
        print(f"  replies naming another case's account: {len(foreign)}")
        print(f"  investigations ended: {dict(statuses)}")
        for call in off_task[:2]:
            print(f"    e.g. {call['case_id']}: {' '.join(call['content'].split())[:160]}")
        for call, stray in foreign[:2]:
            print(f"    e.g. {call['case_id']} ({call['step']}) named {', '.join(stray[:3])}, "
                  f"which only {', '.join(sorted(owners[stray[0]]))[:80]} sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
