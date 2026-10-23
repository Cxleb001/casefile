"""Approximate USD conversion so amounts in different currencies can be compared.

Rates match scripts/prepare_data.py (approximate 2022 values). They are only
used to rank and compare flows, never as reported transaction amounts.
"""

from __future__ import annotations

USD_RATE = {
    "US Dollar": 1.0, "Euro": 1.0, "UK Pound": 1.15, "Yuan": 0.145, "Yen": 0.0072, "Rupee": 0.0125,
    "Ruble": 0.016, "Canadian Dollar": 0.75, "Australian Dollar": 0.68, "Swiss Franc": 1.03,
    "Mexican Peso": 0.05, "Brazil Real": 0.19, "Shekel": 0.29, "Saudi Riyal": 0.27, "Bitcoin": 20000.0,
}


def to_usd(amount: float | None, currency: str | None) -> float:
    if amount is None:
        return 0.0
    return float(amount) * USD_RATE.get(str(currency or ""), 1.0)


def usd_sql(amount_col: str, currency_col: str) -> str:
    """A portable SQL expression converting ``amount_col`` to approximate USD."""
    cases = " ".join(f"WHEN '{name}' THEN {rate}" for name, rate in USD_RATE.items())
    return f"({amount_col} * (CASE {currency_col} {cases} ELSE 1.0 END))"
