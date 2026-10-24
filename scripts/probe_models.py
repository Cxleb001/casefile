"""Try every Nemotron model your NVIDIA key can see and report which ones actually answer.

The model list can include models whose hosted endpoint is down for your account
(they fail with "Function ... Not found for account"). This calls each one with a
tiny prompt and suggests .env lines for the three steps from the ones that work.

    python scripts/probe_models.py
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openai import OpenAI  # noqa: E402

from casefile.config import get_settings  # noqa: E402

SKIP = ("reward", "embed", "retriever", "rerank", "safety", "guard", "parse", "ocr", "-vl", "vlm", "audio", "asr")
# Preferred models per step, best first (substring match on the model id).
PREFER = {
    "plan": ["nemotron-3-super", "nemotron-super", "nemotron-3-ultra", "nemotron-ultra", "nemotron-3-nano", "nemotron"],
    "verify": ["nemotron-3-super", "nemotron-super", "nemotron-3-ultra", "nemotron-ultra", "nemotron-3-nano", "nemotron"],
    "fast": ["nemotron-3-nano", "nemotron-nano-9b", "nemotron-nano", "nemotron-3-super", "nemotron"],
}


def probe(client: OpenAI, model: str) -> tuple[str, bool, str]:
    started = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly one word: ready"}],
            max_tokens=1024,
            temperature=0.6,
        )
        seconds = time.perf_counter() - started
        message = response.choices[0].message if response.choices else None
        content = (getattr(message, "content", None) or "").strip()
        if not content:
            return model, False, f"answered in {seconds:.1f}s but with no text (all reasoning?)"
        return model, True, f"{seconds:.1f}s, said {content[:30]!r}"
    except Exception as error:  # report the provider's own message, shortened
        text = " ".join(str(error).split())
        return model, False, text[:140]


def main() -> int:
    settings = get_settings()
    if settings.llm_provider != "nvidia" or not settings.llm_api_key:
        print("Set CASEFILE_LLM_PROVIDER=nvidia and NVIDIA_API_KEY in .env first "
              "(python scripts/codespace/setup_env.py --models).")
        return 1
    client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=60, max_retries=0)
    models = sorted(m.id for m in client.models.list().data)
    candidates = [m for m in models if "nemotron" in m.lower() and not any(s in m.lower() for s in SKIP)]
    print(f"Trying {len(candidates)} Nemotron chat models (about a minute) ...\n")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = sorted(pool.map(lambda m: probe(client, m), candidates))
    working = [model for model, ok, _ in results if ok]
    for model, ok, detail in results:
        print(f"  [{'OK  ' if ok else 'FAIL'}] {model}: {detail}")

    if not working:
        print("\nNo Nemotron model answered. Check the key at https://build.nvidia.com/settings/api-keys "
              "and try again in a few minutes.")
        return 1

    picks = {}
    for step, patterns in PREFER.items():
        for pattern in patterns:
            match = next((m for m in working if pattern in m.lower()), None)
            if match:
                picks[step] = match
                break
    print("\nSuggested lines for .env (edit them in, replacing the empty CASEFILE_MODEL_* lines):")
    for step in ("fast", "plan", "verify"):
        if step in picks:
            print(f"  CASEFILE_MODEL_{step.upper()}={picks[step]}")
    current = settings.models
    unchanged = [s for s in picks if picks[s] == current.get(s)]
    if len(unchanged) == len(picks):
        print("  (these are already your settings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
