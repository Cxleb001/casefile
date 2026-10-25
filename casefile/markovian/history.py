"""Persistent run history (Markovian spec section 4.7), stored as JSON.

Every record carries its own chunkConfig snapshot (mandatory, section 2.3), and
historical efficiency is always recomputed against that snapshot (section 14).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .config import run_efficiency


class HistoryService:
    def __init__(self, history_dir: str | Path | None = None) -> None:
        self.runs: list[dict[str, Any]] = []
        self.file: Path | None = None
        self._subscribers: list[Callable[[], None]] = []
        if history_dir:
            directory = Path(history_dir)
            directory.mkdir(parents=True, exist_ok=True)
            self.file = directory / "history.json"
            if self.file.exists():
                try:
                    self.runs = json.loads(self.file.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    self.runs = []

    def record(self, run: dict[str, Any]) -> None:
        if not run.get("chunkConfig"):
            raise ValueError("Markovian: run records require a chunkConfig snapshot (spec section 2.3)")
        self.runs.append(run)
        self._persist()
        for fn in list(self._subscribers):
            fn()

    def list(self) -> list[dict[str, Any]]:
        return list(self.runs)

    def get(self, run_id: str) -> dict[str, Any] | None:
        return next((r for r in self.runs if r.get("id") == run_id), None)

    def delete(self, run_id: str) -> None:
        self.runs = [r for r in self.runs if r.get("id") != run_id]
        self._persist()

    def clear(self) -> None:
        self.runs = []
        self._persist()

    def subscribe(self, fn: Callable[[], None]) -> Callable[[], None]:
        self._subscribers.append(fn)
        return lambda: self._subscribers.remove(fn) if fn in self._subscribers else None

    def cumulative_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {"totalRuns": len(self.runs),
                                 "completedRuns": sum(r.get("status") == "completed" for r in self.runs),
                                 "totalTokensUsed": 0, "totalTokensSaved": 0, "averageEfficiency": 0.0,
                                 "totalChunksProcessed": 0, "lastRunTimestamp": 0, "efficiencyHistory": []}
        for run in self.runs:
            eff = run_efficiency(run["chunks"], run["chunkConfig"])
            stats["totalTokensUsed"] += eff["totalTokensUsed"]
            stats["totalTokensSaved"] += eff["tokensSaved"]
            stats["totalChunksProcessed"] += len(run["chunks"])
            stats["lastRunTimestamp"] = max(stats["lastRunTimestamp"], run.get("timestamp", 0))
            stats["efficiencyHistory"].append({"timestamp": run.get("timestamp", 0),
                                               "efficiency": eff["efficiencyPercent"], "tokensSaved": eff["tokensSaved"]})
        if self.runs:
            stats["averageEfficiency"] = sum(e["efficiency"] for e in stats["efficiencyHistory"]) / len(self.runs)
        return stats

    def performance_by_step(self) -> list[dict[str, float]]:
        """Actual per-step averages for the engine view (section 8.1, actual mode)."""
        buckets: dict[int, list[int]] = {}
        for run in self.runs:
            for chunk in run["chunks"]:
                buckets.setdefault(int(chunk["id"]), []).append(int(chunk["tokens"]))
        return [{"step": step, "avgTokens": sum(v) / len(v), "samples": len(v)} for step, v in sorted(buckets.items())]

    def _persist(self) -> None:
        if self.file:
            self.file.write_text(json.dumps(self.runs, indent=2, default=str) + "\n", encoding="utf-8")


__all__ = ["HistoryService"]
