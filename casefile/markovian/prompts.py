"""Prompt builder (Markovian spec section 4.4).

Heavy context (memory, attachments) enters chunk 1 only (section 5). Continuation
chunks carry the truncated goal, the carryover state, the [OVERLAP] window when
overlapTokens > 0, and any step input the host injected after a pause marker
(for example a tool result after [NEEDS_TOOL]).
"""

from __future__ import annotations

from typing import Any

from .config import ChunkConfig

MARKER_RULES = (
    "End every non-final chunk with [STATE_CHECKPOINT] followed by a concise state block:\n"
    "COMPLETED: ...\nREMAINING: ...\nDECISIONS: ...\nCONTEXT: ...\n"
    "End the final chunk with [TASK_COMPLETE]. Exactly one marker per chunk. Never narrate the markers."
)


def overlap_slice(previous_cleaned: str, config: ChunkConfig) -> str:
    """The final overlapTokens * 4 characters of the previous chunk's cleaned output."""
    if config.overlap_tokens <= 0 or not previous_cleaned:
        return ""
    return previous_cleaned[-config.overlap_tokens * 4:]


def build_prompt(*, mode: str, goal: str, step: int, carryover: str, overlap_text: str, config: ChunkConfig,
                 memory_context: str | None = None, attachments: list[dict[str, Any]] | None = None,
                 step_input: str | None = None) -> str:
    parts: list[str] = []
    if step == 1:
        if memory_context:
            parts += [memory_context.strip(), ""]
        if mode == "ARCHITECT":
            parts.append("You are executing a long build task in bounded chunks. Analyze the goal, produce a plan, "
                         "and start implementation.")
        else:
            parts.append("You are executing recursive research in bounded chunks. Log every action as '>> action'. "
                         "Produce multi-file style output where useful and keep a research-tree state snapshot.")
        parts += ["", f"User Request: {goal}"]
        for attachment in attachments or []:
            parts += ["", f"[ATTACHMENT {attachment.get('name', 'attachment')}]\n{attachment.get('content', '')}"]
    else:
        parts += [f"Continuing a chunked {mode.lower()} run. Goal (truncated): {goal[:200]}", "",
                  f"[CARRYOVER STATE]\n{carryover}"]
        if overlap_text:
            parts += ["", f"[OVERLAP]\n{overlap_text}"]
        if step_input:
            parts += ["", f"[STEP INPUT]\n{step_input}"]
        parts += ["", "Continue exactly where the state leaves off. Do not repeat completed work."]
    parts += ["", MARKER_RULES]
    return "\n".join(parts)


__all__ = ["MARKER_RULES", "overlap_slice", "build_prompt"]
