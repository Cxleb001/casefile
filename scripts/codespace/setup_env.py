"""Answer a few questions and this writes .env for you (keys are typed hidden and never printed).

    python scripts/codespace/setup_env.py            # models + Zetaris
    python scripts/codespace/setup_env.py --models   # only the NVIDIA key
    python scripts/codespace/setup_env.py --zetaris  # only the Zetaris login (after start_zetaris.sh)
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV = ROOT / ".env"


def read_env() -> list[str]:
    if not ENV.exists():
        shutil.copy(ROOT / ".env.example", ENV)
    return ENV.read_text(encoding="utf-8").splitlines()


def set_values(values: dict[str, str]) -> None:
    lines = read_env()
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={values[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(ENV, 0o600)


def ask(prompt: str, default: str = "", secret: bool = False) -> str:
    shown = f" [{default}]" if default and not secret else ""
    answer = (getpass.getpass if secret else input)(f"{prompt}{shown}: ").strip()
    return answer or default


def models() -> None:
    print("\nNemotron: create a key at https://build.nvidia.com/settings/api-keys (starts with nvapi-).")
    key = ask("Paste your NVIDIA key (hidden; Enter to skip)", secret=True)
    if key:
        set_values({"CASEFILE_LLM_PROVIDER": "nvidia", "NVIDIA_API_KEY": key})
        print("  saved NVIDIA_API_KEY")


def zetaris(base_url: str = "http://localhost:3000") -> None:
    print("\nZetaris: use the sign-in shown on the 'Run locally' page (step 7, Sign in).")
    email = ask("Zetaris admin email", "admin@zetaris.com")
    password = ask("Zetaris admin password (hidden)", secret=True)
    values = {"ZETARIS_ROUTE": "proxy", "ZETARIS_BASE_URL": base_url,
              "ZETARIS_USERNAME": email, "ZETARIS_PASSWORD": password,
              "ZETARIS_PG_HOST": "casefile-sources", "CASEFILE_TABLE_KYC_B": "inst_b.kyc_b_shared"}
    set_values(values)
    os.environ.update(values)

    sys.path.insert(0, str(ROOT))
    from dataclasses import replace

    from casefile.config import get_settings
    from casefile.data.zetaris import ZetarisBackend, ZetarisError

    get_settings.cache_clear()
    settings = get_settings()
    print("Finding your organisation id ...")
    for org in ["1", "2", "3", "4", "5", "100", "1000"]:
        try:
            ZetarisBackend(replace(settings, zetaris_org_id=org, zetaris_route="proxy")).query(
                "SELECT 1 AS one", columns=["one"])
            set_values({"ZETARIS_ORG_ID": org})
            print(f"  organisation id {org} works; saved")
            return
        except ZetarisError as error:
            message = str(error)
            if "Cannot reach" in message:
                print(f"  {message[:160]}\n  Is Zetaris running? bash scripts/codespace/start_zetaris.sh")
                return
            if "login failed" in message.lower() or "401" in message:
                print(f"  login failed: {message[:160]}\n  Check the email/password and run this again.")
                return
    print("  No id worked. Find it in the Zetaris UI (Management -> Organisation -> ID) and set ZETARIS_ORG_ID in .env.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", action="store_true")
    parser.add_argument("--zetaris", action="store_true")
    parser.add_argument("--base-url", default="http://localhost:3000", help="Zetaris web UI address")
    args = parser.parse_args()
    both = not (args.models or args.zetaris)
    if args.models or both:
        models()
    if args.zetaris or both:
        zetaris(args.base_url)
    print(f"\n.env updated ({ENV}). It stays out of git.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
