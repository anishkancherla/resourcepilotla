"""ResourcePilot's monetization: a visible booking fee on top of each seller's own price.

Sellers receive exactly the subtotal they quoted. The buyer pays subtotal + booking fee into
escrow, and ResourcePilot keeps the fee only after every booking commits.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from resourcepilot.config import env_int


def booking_fee_cents(subtotal_cents: int) -> int:
    if subtotal_cents <= 0:
        return 0
    bps = env_int("PLATFORM_FEE_BPS", 300, minimum=0)
    fixed = env_int("PLATFORM_FEE_FIXED_CENTS", 30, minimum=0)
    percent = (Decimal(subtotal_cents) * bps / Decimal(10_000)).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    return int(percent) + fixed


def totals(subtotal_cents: int) -> dict[str, int]:
    fee = booking_fee_cents(subtotal_cents)
    return {"subtotal_cents": subtotal_cents, "fee_cents": fee, "total_cents": subtotal_cents + fee}


def usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def cents_to_decimal(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))
