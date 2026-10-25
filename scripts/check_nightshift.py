"""Find Meterless Nightshift, check the token, list its routing modes, and send one test request.

Follows the Meterless provider contract (docs/agent-ready/add-meterless-provider.md):
- Auto Detect tries ports 8088, 8090, 8787 and 11434 on 127.0.0.1, localhost and [::1];
- GET /v1/models decides the status: 401/403 is "unauthorized", 200 with meterless ids is "online",
  anything else is "offline" (GET /healthz only proves the process is up);
- every request sends Authorization: Bearer <token> and meterless-app: casefile; chat adds meterless-scope: user.

    python scripts/check_nightshift.py                 # auto-detect, using METERLESS_TOKEN from .env
    python scripts/check_nightshift.py --url http://127.0.0.1:8088
    python scripts/check_nightshift.py --model meterless/text.deep
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from casefile.config import get_settings, normalize_runtime_url  # noqa: E402

PORTS = (8088, 8090, 8787, 11434)
HOSTS = ("127.0.0.1", "localhost", "[::1]")
APP = "casefile"


def _request(url: str, token: str, body: dict | None = None, timeout: float = 5.0) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    req.add_header("Authorization", f"Bearer {token or 'missing'}")
    req.add_header("meterless-app", APP)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.add_header("meterless-scope", "user")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        return 0, str(error)


def status(base: str, token: str) -> tuple[str, list[str]]:
    code, text = _request(f"{base}/models", token)
    if code in (401, 403):
        return "unauthorized", []
    if code == 200:
        try:
            ids = [m.get("id", "") for m in json.loads(text).get("data", [])]
        except (json.JSONDecodeError, AttributeError):
            ids = []
        if any(i.startswith("meterless/") for i in ids):
            return "online", ids
    return "offline", []


def detect(token: str) -> tuple[str | None, str]:
    for port in PORTS:
        for host in HOSTS:
            base = f"http://{host}:{port}/v1"
            state, _ = status(base, token)
            if state in ("online", "unauthorized"):
                return base, state
    return None, "offline"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="runtime address; any of host:port, /v1, /v1/models, /healthz is accepted")
    parser.add_argument("--model", default="meterless/auto")
    args = parser.parse_args()
    settings = get_settings()
    token = os.environ.get("METERLESS_TOKEN") or os.environ.get("METERLESS_RUNTIME_TOKEN") or ""

    base = normalize_runtime_url(args.url) if args.url else None
    if base:
        state, ids = status(base, token)
    else:
        configured = normalize_runtime_url(os.environ.get("METERLESS_BASE_URL") or "")
        state, ids = status(configured, token) if configured else ("offline", [])
        base = configured
        if state == "offline":
            found, state = detect(token)
            base = found or base
            if found:
                state, ids = status(found, token)
    print(f"Nightshift runtime: {base or 'not found'}  status: {state.upper()}")
    if state == "offline":
        print("  Nothing answered on ports 8088, 8090, 8787 or 11434. Is Nightshift running, and (in a Codespace)"
              " is the tunnel from your laptop open? See docs/NIGHTSHIFT.md.")
        return 1
    if state == "unauthorized":
        print("  The runtime is up but refused the token. Paste the token from Nightshift's proxy-token file:"
              " python scripts/codespace/setup_env.py --nightshift")
        return 1
    print("  routing modes: " + ", ".join(i for i in ids if i.startswith("meterless/")))
    others = [i for i in ids if not i.startswith("meterless/")]
    if others:
        print("  other models: " + ", ".join(others[:20]))
    print(f"  Casefile steps: {json.dumps(settings.models)}")

    started = time.perf_counter()
    code, text = _request(f"{base}/chat/completions", token,
                          {"model": args.model, "messages": [{"role": "user", "content": "Reply with exactly one word: ready"}],
                           "max_tokens": 50}, timeout=180)
    seconds = time.perf_counter() - started
    if code != 200:
        print(f"  [FAIL] chat on {args.model}: HTTP {code} {text[:300]}")
        return 1
    reply = json.loads(text)
    content = (reply.get("choices") or [{}])[0].get("message", {}).get("content", "")
    print(f"  [PASS] chat on {args.model}: served by {reply.get('model', '?')} in {seconds:.1f}s, said {content[:40]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
