from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def cents_to_yuan(cents: int) -> str:
    return f"{Decimal(cents) / Decimal(100):.2f}"


def yuan_to_cents(value: str) -> int:
    amount = Decimal(value.strip()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount < 0:
        raise ValueError("price must be non-negative")
    return int(amount * 100)
