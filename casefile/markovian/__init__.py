"""Markovian: a Python implementation of the Meterless Markovian engine spec.

Long reasoning runs as a sequence of bounded chunks. Each chunk sees the goal,
a compressed carryover state (COMPLETED / REMAINING / DECISIONS / CONTEXT) and
the latest step input, never the whole history, so per-step context stays flat
as the chain grows. Casefile runs its investigation loop on it.

Spec and reference: https://github.com/meterless/meterless/tree/main/engines/markovian
(Apache-2.0). This package follows the spec's AGENTS.md contract and passes the
same checks as its conformance suite (tests/test_markovian_conformance.py).
"""

from .carryover import CarryoverResult, CarryoverService, canonical_block
from .config import (BOUNDS, DEFAULT_CONFIG, DOC_DEFAULTS, FRAMING_TOKENS, OUTPUT_BUDGET, ChunkConfig,
                     ConfigValidationError, CostModelConstants, estimate_tokens, model_totals, projection_curve,
                     resolve_config, run_efficiency)
from .engine import ChunkInfo, Markovian, MarkovianRun
from .history import HistoryService
from .markers import ParsedMarkers, clean_markers, needs_tool_marker, parse_markers
from .progress import ProgressService, ProgressState
from .prompts import MARKER_RULES, build_prompt, overlap_slice

__all__ = ["Markovian", "MarkovianRun", "ChunkInfo", "ChunkConfig", "ConfigValidationError", "DEFAULT_CONFIG", "BOUNDS",
           "FRAMING_TOKENS", "OUTPUT_BUDGET", "estimate_tokens", "resolve_config", "projection_curve", "run_efficiency",
           "model_totals", "CostModelConstants", "DOC_DEFAULTS", "parse_markers", "clean_markers", "needs_tool_marker",
           "ParsedMarkers", "CarryoverService", "CarryoverResult", "canonical_block", "HistoryService", "ProgressService",
           "ProgressState", "build_prompt", "overlap_slice", "MARKER_RULES"]
