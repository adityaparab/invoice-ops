"""Isolated exact-decimal context for deterministic financial calculations."""

from decimal import ROUND_HALF_UP, Context, DivisionByZero, InvalidOperation, Overflow

# Extraction bounds numbers to 18 digits and invoices to 500 lines. Products need
# at most 36 digits and summation adds at most 3; 60 leaves ample headroom.
ARITHMETIC_PRECISION = 60


def exact_context() -> Context:
    return Context(
        prec=ARITHMETIC_PRECISION,
        rounding=ROUND_HALF_UP,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )
