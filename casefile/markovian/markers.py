"""Marker protocol (Markovian spec section 3 and docs/marker-protocol.md).

Canonical wire format is square brackets: [STATE_CHECKPOINT] + state block,
[TASK_COMPLETE], and the pause markers [NEEDS_TOOL tool="..." input="..."] and
[NEEDS_CLARIFICATION]. Backward-compatible and XML-alternate forms are accepted
on parse, never emitted. Cleaning strips every form from display text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_STATE_BLOCKS = [re.compile(r"<CARRYOVER>([\s\S]*?)</CARRYOVER>", re.IGNORECASE)]
_STATE_TAILS = [
    re.compile(r"\[STATE_CHECKPOINT\]\s*([\s\S]*)$", re.IGNORECASE),
    re.compile(r"@@@STATE@@@\s*([\s\S]*)$"),
    re.compile(r"(?<!_)\[STATE\]\s*([\s\S]*)$"),
    re.compile(r"---STATE---\s*([\s\S]*)$"),
]
_FINAL = [re.compile(r"\[TASK_COMPLETE\]", re.IGNORECASE), re.compile(r"@@@FINAL@@@"), re.compile(r"(?<!_)\[FINAL\]"),
          re.compile(r"---FINAL---"), re.compile(r"<DONE\s*/?>", re.IGNORECASE)]
_PROGRESS = re.compile(r"<PROGRESS>([\s\S]*?)</PROGRESS>", re.IGNORECASE)
_NEEDS_TOOL = re.compile(r"[<\[]NEEDS_TOOL\s+tool=\"([^\"]+)\"\s+input=(?:\"([^\"]*)\"|'([^']*)')\s*/?[\]>]", re.IGNORECASE)
_NEEDS_CLARIFICATION = re.compile(r"[<\[]NEEDS_CLARIFICATION[\]>]\s*([^\n]*)", re.IGNORECASE)


@dataclass
class ParsedMarkers:
    state: str | None            # text captured after/inside a state marker
    done: bool                   # final completion marker present
    progress: str | None
    needs_tool: dict[str, str] | None
    needs_clarification: str | None
    cleaned: str                 # display-safe content, all markers stripped


def parse_markers(raw: str) -> ParsedMarkers:
    raw = raw or ""
    state = None
    for pattern in _STATE_BLOCKS:
        matches = pattern.findall(raw)
        if matches:
            state = matches[-1].strip()
    if state is None:
        for pattern in _STATE_TAILS:
            m = pattern.search(raw)
            if m:
                state = m.group(1).strip()
                break
    progress = _PROGRESS.search(raw)
    tool = _NEEDS_TOOL.search(raw)
    clarification = _NEEDS_CLARIFICATION.search(raw)
    return ParsedMarkers(
        state=state,
        done=any(p.search(raw) for p in _FINAL),
        progress=progress.group(1).strip() if progress else None,
        needs_tool={"tool": tool.group(1), "input": tool.group(2) if tool.group(2) is not None else (tool.group(3) or "")}
        if tool else None,
        needs_clarification=clarification.group(1).strip() if clarification else None,
        cleaned=clean_markers(raw),
    )


def clean_markers(raw: str) -> str:
    out = raw or ""
    out = re.sub(r"<CARRYOVER>[\s\S]*?</CARRYOVER>", "", out, flags=re.IGNORECASE)
    out = re.sub(r"<PROGRESS>[\s\S]*?</PROGRESS>", "", out, flags=re.IGNORECASE)
    out = re.sub(r"[<\[]NEEDS_TOOL[^\]>]*[\]>]", "", out, flags=re.IGNORECASE)
    out = re.sub(r"[<\[]NEEDS_CLARIFICATION[\]>][^\n]*", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\[STATE_CHECKPOINT\][\s\S]*$", "", out, flags=re.IGNORECASE)
    out = re.sub(r"@@@STATE@@@[\s\S]*$", "", out)
    out = re.sub(r"---STATE---[\s\S]*$", "", out)
    out = re.sub(r"\n\[STATE\][\s\S]*$", "", out)  # bare [STATE] only at a line start, so prose survives
    for pattern in (r"\[TASK_COMPLETE\]", r"@@@FINAL@@@", r"---FINAL---", r"<DONE\s*/?>"):
        out = re.sub(pattern, "", out, flags=re.IGNORECASE)
    out = re.sub(r"\n\[FINAL\][\s\S]*$", "", out)
    out = re.sub(r"[ \t]+$", "", out, flags=re.MULTILINE)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def needs_tool_marker(tool: str, input_text: str) -> str:
    """Emit a canonical [NEEDS_TOOL] marker. The input is single-quoted, so single quotes inside are escaped."""
    safe = input_text.replace("'", "\\u0027")
    return f"[NEEDS_TOOL tool=\"{tool}\" input='{safe}']"


__all__ = ["ParsedMarkers", "parse_markers", "clean_markers", "needs_tool_marker"]
