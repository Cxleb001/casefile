"""Real-time progress (Markovian spec section 4.6): phases, a bounded log queue (last 15), subscribers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable

LOG_LIMIT = 15
PHASES = ("initializing", "generating", "compressing", "reflecting", "complete", "error")


@dataclass
class ProgressState:
    is_active: bool = False
    phase: str = "initializing"
    phase_detail: str = ""
    current_chunk: int = 0
    max_chunks: int = 0
    tokens_used: int = 0
    tokens_saved: int = 0
    carryover_size: int = 0
    start_time: float = 0.0
    last_update_time: float = 0.0
    logs: list[str] = field(default_factory=list)
    completed_chunks: list[Any] = field(default_factory=list)


class ProgressService:
    def __init__(self, clock: Callable[[], float], max_chunks: int) -> None:
        self.clock = clock
        now = clock()
        self.state = ProgressState(max_chunks=max_chunks, start_time=now, last_update_time=now)
        self._subscribers: list[Callable[[ProgressState], None]] = []

    def get(self) -> ProgressState:
        return replace(self.state, logs=list(self.state.logs), completed_chunks=list(self.state.completed_chunks))

    def subscribe(self, fn: Callable[[ProgressState], None]) -> Callable[[], None]:
        self._subscribers.append(fn)
        return lambda: self._subscribers.remove(fn) if fn in self._subscribers else None

    def update(self, log: str | None = None, **patch: Any) -> None:
        for key, value in patch.items():
            setattr(self.state, key, value)
        self.state.last_update_time = self.clock()
        if log:
            self.state.logs.append(log)
            del self.state.logs[:-LOG_LIMIT]
        snapshot = self.get()
        for fn in list(self._subscribers):
            fn(snapshot)

    def phase(self, phase: str, detail: str = "") -> None:
        self.update(f"phase: {phase}{' ' + detail if detail else ''}", phase=phase, phase_detail=detail,
                    is_active=phase not in ("complete", "error"))

    def chunk_done(self, chunk: Any) -> None:
        self.state.completed_chunks.append(chunk)
        self.update(f"chunk {chunk.id} done ({chunk.tokens} tokens estimated)", current_chunk=chunk.id)


__all__ = ["ProgressState", "ProgressService", "PHASES", "LOG_LIMIT"]
