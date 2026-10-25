"""State carryover (Markovian spec section 4.3): the compression cascade, in strict order.

1. the model's own state block after the marker (accepted iff >= 24 chars and within budget)
2. LLM compression through an injected compressor
3. heuristic key-phrase extraction
4. tail truncation to the token budget

Each level falls through on failure, so a chain survives model or API instability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .config import ChunkConfig, estimate_tokens

_KEY_PHRASE = re.compile(r"(?:^|\n|\.\s+)((?:Therefore|Key insight|Status|Decision|Decided|Next|Remaining|Completed)[^.\n]*)",
                         re.IGNORECASE)

Compressor = Callable[[str, str], str]


@dataclass
class CarryoverResult:
    carryover: str
    level: str  # "marker" | "llm" | "heuristic" | "truncation"


def canonical_block(completed: str, remaining: str, decisions: str, context: str) -> str:
    return f"COMPLETED: {completed}\nREMAINING: {remaining}\nDECISIONS: {decisions}\nCONTEXT: {context}"


class CarryoverService:
    def __init__(self, compressor: Compressor | None = None) -> None:
        self.compressor = compressor

    def next(self, *, marker_state: str | None, previous_carryover: str, chunk_text: str,
             config: ChunkConfig) -> CarryoverResult:
        budget = config.carryover_tokens

        if marker_state is not None:
            sanitized = re.sub(r"[<>\[\]@-]{3,}", "", marker_state).strip()
            if len(sanitized) >= 24 and estimate_tokens(sanitized) <= budget:
                return CarryoverResult(sanitized, "marker")

        if self.compressor is not None:
            try:
                prompt = (f"Previous state:\n{previous_carryover or '(none)'}\n\nLatest chunk (tail):\n{chunk_text[-2000:]}\n\n"
                          "Compress into 3-5 critical points as a labeled block:\n"
                          "COMPLETED: ...\nREMAINING: ...\nDECISIONS: ...\nCONTEXT: ...")
                compressed = (self.compressor(prompt, "You compress reasoning state. Be terse and lossless on decisions.")
                              or "").strip()
                if len(compressed) >= 24 and estimate_tokens(compressed) <= budget:
                    return CarryoverResult(compressed, "llm")
            except Exception:  # noqa: BLE001  fall through to the heuristics
                pass

        phrases = [m.group(1).strip() for m in _KEY_PHRASE.finditer(chunk_text)][:5]
        if phrases:
            summary = "; ".join(phrases)
            if estimate_tokens(summary) <= budget:
                return CarryoverResult(summary, "heuristic")

        words = (chunk_text or previous_carryover or "").split()
        tail = ""
        for word in reversed(words):
            candidate = word + (" " + tail if tail else "")
            if estimate_tokens(candidate) > budget:
                break
            tail = candidate
        return CarryoverResult(tail, "truncation")


__all__ = ["CarryoverService", "CarryoverResult", "canonical_block"]
