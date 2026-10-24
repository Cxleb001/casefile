"""Register Casefile's sources in Zetaris through its API, one statement at a time, then check them.

The same statements as `python scripts/publish_local.py sql`, run for you, so you don't need
the Zetaris web UI. Works for a local install or one in a Codespace.

    python scripts/zetaris_apply.py --find-org     # which organisation id your login works with
    python scripts/zetaris_apply.py --dry-run      # show the statements, run nothing
    python scripts/zetaris_apply.py                # register the sources and check them

Needs in .env: ZETARIS_ROUTE=proxy, ZETARIS_BASE_URL (http://localhost:3000), ZETARIS_USERNAME,
ZETARIS_PASSWORD, ZETARIS_ORG_ID. The database password comes from LOCAL_PG_PASSWORD
(default casefile-local) and is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from casefile.config import get_settings  # noqa: E402
from casefile.data.zetaris import ZetarisBackend, ZetarisError  # noqa: E402

CHECKS = [
    ("inst_a.ledger_a", "SELECT count(*) AS n FROM inst_a.ledger_a"),
    ("inst_a.kyc_a", "SELECT count(*) AS n FROM inst_a.kyc_a"),
    ("inst_b.ledger_b", "SELECT count(*) AS n FROM inst_b.ledger_b"),
    ("inst_b.kyc_b_shared", "SELECT count(*) AS n FROM inst_b.kyc_b_shared"),
    ("compliance.watchlist", "SELECT count(*) AS n FROM compliance.watchlist"),
    ("PII withheld", "SELECT * FROM inst_b.kyc_b_shared LIMIT 1"),
]


def statements(sql: str) -> list[str]:
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    parts = re.split(r";\s*(?:\n|$)", "\n".join(lines))
    return [p.strip() for p in parts if p.strip()]


def backend_for(settings, org_id: str | None = None) -> ZetarisBackend:
    if org_id:
        settings = replace(settings, zetaris_org_id=org_id)
    if settings.zetaris_route != "proxy" and not settings.zetaris_api_key:
        settings = replace(settings, zetaris_route="proxy")
    return ZetarisBackend(settings)


def find_org(settings) -> int:
    for org in ["1", "2", "3", "4", "5", "100", "1000"]:
        try:
            backend_for(settings, org).query("SELECT 1 AS one", columns=["one"])
            print(f"Organisation id {org} works. Put ZETARIS_ORG_ID={org} in .env")
            return 0
        except ZetarisError as error:
            print(f"  {org}: {str(error)[:120]}")
    print("No id worked. Find it in the Zetaris UI under Management -> Organisation -> ID.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--find-org", action="store_true")
    parser.add_argument("--checks-only", action="store_true")
    args = parser.parse_args()
    settings = get_settings()

    if args.find_org:
        return find_org(settings)

    from publish_local import local_settings  # same values publish_local uses

    cfg = local_settings()
    template = (ROOT / "scripts" / "zetaris" / "register_local.sql").read_text(encoding="utf-8")
    port = "5432" if cfg["zetaris_host"] == "casefile-sources" else cfg["port"]
    for name, value in {"ZETARIS_PG_HOST": cfg["zetaris_host"], "LOCAL_PG_PORT": port,
                        "LOCAL_PG_USER": cfg["user"]}.items():
        template = template.replace("{{" + name + "}}", value)
    todo = statements(template)

    backend = backend_for(settings)
    if not args.checks_only:
        for i, stmt in enumerate(todo, 1):
            shown = stmt.replace("<LOCAL_PG_PASSWORD>", "********")
            print(f"\n[{i}/{len(todo)}] {shown.splitlines()[0][:110]}")
            if args.dry_run:
                continue
            try:
                payload = backend.raw(stmt.replace("<LOCAL_PG_PASSWORD>", cfg["password"]))
                print("  ok", json.dumps(payload)[:160] if payload else "")
            except ZetarisError as error:
                text = str(error)
                if "already exist" in text.lower():
                    print("  already there, skipping")
                else:
                    print(f"  FAILED: {text[:400]}")
                    print("  Fix the cause, then run this script again (finished steps are skipped).")
                    return 1
    if args.dry_run:
        return 0

    print("\nChecks")
    ok = True
    for label, sql in CHECKS:
        try:
            result = backend.query(sql)
            if label == "PII withheld":
                leaked = {"full_name", "dob_or_incorporation", "national_id"} & {c.lower() for c in result.columns}
                ok &= not leaked
                print(f"  [{'PASS' if not leaked else 'FAIL'}] {label}: columns {', '.join(result.columns)}")
            else:
                print(f"  [PASS] {label}: {result.rows[0][0] if result.rows else '?'} rows ({result.duration_ms} ms)")
        except ZetarisError as error:
            ok = False
            print(f"  [FAIL] {label}: {str(error)[:200]}")
    if ok:
        print("\nAll registered. Set CASEFILE_DATA_BACKEND=zetaris and CASEFILE_TABLE_KYC_B=inst_b.kyc_b_shared in .env,")
        print("then run: python scripts/check_setup.py --data")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
