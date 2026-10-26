"""Does Nightshift carry context from one request into the next? A two-request probe.

Request 1 tells the model a random code word. Request 2 is a brand-new conversation that asks whether any
code word was mentioned before. A model that only sees its own request must answer NONE. The probe repeats
this with the same app id, with a different app id for the second request, and without the
meterless-scope header, so the output shows which setting isolates requests.

    python scripts/probe_nightshift_isolation.py
    python scripts/probe_nightshift_isolation.py --model nvidia/nemotron-3-ultra-550b-a55b --repeats 2
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from casefile.config import get_settings, normalize_runtime_url  # noqa: E402

import urllib.error  # noqa: E402
import urllib.request  # noqa: E402


def chat(base: str, token: str, model: str, text: str, app: str, scope: bool) -> str:
    body = {"model": model, "messages": [{"role": "user", "content": text}], "max_tokens": 60, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(f"{base}/chat/completions", data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("meterless-app", app)
    if scope:
        req.add_header("meterless-scope", "user")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            reply = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return f"unreachable ({error})"
    content = (reply.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return " ".join(content.split())[:80]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="nvidia/nemotron-3.5-lightning-30b-a3b")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    settings = get_settings()
    base = normalize_runtime_url(os.environ.get("METERLESS_BASE_URL") or settings.llm_base_url)
    token = os.environ.get("METERLESS_TOKEN") or os.environ.get("METERLESS_RUNTIME_TOKEN") or ""
    run = secrets.token_hex(3)
    setups = [
        ("same app id, scope header", lambda i: (f"casefile-probe-{run}", f"casefile-probe-{run}"), True),
        ("new app id for request 2", lambda i: (f"casefile-probe-{run}-a{i}", f"casefile-probe-{run}-b{i}"), True),
        ("same app id, no scope header", lambda i: (f"casefile-probe2-{run}", f"casefile-probe2-{run}"), False),
    ]
    print(f"Nightshift at {base}, model {args.model}\n")
    check = chat(base, token, args.model, "Reply only: OK", f"casefile-probe-{run}-check", True)
    if check.startswith(("unreachable", "HTTP")):
        print(f"Nightshift did not answer ({check}). Open Nightshift and the tunnel, check with "
              "python scripts/check_nightshift.py, then run this again.")
        return 1
    leaks = {}
    for name, apps, scope in setups:
        leaks[name] = 0
        for i in range(args.repeats):
            word = f"{secrets.choice(['ZEBRA', 'MAPLE', 'COBALT', 'LANTERN'])}-{secrets.randbelow(9000) + 1000}"
            first, second = apps(i)
            chat(base, token, args.model, f"Remember this code word for later: {word}. Reply only: OK", first, scope)
            time.sleep(1)
            answer = chat(base, token, args.model, "Was any code word mentioned to you before this message? "
                          "If yes, reply with it exactly. If not, reply exactly: NONE", second, scope)
            leaked = word.split("-")[1] in answer or word.split("-")[0] in answer.upper()
            leaks[name] += leaked
            print(f"  {name:<30} try {i + 1}: told {word:<14} -> second request answered {answer!r}"
                  + ("   LEAKED" if leaked else ""))
    print()
    for name, n in leaks.items():
        print(f"{'LEAKS' if n else 'isolated':<9} {name}: {n} of {args.repeats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
