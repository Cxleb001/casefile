"""Python side of the bridge that lets Meterless's own TypeScript conformance runner test casefile.markovian.

    python markovian_bridge.py model_totals 20
    python markovian_bridge.py run_efficiency      < {"chunks": [...], "config": {...}}
    python markovian_bridge.py validate            < {"chunkSize": ...}
    python markovian_bridge.py run                 # JSON lines: see markovian_bridge.ts

In "run" mode the engine's generator is the TypeScript runner's: every call is
written to stdout as {"type": "generate", ...} and answered on stdin with
{"text": ...} or {"error": ...}. The finished run is written as {"type": "result", "run": {...}}.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from casefile.markovian import ConfigValidationError, Markovian, model_totals, resolve_config, run_efficiency  # noqa: E402


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    mode = sys.argv[1]
    if mode == "model_totals":
        emit(model_totals(int(sys.argv[2])))
        return 0
    if mode == "run_efficiency":
        payload = json.loads(sys.stdin.read())
        emit(run_efficiency(payload["chunks"], payload["config"]))
        return 0
    if mode == "validate":
        try:
            emit({"ok": True, "config": resolve_config(json.loads(sys.stdin.read() or "{}")).to_dict()})
        except ConfigValidationError as error:
            emit({"ok": False, "error": str(error)})
        return 0
    if mode == "run":
        request = json.loads(sys.stdin.readline())

        def generator(prompt, attachments, system_prompt=None):
            emit({"type": "generate", "prompt": prompt, "system": system_prompt})
            answer = json.loads(sys.stdin.readline())
            if "error" in answer:
                raise RuntimeError(answer["error"])
            return {"text": answer.get("text", "")}

        engine = Markovian(request.get("config") or {}, clock=(lambda: request["clock"]) if request.get("clock") else None)
        run = engine.run(request["goal"], generator=generator, reflect=request.get("reflect", True))
        emit({"type": "result", "run": run.to_dict()})
        return 0
    raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
