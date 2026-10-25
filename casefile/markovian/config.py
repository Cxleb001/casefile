"""Chunk config, bounds and the efficiency math (Markovian spec sections 2.1, 4.1, 6 and 14).

Validation raises a typed error; nothing is silently truncated or ignored.
Field names follow the spec's canonical camelCase when serialised.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True)
class ChunkConfig:
    chunk_size: int = 8000        # target generation budget per chunk
    max_chunks: int = 24          # recursion cap
    carryover_tokens: int = 800   # carryover state budget
    overlap_tokens: int = 0       # overlap window; implemented, never silently ignored

    def to_dict(self) -> dict[str, int]:
        return {"chunkSize": self.chunk_size, "maxChunks": self.max_chunks,
                "carryoverTokens": self.carryover_tokens, "overlapTokens": self.overlap_tokens}


DEFAULT_CONFIG = ChunkConfig()

BOUNDS = {
    "chunk_size": (1000, 128000),
    "max_chunks": (1, 32),
    "carryover_tokens": (128, 32768),
    "overlap_tokens": (0, 4096),
}

# Engine constants for load-time validation (spec section 2.1).
FRAMING_TOKENS = 400
OUTPUT_BUDGET = 1200

_ALIASES = {"chunkSize": "chunk_size", "tokensPerChunk": "chunk_size", "maxChunks": "max_chunks",
            "carryoverTokens": "carryover_tokens", "overlapTokens": "overlap_tokens"}


class ConfigValidationError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(f"Markovian config: {message}")


def estimate_tokens(text: str) -> int:
    """The reference estimator, ceil(chars / 4). Numbers derived from it are labelled "estimated"."""
    return math.ceil(len(text or "") / 4)


def resolve_config(partial: dict[str, Any] | ChunkConfig | None = None) -> ChunkConfig:
    """Merge a partial config over the defaults and validate it.

    Ruling (same as the reference): the spec's slider "step" values are UI
    granularity, not validation; only min and max are enforced, otherwise the
    documented default carryoverTokens=800 would itself be rejected.
    """
    if isinstance(partial, ChunkConfig):
        values = {f.name: getattr(partial, f.name) for f in fields(ChunkConfig)}
    else:
        values = {f.name: getattr(DEFAULT_CONFIG, f.name) for f in fields(ChunkConfig)}
        for key, value in (partial or {}).items():
            name = _ALIASES.get(key, key)
            if name not in values:
                raise ConfigValidationError(f"unknown field {key!r}")
            values[name] = value
    for name, (low, high) in BOUNDS.items():
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ConfigValidationError(f"{name} must be a number")
        if value < low or value > high:
            raise ConfigValidationError(f"{name}={value} outside [{low}, {high}]")
        values[name] = int(value)
    if values["carryover_tokens"] <= 0:
        raise ConfigValidationError("carryover_tokens must be > 0")
    needed = FRAMING_TOKENS + values["carryover_tokens"] + OUTPUT_BUDGET
    if values["chunk_size"] < needed:
        raise ConfigValidationError(f"chunk_size={values['chunk_size']} < framing({FRAMING_TOKENS}) + "
                                    f"carryover({values['carryover_tokens']}) + outputBudget({OUTPUT_BUDGET})")
    return ChunkConfig(**values)


def projection_curve(config: ChunkConfig, steps: int = 10) -> list[dict[str, int]]:
    """Projected standard vs Markovian per-step cost (section 4.1)."""
    return [{"step": step, "standard": (step - 1) * config.chunk_size + config.chunk_size,
             "markovian": config.carryover_tokens + config.chunk_size} for step in range(1, steps + 1)]


def run_efficiency(chunks: list[Any], config: ChunkConfig | dict[str, Any]) -> dict[str, float]:
    """Historical run efficiency (section 6.3), always against the config passed in: the run's own snapshot."""
    cfg = config if isinstance(config, ChunkConfig) else resolve_config(config)
    tokens = [int(c["tokens"] if isinstance(c, dict) else c.tokens) for c in chunks]
    total = sum(tokens)
    standard, prefix = 0, 0
    for t in tokens:
        standard += prefix + t
        prefix += t
    markovian = len(tokens) * cfg.carryover_tokens + total
    saved = max(0, standard - markovian)
    return {"totalTokensUsed": total, "standardCost": standard, "markovianCost": markovian,
            "tokensSaved": saved, "efficiencyPercent": (saved / standard * 100) if standard > 0 else 0.0}


@dataclass(frozen=True)
class CostModelConstants:
    F: int = 400   # framing
    G: int = 200   # goal
    S: int = 300   # step input
    O: int = 1200  # per-step output  # noqa: E741
    C: int = 800   # carryover


DOC_DEFAULTS = CostModelConstants()


def model_totals(n: int, k: CostModelConstants = DOC_DEFAULTS) -> dict[str, float]:
    """The symbolic cost model of the spec's efficiency-model doc.

    naive_total = N*(F+G+S) + O*N*(N-1)/2 ;  markovian_total = N*(F+G+C+S)
    """
    naive = n * (k.F + k.G + k.S) + (k.O * n * (n - 1)) // 2
    markovian = n * (k.F + k.G + k.C + k.S)
    savings = naive - markovian
    return {"naiveTotal": naive, "markovianTotal": markovian, "savings": savings,
            "efficiency": savings / naive if naive else 0.0}


__all__ = ["ChunkConfig", "DEFAULT_CONFIG", "BOUNDS", "FRAMING_TOKENS", "OUTPUT_BUDGET", "ConfigValidationError",
           "estimate_tokens", "resolve_config", "projection_curve", "run_efficiency", "CostModelConstants",
           "DOC_DEFAULTS", "model_totals"]
