"""The standard workup: the checks every analyst runs on every alert, before any judgement.

Running these deterministically first guarantees coverage (no case is closed
without looking at both ledgers, the counterparties, the onward flow and the
watchlist) and gives the model measured facts to reason from. The agent's own
follow-up questions come after this, in the investigation loop.
"""

from __future__ import annotations

from typing import Callable

from .tools import Toolbox

STEPS: list[tuple[str, dict]] = [
    ("profile_account", {}),
    ("get_counterparties", {"direction": "out", "order_by": "usd", "limit": 15}),
    ("get_counterparties", {"direction": "in", "order_by": "usd", "limit": 15}),
    ("follow_money", {"direction": "out", "hops": 2, "window_hours": 72}),
    ("follow_money", {"direction": "in", "hops": 2, "window_hours": 72}),
    ("screen_watchlist", {}),
    ("analyze_network", {}),
]


def standard_workup(toolbox: Toolbox, on_step: Callable | None = None, single_institution: str | None = None) -> list:
    """Run every standard step; failures become observations, never exceptions."""
    observations = []
    for name, args in STEPS:
        obs = toolbox.call(name, args)
        observations.append(obs)
        if on_step:
            on_step(obs)
    return observations
